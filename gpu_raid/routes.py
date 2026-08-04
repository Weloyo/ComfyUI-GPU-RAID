"""HTTP-маршруты /gpuraid/* (регистрируются при импорте расширения).

Master-эндпоинты защищены guard'ом: loopback без forwarded-заголовков, либо
валидный токен (middleware auth уже пропустил запрос).
"""

import asyncio
import logging
import os
import platform
import time

from aiohttp import web

import server

from . import auth, config, downloads, events, parity, results
from . import longvideo as lv
from .consts import MODEL_CATALOG, VERSION
from .dispatcher import MANAGER
from .graph_rewrite import RewriteError, extract_requirements
from .workers import LOCAL_ID, REGISTRY

log = logging.getLogger("gpu_raid")
routes = server.PromptServer.instance.routes

_obj_cache = {}  # worker_id -> (ts, set(classes))


def _guard_master(request):
    if auth.is_local_request(request) or auth.configured_token():
        return
    raise web.HTTPForbidden(reason="master endpoints: loopback only")


async def _json(request):
    try:
        return await request.json()
    except Exception:
        raise web.HTTPBadRequest(reason="invalid json body")


def _err(status, reason):
    return web.json_response({"reason": str(reason)}, status=status)


# ---------------------------------------------------------------------------
# info / status
# ---------------------------------------------------------------------------

@routes.get("/gpuraid/info")
async def info(request):
    gpu, vram_total = "", 0
    try:
        import torch

        if torch.cuda.is_available():
            gpu = torch.cuda.get_device_name(0)
            vram_total = round(torch.cuda.get_device_properties(0).total_memory / (1024 ** 3), 1)
    except Exception:
        pass
    comfy_version = ""
    try:
        import comfyui_version

        comfy_version = comfyui_version.__version__
    except Exception:
        pass
    return web.json_response({
        "version": VERSION,
        "gpu": gpu,
        "vram_total_gb": vram_total,
        "hostname": platform.node(),
        "python": platform.python_version(),
        "comfy": comfy_version,
        "auth": bool(auth.configured_token()),
    })


@routes.get("/gpuraid/status")
async def status(request):
    _guard_master(request)
    workers = []
    for record in REGISTRY.records():
        st = REGISTRY.status.get(record["id"], {})
        workers.append({
            "id": record["id"], "name": record["name"], "enabled": record.get("enabled", True),
            "state": st.get("state", "unknown"),
        })
    online = sum(1 for w in workers if w["enabled"] and w["state"] == "online")
    return web.json_response({
        "workers": workers,
        "online": online,
        "eligible_for_stripe": online >= 2,
        "active_jobs": sum(1 for j in MANAGER.jobs.values() if not j.done_event.is_set()),
    })


# ---------------------------------------------------------------------------
# workers CRUD
# ---------------------------------------------------------------------------

def _worker_view(record):
    st = dict(REGISTRY.status.get(record["id"], {}))
    st.pop("_object_classes", None)
    view = {k: v for k, v in record.items() if k != "token"}
    view["has_token"] = bool(record.get("token"))
    view["status"] = st
    return view


@routes.get("/gpuraid/workers")
async def workers_list(request):
    _guard_master(request)
    return web.json_response({
        "workers": [_worker_view(r) for r in REGISTRY.records()],
        "settings": REGISTRY.settings(),
    })


@routes.post("/gpuraid/workers")
async def workers_add(request):
    _guard_master(request)
    data = await _json(request)
    added, errors = await REGISTRY.add_from_lines(data.get("connection_strings", ""))
    for record in added:
        asyncio.get_running_loop().create_task(_probe_safe(record))
    return web.json_response({
        "added": [_worker_view(r) for r in added],
        "errors": errors,
    }, status=200 if added or not errors else 400)


async def _probe_safe(record):
    try:
        await REGISTRY.probe_worker(record, full=True)
    except Exception as e:
        REGISTRY.set_status(record["id"], state="offline", error=str(e))


@routes.patch("/gpuraid/workers/{wid}")
async def workers_update(request):
    _guard_master(request)
    record = await REGISTRY.update(request.match_info["wid"], await _json(request))
    if record is None:
        return _err(404, "worker not found")
    asyncio.get_running_loop().create_task(_probe_safe(record))
    _obj_cache.pop(record["id"], None)
    return web.json_response(_worker_view(record))


