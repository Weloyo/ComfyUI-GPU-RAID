"""Pipeline-шардинг: одна большая модель по компонентам на разных GPU.

analyze() режет текущий workflow на острова (pipeline_split), оценивает VRAM и
трафик разрезов, предлагает размещение. Раскладку решают привязки нод-лоадеров
на канве (properties.gpuraid_runtime, см. placement.py); незатронутые острова
дозаполняет auto_place. start() собирает граф каждой стадии (Save/LoadBundle
на границах), гонит стадии топологическими волнами: мастер — звезда: скачивает
бандлы стадии через /view в свой input-каталог, а существующий upload-механизм
сам доставляет их следующим стадиям (LoadBundle в UPLOAD_TABLE).

После последней стадии воркера ему шлётся /free — VRAM освобождается сразу.
"""

import asyncio
import logging
import os
import shutil

import folder_paths

from . import events, pipeline_split as ps
from . import placement as pm
from .dispatcher import DEAD, DONE, MANAGER, Job, Unit, UnitCancelled, UnitFailure
from .graph_rewrite import (RewriteError, classify_job_type, collect_upload_refs,
                            strip_markers)
from .workers import LOCAL_ID, REGISTRY

log = logging.getLogger("gpu_raid")


def _type_table():
    """Таблица типов выходов из реестра нод ComfyUI (мастер знает все классы)."""
    table = {}
    try:
        import nodes as comfy_nodes
        mappings = comfy_nodes.NODE_CLASS_MAPPINGS
    except Exception:
        return table
    for name, cls in mappings.items():
        rt = getattr(cls, "RETURN_TYPES", None)
        if rt is None and hasattr(cls, "define_schema"):
            try:
                schema = cls.define_schema()
                rt = tuple(getattr(o, "io_type", None) or "?" for o in schema.outputs)
            except Exception:
                rt = None
        if rt is None:
            continue
        try:
            table[name] = tuple(str(t) for t in rt)
        except Exception:
            continue
    return table


def _model_sizes(graph):
    from .consts import LOADER_TABLE

    sizes = {}
    for node in graph.values():
        tab = LOADER_TABLE.get(node.get("class_type"))
        if not tab:
            continue
        for key, folder in tab.items():
            name = (node.get("inputs") or {}).get(key)
            if not isinstance(name, str) or not name:
                continue
            try:
                path = folder_paths.get_full_path(folder, name)
                if path:
                    sizes[(folder, name)] = os.path.getsize(path)
            except Exception:
                continue
    return sizes


def _online_workers():
    out = []
    for record in REGISTRY.enabled_records():
        st = REGISTRY.status.get(record["id"], {})
        vram = st.get("vram_total_gb") or 0
        if record["id"] == LOCAL_ID:
            if not vram:
                try:
                    import torch
                    if torch.cuda.is_available():
                        vram = round(torch.cuda.get_device_properties(0).total_memory
                                     / (1024 ** 3), 1)
                except Exception:
                    vram = 0
        elif st.get("state") != "online":
            continue
        out.append({"id": record["id"], "name": record["name"],
                    "vram_gb": vram, "kind": record.get("kind")})
    return out


def _worker_views():
    """Все включённые записи (и офлайн тоже) — для резолва привязок лоадеров."""
    out = []
    for record in REGISTRY.enabled_records():
        st = REGISTRY.status.get(record["id"], {})
        out.append({
            "id": record["id"], "name": record["name"],
            "platform": record.get("platform") or st.get("platform") or "",
            "kind": record.get("kind"),
            "state": "online" if record["id"] == LOCAL_ID else st.get("state", "unknown"),
            "enabled": True,
        })
    return out


def _loader_weights(graph, sizes):
    """{nid: суммарный вес моделей лоадера в байтах} — арбитр конфликтов привязок.

    Файла может не быть у мастера (модель живёт только на воркере) — тогда
    берём размер из библиотеки источников.
    """
    from . import modelsrc
    from .consts import LOADER_TABLE

    weights = {}
    for nid, node in graph.items():
        table = LOADER_TABLE.get(node.get("class_type"))
        if not table:
            continue
        total = 0
        for key, folder in table.items():
            name = (node.get("inputs") or {}).get(key)
            if not isinstance(name, str) or not name:
                continue
            size = sizes.get((folder, name)) or 0
            if not size:
                source = modelsrc.resolve(name, folder)
                if source and source.get("size_gb"):
                    size = int(float(source["size_gb"]) * (1024 ** 3))
            total += size
        weights[nid] = total
    return weights


