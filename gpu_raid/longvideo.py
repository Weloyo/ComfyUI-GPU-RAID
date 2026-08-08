"""Long Video: видео любой длительности из коротких сегментов + редактирование.

Режимы:
  chain     — сегмент i+1 стартует с последнего кадра сегмента i (i2v-продолжение).
              Последовательный по своей природе; длительность не ограничена; воркеры
              дают отказоустойчивость и разгрузку локальной GPU.
  keyframes — сегменты FLF2V(key_i, key_i+1) между парами ключевых кадров,
              исполняются ПАРАЛЛЕЛЬНО на всех воркерах («торрент по сегментам»).

Шаблон сегмента = текущий workflow на канвасе с маркировкой нод заголовками
GPURAID:START_IMAGE / GPURAID:END_IMAGE / GPURAID:PROMPT / GPURAID:VIDEO_OUT.

Результат: output/gpuraid/<label>/seg_###.mp4 + manifest.json (шаблон сохраняется
в manifest — перегенерация и экспорт работают без открытого канваса и после
перезапуска). Экспорт: concat без перекодирования или монтаж с тримами/кроссфейдом.
"""

import logging
import os
import shutil
import time

import folder_paths

from . import config, events, results, storyplan, video
from .dispatcher import MANAGER, DEAD, DONE, Job, Unit, UnitCancelled, UnitFailure
from .graph_rewrite import (
    RewriteError,
    collect_upload_refs,
    prepare_segment_template,
    render_segment,
)
from .workers import LOCAL_ID, REGISTRY

log = logging.getLogger("gpu_raid")

MANIFEST = "manifest.json"


def _unique_outdir(label):
    base = config.deliver_base()
    name = config.sanitize_name(label, "longvideo")
    path = os.path.join(base, name)
    n = 2
    while os.path.exists(path):
        path = os.path.join(base, f"{name}-{n}")
        n += 1
    os.makedirs(path, exist_ok=True)
    return path, os.path.basename(path)


def _manifest_path(name):
    return os.path.join(config.deliver_base(), config.sanitize_name(name), MANIFEST)


def load_manifest(name):
    return config.load_json(_manifest_path(name), None)


def save_manifest(manifest):
    config.save_json_atomic(_manifest_path(manifest["label"]), manifest)
    # в WS уходит облегчённый вид: template_graph может весить сотни КБ
    events.send("longvideo", {"label": manifest["label"],
                              "manifest": storyplan.trim_manifest_view(manifest)})


def list_jobs():
    base = config.deliver_base()
    out = []
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return out
    for name in names:
        m = config.load_json(os.path.join(base, name, MANIFEST), None)
        if m:
            out.append({
                "label": m.get("label", name),
                "mode": m.get("mode"),
                "state": m.get("state"),
                "created": m.get("created"),
                "segments": len(m.get("segments", [])),
                "done": sum(1 for s in m.get("segments", []) if s.get("status") == "done"),
                "final": m.get("final"),
            })
    return out


def _seed_for(policy, base_seed, index):
    if policy == "fixed":
        return base_seed
    if policy == "random":
        return int.from_bytes(os.urandom(6), "big")
    return (base_seed + index) % (2 ** 64)


def _resolve_input(value):
    """Имя файла относительно input-каталога -> абсолютный путь (с проверкой)."""
    path = folder_paths.get_annotated_filepath(value)
    if not path or not os.path.isfile(path):
        raise RewriteError(f"Файл не найден в input: {value}")
    return path


def _uploads_for_graph(graph, job_id):
    refs = collect_upload_refs(graph)
    return MANAGER._resolve_upload_specs(refs, job_id)


def _make_unit(job, spec, index, start_image, end_image, prompt, seed):
    unit = Unit(index, meta={
        "label": f"seg {index:03d}",
        "out_file": os.path.join(job.outdir, f"seg_{index:03d}.mp4"),
        "out_node": spec["out"],
        "start_image": start_image,
        "end_image": end_image,
        "prompt": prompt,
        "seed": seed,
    })
    graph = render_segment(
        spec, start_image, end_image, prompt, seed,
        prefix=f"gpuraid_tmp/{job.job_id}/s{index:03d}",
    )
    return unit, graph


def _manifest_segment(unit):
    return {
        "index": unit.index,
        "file": os.path.basename(unit.meta["out_file"]),
        "status": "done" if unit.state == DONE else ("failed" if unit.state == DEAD else "pending"),
        "seed": unit.meta.get("seed"),
        "prompt": unit.meta.get("prompt"),
        "start_image": unit.meta.get("start_image"),
        "end_image": unit.meta.get("end_image"),
        "worker": unit.worker_id,
        "error": unit.error,
    }


