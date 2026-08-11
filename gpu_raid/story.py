"""«История» / «Раскадровка» / «Видеоряд»: сюжет -> план (LLM/эвристика) ->
ключевые кадры (T2I, параллельно) -> сегменты FLF2V (параллельно) -> одно
видео. Три ноды-пульта над одним и тем же проектом, связаны коннектором
GPURAID_PROJECT (или дропдауном «проект:» вручную).

Строится поверх подсистемы Long Video: тот же манифест (schema 2), тот же
каталог проектов output/gpuraid/<label>/, тот же редактор (он живёт в теле
каждой из трёх нод, отрисовывает свою стадию — см. web/lib/editor.js). N
сегментов = N+1 ключевых кадров; кадр i — конец сегмента i-1 и начало
сегмента i, поэтому кадры рендерятся ровно в WxH канвы сегментов (иначе H3
скомпонует stretch/cover по-разному и стык будет виден).

Кадры складываются в input/gpuraid_story/<label>/ (input-каталог: существующий
upload-механизм сам разносит их по воркерам для FLF2V-графов).
"""

import logging
import os

import aiohttp

from . import config, events, storyplan, video
from . import longvideo as lv
from . import secrets as secret_store
from .dispatcher import DEAD, DONE, MANAGER, Job, Unit
from .graph_rewrite import (
    RewriteError,
    prepare_keyframe_template,
    prepare_segment_template,
    render_keyframe,
    render_segment,
)
from .workers import REGISTRY

log = logging.getLogger("gpu_raid")

KF_SUBDIR = "gpuraid_story"

TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def _bundled_keyframe_template():
    """Встроенный T2I-шаблон кадров (используется, если свой не задан) —
    Z-Image-Turbo: реальная локальная цель проекта, в отличие от SDXL, чьих
    весов у пользователя вообще нет на диске."""
    path = os.path.join(TEMPLATES_DIR, "keyframe_zimage_turbo_api.json")
    data = config.load_json(path, None)
    return data if isinstance(data, dict) else None


def _kf_rel_dir(label):
    return f"{KF_SUBDIR}/{config.sanitize_name(label)}"


def _kf_abs_dir(label):
    path = os.path.join(config.input_dir(), KF_SUBDIR, config.sanitize_name(label))
    os.makedirs(path, exist_ok=True)
    return path


def _spec_from_manifest(manifest):
    if not manifest.get("template_graph"):
        raise RewriteError("У проекта нет шаблона сегмента — задайте его "
                           "(нода Видеоряд → «Шаблон сегмента из канвы»)")
    spec = dict(manifest["spec_meta"])
    spec["template"] = manifest["template_graph"]
    spec["job_type"] = "video"
    return spec


def _kf_spec_from_manifest(manifest):
    """Шаблон кадра проекта, а если его никогда не задавали (кнопка «Шаблон
    кадра из канвы» ни разу не нажата) — встроенный дефолт (Z-Image), не
    сохраняя его в манифест: как только пользователь захватит свой, он и
    станет использоваться, без риска залипшей бандл-копии."""
    template = manifest.get("keyframe_template")
    meta = manifest.get("keyframe_meta")
    if not template:
        bundled = _bundled_keyframe_template()
        if not bundled:
            raise RewriteError("У проекта нет шаблона ключевых кадров — задайте его "
                               "(нода Раскадровка → «Шаблон кадра из канвы»)")
        kf_spec = prepare_keyframe_template(bundled)
        template = kf_spec["template"]
        meta = {"prompt": kf_spec["prompt"], "prompt_key": kf_spec["prompt_key"]}
        events.toast("info", "Раскадровка: использую встроенный Z-Image-шаблон кадров — "
                             "замените кнопкой «Шаблон кадра из канвы», если нужно")
    spec = dict(meta or {})
    spec["template"] = template
    spec["job_type"] = "image"
    return spec