def _placement_from_canvas(graph, part, workflow_ui, sizes):
    """Раскладка островов по привязкам лоадеров из workflow.

    -> (resolved {island_id: worker_id}, island_assign {island_id: привязка},
        decided_by {island_id: nid}, problems [str], warnings [str])
    """
    views = _worker_views()
    assignments = pm.extract_assignments(workflow_ui or {})
    island_assign, decided_by, warnings = pm.place_islands(
        graph, part, assignments, _loader_weights(graph, sizes))
    resolved, problems = {}, []
    for isl_id, assign in island_assign.items():
        wid, reason = pm.resolve_assignment(assign, views)
        if wid:
            resolved[isl_id] = wid
        else:
            problems.append(f"«{pm.assign_label(assign)}»: {reason}")
    return resolved, island_assign, decided_by, sorted(set(problems)), warnings


async def analyze(graph, workflow_ui=None):
    graph = strip_markers(graph)   # нода «Конвейер» сама живёт на этой же канве
    part = ps.partition(graph, _type_table())
    sizes = _model_sizes(graph)
    workers = _online_workers()
    resolved, island_assign, decided_by, problems, place_warnings = \
        _placement_from_canvas(graph, part, workflow_ui, sizes)
    if workers:
        placement = ps.auto_place(graph, part, workers, sizes, preplaced=resolved)
    else:
        placement = dict(resolved)

    islands_view = []
    for isl in part["islands"]:
        deciding = decided_by.get(isl["id"])
        islands_view.append({
            "id": isl["id"],
            "classes": sorted({graph[nid].get("class_type", "?") for nid in isl["nodes"]}),
            "replicated": sorted({graph[nid].get("class_type", "?")
                                  for nid in isl["replicated"]}),
            "models": {f: sorted(n) for f, n in ps.island_models(graph, isl).items()},
            "vram_est_gb": ps.estimate_island_vram_gb(graph, isl, sizes),
            "worker_id": placement.get(isl["id"]),
            # привязка с канвы (если есть) и кто её решил — для отчёта в ноде
            "assign": island_assign.get(isl["id"], ""),
            "assign_label": pm.assign_label(island_assign[isl["id"]])
                            if isl["id"] in island_assign else "",
            "decided_by": (graph.get(deciding) or {}).get("class_type") if deciding else "",
        })
    cuts_view = []
    warnings = list(part["warnings"]) + place_warnings + [
        f"привязка {p} — стадия не разложена" for p in problems]
    for cut in part["cuts"]:
        mb = ps.estimate_cut_mb(cut)
        cross = placement.get(cut["src_island"]) != placement.get(cut["dst_island"])
        view = {
            "type": cut["type"],
            "from": graph[cut["src"]].get("class_type", "?"),
            "to": graph[cut["dst"]].get("class_type", "?"),
            "src_island": cut["src_island"], "dst_island": cut["dst_island"],
            "est_mb": mb, "warn": bool(cross and mb > ps.TUNNEL_LIMIT_MB),
        }
        if view["warn"]:
            warnings.append(
                f"разрез {view['type']} {view['from']}→{view['to']} ~{mb} МБ — "
                f"больше лимита туннеля ~{ps.TUNNEL_LIMIT_MB} МБ; оставьте эти острова "
                "на одном воркере")
        cuts_view.append(view)
    return {"islands": islands_view, "cuts": cuts_view, "placement": placement,
            "workers": workers, "warnings": warnings,
            "assigned": sorted(island_assign), "problems": problems}


async def start(graph, workflow_ui, placement, label, client_id):
    graph = strip_markers(graph)
    part = ps.partition(graph, _type_table())
    sizes = _model_sizes(graph)
    resolved, island_assign, _decided, problems, place_warnings = \
        _placement_from_canvas(graph, part, workflow_ui, sizes)
    if problems:
        # привязка на канве есть, а воркера под ней нет — запуск бессмыслен:
        # auto_place молча увёз бы модель на чужой GPU
        raise RewriteError(
            "не выполнимы привязки лоадеров: " + "; ".join(problems)
            + ". Запустите рантаймы кнопкой ▶ в нодах-лоадерах и дождитесь "
            "статуса «в сети»")
    for text in place_warnings:
        events.toast("warn", f"Конвейер: {text}")
    if placement:
        # легаси-вызов с явной раскладкой (старый UI ноды) — он главнее
        for k, v in placement.items():
            resolved[int(k)] = str(v)
    if len(resolved) < len(part["islands"]):
        workers = _online_workers()
        if not workers:
            raise RewriteError("Нет воркеров для конвейера")
        placement = ps.auto_place(graph, part, workers, sizes, preplaced=resolved)
    else:
        placement = resolved
    for isl in part["islands"]:
        wid = placement.get(isl["id"])
        if not wid or REGISTRY.get(wid) is None:
            raise RewriteError(f"Остров {isl['id']}: воркер «{wid}» не найден")

    from . import results
    job = Job("pipeline", client_id=client_id, label=label or "pipeline")
    job.job_type = classify_job_type(graph)
    job.timeouts = MANAGER._timeouts_for(job.job_type)
    job.unit_uploads = {}
    stages = ps.build_stage_graphs(graph, part, placement, job.job_id)
    job.outdir, _ = results.deliver_dir(job.label)
    for stage in stages:
        job.units.append(Unit(stage["index"], meta={
            "label": f"стадия {stage['index']} @ {stage['worker_id']}",
            "worker_id": stage["worker_id"],
            "graph": stage["graph"],
            "in_bundles": stage["in_bundles"],
            "out_bundles": stage["out_bundles"],
            "deps": stage["deps"],
        }))
    job.build_graph = lambda u: u.meta["graph"]
    job.state = "DISPATCHING"
    MANAGER._register(job)
    MANAGER.loop.create_task(_run_pipeline(job))
    return job


