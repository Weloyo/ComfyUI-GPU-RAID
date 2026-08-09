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
# сколько терпеть подряд идущий "unknown" (сессия воркера могла на секунды
# пропасть при рестарте туннеля/ComfyUI watchdog'ом), прежде чем считать
# закачку потерянной вместе с сессией — не завершившейся тихо навсегда
UNKNOWN_TIMEOUT_S = 90


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


def is_downloading(worker_id):
    """Тянет ли воркер сейчас модель.

    Для lifecycle это тоже работа: качающий 40 ГБ воркер не выполняет заданий
    и без этой проверки выглядит простаивающим — политики eco/instant погасили
    бы его посреди закачки, и всё пришлось бы начинать заново.
    """
    return any(t.get("worker_id") == worker_id
               and t.get("state") in ("starting", "downloading")
               for t in TASKS.values())


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
            # резервируем слот ДО await: _start_one — полный раунд через
            # туннель (секунды), и без резерва два конкурентных POST (две
            # вкладки панели, ретрай после ошибки) проходят проверку выше
            # одновременно и оба стартуют закачку одного файла
            TASKS[key] = {
                "worker_id": wid, "worker": record["name"], "folder": folder,
                "filename": filename, "task_id": None, "state": "starting",
                "bytes_done": 0, "bytes_total": 0, "error": "", "started": time.time(),
            }
            try:
                task_id = await _start_one(record, folder, filename, url)
            except Exception as e:
                errors.append(f"{filename} → {record['name']}: {e}")
                TASKS.pop(key, None)     # откатываем резерв — слот не должен виснуть навсегда
                continue
            TASKS[key]["task_id"] = task_id
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
        new_state = status.get("state", task["state"])
        if new_state == "unknown":
            # is_downloading() для unknown уже отдаёт False (lifecycle воркера
            # не защищает), но САМА задача не терминальна — без таймаута висит
            # в UI вечно без прогресса и без ошибки, пока сессия воркера
            # (Kaggle/Colab) умерла вместе с закачкой
            since = task.get("unknown_since") or time.time()
            task["unknown_since"] = since
            if time.time() - since > UNKNOWN_TIMEOUT_S:
                task.update(state="error",
                            error="воркер перезапущен — закачка потеряна, запустите снова",
                            finished=time.time())
                _CLASSES.pop(task["worker_id"], None)
                out.append(task)
                continue
        else:
            task.pop("unknown_since", None)
        task.update({
            "state": new_state,
            "bytes_done": status.get("bytes_done", task["bytes_done"]),
            "bytes_total": status.get("bytes_total", task["bytes_total"]),
            "error": status.get("error", ""),
        })
        if task["state"] in ("done", "error"):
            task["finished"] = time.time()
            _CLASSES.pop(task["worker_id"], None)   # инвентарь поменялся
        out.append(task)
    return {"tasks": sorted(out, key=lambda t: (t["worker"], t["filename"]))}
