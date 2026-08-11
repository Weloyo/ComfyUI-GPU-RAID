"""Чистая логика Сценариста и манифестов Long Video schema 2.

Чистый модуль (как graph_rewrite): никаких импортов ComfyUI — импортируется
тестами вне ComfyUI.

Манифест v2 — надмножество v1: добавляются story-поля (story_text, spec,
keyframes) и персистентное состояние редактора (edit). Старые манифесты
(без "schema") продолжают работать со всеми функциями ниже.

Схема Сценариста: N сегментов = N+1 ключевых кадров; кадр i — общий для
конца сегмента i-1 и начала сегмента i (непрерывность склейки FLF2V).
"""

import json
import math
import re
import time

from .consts import DEFAULT_SETTINGS

SCHEMA = 2

# тяжёлые поля, которые не ходят в WS-события и GET-ответы
VIEW_DROP = ("template_graph", "spec_meta", "keyframe_template", "keyframe_meta")


def default_edit():
    return {"order": [], "excluded": [], "trims": {}, "crossfade_s": 0.0}


def merge_edit(manifest, patch):
    """Вливает правки редактора (order/excluded/trims/crossfade_s) в манифест."""
    edit = manifest.setdefault("edit", default_edit())
    patch = patch or {}
    if "order" in patch:
        edit["order"] = [int(i) for i in (patch["order"] or [])]
    if "excluded" in patch:
        edit["excluded"] = sorted({int(i) for i in (patch["excluded"] or [])})
    if "trims" in patch and isinstance(patch["trims"], dict):
        trims = {}
        for key, t in patch["trims"].items():
            if not isinstance(t, dict):
                continue
            entry = {"in_s": float(t.get("in_s") or 0), "out_s": float(t.get("out_s") or 0)}
            if entry["in_s"] or entry["out_s"]:
                trims[str(int(key))] = entry
        edit["trims"] = trims
    if "crossfade_s" in patch:
        edit["crossfade_s"] = max(0.0, float(patch["crossfade_s"] or 0))
    return manifest


def apply_segment_patch(seg, patch):
    """Правка сегмента: prompt/seed/duration_s.

    Смена промпта у готового сегмента ставит dirty — редактор показывает
    «нужен перерендер»; сам файл не трогаем.
    """
    patch = patch or {}
    if "prompt" in patch:
        new_prompt = str(patch["prompt"] or "")
        if new_prompt != (seg.get("prompt") or "") and seg.get("status") == "done":
            seg["dirty"] = True
        seg["prompt"] = new_prompt
    if patch.get("seed") is not None:
        seg["seed"] = int(patch["seed"]) % (2 ** 64)
    if patch.get("duration_s") is not None:
        seg["duration_s"] = max(0.1, float(patch["duration_s"]))
    return seg


def trim_manifest_view(manifest):
    """Манифест без тяжёлых полей — для WS-событий и GET-ответов."""
    return {k: v for k, v in manifest.items() if k not in VIEW_DROP}


# ---------------------------------------------------------------------------
# геометрия и время кадров
# ---------------------------------------------------------------------------

# зеркала логики MiniMax H3 (comfy_extras/nodes_minimax_h3.py):
# сетка кадров 17k+5, канва: короткая сторона 768, кап площади 768*1344,
# кратность 32. Обобщено на произвольную short_edge.
_H3_MULTIPLE = 32
_H3_AREA_RATIO = 1344 / 768  # кап площади = short_edge^2 * ratio


def align_frames(duration_s, fps, snap="none"):
    """Длительность (с) -> число кадров; для minimax_h3 — вверх до сетки 17k+5."""
    n = max(1, round(float(duration_s) * int(fps)))
    if snap == "minimax_h3":
        n = max(5, n)
        while n % 17 != 5:
            n += 1
    return n


def canvas(aspect, short_edge=768, snap="none"):
    """Аспект ('16:9' | 'W:H') + короткая сторона -> (width, height)."""
    try:
        aw, ah = str(aspect).split(":")
        ratio = float(aw) / float(ah)
    except (ValueError, ZeroDivisionError):
        raise ValueError(f"Непонятный аспект: {aspect!r} (ожидается вид '16:9')")
    short = int(short_edge)
    if ratio >= 1.0:
        w, h = short * ratio, float(short)
    else:
        w, h = float(short), short / ratio
    if snap == "minimax_h3":
        cap = short * short * _H3_AREA_RATIO
        if w * h > cap:
            s = math.sqrt(cap / (w * h))
            w, h = w * s, h * s
    m = _H3_MULTIPLE
    return (max(m, round(w / m) * m), max(m, round(h / m) * m))