def _seg_overrides(manifest, seg, variant="final"):
    vid = manifest.get("spec") or {}
    overrides = {
        "duration_s": seg.get("duration_s"),
        "fps": vid.get("fps"),
        "aspect": vid.get("aspect"),
        "short_edge": vid.get("short_edge"),
        "snap": vid.get("snap"),
    }
    if variant == "preview":
        vs = manifest.get("video_settings") or {}
        short_edge = vs.get("preview_short_edge")
        if not short_edge:
            base = int(vid.get("short_edge") or 768)
            short_edge = max(32, round(base / 2 / 32) * 32)
        overrides["short_edge"] = short_edge
        steps = vs.get("preview_steps")
        if steps:
            overrides["steps"] = int(steps)
    return overrides


def _seg_target(seg, variant):
    """Под-словарь сегмента для ЗАПИСИ результата рендера: сам сегмент для
    variant="final", вложенный ["preview"] для variant="preview" (создаётся
    при первой записи, setdefault — поэтому только для записи, не для чтения)."""
    if variant == "preview":
        return seg.setdefault("preview", {"file": None, "status": "draft",
                                          "worker": None, "error": ""})
    return seg


def _seg_read(seg, variant):
    """Тот же выбор под-словаря, но для ЧТЕНИЯ — не создаёт "preview" на лету."""
    if variant == "preview":
        return seg.get("preview") or {}
    return seg


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

async def call_llm(story, params):
    """base_url/api_key — глобальное подключение (панель, «Подключения и ключи»).
    model/temperature/max_tokens/system_prompt — «характер» конкретного проекта,
    приходят с ноды Истории через params (не из глобальных настроек) — так
    можно держать разных «сценаристов» под разные сюжеты одновременно."""
    cfg = REGISTRY.settings().get("llm") or {}
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("LLM не настроен: задайте base_url в панели (Режимы → LLM)")
    headers = {"Content-Type": "application/json"}
    key = secret_store.get("llm_api_key")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    model = str(params.get("model") or cfg.get("model") or "")
    temperature = params.get("temperature")
    payload = {
        "model": model,
        "messages": storyplan.llm_messages(story, params),
        "temperature": float(temperature) if temperature is not None else float(cfg.get("temperature") or 0.7),
    }
    max_tokens = params.get("max_tokens")
    if max_tokens:
        payload["max_tokens"] = int(max_tokens)
    async with aiohttp.ClientSession() as s:
        async with s.post(base_url + "/chat/completions", json=payload, headers=headers,
                          timeout=aiohttp.ClientTimeout(
                              total=storyplan.llm_timeout(cfg))) as r:
            if r.status != 200:
                text = (await r.text())[:200]
                raise RuntimeError(f"LLM HTTP {r.status}: {text}")
            data = await r.json()
    content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")
    plan_data = storyplan.parse_llm_plan(content)
    return plan_data, str(data.get("model") or model or "")


# ---------------------------------------------------------------------------
# план
# ---------------------------------------------------------------------------