async def start(graph, params, client_id):
    """params: {mode, label, prompts: [str] | None, count, keyframes: [names],
    seed, seed_policy, crossfade_s, min_vram_gb}"""
    mode = params.get("mode", "chain")
    spec = prepare_segment_template(graph)
    if mode == "keyframes" and spec["end"] is None:
        raise RewriteError(
            'Для режима keyframes пометьте второй LoadImage заголовком "GPURAID:END_IMAGE" '
            "(FLF2V: первый и последний кадр сегмента)"
        )

    prompts = [p for p in (params.get("prompts") or []) if str(p).strip()] or None
    base_seed = int(params.get("seed") or 0)
    policy = params.get("seed_policy", "increment")

    job = Job("longvideo", client_id=client_id, label=params.get("label") or "longvideo")
    job.job_type = "video"
    job.min_vram_gb = float(params.get("min_vram_gb") or 0)
    job.timeouts = MANAGER._timeouts_for("video")
    job.outdir, unique_label = _unique_outdir(job.label)
    job.label = unique_label
    job.unit_uploads = {}

    manifest = {
        "schema": storyplan.SCHEMA,
        "label": unique_label,
        "mode": mode,
        "created": int(time.time()),
        "state": "running",
        "crossfade_s": float(params.get("crossfade_s") or 0),
        "template_graph": spec["template"],
        "spec_meta": {k: spec[k] for k in ("start", "start_key", "end", "end_key",
                                           "prompt", "prompt_key", "out")},
        "seed": base_seed,
        "seed_policy": policy,
        "segments": [],
        "edit": storyplan.default_edit(),
        "final": None,
    }

    if mode == "keyframes":
        keys = [k.strip() for k in (params.get("keyframes") or []) if str(k).strip()]
        if len(keys) < 2:
            raise RewriteError("Нужно минимум 2 ключевых кадра (имена файлов из input)")
        for k in keys:
            _resolve_input(k)
        n = len(keys) - 1
        for i in range(n):
            prompt = prompts[i % len(prompts)] if prompts else None
            unit, ugraph = _make_unit(job, spec, i, keys[i], keys[i + 1],
                                      prompt, _seed_for(policy, base_seed, i))
            job.units.append(unit)
            job.unit_graphs = getattr(job, "unit_graphs", {})
            job.unit_graphs[i] = ugraph
            job.unit_uploads[i] = _uploads_for_graph(ugraph, job.job_id)
        job.build_graph = lambda unit: job.unit_graphs[unit.index]

        job.eligible = await MANAGER._eligible_workers(job)
        if not job.eligible:
            raise RewriteError("Нет доступных воркеров для Long Video")
        for unit in job.units:
            job.queue.put_nowait((1, unit.index))
        manifest["segments"] = [_manifest_segment(u) for u in job.units]
        save_manifest(manifest)
        job.state = "DISPATCHING"
        MANAGER._register(job)
        MANAGER.loop.create_task(
            MANAGER._run_parallel(job, finalize=lambda j: _finalize(j, manifest))
        )
        return job

    # ---- chain ----
    count = int(params.get("count") or (len(prompts) if prompts else 0))
    if count < 1:
        raise RewriteError("Укажите число сегментов (count) или список промптов")
    start_value = spec["template"][spec["start"]]["inputs"][spec["start_key"]]
    _resolve_input(start_value)

    job.state = "DISPATCHING"
    MANAGER._register(job)
    MANAGER.loop.create_task(
        _run_chain(job, spec, manifest, count, prompts, base_seed, policy, start_value)
    )
    return job


