"""Серверное скачивание моделей на этом инстансе (роль воркера).

POST /gpuraid/download_model {folder, url, filename?, hf_token?, civitai_token?}
Файл льётся стримом в models/<folder>/<filename>.part -> rename.
"""

import asyncio
import logging
import os
import shutil
import time
import uuid
from urllib.parse import urlsplit

import aiohttp
import folder_paths

log = logging.getLogger("gpu_raid")

# запас поверх размера файла: рядом лежит .part, а платформе нужно место под свои
# логи — упереться в ноль на 39-м гигабайте из 40 обиднее, чем не начать
FREE_SPACE_MARGIN = 1 << 30

TASKS = {}  # task_id -> {state, bytes_done, bytes_total, filename, folder, error, started}


def _dirs(folder):
    """(куда льём, куда ставим symlink или None).

    На Kaggle рабочий каталог воркера (`/kaggle/working`, там же ComfyUI и его
    `models/`) ограничен 20 ГБ — один набор моделей его переполняет. Поэтому
    bootstrap может увести веса на эфемерный диск через GPURAID_MODELS_DIR:
    файл ложится туда, а в `models/<folder>` остаётся symlink, и ComfyUI видит
    модель обычным образом.
    """
    paths = folder_paths.get_folder_paths(folder)
    if not paths:
        raise ValueError(f"неизвестная папка моделей: {folder}")
    os.makedirs(paths[0], exist_ok=True)
    scratch = os.environ.get("GPURAID_MODELS_DIR", "").strip()
    if not scratch:
        return paths[0], None
    store = os.path.join(scratch, folder)
    os.makedirs(store, exist_ok=True)
    return store, paths[0]


def _guess_filename(url):
    name = os.path.basename(urlsplit(url).path)
    return name or f"model_{int(time.time())}.safetensors"


def _check_space(dest_dir, need_bytes):
    """Ругаемся ДО закачки, а не на последнем гигабайте."""
    if not need_bytes:
        return
    free = shutil.disk_usage(dest_dir).free
    if free < need_bytes + FREE_SPACE_MARGIN:
        raise RuntimeError(
            f"не хватит места: нужно {need_bytes / 2**30:.1f} ГБ, "
            f"свободно {free / 2**30:.1f} ГБ в {dest_dir}")


def _publish(dest, link_dir, filename):
    """Symlink в настоящую папку моделей, если файл лежит на эфемерном диске."""
    if not link_dir:
        return
    link = os.path.join(link_dir, filename)
    if os.path.lexists(link):
        os.remove(link)
    try:
        os.symlink(dest, link)
    except OSError as e:
        raise RuntimeError(
            f"файл скачан в {dest}, но symlink в {link_dir} не создался ({e}) — "
            "уберите GPURAID_MODELS_DIR, чтобы качать прямо в models/") from e


async def _run(task_id, folder, url, filename, hf_token, civitai_token):
    task = TASKS[task_id]
    tmp = ""
    try:
        dest_dir, link_dir = _dirs(folder)
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
                _check_space(dest_dir, task["bytes_total"])
                task["state"] = "downloading"
                with open(tmp, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 20):
                        f.write(chunk)
                        task["bytes_done"] += len(chunk)
        os.replace(tmp, dest)
        _publish(dest, link_dir, filename)
        task["state"] = "done"
        log.info("GPU RAID: скачано %s -> %s", filename, dest_dir)
    except Exception as e:
        task["state"] = "error"
        task["error"] = f"{type(e).__name__}: {e}"
        log.warning("GPU RAID: download %s failed: %s", filename, e)
        # недокачанный .part на диске воркера — это те же гигабайты, из-за
        # которых упала следующая попытка
        try:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


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