async def plan(params, client_id):
    """Разбирает сюжет в черновой манифест. Ничего не рендерит.

    Больше не требует графа канвы — шаблоны сегмента (FLF2V) и кадра (T2I)
    захватываются отдельно, уже над готовым проектом: нодой Видеоряд
    (set_segment_template) и нодой Раскадровка (set_keyframe_template).
    """
    p = dict(params or {})
    story_text = str(p.get("story") or "").strip()
    if not story_text:
        raise RewriteError("Пустой сюжет: заполните поле story в ноде Истории")

    vid = {
        "fps": int(p.get("fps") or 24),
        "aspect": str(p.get("aspect") or "16:9"),
        "short_edge": int(p.get("short_edge") or 768),
        "snap": str(p.get("snap") or "minimax_h3"),
        "segment_duration_s": float(p.get("segment_duration_s") or 5.0),
        "max_segment_duration_s": float(p["max_segment_duration_s"])
            if p.get("max_segment_duration_s") else None,
        "max_total_duration_s": float(p["max_total_duration_s"])
            if p.get("max_total_duration_s") else None,
    }
    vid["width"], vid["height"] = storyplan.canvas(vid["aspect"], vid["short_edge"],
                                                   vid["snap"])

    llm_params = {
        "segments_count": p.get("segments_count") or 0,
        "segment_duration_s": vid["segment_duration_s"],
        "max_segment_duration_s": vid["max_segment_duration_s"],
        "max_total_duration_s": vid["max_total_duration_s"],
        "model": p.get("model"),
        "temperature": p.get("temperature"),
        "max_tokens": p.get("max_tokens"),
        "system_prompt": p.get("system_prompt"),
    }
    llm_meta = {"used": False, "model": "", "error": "",
               "system_prompt": str(p.get("system_prompt") or ""),
               "temperature": p.get("temperature"), "max_tokens": p.get("max_tokens")}
    plan_data = None
    if p.get("use_llm", True):
        try:
            plan_data, model = await call_llm(story_text, llm_params)
            llm_meta.update({"used": True, "model": model, "error": ""})
        except Exception as e:
            cfg = REGISTRY.settings().get("llm") or {}
            reason = storyplan.llm_error_text(e, storyplan.llm_timeout(cfg))
            llm_meta.update({"used": False, "model": "", "error": reason})
            events.toast("warn", f"История: LLM недоступен ({reason}) — "
                                 "разбиваю эвристикой, промпты правьте в ноде")
    if plan_data is None:
        plan_data = storyplan.heuristic_split(story_text,
                                              int(p.get("segments_count") or 0))

    storyplan.clamp_segment_and_total_duration(
        plan_data["segments"], vid["max_segment_duration_s"], vid["max_total_duration_s"])

    outdir, label = lv._unique_outdir(str(p.get("label") or "story"))
    manifest = storyplan.new_story_manifest(label, story_text, plan_data, vid,
                                            int(p.get("seed") or 0), llm_meta,
                                            kf_dir=_kf_rel_dir(label))
    lv.save_manifest(manifest)
    events.toast("success",
                 f"История: план «{label}» готов — {len(manifest['segments'])} сегментов, "
                 f"{len(manifest['keyframes'])} кадров. Подключите Раскадровку и Видеоряд "
                 "(коннектором или дропдауном «проект:»).")
    return manifest


async def set_keyframe_template(label, graph):
    manifest = lv.load_manifest(label)
    if not manifest:
        raise RewriteError(f"Проект {label} не найден")
    kf_spec = prepare_keyframe_template(graph)
    manifest["keyframe_template"] = kf_spec["template"]
    manifest["keyframe_meta"] = {"prompt": kf_spec["prompt"],
                                 "prompt_key": kf_spec["prompt_key"]}
    lv.save_manifest(manifest)
    return True


async def set_segment_template(label, graph):
    manifest = lv.load_manifest(label)
    if not manifest:
        raise RewriteError(f"Проект {label} не найден")
    spec = prepare_segment_template(graph)
    if spec["end"] is None:
        raise RewriteError(
            'Видеоряду нужен FLF2V-шаблон: пометьте второй LoadImage заголовком '
            '"GPURAID:END_IMAGE"'
        )
    manifest["template_graph"] = spec["template"]
    manifest["spec_meta"] = {k: spec[k] for k in
                             ("start", "start_key", "end", "end_key", "prompt", "prompt_key",
                              "steps", "steps_key", "out")}
    lv.save_manifest(manifest)
    return True


async def update_video_settings(label, patch):
    manifest = lv.load_manifest(label)
    if not manifest:
        raise RewriteError(f"Проект {label} не найден")
    vs = manifest.setdefault("video_settings", {"prompt_format": "minimax_h3",
                                                "preview_short_edge": None, "preview_steps": None})
    for key in ("prompt_format", "preview_short_edge", "preview_steps"):
        if key in (patch or {}):
            vs[key] = patch[key]
    lv.save_manifest(manifest)
    return vs


async def update_storyboard_settings(label, patch):
    manifest = lv.load_manifest(label)
    if not manifest:
        raise RewriteError(f"Проект {label} не найден")
    ss = manifest.setdefault("storyboard_settings", {"continuity_mode": "style_only"})
    if "continuity_mode" in (patch or {}):
        ss["continuity_mode"] = patch["continuity_mode"]
    lv.save_manifest(manifest)
    return ss


# ---------------------------------------------------------------------------
# рендер ключевых кадров
# ---------------------------------------------------------------------------

