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

from . import (auth, config, downloads, events, kaggle_api, parity, providers,
               results, storyplan)
from . import longvideo as lv
from . import pipeline
from . import secrets as secret_store
from . import story
from .consts import COLAB_NOTEBOOK_URL, MODEL_CATALOG, REPO_URL, VERSION
from .dispatcher import MANAGER
from .graph_rewrite import RewriteError, extract_requirements
from .lifecycle import LIFECYCLE
from .rendezvous import RENDEZVOUS
from .workers import LOCAL_ID, REGISTRY

log = logging.getLogger("gpu_raid")
routes = server.PromptServer.instance.routes

_obj_cache = {}  # worker_id -> (ts, set(classes))
_STARTED = time.time()


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
        "platform": os.environ.get("GPURAID_PLATFORM", ""),
        "session": os.environ.get("GPURAID_SESSION", ""),
        "started_ts": _STARTED,
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
# настройки и секреты
# ---------------------------------------------------------------------------

@routes.get("/gpuraid/settings")
async def settings_get(request):
    _guard_master(request)
    return web.json_response({
        "settings": REGISTRY.settings(),
        "secrets": secret_store.public_view(),
    })


_SETTINGS_KEYS = ("lifecycle", "llm", "rendezvous", "timeouts", "connections",
                  "max_retries", "keep_last_jobs", "heartbeat_s", "free_after_job")


@routes.patch("/gpuraid/settings")
async def settings_patch(request):
    _guard_master(request)
    data = await _json(request)
    patch = {k: v for k, v in data.items() if k in _SETTINGS_KEYS}
    if not patch:
        return _err(400, "нет известных ключей настроек")
    await REGISTRY.update_settings(patch)
    return web.json_response({"settings": REGISTRY.settings()})


@routes.post("/gpuraid/secrets")
async def secrets_set(request):
    _guard_master(request)
    data = await _json(request)
    if "kaggle_json" in data:
        secret_store.save_kaggle_json(data.pop("kaggle_json"))
    secret_store.save(data)
    return web.json_response({"ok": True, "secrets": secret_store.public_view()})


# ---------------------------------------------------------------------------
# подключения: одно место для всех внешних регистраций (см. providers.py)
# ---------------------------------------------------------------------------

@routes.get("/gpuraid/connections")
async def connections_get(request):
    _guard_master(request)
    return web.json_response(
        providers.status_view(REGISTRY.settings(), secret_store.public_view()))


@routes.post("/gpuraid/connections/{pid}")
async def connections_save(request):
    _guard_master(request)
    pid = request.match_info["pid"]
    data = await _json(request)
    try:
        await providers.save(pid, data)
    except KeyError:
        return _err(404, f"нет такого подключения: {pid}")
    except ValueError as e:
        return _err(400, e)
    except Exception as e:
        log.exception("connections save failed")
        return _err(500, e)
    result = await providers.check(pid) if data.get("check", True) else {}
    return web.json_response({
        "ok": True, "check": result,
        "status": providers.status_view(REGISTRY.settings(), secret_store.public_view()),
    })


@routes.post("/gpuraid/connections/{pid}/check")
async def connections_check(request):
    _guard_master(request)
    pid = request.match_info["pid"]
    try:
        result = await providers.check(pid)
    except KeyError:
        return _err(404, f"нет такого подключения: {pid}")
    return web.json_response({"check": result})


@routes.post("/gpuraid/connections/{pid}/forget")
async def connections_forget(request):
    _guard_master(request)
    pid = request.match_info["pid"]
    try:
        check = await providers.forget(pid)
    except KeyError:
        return _err(404, f"нет такого подключения: {pid}")
    return web.json_response({
        "check": check,
        "status": providers.status_view(REGISTRY.settings(), secret_store.public_view()),
    })