@routes.delete("/gpuraid/workers/{wid}")
async def workers_delete(request):
    _guard_master(request)
    ok = await REGISTRY.delete(request.match_info["wid"])
    return web.json_response({"deleted": ok}, status=200 if ok else 404)


@routes.post("/gpuraid/workers/{wid}/check")
async def workers_check(request):
    _guard_master(request)
    wid = request.match_info["wid"]
    record = REGISTRY.get(wid)
    if record is None:
        return _err(404, "worker not found")
    data = await _json(request)
    graph = data.get("graph") or {}
    requirements = extract_requirements(graph)

    if wid == LOCAL_ID:
        return web.json_response({"level": parity.GREEN, "missing_classes": [],
                                  "missing_models": {}, "suggestions": {}, "notes": ["мастер"]})
    wc = REGISTRY.client(record)
    try:
        cached = _obj_cache.get(wid)
        if cached and time.time() - cached[0] < 600:
            classes = cached[1]
        else:
            obj = await wc.object_info()
            classes = set(obj.keys())
            _obj_cache[wid] = (time.time(), classes)
        listings = {}
        for folder in parity.folders_to_query(requirements):
            listings[folder] = await wc.models(folder)
    except Exception as e:
        return _err(502, f"воркер недоступен: {e}")
    merged = parity.merge_folder_listings(requirements, listings)
    report = parity.check(requirements, classes, merged, record.get("model_remap"))
    return web.json_response(report)


@routes.post("/gpuraid/workers/{wid}/download_model")
async def workers_download(request):
    _guard_master(request)
    wid = request.match_info["wid"]
    record = REGISTRY.get(wid)
    if record is None:
        return _err(404, "worker not found")
    payload = await _json(request)
    if wid == LOCAL_ID:
        task_id = downloads.start(asyncio.get_running_loop(), payload.get("folder"),
                                  payload.get("url"), payload.get("filename"),
                                  payload.get("hf_token"), payload.get("civitai_token"))
        return web.json_response({"task_id": task_id})
    try:
        body = await REGISTRY.client(record).download_model(payload)
        return web.json_response(body)
    except Exception as e:
        return _err(502, str(e))


@routes.get("/gpuraid/workers/{wid}/download_status/{task_id}")
async def workers_download_status(request):
    _guard_master(request)
    wid = request.match_info["wid"]
    task_id = request.match_info["task_id"]
    if wid == LOCAL_ID:
        return web.json_response(downloads.status(task_id) or {"state": "unknown"})
    record = REGISTRY.get(wid)
    if record is None:
        return _err(404, "worker not found")
    try:
        data = await REGISTRY.client(record).download_status(task_id)
        return web.json_response(data or {"state": "unknown"})
    except Exception as e:
        return _err(502, str(e))


# ---------------------------------------------------------------------------
# воркер-эндпоинты (скачивание моделей на себя)
# ---------------------------------------------------------------------------

@routes.post("/gpuraid/download_model")
async def self_download(request):
    data = await _json(request)
    folder = data.get("folder")
    url = data.get("url")
    if not folder or not url:
        return _err(400, "folder и url обязательны")
    try:
        task_id = downloads.start(asyncio.get_running_loop(), folder, url,
                                  data.get("filename"), data.get("hf_token"),
                                  data.get("civitai_token"))
    except ValueError as e:
        return _err(400, str(e))
    return web.json_response({"task_id": task_id})


@routes.get("/gpuraid/download_status/{task_id}")
async def self_download_status(request):
    return web.json_response(downloads.status(request.match_info["task_id"]) or {"state": "unknown"})


@routes.get("/gpuraid/catalog")
async def catalog(request):
    return web.json_response({"catalog": MODEL_CATALOG})


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

@routes.post("/gpuraid/stripe")
async def stripe(request):
    _guard_master(request)
    data = await _json(request)
    graph = data.get("graph")
    if not isinstance(graph, dict) or not graph:
        return _err(400, "graph отсутствует")
    try:
        job = await MANAGER.start_stripe(graph, data.get("workflow_ui"), data.get("client_id") or "")
    except RewriteError as e:
        return _err(409, e)
    except Exception as e:
        log.exception("stripe start failed")
        return _err(500, e)
    return web.json_response({
        "job_id": job.job_id,
        "units": len(job.units),
        "workers": [{"id": r["id"], "name": r["name"]} for r in job.eligible],
        "excluded": job.excluded,
    })