async def _run_chain(job, spec, manifest, count, prompts, base_seed, policy, start_value):
    frames_rel_dir = f"gpuraid_lv/{job.job_id}"
    frames_abs_dir = os.path.join(config.input_dir(), frames_rel_dir)
    manifest["frames_dir"] = frames_rel_dir  # кадры нужны rerender'у; чистятся при удалении проекта
    current_start = start_value
    try:
        for i in range(count):
            if job.cancelled:
                manifest["state"] = "cancelled"
                break
            prompt = None
            if prompts:
                prompt = prompts[i] if i < len(prompts) else prompts[-1]
            unit, ugraph = _make_unit(job, spec, i, current_start, None,
                                      prompt, _seed_for(policy, base_seed, i))
            job.units.append(unit)
            job.unit_uploads[i] = _uploads_for_graph(ugraph, job.job_id)
            job.unit_graphs = getattr(job, "unit_graphs", {})
            job.unit_graphs[i] = ugraph
            job.build_graph = lambda u: job.unit_graphs[u.index]
            manifest["segments"].append(_manifest_segment(unit))
            save_manifest(manifest)

            ok = await _execute_on_any(job, unit)
            manifest["segments"][i] = _manifest_segment(unit)
            save_manifest(manifest)
            if not ok:
                manifest["state"] = "failed"
                events.toast("error",
                             f"Long Video «{job.label}»: сегмент {i} не выполнен — цепочка остановлена "
                             f"({unit.error})")
                job.finished = "FAILED"
                break

            frame_abs = os.path.join(frames_abs_dir, f"f{i + 1:03d}.png")
            await video.extract_last_frame(unit.meta["out_file"], frame_abs)
            current_start = f"{frames_rel_dir}/f{i + 1:03d}.png"
        else:
            job.finished = "COMPLETE"
            manifest["state"] = "done"
        await _finalize(job, manifest)
    except Exception as e:
        log.exception("chain failed")
        job.finished = "FAILED"
        manifest["state"] = "failed"
        job.errors.append(str(e))
        save_manifest(manifest)
        events.toast("error", f"Long Video «{job.label}»: {e}")
    finally:
        job.state = job.finished or "FAILED"
        job.done_event.set()
        MANAGER._archive(job)


async def _execute_on_any(job, unit, fetch="longvideo"):
    """Последовательный сегмент: пробуем воркеров по очереди (локальный первым)."""
    records = await MANAGER._eligible_workers(job)
    records.sort(key=lambda r: (r["id"] != LOCAL_ID,
                                -(REGISTRY.status.get(r["id"], {}).get("vram_total_gb") or 0)))
    if not records:
        unit.error = "нет доступных воркеров"
        unit.state = DEAD
        return False
    last_error = ""
    for record in records:
        wc = REGISTRY.client(record)
        try:
            await MANAGER._execute_unit(job, record, wc, unit, fetch=fetch)
            job.stats["per_worker"][record["id"]] = job.stats["per_worker"].get(record["id"], 0) + 1
            return True
        except UnitCancelled:
            unit.error = "отменено"
            unit.state = DEAD
            return False
        except UnitFailure as e:
            last_error = str(e)
            unit.attempts += 1
            continue
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            continue
    unit.error = last_error
    unit.state = DEAD
    return False


async def _finalize(job, manifest):
    done = [u for u in job.units if u.state == DONE]
    manifest["segments"] = [_manifest_segment(u) for u in job.units]
    if job.cancelled:
        manifest["state"] = "cancelled"
    elif not done:
        manifest["state"] = "failed"
        job.finished = "FAILED"
    elif len(done) < len(job.units):
        manifest["state"] = "partial"
        job.finished = "PARTIAL"
        events.toast("warn", f"Long Video «{job.label}»: готово {len(done)}/{len(job.units)} сегментов")
    else:
        manifest["state"] = manifest.get("state") if manifest.get("state") == "done" else "done"
        job.finished = job.finished or "COMPLETE"

    # автосклейка без перекодирования, если все сегменты готовы и кроссфейд не задан
    if manifest["state"] == "done" and not manifest.get("crossfade_s"):
        try:
            files = [os.path.join(job.outdir, s["file"]) for s in manifest["segments"]]
            final = os.path.join(job.outdir, f"{manifest['label']}_full.mp4")
            await video.concat_copy(files, final)
            manifest["final"] = os.path.basename(final)
            events.toast("success",
                         f"Long Video «{job.label}»: {len(files)} сегментов склеены → {final}")
        except Exception as e:
            log.warning("auto-concat failed: %s", e)
            events.toast("warn",
                         f"Long Video «{job.label}»: сегменты готовы, авто-склейка не удалась ({e}) — "
                         "используйте Export в панели")
    save_manifest(manifest)


# ---------------------------------------------------------------------------
# операции над готовым проектом (панель-редактор)
# ---------------------------------------------------------------------------