# ---------------------------------------------------------------------------
# разбиение сюжета: эвристика и LLM
# ---------------------------------------------------------------------------

_SENT_RE = re.compile(r"(?<=[.!?…])\s+")


def heuristic_split(story, target_segments=0, max_chars=350):
    """Детерминированное разбиение без LLM: абзацы -> предложения -> корзины.

    Возвращает ту же структуру, что parse_llm_plan.
    """
    text = str(story or "").strip()
    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    sentences = []
    for p in paragraphs:
        sentences.extend(s.strip() for s in _SENT_RE.split(p) if s.strip())
    if not sentences and text:
        sentences = [text]

    if target_segments and int(target_segments) > 0 and sentences:
        n = min(int(target_segments), len(sentences))
        buckets = [[] for _ in range(n)]
        for i, s in enumerate(sentences):
            buckets[min(i * n // len(sentences), n - 1)].append(s)
        chunks = [" ".join(b) for b in buckets if b]
    else:
        chunks, cur = [], ""
        for s in sentences:
            if cur and len(cur) + len(s) + 1 > max_chars:
                chunks.append(cur)
                cur = s
            else:
                cur = f"{cur} {s}".strip()
        if cur:
            chunks.append(cur)

    segments = [{"prompt": c, "keyframe_prompt": c, "duration_s": None} for c in chunks]
    return {"style_bible": "", "segments": segments,
            "final_keyframe_prompt": chunks[-1] if chunks else ""}


def llm_timeout(cfg):
    """Сколько ждём LLM. 0/мусор в настройках — дефолт, а не мгновенный отказ."""
    try:
        value = float((cfg or {}).get("timeout_s") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else float(DEFAULT_SETTINGS["llm"]["timeout_s"])


def llm_error_text(e, timeout_s):
    """Человеческая причина отказа LLM.

    aiohttp бросает TimeoutError БЕЗ текста: `str(e)` — пустая строка, и в
    манифесте оставалось «LLM не использован» без единого слова почему, а план
    молча уезжал в эвристику (живьём: локальная 30B грузилась с диска дольше
    таймаута).
    """
    if isinstance(e, TimeoutError):      # asyncio.TimeoutError с 3.11 — он же
        return (f"LLM не ответил за {int(timeout_s)} с — локальная модель могла "
                "грузиться с диска; повторите план или увеличьте "
                "settings.llm.timeout_s")
    return str(e).strip() or type(e).__name__


def llm_messages(story, params):
    """Сообщения для OpenAI-совместимого /chat/completions (строгий JSON-план)."""
    n = int(params.get("segments_count") or 0)
    dur = float(params.get("segment_duration_s") or 5.0)
    max_seg = params.get("max_segment_duration_s")
    max_total = params.get("max_total_duration_s")
    count_rule = (f"Раздели сюжет ровно на {n} сегментов."
                  if n else "Сам выбери число сегментов (от 2 до 16) по драматургии сюжета.")
    duration_rule = f" Каждый сегмент по умолчанию ~{dur:g} секунд (можно варьировать по смыслу сцены)."
    if max_seg:
        duration_rule += f" Ни один сегмент не должен превышать {float(max_seg):g} секунд."
    if max_total:
        duration_rule += f" Сумма всех сегментов не должна превышать {float(max_total):g} секунд."
    system = (
        "Ты — режиссёр раскадровки и оператор для видео-диффузионной модели: "
        "придумываешь и сюжетную структуру, и то, как её снять камерой.\n"
        + count_rule + duration_rule + "\n"
        "Ответь СТРОГО одним JSON-объектом, без пояснений и markdown:\n"
        '{"style_bible": "устойчивые визуальные приметы всей истории: как выглядят '
        'персонажи, место действия, освещение, эпоха и техника съёмки — то, что '
        'должно оставаться неизменным от кадра к кадру",\n'
        ' "segments": [{"prompt": "...", "keyframe_prompt": "...", '
        f'"duration_s": {dur:g}}}],\n'
        ' "final_keyframe_prompt": "..."}\n'
        "Правила: prompt сегмента описывает действие И движение камеры внутри "
        "сегмента (тип — наезд/отъезд/панорама/трекинг/статика и т.п., амплитуда "
        "и скорость — прозой, не списком тегов); keyframe_prompt — статичный "
        "кадр НАЧАЛА сегмента; final_keyframe_prompt — финальный кадр всего "
        "видео. Соседние сегменты стыкуются: начало сегмента i+1 продолжает "
        "конец сегмента i. Не повторяй style_bible внутри prompt/keyframe_prompt "
        "— он добавляется автоматически при рендере. Все prompt пиши на английском."
    )
    profile = str(params.get("system_prompt") or "").strip()
    if profile:
        system += "\n\nДополнительно об этом сценаристе (характер, стиль подачи): " + profile
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": str(story or "").strip()},
    ]


def parse_llm_plan(text):
    """Терпимый парсер ответа LLM -> {style, segments, final_keyframe_prompt}.

    Срезает ```-заборы, ищет первый {...последний}. ValueError при мусоре.
    """
    s = str(text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("в ответе LLM нет JSON-объекта")
    data = json.loads(s[start:end + 1])
    raw = data.get("segments")
    if not isinstance(raw, list) or not raw:
        raise ValueError("план без segments")
    out = []
    for item in raw[:64]:
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        if not prompt:
            continue
        duration = item.get("duration_s")
        try:
            duration = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None
        out.append({
            "prompt": prompt,
            "keyframe_prompt": str(item.get("keyframe_prompt") or "").strip() or prompt,
            "duration_s": duration,
        })
    if not out:
        raise ValueError("в плане нет валидных сегментов")
    return {
        "style_bible": str(data.get("style_bible") or data.get("style") or "").strip(),
        "segments": out,
        "final_keyframe_prompt": str(data.get("final_keyframe_prompt") or "").strip(),
    }


def clamp_segment_and_total_duration(segments, max_segment_s=None, max_total_s=None):
    """Клампит duration_s по двум потолкам: сначала каждый сегмент отдельно
    (максимум на сегмент), затем — если сумма всё ещё больше потолка на весь
    ролик — пропорционально сжимает все сегменты разом. Пропорциональное
    сжатие, а не обрезка хвоста: иначе конец истории молча терялся бы.

    segments: список словарей с "duration_s" (как из parse_llm_plan/
    heuristic_split, ДО new_story_manifest). Мутирует и возвращает тот же список.
    """
    if max_segment_s:
        cap = float(max_segment_s)
        for seg in segments:
            cur = seg.get("duration_s")
            if cur is not None and cur > cap:
                seg["duration_s"] = cap
    if max_total_s:
        total = sum(float(seg.get("duration_s") or 5.0) for seg in segments)
        cap = float(max_total_s)
        if total > cap > 0:
            scale = cap / total
            for seg in segments:
                seg["duration_s"] = float(seg.get("duration_s") or 5.0) * scale
    return segments


def scene_timeline(segments):
    """Кумулятивные секунды начала/конца каждого сегмента — не хранится в
    манифесте, считается по требованию (иначе рассинхронизируется с диска,
    если пользователь поправит duration_s одного сегмента задним числом).
    """
    out = []
    t = 0.0
    for seg in segments:
        dur = float(seg.get("duration_s") or 0.0)
        out.append({"index": seg.get("index"), "start_s": round(t, 2),
                    "end_s": round(t + dur, 2)})
        t += dur
    return out


def render_prompt_for_segment(manifest, action_prompt, duration_s):
    """Промпт сегмента для рендера: style_bible проекта + (MiniMax-формат,
    если manifest.video_settings.prompt_format == "minimax_h3", иначе сырой
    action_prompt с приклеенным style_bible). Общая точка для массового
    рендера (story.render_segments) и поштучного «заново»
    (longvideo.rerender_segment) — чтобы оба пути давали одинаковый
    результат для одного и того же сегмента.
    """
    style_bible = str(manifest.get("style_bible") or "").strip().rstrip(".")
    prompt_format = (manifest.get("video_settings") or {}).get("prompt_format", "minimax_h3")
    action = str(action_prompt or "").strip()
    if prompt_format == "minimax_h3":
        return format_minimax_segment_prompt(action, duration_s, style_bible)
    if style_bible and action:
        return f"{style_bible}. {action}"
    return action or style_bible


def format_minimax_segment_prompt(action_prompt, duration_s, style_bible=""):
    """Промпт сегмента в официальном формате MiniMax H3 FL2VA (first+last frame).

    По docs/VIDEO_PROMPT_WRITING_GUIDE_base_en.md (MiniMaxAI/MiniMax-H3,
    первоисточник): один сегмент = один [Shot 1] (без таймстампа — сегмент
    целиком один непрерывный шот), обязательная фраза выравнивания первого и
    последнего кадра по секундам, длительность двумя знаками после запятой.
    """
    dur = f"{float(duration_s or 0):.2f}"
    bible = str(style_bible or "").strip().rstrip(".")
    action = str(action_prompt or "").strip()
    prefix = f"{bible}. " if bible else ""
    return (
        f"[Shot 1] {prefix}Picture 1 (from Shot 1) aligns with the 0.00-second "
        f"mark; Picture 2 (from Shot 1) aligns with the {dur}-second mark. "
        f"{action}"
    )


# ---------------------------------------------------------------------------
# манифест Сценариста
# ---------------------------------------------------------------------------

def new_story_manifest(label, story, plan_data, vid, seed, llm_meta=None, kf_dir=None):
    """Черновой манифест v2 из плана.

    vid: {fps, aspect, short_edge, snap, segment_duration_s, width, height}.
    Кадров N+1; сегмент i идёт от кадра i к кадру i+1. kf_dir — каталог кадров
    относительно input (пути кадров пишутся в сегменты сразу: их имена
    детерминированы, а rerender-механика Long Video читает start/end_image
    из манифеста).
    """
    kf_dir = kf_dir or f"gpuraid_story/{label}"
    style_bible = str(plan_data.get("style_bible") or "").strip()

    seed = int(seed or 0)
    segs_in = plan_data.get("segments") or []
    keyframes, segments = [], []
    for i, item in enumerate(segs_in):
        kf_prompt = str(item.get("keyframe_prompt") or item.get("prompt") or "").strip()
        keyframes.append({
            "index": i, "prompt": kf_prompt,
            "seed": (seed + 1000 + i) % (2 ** 64),
            "file": None, "status": "draft", "worker": None, "error": "",
        })
        duration = float(item.get("duration_s") or vid["segment_duration_s"])
        segments.append({
            "index": i, "prompt": str(item.get("prompt") or "").strip(),
            "duration_s": duration,
            "length_frames": align_frames(duration, vid["fps"], vid["snap"]),
            "start_kf": i, "end_kf": i + 1,
            "start_image": f"{kf_dir}/key_{i:03d}.png",
            "end_image": f"{kf_dir}/key_{i + 1:03d}.png",
            "file": f"seg_{i:03d}.mp4", "status": "draft",
            "seed": (seed + i) % (2 ** 64),
            "worker": None, "error": "", "stale": False,
        })
    n = len(segs_in)
    final_prompt = str(plan_data.get("final_keyframe_prompt")
                        or (segs_in[-1].get("prompt") if segs_in else "") or "").strip()
    keyframes.append({
        "index": n, "prompt": final_prompt,
        "seed": (seed + 1000 + n) % (2 ** 64),
        "file": None, "status": "draft", "worker": None, "error": "",
    })
    return {
        "schema": SCHEMA, "label": label, "mode": "story",
        "created": int(time.time()), "state": "draft",
        "story_text": str(story or ""),
        "style_bible": style_bible,
        "llm": llm_meta or {"used": False, "model": "", "error": ""},
        "spec": dict(vid), "crossfade_s": 0.0,
        "keyframes": keyframes, "segments": segments,
        "edit": default_edit(), "seed": seed, "seed_policy": "increment",
        "final": None, "final_preview": None,
        "storyboard_settings": {"continuity_mode": "style_only"},
        "video_settings": {"prompt_format": "minimax_h3",
                           "preview_short_edge": None, "preview_steps": None},
    }


def mark_stale_for_keyframe(manifest, index):
    """Кадр index перегенерирован: соседние ГОТОВЫЕ сегменты -> stale."""
    touched = []
    for seg in manifest.get("segments", []):
        if index in (seg.get("start_kf"), seg.get("end_kf")) and seg.get("status") == "done":
            seg["stale"] = True
            touched.append(seg["index"])
    return touched