async def _exec_stage(job, unit):
    wid = unit.meta["worker_id"]
    record = REGISTRY.get(wid)
    if record is None:
        unit.error = f"воркер {wid} не найден"
        unit.state = DEAD
        return False
    wc = REGISTRY.client(record)
    try:
        # бандлы зависимостей уже в input-каталоге мастера — резолвим прямо сейчас
        job.unit_uploads[unit.index] = MANAGER._resolve_upload_specs(
            collect_upload_refs(unit.meta["graph"]), job.job_id)
    except RewriteError as e:
        unit.error = str(e)
        unit.state = DEAD
        return False
    attempts = int(REGISTRY.settings().get("max_retries", 2)) + 1
    for attempt in range(attempts):
        try:
            await MANAGER._execute_unit(job, record, wc, unit, fetch="pipeline")
            job.stats["per_worker"][wid] = job.stats["per_worker"].get(wid, 0) + 1
            return True
        except UnitCancelled:
            unit.error = "отменено"
            unit.state = DEAD
            return False
        except UnitFailure as e:
            unit.error = str(e)
            unit.attempts += 1
            if not e.retriable or attempt >= attempts - 1:
                unit.state = DEAD
                return False
            unit.progress = (0, 0)
        except Exception as e:
            log.exception("pipeline stage %s crashed", unit.index)
            unit.error = f"{type(e).__name__}: {e}"
            unit.state = DEAD
            return False
    unit.state = DEAD
    return False


async def _run_pipeline(job):
    failed = False
    try:
        remaining = {}
        for u in job.units:
            remaining[u.meta["worker_id"]] = remaining.get(u.meta["worker_id"], 0) + 1
        pending = {u.index: u for u in job.units}
        done = set()
        while pending and not job.cancelled and not failed:
            wave = [u for u in pending.values()
                    if all(d in done for d in u.meta["deps"])]
            if not wave:
                raise RewriteError("Цикл в зависимостях стадий конвейера")
            job.state = f"WAVE {sorted(u.index for u in wave)}"
            oks = await asyncio.gather(*[_exec_stage(job, u) for u in wave])
            for u, ok in zip(wave, oks):
                pending.pop(u.index, None)
                if ok:
                    done.add(u.index)
                else:
                    failed = True
                    events.toast("error",
                                 f"Pipeline «{job.label}»: стадия {u.index} упала "
                                 f"({u.error}) — зависимые стадии отменены")
                # /free воркеру, когда его стадий больше не осталось
                wid = u.meta["worker_id"]
                remaining[wid] -= 1
                if remaining[wid] == 0 and wid != LOCAL_ID:
                    record = REGISTRY.get(wid)
                    if record is not None:
                        MANAGER.loop.create_task(REGISTRY.client(record).free())
        for u in pending.values():
            u.state = DEAD
            u.error = u.error or "стадия-зависимость не выполнена"
        if job.cancelled:
            job.finished = "CANCELLED"
        elif failed or pending:
            job.finished = "FAILED" if not done else "PARTIAL"
        else:
            job.finished = "COMPLETE"
    except Exception as e:
        log.exception("pipeline run failed")
        job.errors.append(str(e))
        job.finished = "FAILED"
        events.toast("error", f"Pipeline «{job.label}»: {e}")
    finally:
        job.state = job.finished or "FAILED"
        job.done_event.set()
        MANAGER._archive(job)
        if job.finished == "COMPLETE":
            events.toast("success", f"Pipeline «{job.label}»: готово → {job.outdir}")
        # бандлы больше не нужны НЕЗАВИСИМО от исхода — раньше чистка была
        # только при COMPLETE, и FAILED/PARTIAL/CANCELLED оставляли
        # input/gpuraid_bundle/<job_id> на диске навсегда (не покрыт
        # results.gc_jobs — тот чистит только output/gpuraid_tmp). rmtree — в
        # поток, не на event loop
        await asyncio.to_thread(
            shutil.rmtree,
            os.path.join(folder_paths.get_input_directory(), "gpuraid_bundle", job.job_id),
            ignore_errors=True)