@routes.post("/gpuraid/connections/{pid}/action/{action}")
async def connections_action(request):
    _guard_master(request)
    pid, action = request.match_info["pid"], request.match_info["action"]
    handlers = {
        ("github", "create_gist"): providers.create_gist,
        ("kaggle", "install_cli"): providers.install_kaggle_cli,
    }
    fn = handlers.get((pid, action))
    if fn is None:
        return _err(404, f"нет такого действия: {pid}/{action}")
    try:
        result = await fn()
    except Exception as e:
        return _err(409, e)
    check = await providers.check(pid)
    return web.json_response({
        "result": result, "check": check,
        "status": providers.status_view(REGISTRY.settings(), secret_store.public_view()),
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
# lifecycle / rendezvous / kaggle
# ---------------------------------------------------------------------------

@routes.get("/gpuraid/lifecycle")
async def lifecycle_get(request):
    _guard_master(request)
    return web.json_response({
        "workers": LIFECYCLE.preview(),
        "rendezvous": RENDEZVOUS.snapshot(REGISTRY.settings(), secret_store.public_view()),
        "colab_notebook_url": COLAB_NOTEBOOK_URL,
    })


@routes.post("/gpuraid/workers/{wid}/stop")
async def worker_stop(request):
    _guard_master(request)
    wid = request.match_info["wid"]
    if wid == LOCAL_ID:
        return _err(400, "локальный инстанс не останавливается")
    record = REGISTRY.get(wid)
    if record is None:
        return _err(404, "worker not found")
    ok = await LIFECYCLE.stop_worker(record, "остановлено вручную")
    return web.json_response({"stopped": ok})


@routes.post("/gpuraid/kaggle/start")
async def kaggle_start(request):
    _guard_master(request)
    data = await _json(request)
    settings = REGISTRY.settings()
    gist_id = (settings.get("rendezvous") or {}).get("gist_id", "")
    secrets_view = secret_store.public_view()
    if not gist_id or not secrets_view["has_gh_token"]:
        return _err(409, "автозапуску Kaggle нужен gist-rendezvous: задайте gist_id "
                         "и GitHub-токен в панели (Режимы)")
    if not secrets_view["has_kaggle_json"]:
        return _err(409, "kaggle.json не сохранён (панель → Режимы → секреты)")
    params = {
        "repo_url": data.get("repo_url") or REPO_URL,
        "gist_id": gist_id,
        "model_preset": data.get("model_preset") or "none",
        "max_session_min": (settings.get("lifecycle") or {}).get("budget_min") or 0,
        "name_prefix": data.get("name_prefix") or "kaggle",
    }
    try:
        result = await kaggle_api.push(params)
    except RuntimeError as e:
        return _err(409, e)
    except Exception as e:
        log.exception("kaggle push failed")
        return _err(500, e)
    events.toast("info", f"Kaggle-кернел «{result['kernel']}» запущен — воркер "
                         "зарегистрируется сам через несколько минут")
    return web.json_response(result)


@routes.get("/gpuraid/kaggle/status")
async def kaggle_status(request):
    _guard_master(request)
    try:
        return web.json_response(await kaggle_api.status())
    except RuntimeError as e:
        return _err(409, e)
    except Exception as e:
        return _err(500, e)


# ---------------------------------------------------------------------------
# воркер-эндпоинты (скачивание моделей на себя)
# ---------------------------------------------------------------------------

@routes.post("/gpuraid/worker/shutdown")
async def worker_shutdown(request):
    """Воркер-роль: мастер просит инстанс погаситься (токен-защита в middleware).

    Пишем sentinel-файл — его видит watchdog в ноутбуке/кернеле и завершает
    рантайм платформенно (Colab: runtime.unassign, Kaggle: выход из скрипта).
    """
    path = os.environ.get("GPURAID_SHUTDOWN_FILE")
    if not path:
        return _err(409, "инстанс не под watchdog'ом (нет GPURAID_SHUTDOWN_FILE) — "
                         "остановите рантайм вручную")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(str(int(time.time())))
    except OSError as e:
        return _err(500, e)
    return web.json_response({"ok": True})


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


_EXAMPLES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "docs", "example_workflows")


@routes.get("/gpuraid/example/{name}")
async def example_workflow(request):
    """Пример-workflow из docs/example_workflows (whitelist по именам файлов)."""
    _guard_master(request)
    name = config.sanitize_name(request.match_info["name"])
    path = os.path.join(_EXAMPLES_DIR, f"{name}.json")
    data = config.load_json(path, None)
    if data is None:
        try:
            available = sorted(f[:-5] for f in os.listdir(_EXAMPLES_DIR)
                               if f.endswith(".json"))
        except OSError:
            available = []
        return _err(404, f"пример «{name}» не найден (есть: {', '.join(available)})")
    return web.json_response({"name": name, "workflow": data})


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
    return web.json_response(storyplan.trim_manifest_view(manifest))


@routes.post("/gpuraid/longvideo/{label}/rerender")
async def lv_rerender(request):
    _guard_master(request)
    data = await _json(request)
    try:
        job = await lv.rerender_segment(request.match_info["label"],
                                        int(data.get("index", -1)), data.get("seed"),
                                        data.get("prompt"))
    except RewriteError as e:
        return _err(409, e)
    return web.json_response({"job_id": job.job_id})


@routes.patch("/gpuraid/longvideo/{label}/segments/{index}")
async def lv_segment_patch(request):
    _guard_master(request)
    data = await _json(request)
    try:
        seg = await lv.update_segment(request.match_info["label"],
                                      int(request.match_info["index"]), data)
    except RewriteError as e:
        return _err(409, e)
    except ValueError as e:
        return _err(400, e)
    return web.json_response({"segment": seg})


@routes.patch("/gpuraid/longvideo/{label}/edit")
async def lv_edit_patch(request):
    _guard_master(request)
    data = await _json(request)
    try:
        edit = await lv.update_edit(request.match_info["label"], data)
    except RewriteError as e:
        return _err(409, e)
    except (TypeError, ValueError) as e:
        return _err(400, e)
    return web.json_response({"edit": edit})


@routes.delete("/gpuraid/longvideo/{label}")
async def lv_delete(request):
    _guard_master(request)
    try:
        await lv.delete_project(request.match_info["label"])
    except RewriteError as e:
        return _err(404, e)
    return web.json_response({"deleted": True})


@routes.post("/gpuraid/longvideo/{label}/export")
async def lv_export(request):
    _guard_master(request)
    data = await _json(request)
    try:
        final = await lv.export(request.match_info["label"], data.get("order"),
                                data.get("trims"), data.get("crossfade_s"))
    except RewriteError as e:
        return _err(409, e)
    except Exception as e:
        log.exception("export failed")
        return _err(500, e)
    return web.json_response({"final": final})


# ---------------------------------------------------------------------------
# pipeline (шардинг «Большая модель»)
# ---------------------------------------------------------------------------

@routes.post("/gpuraid/pipeline/analyze")
async def pipeline_analyze(request):
    _guard_master(request)
    data = await _json(request)
    graph = data.get("graph")
    if not isinstance(graph, dict) or not graph:
        return _err(400, "graph отсутствует")
    try:
        report = await pipeline.analyze(graph)
    except RewriteError as e:
        return _err(409, e)
    except Exception as e:
        log.exception("pipeline analyze failed")
        return _err(500, e)
    return web.json_response(report)


@routes.post("/gpuraid/pipeline/start")
async def pipeline_start(request):
    _guard_master(request)
    data = await _json(request)
    graph = data.get("graph")
    if not isinstance(graph, dict) or not graph:
        return _err(400, "graph отсутствует")
    try:
        job = await pipeline.start(graph, data.get("workflow_ui"),
                                   data.get("placement") or {},
                                   data.get("label") or "pipeline",
                                   data.get("client_id") or "")
    except RewriteError as e:
        return _err(409, e)
    except Exception as e:
        log.exception("pipeline start failed")
        return _err(500, e)
    return web.json_response({"job_id": job.job_id, "stages": len(job.units)})


# ---------------------------------------------------------------------------
# story («Сценарист»)
# ---------------------------------------------------------------------------

@routes.post("/gpuraid/story/plan")
async def story_plan(request):
    _guard_master(request)
    data = await _json(request)
    graph = data.get("graph")
    if not isinstance(graph, dict) or not graph:
        return _err(400, "graph отсутствует")
    try:
        manifest = await story.plan(graph, data.get("params") or {},
                                    data.get("keyframe_graph"),
                                    data.get("client_id") or "")
    except RewriteError as e:
        return _err(409, e)
    except Exception as e:
        log.exception("story plan failed")
        return _err(500, e)
    return web.json_response({"label": manifest["label"],
                              "manifest": storyplan.trim_manifest_view(manifest)})


@routes.post("/gpuraid/story/{label}/keyframe_template")
async def story_kf_template(request):
    _guard_master(request)
    data = await _json(request)
    graph = data.get("graph")
    if not isinstance(graph, dict) or not graph:
        return _err(400, "graph отсутствует")
    try:
        await story.set_keyframe_template(request.match_info["label"], graph)
    except RewriteError as e:
        return _err(409, e)
    return web.json_response({"ok": True})


@routes.post("/gpuraid/story/{label}/keyframes/render")
async def story_kf_render(request):
    _guard_master(request)
    data = await _json(request)
    try:
        job = await story.render_keyframes(request.match_info["label"],
                                           data.get("indices"),
                                           data.get("client_id") or "")
    except RewriteError as e:
        return _err(409, e)
    except Exception as e:
        log.exception("story keyframes failed")
        return _err(500, e)
    return web.json_response({"job_id": job.job_id})


@routes.post("/gpuraid/story/{label}/render")
async def story_render(request):
    _guard_master(request)
    data = await _json(request)
    try:
        job = await story.render_segments(request.match_info["label"],
                                          data.get("indices"),
                                          data.get("client_id") or "")
    except RewriteError as e:
        return _err(409, e)
    except Exception as e:
        log.exception("story render failed")
        return _err(500, e)
    return web.json_response({"job_id": job.job_id})


@routes.patch("/gpuraid/story/{label}/keyframes/{index}")
async def story_kf_patch(request):
    _guard_master(request)
    data = await _json(request)
    try:
        kf = await story.update_keyframe(request.match_info["label"],
                                         int(request.match_info["index"]), data)
    except RewriteError as e:
        return _err(409, e)
    except ValueError as e:
        return _err(400, e)
    return web.json_response({"keyframe": kf})


@routes.post("/gpuraid/story/{label}/keyframes/{index}/rerender")
async def story_kf_rerender(request):
    _guard_master(request)
    data = await _json(request)
    try:
        job = await story.rerender_keyframe(request.match_info["label"],
                                            int(request.match_info["index"]),
                                            data.get("seed"), data.get("prompt"),
                                            data.get("client_id") or "")
    except RewriteError as e:
        return _err(409, e)
    return web.json_response({"job_id": job.job_id})


# ---------------------------------------------------------------------------
# запуск фоновых задач
# ---------------------------------------------------------------------------

async def _on_startup(app):
    loop = asyncio.get_running_loop()
    REGISTRY.start_heartbeat(loop)
    LIFECYCLE.start(loop)
    RENDEZVOUS.start(loop)
    try:
        results.gc_jobs(REGISTRY.settings().get("keep_last_jobs", 5))
    except Exception:
        pass
    log.info("GPU RAID v%s: маршруты, heartbeat, lifecycle и rendezvous запущены", VERSION)


server.PromptServer.instance.app.on_startup.append(_on_startup)
