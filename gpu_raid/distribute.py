"""Рассылка моделей на воркеров: план по текущему графу и параллельная закачка.

План строится из того же `extract_requirements`, что и parity: какие модели
нужны графу на канве, у кого из воркеров они есть, у кого нет и известен ли
источник. Закачка идёт НА воркерах — каждый тянет файл сам с публичной ссылки
(мастер только раздаёт команды и опрашивает прогресс), поэтому три машины
качают 40 ГБ одновременно и независимо от домашнего канала.
"""

import logging
import time

from . import modelsrc, parity
from .graph_rewrite import extract_requirements, strip_markers
from .workers import LOCAL_ID, REGISTRY

log = logging.getLogger("gpu_raid")

CLASSES_TTL_S = 600
# ключ f"{worker_id}|{folder}/{filename}" -> запись о запущенной закачке
TASKS = {}
_CLASSES = {}   # worker_id -> (ts, set(classes))


def _flat(names):
    return set(names) | {str(n).replace("\\", "/").split("/")[-1] for n in names}


async def _worker_models(record, folders):
    """{folder: [имена]} — для мастера читаем локально, для воркера по HTTP."""
    if record["id"] == LOCAL_ID:
        import folder_paths

        out = {}
        for folder in folders:
            try:
                out[folder] = list(folder_paths.get_filename_list(folder))
            except Exception:
                out[folder] = []
        return out
    wc = REGISTRY.client(record)
    return {folder: await wc.models(folder) for folder in folders}


async def _worker_classes(record):
    """Набор классов нод воркера (мастер знает свои из реестра ComfyUI)."""
    wid = record["id"]
    if wid == LOCAL_ID:
        import nodes as comfy_nodes

        return set(comfy_nodes.NODE_CLASS_MAPPINGS.keys())
    cached = _CLASSES.get(wid)
    if cached and time.time() - cached[0] < CLASSES_TTL_S:
        return cached[1]
    classes = set((await REGISTRY.client(record).object_info()).keys())
    _CLASSES[wid] = (time.time(), classes)
    return classes


async def plan(graph):
    """Матрица «модель × воркер» + недостающие ноды. Ничего не качает."""
    req = extract_requirements(strip_markers(graph or {}))
    folders = parity.folders_to_query(req)
    rows = {}
    workers_view = []

    for record in REGISTRY.enabled_records():
        wid = record["id"]
        view = {"id": wid, "name": record["name"], "error": "", "missing_classes": []}
        merged, classes = {}, set()
        try:
            merged = parity.merge_folder_listings(req, await _worker_models(record, folders))
            classes = await _worker_classes(record)
        except Exception as e:
            view["error"] = f"{type(e).__name__}: {e}"
        if classes:
            view["missing_classes"] = sorted(
                ct for ct in req["classes"]
                if ct not in classes and not ct.startswith("GPURAID_"))
        workers_view.append(view)

        remap = record.get("model_remap") or {}
        for folder, names in req["models"].items():
            have = _flat(merged.get(folder, []))
            for name in sorted(names):
                row = rows.setdefault(modelsrc.key(folder, name), {
                    "folder": folder, "filename": name, "workers": {},
                })
                if view["error"]:
                    row["workers"][wid] = "unknown"
                    continue
                candidate = (remap.get(folder) or {}).get(name) or name
                flat = candidate.replace("\\", "/").split("/")[-1]
                row["workers"][wid] = "have" if (candidate in have or flat in have)  else "missing"

    for row in rows.values():
        source = modelsrc.resolve(row["filename"], row["folder"])
        row["url"] = (source or {}).get("url", "")
        row["size_gb"] = (source or {}).get("size_gb")
        row["missing_on"] = sorted(w for w, s in row["workers"].items() if s == "missing")
    return {
        "workers": workers_view,
        "models": sorted(rows.values(), key=lambda r: (r["folder"], r["filename"])),
    }


def _task_key(wid, folder, filename):
    return f"{wid}|{modelsrc.key(folder, filename)}"


async def _start_one(record, folder, filename, url):
    from . import secrets as secret_store

    payload = {
        "folder": folder, "url": url, "filename": filename,
        "hf_token": secret_store.get("hf_token") or None,
        "civitai_token": secret_store.get("civitai_token") or None,
    }
    if record["id"] == LOCAL_ID:
        import asyncio

        from . import downloads

        return downloads.start(asyncio.get_running_loop(), folder, url, filename,
                               payload["hf_token"], payload["civitai_token"])
    body = await REGISTRY.client(record).download_model(payload)
    task_id = (body or {}).get("task_id")
    if not task_id:
        raise RuntimeError("воркер не вернул task_id")
    return task_id


async def start(items):
    """items: [{folder, filename, url?, workers: [worker_id]}] -> что запустилось."""
    started, errors = [], []
    for item in items or []:
        folder = str(item.get("folder") or "").strip()
        filename = str(item.get("filename") or "").strip()
        url = modelsrc.normalize_url(item.get("url") or "")
        if not url:
            source = modelsrc.resolve(filename, folder)
            url = (source or {}).get("url", "")
        if not url:
            errors.append(f"{filename}: нет источника — добавьте ссылку в библиотеку")
            continue
        for wid in item.get("workers") or []:
            record = REGISTRY.get(wid)
            if record is None:
                errors.append(f"{filename}: воркер {wid} не найден")
                continue
            key = _task_key(wid, folder, filename)
            live = TASKS.get(key)
            if live and live.get("state") in ("starting", "downloading"):
                continue      # уже качается — второй раз не запускаем
            try:
                task_id = await _start_one(record, folder, filename, url)
            except Exception as e:
                errors.append(f"{filename} → {record['name']}: {e}")
                continue
            TASKS[key] = {
                "worker_id": wid, "worker": record["name"], "folder": folder,
                "filename": filename, "task_id": task_id, "state": "starting",
                "bytes_done": 0, "bytes_total": 0, "error": "", "started": time.time(),
            }
            started.append(dict(TASKS[key]))
    return {"started": started, "errors": errors}


async def progress():
    """Опрашивает воркеров по всем живым закачкам и отдаёт свежий срез."""
    out = []
    for key, task in list(TASKS.items()):
        if task["state"] in ("done", "error"):
            # готовые держим ещё пять минут, чтобы UI успел показать итог
            if time.time() - task.get("finished", time.time()) > 300:
                TASKS.pop(key, None)
                continue
            out.append(task)
            continue
        record = REGISTRY.get(task["worker_id"])
        if record is None:
            task.update(state="error", error="воркер удалён", finished=time.time())
            out.append(task)
            continue
        try:
            if task["worker_id"] == LOCAL_ID:
                from . import downloads

                status = downloads.status(task["task_id"]) or {"state": "unknown"}
            else:
                status = await REGISTRY.client(record).download_status(task["task_id"]) or {}
        except Exception as e:
            status = {"state": "unknown", "error": str(e)}
        task.update({
            "state": status.get("state", task["state"]),
            "bytes_done": status.get("bytes_done", task["bytes_done"]),
            "bytes_total": status.get("bytes_total", task["bytes_total"]),
            "error": status.get("error", ""),
        })
        if task["state"] in ("done", "error"):
            task["finished"] = time.time()
            _CLASSES.pop(task["worker_id"], None)   # инвентарь поменялся
        out.append(task)
    return {"tasks": sorted(out, key=lambda t: (t["worker"], t["filename"]))}
