"""Хранилище результатов юнитов + загрузка батча для Collector + GC."""

import logging
import os
import shutil
import time

from . import config

log = logging.getLogger("gpu_raid")


def job_dir(job_id):
    return os.path.join(config.temp_base(), job_id)


def recv_dir(job_id):
    d = os.path.join(job_dir(job_id), "recv")
    os.makedirs(d, exist_ok=True)
    return d


def unit_prefix(job_id, index):
    # forward slashes: тот же префикс исполняется и на linux-воркерах
    return f"gpuraid_tmp/{job_id}/u{index:04d}"


def load_batch(job_id):
    """Читает recv/*.png (порядок по имени = порядок юнитов) -> IMAGE-тензор [N,H,W,C]."""
    import numpy as np
    import torch
    from PIL import Image

    d = recv_dir(job_id)
    files = sorted(
        f for f in os.listdir(d) if f.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))
    )
    if not files:
        raise RuntimeError(f"GPU RAID: нет результатов для job {job_id} (каталог {d})")
    tensors = []
    shape = None
    for name in files:
        img = Image.open(os.path.join(d, name)).convert("RGB")
        arr = np.asarray(img, dtype=np.float32) / 255.0
        if shape is None:
            shape = arr.shape
        elif arr.shape != shape:
            raise RuntimeError(
                f"GPU RAID: размер {name} {arr.shape} не совпадает с {shape} — юниты должны быть одного размера"
            )
        tensors.append(torch.from_numpy(arr))
    return torch.stack(tensors, dim=0)


def gc_jobs(keep_last=5):
    base = config.temp_base()
    try:
        dirs = [
            (os.path.getmtime(os.path.join(base, n)), os.path.join(base, n))
            for n in os.listdir(base)
            if os.path.isdir(os.path.join(base, n))
        ]
    except OSError:
        return
    dirs.sort(reverse=True)
    for _, path in dirs[max(0, int(keep_last)):]:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def deliver_dir(label):
    """Уникальный каталог доставки offload: output/gpuraid/<label>_<ts>/"""
    name = f"{config.sanitize_name(label)}_{time.strftime('%Y%m%d_%H%M%S')}"
    d = os.path.join(config.deliver_base(), name)
    os.makedirs(d, exist_ok=True)
    return d, name