def _kf_prompt_with_bible(manifest, raw_prompt):
    """style_bible клеится к промпту кадра тут, в момент рендера — не при
    создании плана (иначе правка стиля задним числом не решала бы N+1 кадров
    и N сегментов уже сгенерированных промптов)."""
    bible = str(manifest.get("style_bible") or "").strip().rstrip(".")
    raw = str(raw_prompt or "").strip()
    if bible and raw:
        return f"{bible}. {raw}"
    return raw or bible


def _make_kf_unit(job, manifest, kf_spec, kf, outdir):
    i = kf["index"]
    vid = manifest["spec"]
    prompt = _kf_prompt_with_bible(manifest, kf.get("prompt"))
    unit = Unit(i, meta={
        "label": f"key {i:03d}",
        "out_file": os.path.join(outdir, f"key_{i:03d}.png"),
        "prompt": prompt,
        "seed": int(kf.get("seed") or 0),
    })
    graph = render_keyframe(kf_spec, prompt, int(kf.get("seed") or 0),
                            vid["width"], vid["height"],
                            prefix=f"gpuraid_tmp/{job.job_id}/k{i:03d}")
    return unit, graph


async def render_keyframes(label, indices=None, client_id=""):
    manifest = lv.load_manifest(label)
    if not manifest:
        raise RewriteError(f"Проект {label} не найден")
    kf_spec = _kf_spec_from_manifest(manifest)
    targets = [k for k in manifest.get("keyframes", [])
               if indices is None or k["index"] in indices]
    if not targets:
        raise RewriteError("Нет кадров для рендера")

    outdir = _kf_abs_dir(label)
    job = Job("storykf", client_id=client_id, label=f"{label}/keyframes")
    job.job_type = "image"
    job.timeouts = MANAGER._timeouts_for("image")
    job.outdir = outdir
    job.unit_uploads = {}
    job.unit_graphs = {}
    for kf in targets:
        unit, graph = _make_kf_unit(job, manifest, kf_spec, kf, outdir)
        job.units.append(unit)
        job.unit_graphs[unit.index] = graph
        job.unit_uploads[unit.index] = lv._uploads_for_graph(graph, job.job_id)
        kf["status"] = "rendering"
        kf["error"] = ""
    job.build_graph = lambda u: job.unit_graphs[u.index]

    job.eligible = await MANAGER._eligible_workers(job)
    if not job.eligible:
        raise RewriteError("Нет доступных воркеров для рендера кадров")
    for unit in job.units:
        job.queue.put_nowait((1, unit.index))
    manifest["state"] = "kf_rendering"
    lv.save_manifest(manifest)

    def on_unit_done(_job, unit):
        m = lv.load_manifest(label)
        if not m:
            return
        kf = next((x for x in m.get("keyframes", []) if x["index"] == unit.index), None)
        if kf is None:
            return
        kf["status"] = "done"
        kf["file"] = os.path.basename(unit.meta["out_file"])
        kf["worker"] = unit.worker_id
        kf["error"] = ""
        storyplan.mark_stale_for_keyframe(m, unit.index)
        lv.save_manifest(m)

    job.on_unit_done = on_unit_done

    async def finalize(j):
        m = lv.load_manifest(label) or manifest
        for unit in j.units:
            kf = next((x for x in m.get("keyframes", []) if x["index"] == unit.index), None)
            if kf is not None and unit.state != DONE:
                kf["status"] = "failed"
                kf["error"] = unit.error or ""
        done = sum(1 for k in m.get("keyframes", []) if k.get("status") == "done")
        total = len(m.get("keyframes", []))
        m["state"] = "kf_done" if done == total else "kf_partial"
        lv.save_manifest(m)
        j.finished = "COMPLETE" if all(u.state == DONE for u in j.units) else "PARTIAL"
        if done == total:
            events.toast("success", f"Сценарист «{label}»: все {total} кадров готовы — "
                                    "проверьте их и жмите «Рендер сегментов»")

    job.state = "DISPATCHING"
    MANAGER._register(job)
    MANAGER.loop.create_task(MANAGER._run_parallel(job, finalize=finalize))
    return job


