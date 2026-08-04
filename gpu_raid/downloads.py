"""Серверное скачивание моделей на этом инстансе (роль воркера).

POST /gpuraid/download_model {folder, url, filename?, hf_token?, civitai_token?}
Файл льётся стримом в models/<folder>/<filename>.part -> rename.
"""

import asyncio
import logging
import os
import time
import uuid
from urllib.parse import urlsplit

import aiohttp
import folder_paths

log = logging.getLogger("gpu_raid")

TASKS = {}  # task_id -> {state, bytes_done, bytes_total, filename, folder, error, started}


def _dest_dir(folder):
    paths = folder_paths.get_folder_paths(folder)
    if not paths:
        raise ValueError(f"неизвестная папка моделей: {folder}")
    os.makedirs(paths[0], exist_ok=True)
    return paths[0]


def _guess_filename(url):
    name = os.path.basename(urlsplit(url).path)
    return name or f"model_{int(time.time())}.safetensors"


async def _run(task_id, folder, url, filename, hf_token, civitai_token):
    task = TASKS[task_id]
    try:
        dest_dir = _dest_dir(folder)
        headers = {}
        host = (urlsplit(url).hostname or "").lower()
        if hf_token and "huggingface.co" in host:
            headers["Authorization"] = f"Bearer {hf_token}"
        if civitai_token and "civitai.com" in host and "token=" not in url:
            url += ("&" if "?" in url else "?") + "token=" + civitai_token

        dest = os.path.join(dest_dir, filename)
        tmp = dest + ".part"
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=True) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                task["bytes_total"] = int(r.headers.get("Content-Length") or 0)
                task["state"] = "downloading"
                with open(tmp, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 20):
                        f.write(chunk)
                        task["bytes_done"] += len(chunk)
        os.replace(tmp, dest)
        task["state"] = "done"
        log.info("GPU RAID: скачано %s -> %s", filename, dest_dir)
    except Exception as e:
        task["state"] = "error"
        task["error"] = f"{type(e).__name__}: {e}"
        log.warning("GPU RAID: download %s failed: %s", filename, e)


def start(loop, folder, url, filename=None, hf_token=None, civitai_token=None):
    filename = filename or _guess_filename(url)
    task_id = uuid.uuid4().hex[:12]
    TASKS[task_id] = {
        "state": "starting", "bytes_done": 0, "bytes_total": 0,
        "filename": filename, "folder": folder, "error": "", "started": time.time(),
    }
    loop.create_task(_run(task_id, folder, url, filename, hf_token, civitai_token))
    return task_id


def status(task_id):
    return TASKS.get(task_id)
