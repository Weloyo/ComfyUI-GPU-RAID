"""«Сценарист»: сюжет -> план (LLM/эвристика) -> ключевые кадры (T2I,
параллельно) -> сегменты FLF2V (параллельно) -> одно видео.

Строится поверх подсистемы Long Video: тот же манифест (schema 2), тот же
каталог проектов output/gpuraid/<label>/, тот же редактор (он живёт в ноде
Сценариста на канве, см. web/lib/editor.js). N сегментов
= N+1 ключевых кадров; кадр i — конец сегмента i-1 и начало сегмента i, поэтому
кадры рендерятся ровно в WxH канвы сегментов (иначе H3 скомпонует stretch/cover
по-разному и стык будет виден).

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
    extract_story_director,
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
    """Встроенный SDXL T2I-шаблон кадров (используется, если свой не задан)."""
    path = os.path.join(TEMPLATES_DIR, "keyframe_sdxl_api.json")
    data = config.load_json(path, None)
    return data if isinstance(data, dict) else None


def _kf_rel_dir(label):
    return f"{KF_SUBDIR}/{config.sanitize_name(label)}"


def _kf_abs_dir(label):
    path = os.path.join(config.input_dir(), KF_SUBDIR, config.sanitize_name(label))
    os.makedirs(path, exist_ok=True)
    return path


def _spec_from_manifest(manifest):
    spec = dict(manifest["spec_meta"])
    spec["template"] = manifest["template_graph"]
    spec["job_type"] = "video"
    return spec


def _kf_spec_from_manifest(manifest):
    template = manifest.get("keyframe_template")
    if not template:
        raise RewriteError("У проекта нет шаблона ключевых кадров — задайте его "
                           "(нода Сценариста → «Шаблон кадра из канвы»)")
    spec = dict(manifest.get("keyframe_meta") or {})
    spec["template"] = template
    spec["job_type"] = "image"
    return spec


def _seg_overrides(manifest, seg):
    vid = manifest.get("spec") or {}
    return {
        "duration_s": seg.get("duration_s"),
        "fps": vid.get("fps"),
        "aspect": vid.get("aspect"),
        "short_edge": vid.get("short_edge"),
        "snap": vid.get("snap"),
    }


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

async def call_llm(story, params):
    cfg = REGISTRY.settings().get("llm") or {}
    base_url = str(cfg.get("base_url") or "").rstrip("/")
    if not base_url:
        raise RuntimeError("LLM не настроен: задайте base_url в панели (Режимы → LLM)")
    headers = {"Content-Type": "application/json"}
    key = secret_store.get("llm_api_key")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    payload = {
        "model": str(cfg.get("model") or ""),
        "messages": storyplan.llm_messages(story, params),
        "temperature": float(cfg.get("temperature") or 0.7),
    }
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
    return plan_data, str(data.get("model") or cfg.get("model") or "")


# ---------------------------------------------------------------------------
# план
# ---------------------------------------------------------------------------

async def plan(graph, params, keyframe_graph, client_id):
    """Разбирает сюжет в черновой манифест. Ничего не рендерит."""
    director, seg_graph = extract_story_director(graph)
    p = dict(director or {})
    for key, value in (params or {}).items():
        if value is not None:
            p[key] = value

    story_text = str(p.get("story") or "").strip()
    if not story_text:
        raise RewriteError("Пустой сюжет: заполните поле story в ноде Сценариста")

    spec = prepare_segment_template(seg_graph)
    if spec["end"] is None:
        raise RewriteError(
            'Сценаристу нужен FLF2V-шаблон: пометьте второй LoadImage заголовком '
            '"GPURAID:END_IMAGE"'
        )
    if not keyframe_graph:
        keyframe_graph = _bundled_keyframe_template()
        if keyframe_graph:
            events.toast("info", "Сценарист: использую встроенный SDXL-шаблон кадров — "
                                 "замените кнопкой «Шаблон кадра из канвы», если нужно")
    kf_spec = prepare_keyframe_template(keyframe_graph) if keyframe_graph else None

    vid = {
        "fps": int(p.get("fps") or 24),
        "aspect": str(p.get("aspect") or "16:9"),
        "short_edge": int(p.get("short_edge") or 768),
        "snap": str(p.get("snap") or "minimax_h3"),
        "segment_duration_s": float(p.get("segment_duration_s") or 5.0),
    }
    vid["width"], vid["height"] = storyplan.canvas(vid["aspect"], vid["short_edge"],
                                                   vid["snap"])

    llm_meta = {"used": False, "model": "", "error": ""}
    plan_data = None
    if p.get("use_llm", True):
        try:
            plan_data, model = await call_llm(story_text, {
                "segments_count": p.get("segments_count") or 0,
                "segment_duration_s": vid["segment_duration_s"],
            })
            llm_meta = {"used": True, "model": model, "error": ""}
        except Exception as e:
            cfg = REGISTRY.settings().get("llm") or {}
            reason = storyplan.llm_error_text(e, storyplan.llm_timeout(cfg))
            llm_meta = {"used": False, "model": "", "error": reason}
            events.toast("warn", f"Сценарист: LLM недоступен ({reason}) — "
                                 "разбиваю эвристикой, промпты правьте в ноде")
    if plan_data is None:
        plan_data = storyplan.heuristic_split(story_text,
                                              int(p.get("segments_count") or 0))

    outdir, label = lv._unique_outdir(str(p.get("label") or "story"))
    manifest = storyplan.new_story_manifest(label, story_text, plan_data, vid,
                                            int(p.get("seed") or 0), llm_meta,
                                            kf_dir=_kf_rel_dir(label))
    manifest["template_graph"] = spec["template"]
    manifest["spec_meta"] = {k: spec[k] for k in ("start", "start_key", "end", "end_key",
                                                  "prompt", "prompt_key", "out")}
    if kf_spec:
        manifest["keyframe_template"] = kf_spec["template"]
        manifest["keyframe_meta"] = {"prompt": kf_spec["prompt"],
                                     "prompt_key": kf_spec["prompt_key"]}
    lv.save_manifest(manifest)
    events.toast("success",
                 f"Сценарист: план «{label}» готов — {len(manifest['segments'])} сегментов, "
                 f"{len(manifest['keyframes'])} кадров. Правьте в ноде Сценариста.")
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


# ---------------------------------------------------------------------------
# рендер ключевых кадров
# ---------------------------------------------------------------------------

def _make_kf_unit(job, manifest, kf_spec, kf, outdir):
    i = kf["index"]
    vid = manifest["spec"]
    unit = Unit(i, meta={
        "label": f"key {i:03d}",
        "out_file": os.path.join(outdir, f"key_{i:03d}.png"),
        "prompt": kf.get("prompt"),
        "seed": int(kf.get("seed") or 0),
    })
    graph = render_keyframe(kf_spec, kf.get("prompt") or "", int(kf.get("seed") or 0),
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


async def render_segments(label, indices=None, client_id=""):
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

    job = Job("longvideo", client_id=client_id, label=f"{label}/segments")
    job.job_type = "video"
    job.timeouts = MANAGER._timeouts_for("video")
    job.outdir = os.path.join(config.deliver_base(), config.sanitize_name(label))
    job.unit_uploads = {}
    job.unit_graphs = {}
    for seg in targets:
        i = seg["index"]
        start_rel = _kf_file_rel(manifest, seg["start_kf"])
        end_rel = _kf_file_rel(manifest, seg["end_kf"])
        unit = Unit(i, meta={
            "label": f"seg {i:03d}",
            "out_file": os.path.join(job.outdir, seg.get("file") or f"seg_{i:03d}.mp4"),
            "out_node": spec["out"],
            "start_image": start_rel,
            "end_image": end_rel,
            "prompt": seg.get("prompt"),
            "seed": int(seg.get("seed") or 0),
        })
        graph = render_segment(spec, start_rel, end_rel, seg.get("prompt"),
                               int(seg.get("seed") or 0),
                               prefix=f"gpuraid_tmp/{job.job_id}/s{i:03d}",
                               overrides=_seg_overrides(manifest, seg))
        job.units.append(unit)
        job.unit_graphs[i] = graph
        job.unit_uploads[i] = lv._uploads_for_graph(graph, job.job_id)
        seg["status"] = "rendering"
        seg["error"] = ""
        seg["stale"] = False
    job.build_graph = lambda u: job.unit_graphs[u.index]

    job.eligible = await MANAGER._eligible_workers(job)
    if not job.eligible:
        raise RewriteError("Нет доступных воркеров для рендера сегментов")
    for unit in job.units:
        job.queue.put_nowait((1, unit.index))
    manifest["state"] = "running"
    lv.save_manifest(manifest)

    def on_unit_done(_job, unit):
        m = lv.load_manifest(label)
        if not m:
            return
        seg = next((s for s in m.get("segments", []) if s["index"] == unit.index), None)
        if seg is None:
            return
        seg["status"] = "done"
        seg["worker"] = unit.worker_id
        seg["error"] = ""
        seg["stale"] = False
        seg["start_image"] = unit.meta.get("start_image")
        seg["end_image"] = unit.meta.get("end_image")
        lv.save_manifest(m)

    job.on_unit_done = on_unit_done

    async def finalize(j):
        await _finalize_segments(j, label)

    job.state = "DISPATCHING"
    MANAGER._register(job)
    MANAGER.loop.create_task(MANAGER._run_parallel(job, finalize=finalize))
    return job


async def _finalize_segments(job, label):
    """Мерж статусов по индексам (не затирая чужие сегменты) + авто-склейка."""
    manifest = lv.load_manifest(label)
    if not manifest:
        return
    for unit in job.units:
        seg = next((s for s in manifest.get("segments", []) if s["index"] == unit.index),
                   None)
        if seg is None:
            continue
        if unit.state == DONE:
            seg["status"] = "done"
            seg["worker"] = unit.worker_id
            seg["error"] = ""
        else:
            seg["status"] = "failed"
            seg["error"] = unit.error or ""
    segments = manifest.get("segments", [])
    done = sum(1 for s in segments if s.get("status") == "done")
    if job.cancelled:
        manifest["state"] = "cancelled"
        job.finished = "CANCELLED"
    elif done == len(segments):
        manifest["state"] = "done"
        job.finished = "COMPLETE"
    elif done:
        manifest["state"] = "partial"
        job.finished = "PARTIAL"
        events.toast("warn", f"Сценарист «{label}»: готово {done}/{len(segments)} сегментов")
    else:
        manifest["state"] = "failed"
        job.finished = "FAILED"

    if manifest["state"] == "done":
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
                if seg:
                    files.append(os.path.join(outdir, seg["file"]))
            if files and not (edit.get("crossfade_s") or manifest.get("crossfade_s")):
                final = os.path.join(outdir, f"{manifest['label']}_full.mp4")
                await video.concat_copy(files, final)
                manifest["final"] = os.path.basename(final)
                events.toast("success",
                             f"Сценарист «{label}»: {len(files)} сегментов склеены → {final}")
        except Exception as e:
            log.warning("story auto-concat failed: %s", e)
            events.toast("warn", f"Сценарист «{label}»: сегменты готовы, авто-склейка "
                                 f"не удалась ({e}) — используйте Export")
    lv.save_manifest(manifest)