async def rerender_keyframe(label, index, seed=None, prompt=None, client_id=""):
    """Перегенерация одного кадра; соседние готовые сегменты помечаются stale."""
    manifest = lv.load_manifest(label)
    if not manifest:
        raise RewriteError(f"Проект {label} не найден")
    kf = next((k for k in manifest.get("keyframes", []) if k["index"] == index), None)
    if kf is None:
        raise RewriteError(f"Кадр {index} не найден")
    if seed is not None:
        kf["seed"] = int(seed) % (2 ** 64)
    else:
        kf["seed"] = int.from_bytes(os.urandom(6), "big")
    if prompt is not None:
        kf["prompt"] = str(prompt)
    lv.save_manifest(manifest)
    return await render_keyframes(label, indices=[index], client_id=client_id)


async def update_keyframe(label, index, patch):
    manifest = lv.load_manifest(label)
    if not manifest:
        raise RewriteError(f"Проект {label} не найден")
    kf = next((k for k in manifest.get("keyframes", []) if k["index"] == index), None)
    if kf is None:
        raise RewriteError(f"Кадр {index} не найден")
    if "prompt" in patch:
        kf["prompt"] = str(patch["prompt"] or "")
    if patch.get("seed") is not None:
        kf["seed"] = int(patch["seed"]) % (2 ** 64)
    lv.save_manifest(manifest)
    return kf


# ---------------------------------------------------------------------------
# рендер сегментов
# ---------------------------------------------------------------------------

def _kf_file_rel(manifest, index):
    kf = next((k for k in manifest.get("keyframes", []) if k["index"] == index), None)
    if kf is None or kf.get("status") != "done" or not kf.get("file"):
        raise RewriteError(f"Кадр {index} ещё не готов — сначала отрендерите кадры")
    return f"{_kf_rel_dir(manifest['label'])}/{kf['file']}"


async def render_segments(label, indices=None, variant="final", client_id=""):
    manifest = lv.load_manifest(label)
    if not manifest:
        raise RewriteError(f"Проект {label} не найден")
    spec = _spec_from_manifest(manifest)
    if spec.get("end") is None:
        raise RewriteError("Шаблон сегмента без GPURAID:END_IMAGE")
    targets = [s for s in manifest.get("segments", [])
               if indices is None or s["index"] in indices]
    if not targets:
        raise RewriteError("Нет сегментов для рендера")

    suffix = " (черновик)" if variant == "preview" else ""

    job = Job("longvideo", client_id=client_id, label=f"{label}/segments{suffix}")
    job.job_type = "video"
    job.timeouts = MANAGER._timeouts_for("video")
    job.outdir = os.path.join(config.deliver_base(), config.sanitize_name(label))
    job.unit_uploads = {}
    job.unit_graphs = {}
    for seg in targets:
        i = seg["index"]
        start_rel = _kf_file_rel(manifest, seg["start_kf"])
        end_rel = _kf_file_rel(manifest, seg["end_kf"])
        render_prompt = storyplan.render_prompt_for_segment(
            manifest, seg.get("prompt"), seg.get("duration_s"))
        if variant == "preview":
            filename = (seg.get("preview") or {}).get("file") or f"seg_{i:03d}_preview.mp4"
        else:
            filename = seg.get("file") or f"seg_{i:03d}.mp4"
        unit = Unit(i, meta={
            "label": f"seg {i:03d}{suffix}",
            "out_file": os.path.join(job.outdir, filename),
            "out_node": spec["out"],
            "start_image": start_rel,
            "end_image": end_rel,
            "prompt": seg.get("prompt"),
            "seed": int(seg.get("seed") or 0),
            "variant": variant,
        })
        graph = render_segment(spec, start_rel, end_rel, render_prompt,
                               int(seg.get("seed") or 0),
                               prefix=f"gpuraid_tmp/{job.job_id}/s{i:03d}",
                               overrides=_seg_overrides(manifest, seg, variant))
        job.units.append(unit)
        job.unit_graphs[i] = graph
        job.unit_uploads[i] = lv._uploads_for_graph(graph, job.job_id)
        target = _seg_target(seg, variant)
        target["status"] = "rendering"
        target["error"] = ""
        if variant == "final":
            seg["stale"] = False
    job.build_graph = lambda u: job.unit_graphs[u.index]

    job.eligible = await MANAGER._eligible_workers(job)
    if not job.eligible:
        raise RewriteError("Нет доступных воркеров для рендера сегментов")
    for unit in job.units:
        job.queue.put_nowait((1, unit.index))
    if variant == "final":
        manifest["state"] = "running"
    lv.save_manifest(manifest)

    def on_unit_done(_job, unit):
        m = lv.load_manifest(label)
        if not m:
            return
        seg = next((s for s in m.get("segments", []) if s["index"] == unit.index), None)
        if seg is None:
            return
        target = _seg_target(seg, variant)
        target["status"] = "done"
        target["file"] = os.path.basename(unit.meta["out_file"])
        target["worker"] = unit.worker_id
        target["error"] = ""
        if variant == "final":
            seg["stale"] = False
            seg["start_image"] = unit.meta.get("start_image")
            seg["end_image"] = unit.meta.get("end_image")
        lv.save_manifest(m)

    job.on_unit_done = on_unit_done

    async def finalize(j):
        await _finalize_segments(j, label, variant)

    job.state = "DISPATCHING"
    MANAGER._register(job)
    MANAGER.loop.create_task(MANAGER._run_parallel(job, finalize=finalize))
    return job