async def rerender_segment(label, index, seed=None, prompt=None):
    manifest = load_manifest(label)
    if not manifest:
        raise RewriteError(f"Проект {label} не найден")
    segments = manifest.get("segments", [])
    seg = next((s for s in segments if s["index"] == index), None)
    if seg is None:
        raise RewriteError(f"Сегмент {index} не найден")

    spec = dict(manifest["spec_meta"])
    spec["template"] = manifest["template_graph"]
    spec["job_type"] = "video"

    job = Job("longvideo", label=f"{label}/seg{index:03d}")
    job.job_type = "video"
    job.timeouts = MANAGER._timeouts_for("video")
    job.outdir = os.path.join(config.deliver_base(), config.sanitize_name(label))
    job.unit_uploads = {}

    new_seed = int(seed) if seed is not None else int.from_bytes(os.urandom(6), "big")
    new_prompt = str(prompt) if prompt is not None else seg.get("prompt")
    unit, ugraph = _make_unit(job, spec, index, seg.get("start_image"),
                              seg.get("end_image"), new_prompt, new_seed)
    job.units.append(unit)
    job.unit_graphs = {index: ugraph}
    job.unit_uploads[index] = _uploads_for_graph(ugraph, job.job_id)
    job.build_graph = lambda u: job.unit_graphs[u.index]
    seg["status"] = "rendering"
    seg["prompt"] = new_prompt      # новый промпт персистится до рендера:
    seg.pop("dirty", None)          # переживает рестарт и снимает флаг dirty
    save_manifest(manifest)
    MANAGER._register(job)

    async def _run():
        ok = await _execute_on_any(job, unit)
        current = load_manifest(label) or manifest
        cseg = next((s for s in current.get("segments", []) if s["index"] == index), None)
        if cseg is not None:
            updated = _manifest_segment(unit)
            updated["file"] = cseg["file"]  # имя файла в проекте неизменно
            current["segments"][current["segments"].index(cseg)] = updated
            save_manifest(current)
        job.finished = "COMPLETE" if ok else "FAILED"
        job.state = job.finished
        job.done_event.set()
        MANAGER._archive(job)
        if ok:
            events.toast("success", f"Long Video «{label}»: сегмент {index} перегенерирован (seed {new_seed})")

    MANAGER.loop.create_task(_run())
    return job


async def update_segment(label, index, patch):
    """Правка prompt/seed/duration_s сегмента с персистом в манифест."""
    manifest = load_manifest(label)
    if not manifest:
        raise RewriteError(f"Проект {label} не найден")
    seg = next((s for s in manifest.get("segments", []) if s["index"] == index), None)
    if seg is None:
        raise RewriteError(f"Сегмент {index} не найден")
    storyplan.apply_segment_patch(seg, patch)
    save_manifest(manifest)
    return seg


async def update_edit(label, edit):
    """Персист состояния редактора (order/excluded/trims/crossfade_s)."""
    manifest = load_manifest(label)
    if not manifest:
        raise RewriteError(f"Проект {label} не найден")
    storyplan.merge_edit(manifest, edit)
    save_manifest(manifest)
    return manifest["edit"]


async def delete_project(label):
    manifest = load_manifest(label)
    if not manifest:
        raise RewriteError(f"Проект {label} не найден")
    input_base = os.path.abspath(config.input_dir())
    for rel in (manifest.get("frames_dir"),
                f"gpuraid_story/{config.sanitize_name(label)}"):
        if not rel:
            continue
        path = os.path.abspath(os.path.join(input_base, rel))
        if path.startswith(input_base):
            shutil.rmtree(path, ignore_errors=True)
    shutil.rmtree(os.path.join(config.deliver_base(), config.sanitize_name(label)),
                  ignore_errors=True)
    events.send("longvideo", {"label": label, "deleted": True})
    return True


async def export(label, order=None, trims=None, crossfade_s=None):
    manifest = load_manifest(label)
    if not manifest:
        raise RewriteError(f"Проект {label} не найден")
    outdir = os.path.join(config.deliver_base(), config.sanitize_name(label))
    segments = {s["index"]: s for s in manifest.get("segments", [])}
    stored = manifest.get("edit") or {}
    if order is None:
        order = stored.get("order") or sorted(segments)
        excluded = {int(i) for i in (stored.get("excluded") or [])}
        order = [i for i in order if int(i) not in excluded]
    if trims is None:
        trims = stored.get("trims") or {}
    if crossfade_s is None:
        crossfade_s = stored.get("crossfade_s") or 0
    trims = trims or {}

    items = []
    for idx in order:
        seg = segments.get(int(idx))
        if not seg or seg.get("status") != "done":
            continue
        path = os.path.join(outdir, seg["file"])
        if not os.path.isfile(path):
            continue
        trim = trims.get(str(idx)) or trims.get(int(idx)) or {}
        items.append({"file": path, "in_s": trim.get("in_s") or 0,
                      "out_s": trim.get("out_s") or 0})
    if not items:
        raise RewriteError("Нет готовых сегментов для экспорта")

    final = os.path.join(outdir, f"{manifest['label']}_final.mp4")
    no_trims = all(not it["in_s"] and not it["out_s"] for it in items)
    if no_trims and not crossfade_s:
        await video.concat_copy([it["file"] for it in items], final)
    else:
        await video.render_edit(items, final, crossfade_s=float(crossfade_s or 0))
    manifest["final"] = os.path.basename(final)
    save_manifest(manifest)
    events.toast("success", f"Long Video «{label}»: экспорт готов → {final}")
    return final
