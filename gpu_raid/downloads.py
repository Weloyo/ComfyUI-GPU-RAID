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

from . import config

log = logging.getLogger("gpu_raid")

# запас поверх размера файла: рядом лежит .part, а платформе нужно место под свои
# логи — упереться в ноль на 39-м гигабайте из 40 обиднее, чем не начать
FREE_SPACE_MARGIN = 1 << 30

ATTEMPTS = 6            # обрыв многогигабайтной закачки — норма, а не исключение
RETRY_PAUSE_S = 3       # пауза растёт линейно: 3, 6, 9…

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


def _part_size(tmp):
    try:
        return os.path.getsize(tmp)
    except OSError:
        return 0


async def _fetch_once(session, url, headers, tmp, task, dest_dir):
    """Одна попытка: докачивает .part с того места, где оборвалось.

    Возвращает True, если файл дошёл до конца.
    """
    have = _part_size(tmp)
    hdrs = dict(headers)
    if have:
        hdrs["Range"] = f"bytes={have}-"
    async with session.get(url, headers=hdrs, allow_redirects=True) as r:
        if r.status == 416 and have:
            return True                  # сервер говорит «дальше нечего» — всё уже есть
        if r.status not in (200, 206):
            raise RuntimeError(f"HTTP {r.status}")
        if have and r.status == 200:
            have = 0                     # Range проигнорирован — начинаем сначала
        total = int(r.headers.get("Content-Length") or 0) + have
        if total:
            task["bytes_total"] = total
        _check_space(dest_dir, max(0, total - have))
        task["state"] = "downloading"
        task["bytes_done"] = have
        with open(tmp, "ab" if have else "wb") as f:
            async for chunk in r.content.iter_chunked(1 << 20):
                f.write(chunk)
                task["bytes_done"] += len(chunk)
    total = task.get("bytes_total") or 0
    return not total or _part_size(tmp) >= total


async def _run(task_id, folder, url, filename, hf_token, civitai_token):
    task = TASKS[task_id]
    try:
        dest_dir, link_dir = _dirs(folder)
        headers = {}
        host = (urlsplit(url).hostname or "").lower()
        # Токен цепляем ТОЛЬКО к точному хосту (или его поддомену): подстрочная
        # проверка уносила бы Bearer на huggingface.co.evil.com/resolve/…
        if hf_token and (host == "huggingface.co" or host.endswith(".huggingface.co")):
            headers["Authorization"] = f"Bearer {hf_token}"
        if (civitai_token and (host == "civitai.com" or host.endswith(".civitai.com"))
                and "token=" not in url):
            url += ("&" if "?" in url else "?") + "token=" + civitai_token

        dest = os.path.join(dest_dir, filename)
        tmp = dest + ".part"
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=120)
        # Многогигабайтная закачка по чужому каналу рвётся штатно: живьём HF
        # оборвал 11 ГБ на 25% («Response payload is not completed»). Без
        # докачки это означало бы начинать заново — и так до бесконечности.
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(1, ATTEMPTS + 1):
                try:
                    if await _fetch_once(session, url, headers, tmp, task, dest_dir):
                        break
                    reason = "файл пришёл не целиком"
                except (aiohttp.ClientError, TimeoutError, OSError) as e:
                    if isinstance(e, OSError) and not isinstance(e, aiohttp.ClientError):
                        raise           # диск/права — повторять бессмысленно
                    reason = f"{type(e).__name__}: {e}"
                if attempt == ATTEMPTS:
                    raise RuntimeError(
                        f"{reason} (попыток: {ATTEMPTS}, скачано "
                        f"{_part_size(tmp) / 2**30:.1f} ГБ — повтор продолжит с этого места)")
                task["error"] = f"обрыв на попытке {attempt}/{ATTEMPTS}: {reason}"
                log.warning("GPU RAID: %s — %s, докачиваю", filename, reason)
                await asyncio.sleep(RETRY_PAUSE_S * attempt)
        os.replace(tmp, dest)
        _publish(dest, link_dir, filename)
        task["error"] = ""
        task["state"] = "done"
        log.info("GPU RAID: скачано %s -> %s", filename, dest_dir)
    except Exception as e:
        task["state"] = "error"
        task["error"] = f"{type(e).__name__}: {e}"
        log.warning("GPU RAID: download %s failed: %s", filename, e)


def start(loop, folder, url, filename=None, hf_token=None, civitai_token=None):
    # filename недоверенный (тело запроса) — режем до базового имени, иначе
    # '../…' писал бы модель за пределы каталога моделей
    filename = config.safe_filename(filename or _guess_filename(url))
    # дедуп по (folder, filename): без него два конкурентных вызова (обходной
    # путь мимо distribute.TASKS — например, повторный клик по «Скачать на
    # воркера» или прямой POST /gpuraid/download_model) льют в один .part
    # независимыми хендлами (wb/ab) — смешанный мусор публикуется как done без
    # проверки целостности. Функция синхронная — между проверкой и записью в
    # TASKS нет await, гонки на этом event loop'е нет.
    for existing_id, t in TASKS.items():
        if (t.get("folder") == folder and t.get("filename") == filename
                and t.get("state") in ("starting", "downloading")):
            return existing_id
    task_id = uuid.uuid4().hex[:12]
    TASKS[task_id] = {
        "state": "starting", "bytes_done": 0, "bytes_total": 0,
        "filename": filename, "folder": folder, "error": "", "started": time.time(),
    }
    loop.create_task(_run(task_id, folder, url, filename, hf_token, civitai_token))
    return task_id


def status(task_id):
    return TASKS.get(task_id)