async def _finalize_segments(job, label, variant="final"):
    """Мерж статусов по индексам (не затирая чужие сегменты) + авто-склейка.

    variant="preview": пишет в segments[i]["preview"] и manifest["final_preview"],
    НЕ трогает manifest["state"]/["final"] — черновой прогон не должен
    переключать статус готовности проекта, только финальный рендер это делает.
    """
    manifest = lv.load_manifest(label)
    if not manifest:
        return
    for unit in job.units:
        seg = next((s for s in manifest.get("segments", []) if s["index"] == unit.index),
                   None)
        if seg is None:
            continue
        target = _seg_target(seg, variant)
        if unit.state == DONE:
            target["status"] = "done"
            target["worker"] = unit.worker_id
            target["error"] = ""
        else:
            target["status"] = "failed"
            target["error"] = unit.error or ""

    segments = manifest.get("segments", [])
    done = sum(1 for s in segments if _seg_read(s, variant).get("status") == "done")
    label_kind = "черновик" if variant == "preview" else "финал"
    if job.cancelled:
        job.finished = "CANCELLED"
        if variant == "final":
            manifest["state"] = "cancelled"
    elif done == len(segments):
        job.finished = "COMPLETE"
        if variant == "final":
            manifest["state"] = "done"
    elif done:
        job.finished = "PARTIAL"
        if variant == "final":
            manifest["state"] = "partial"
            events.toast("warn", f"«{label}»: готово {done}/{len(segments)} сегментов")
    else:
        job.finished = "FAILED"
        if variant == "final":
            manifest["state"] = "failed"

    ready = (variant == "final" and manifest["state"] == "done") \
        or (variant == "preview" and segments and done == len(segments))
    if ready:
        edit = manifest.get("edit") or {}
        excluded = {int(i) for i in (edit.get("excluded") or [])}
        order = edit.get("order") or [s["index"] for s in segments]
        try:
            outdir = os.path.join(config.deliver_base(), config.sanitize_name(label))
            files = []
            seg_by_index = {s["index"]: s for s in segments}
            for i in order:
                if int(i) in excluded:
                    continue
                seg = seg_by_index.get(int(i))
                file = _seg_read(seg, variant).get("file") if seg else None
                if file:
                    files.append(os.path.join(outdir, file))
            if files and not (edit.get("crossfade_s") or manifest.get("crossfade_s")):
                out_suffix = "_preview" if variant == "preview" else "_full"
                final = os.path.join(outdir, f"{manifest['label']}{out_suffix}.mp4")
                await video.concat_copy(files, final)
                manifest["final_preview" if variant == "preview" else "final"] = \
                    os.path.basename(final)
                events.toast("success",
                             f"«{label}»: {len(files)} сегментов ({label_kind}) "
                             f"склеены → {final}")
        except Exception as e:
            log.warning("story auto-concat failed (variant=%s): %s", variant, e)
            events.toast("warn", f"«{label}»: сегменты ({label_kind}) готовы, авто-склейка "
                                 f"не удалась ({e}) — используйте Export")
    lv.save_manifest(manifest)