@routes.post("/gpuraid/offload")
async def offload(request):
    _guard_master(request)
    data = await _json(request)
    graph = data.get("graph")
    if not isinstance(graph, dict) or not graph:
        return _err(400, "graph отсутствует")
    try:
        job = await MANAGER.start_offload(
            graph, data.get("workflow_ui"), data.get("worker_id"),
            data.get("label") or "offload", data.get("client_id") or "",
        )
    except RewriteError as e:
        return _err(409, e)
    except Exception as e:
        log.exception("offload start failed")
        return _err(500, e)
    return web.json_response({"job_id": job.job_id, "warnings": job.warnings})


@routes.get("/gpuraid/jobs")
async def jobs_list(request):
    _guard_master(request)
    active = [j.snapshot() for j in MANAGER.jobs.values() if not j.done_event.is_set()]
    return web.json_response({"active": active, "history": MANAGER.history})


@routes.get("/gpuraid/jobs/{job_id}")
async def jobs_get(request):
    _guard_master(request)
    job = MANAGER.jobs.get(request.match_info["job_id"])
    if job is None:
        return _err(404, "job not found")
    return web.json_response(job.snapshot())


@routes.post("/gpuraid/jobs/{job_id}/cancel")
async def jobs_cancel(request):
    _guard_master(request)
    ok = await MANAGER.cancel(request.match_info["job_id"])
    return web.json_response({"cancelled": ok})


# ---------------------------------------------------------------------------
# long video
# ---------------------------------------------------------------------------

@routes.post("/gpuraid/longvideo/start")
async def lv_start(request):
    _guard_master(request)
    data = await _json(request)
    graph = data.get("graph")
    if not isinstance(graph, dict) or not graph:
        return _err(400, "graph отсутствует")
    try:
        job = await lv.start(graph, data.get("params") or {}, data.get("client_id") or "")
    except RewriteError as e:
        return _err(409, e)
    except Exception as e:
        log.exception("longvideo start failed")
        return _err(500, e)
    return web.json_response({"job_id": job.job_id, "label": job.label})


@routes.get("/gpuraid/longvideo")
async def lv_list(request):
    _guard_master(request)
    return web.json_response({"projects": lv.list_jobs()})


@routes.get("/gpuraid/longvideo/{label}")
async def lv_get(request):
    _guard_master(request)
    manifest = lv.load_manifest(request.match_info["label"])
    if manifest is None:
        return _err(404, "проект не найден")
    view = dict(manifest)
    view.pop("template_graph", None)
    view.pop("spec_meta", None)
    return web.json_response(view)


@routes.post("/gpuraid/longvideo/{label}/rerender")
async def lv_rerender(request):
    _guard_master(request)
    data = await _json(request)
    try:
        job = await lv.rerender_segment(request.match_info["label"],
                                        int(data.get("index", -1)), data.get("seed"))
    except RewriteError as e:
        return _err(409, e)
    return web.json_response({"job_id": job.job_id})


@routes.post("/gpuraid/longvideo/{label}/export")
async def lv_export(request):
    _guard_master(request)
    data = await _json(request)
    try:
        final = await lv.export(request.match_info["label"], data.get("order"),
                                data.get("trims"), data.get("crossfade_s") or 0)
    except RewriteError as e:
        return _err(409, e)
    except Exception as e:
        log.exception("export failed")
        return _err(500, e)
    return web.json_response({"final": final})


# ---------------------------------------------------------------------------
# запуск фоновых задач
# ---------------------------------------------------------------------------

async def _on_startup(app):
    loop = asyncio.get_running_loop()
    REGISTRY.start_heartbeat(loop)
    try:
        results.gc_jobs(REGISTRY.settings().get("keep_last_jobs", 5))
    except Exception:
        pass
    log.info("GPU RAID v%s: маршруты активны, heartbeat запущен", VERSION)


server.PromptServer.instance.app.on_startup.append(_on_startup)
