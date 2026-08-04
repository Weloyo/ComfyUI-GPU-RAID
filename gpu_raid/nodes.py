"""Ноды GPU RAID: Distributor, Collector, TiledUpscale."""

import logging
import os
import queue as thread_queue
import time
import uuid

log = logging.getLogger("gpu_raid")

from .consts import NODE_COLLECTOR, NODE_DISTRIBUTOR, NODE_TILED_UPSCALE, SAVE_NODE_ID
from . import results


class GPURaidDistributor:
    """Источник сида для распределяемой ветки.

    При запуске через GPU RAID каждый вариант получает seed = base + index.
    Без перехвата (расширение выключено / нет воркеров) — обычная нода-сид.
    """

    CATEGORY = "GPU RAID"
    RETURN_TYPES = ("INT", "INT")
    RETURN_NAMES = ("seed", "index")
    FUNCTION = "run"
    DESCRIPTION = (
        "Подключите выход seed к KSampler.seed. total_variants — сколько вариантов "
        "сгенерировать на всех GPU за одно нажатие Queue; min_vram_gb отсекает слабых воркеров."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF,
                                 "control_after_generate": True}),
                "total_variants": ("INT", {"default": 4, "min": 1, "max": 256}),
                "min_vram_gb": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 256.0, "step": 0.5}),
            }
        }

    def run(self, seed, total_variants, min_vram_gb):
        return (int(seed), 0)


class GPURaidCollector:
    """Собирает варианты со всех GPU в один IMAGE-батч.

    В обычном запуске — passthrough своего входа images. В распределённом запуске
    диспетчер ставит «хвост» графа с заполненным job_id, и нода читает готовые
    файлы юнитов с диска.
    """

    CATEGORY = "GPU RAID"
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    DESCRIPTION = "Поставьте после VAEDecode. Выход — батч всех вариантов со всех GPU."

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "optional": {
                "images": ("IMAGE",),
                "job_id": ("STRING", {"default": ""}),
            },
        }

    def run(self, images=None, job_id=""):
        job_id = (job_id or "").strip()
        if job_id:
            return (results.load_batch(job_id),)
        if images is None:
            raise RuntimeError(
                "GPU RAID Collector: подключите вход images (после VAEDecode) — "
                "он используется при обычном локальном запуске"
            )
        return (images,)


class GPURaidTiledUpscale:
    """Тайловый апскейл одной картинки на всех GPU (модельный апскейлер ESRGAN-класса).

    Локальная GPU считает тайлы в самой ноде, облачные — через диспетчер; отстающие
    тайлы в конце «воруются» обратно на локалку. Швы гасятся линейным фезером.
    """

    CATEGORY = "GPU RAID"
    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "upscale"
    DESCRIPTION = (
        "Апскейл большого изображения тайлами на всех доступных GPU. "
        "Модель должна существовать (или быть remap-нутой) на воркерах."
    )

    @classmethod
    def INPUT_TYPES(cls):
        import folder_paths

        return {
            "required": {
                "image": ("IMAGE",),
                "model_name": (folder_paths.get_filename_list("upscale_models"),),
                "tile": ("INT", {"default": 768, "min": 256, "max": 2048, "step": 64}),
                "overlap": ("INT", {"default": 64, "min": 16, "max": 256, "step": 8}),
                "use_workers": ("BOOLEAN", {"default": True}),
            }
        }

    # ---------------- helpers ----------------

    @staticmethod
    def _starts(size, tile, step):
        if size <= tile:
            return [0]
        starts = list(range(0, size - tile + 1, step))
        if starts[-1] != size - tile:
            starts.append(size - tile)
        return starts

    @staticmethod
    def _save_png(tensor_hw_c, path):
        import numpy as np
        from PIL import Image

        os.makedirs(os.path.dirname(path), exist_ok=True)
        arr = (tensor_hw_c.clamp(0, 1).cpu().numpy() * 255.0).astype(np.uint8)
        Image.fromarray(arr).save(path, compress_level=4)

    @staticmethod
    def _load_png(path):
        import numpy as np
        import torch
        from PIL import Image

        arr = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) / 255.0
        return torch.from_numpy(arr)

    @staticmethod
    def _tile_graph(rel_image, model_name, prefix):
        return {
            "1": {"class_type": "LoadImage", "inputs": {"image": rel_image}},
            "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model_name}},
            "3": {"class_type": "ImageUpscaleWithModel",
                  "inputs": {"upscale_model": ["2", 0], "image": ["1", 0]}},
            SAVE_NODE_ID: {"class_type": "SaveImage",
                           "inputs": {"images": ["3", 0], "filename_prefix": prefix}},
        }

    # ---------------- main ----------------

    def upscale(self, image, model_name, tile, overlap, use_workers):
        import torch
        import comfy.model_management as mm
        from comfy_extras.nodes_upscale_model import ImageUpscaleWithModel, UpscaleModelLoader

        from . import config
        from .dispatcher import MANAGER
        from .workers import LOCAL_ID, REGISTRY

        if image.shape[0] > 1:
            outs = [self.upscale(image[b:b + 1], model_name, tile, overlap, use_workers)[0]
                    for b in range(image.shape[0])]
            return (torch.cat(outs, dim=0),)

        _, H, W, _ = image.shape
        step = max(1, tile - overlap)
        ys, xs = self._starts(H, tile, step), self._starts(W, tile, step)
        jobs = [(y, x) for y in ys for x in xs]
        n = len(jobs)

        loader = UpscaleModelLoader()
        upscaler = ImageUpscaleWithModel()
        model = None

        def local_compute(idx):
            nonlocal model
            if model is None:
                model = loader.load_model(model_name)[0]
            y, x = jobs[idx]
            tile_t = image[:, y:min(y + tile, H), x:min(x + tile, W), :]
            return upscaler.upscale(model, tile_t)[0][0]  # [h,w,c]

        if n == 1:
            return (local_compute(0).unsqueeze(0),)

        # --- удалённый пул ---
        job_id_remote, tq, uid = None, None, uuid.uuid4().hex[:8]
        tiles_rel_dir = f"gpuraid_tiles/{uid}"
        tiles_abs_dir = os.path.join(config.input_dir(), tiles_rel_dir)
        recv_dir = os.path.join(config.temp_base(), f"upscale-{uid}")
        remote_enabled = bool(use_workers)
        if remote_enabled:
            has_remote = any(
                r["id"] != LOCAL_ID and REGISTRY.status.get(r["id"], {}).get("state") == "online"
                for r in REGISTRY.enabled_records()
            )
            remote_enabled = has_remote
        if remote_enabled:
            try:
                payload = []
                for idx, (y, x) in enumerate(jobs):
                    tpath = os.path.join(tiles_abs_dir, f"t{idx:03d}.png")
                    self._save_png(image[0, y:min(y + tile, H), x:min(x + tile, W), :], tpath)
                    payload.append({
                        "index": idx,
                        "graph": self._tile_graph(f"{tiles_rel_dir}/t{idx:03d}.png", model_name,
                                                  f"gpuraid_tmp/upscale-{uid}/t{idx:03d}"),
                        "uploads": [("1", "image", tpath, f"{uid}_t{idx:03d}.png")],
                        "out_file": os.path.join(recv_dir, f"t{idx:03d}.png"),
                    })
                job_id_remote, tq = MANAGER.start_upscale_blocking(payload, 0, label=f"upscale-{uid}")
            except Exception as e:
                log.warning("GPU RAID upscale: удалённый пул недоступен (%s) — считаю локально", e)
                job_id_remote, tq = None, None

        # --- совместный цикл: локалка с конца, облако с начала, steal-back в конце ---
        computed = {}
        stolen = set()
        my_order = list(range(n - 1, -1, -1))
        remote_done = job_id_remote is None
        job_obj = MANAGER.jobs.get(job_id_remote) if job_id_remote else None

        def drain_remote(block_s=0.0):
            nonlocal remote_done
            if tq is None:
                return
            while True:
                try:
                    item = tq.get(timeout=block_s) if block_s else tq.get_nowait()
                except thread_queue.Empty:
                    return
                block_s = 0.0
                idx, path = item
                if idx == "__job_done__":
                    remote_done = True
                    continue
                if idx not in computed:
                    try:
                        computed[idx] = self._load_png(path)
                    except Exception as e:
                        log.warning("tile %s: не удалось прочитать результат (%s)", idx, e)

        while len(computed) < n:
            mm.throw_exception_if_processing_interrupted()
            drain_remote()
            pick = None
            inflight = dict(job_obj.inflight) if job_obj else {}
            for idx in my_order:
                if idx not in computed and idx not in inflight and idx not in stolen:
                    pick = idx
                    break
            if pick is None:
                # остались только тайлы в полёте у облака: воруем самый старый
                pending = [i for i in range(n) if i not in computed]
                if not pending:
                    break
                if remote_done:
                    pick = pending[0]  # облако закончилось, добираем сами
                else:
                    steal = next((i for i in pending if i not in stolen), None)
                    if steal is None:
                        drain_remote(block_s=0.5)
                        continue
                    stolen.add(steal)
                    pick = steal
            result = local_compute(pick)
            if pick not in computed:
                computed[pick] = result
                if pick in stolen and job_id_remote:
                    MANAGER.cancel_unit_blocking(job_id_remote, pick)

        if job_id_remote:
            MANAGER.cancel_job_blocking(job_id_remote)

        # --- определяем масштаб и склеиваем с фезером ---
        first = computed[0]
        t0_y, t0_x = jobs[0]
        in_h = min(t0_y + tile, H) - t0_y
        in_w = min(t0_x + tile, W) - t0_x
        sy = first.shape[0] / in_h
        sx = first.shape[1] / in_w
        out_h, out_w = round(H * sy), round(W * sx)
        canvas = torch.zeros((out_h, out_w, 3), dtype=torch.float32)
        weight = torch.zeros((out_h, out_w, 1), dtype=torch.float32)

        for idx, (y, x) in enumerate(jobs):
            t = computed[idx]
            th, tw = t.shape[0], t.shape[1]
            oy, ox = round(y * sy), round(x * sx)
            oy2, ox2 = min(oy + th, out_h), min(ox + tw, out_w)
            th, tw = oy2 - oy, ox2 - ox
            wy = torch.ones(th)
            wx = torch.ones(tw)
            fy = max(1, round(overlap * sy))
            fx = max(1, round(overlap * sx))
            if y > 0:
                ramp = torch.linspace(0, 1, min(fy, th))
                wy[: ramp.numel()] = ramp
            if y + tile < H:
                ramp = torch.linspace(1, 0, min(fy, th))
                wy[-ramp.numel():] = torch.minimum(wy[-ramp.numel():], ramp)
            if x > 0:
                ramp = torch.linspace(0, 1, min(fx, tw))
                wx[: ramp.numel()] = ramp
            if x + tile < W:
                ramp = torch.linspace(1, 0, min(fx, tw))
                wx[-ramp.numel():] = torch.minimum(wx[-ramp.numel():], ramp)
            mask = (wy.unsqueeze(1) * wx.unsqueeze(0)).unsqueeze(-1)
            canvas[oy:oy2, ox:ox2, :] += t[:th, :tw, :].cpu() * mask
            weight[oy:oy2, ox:ox2, :] += mask

        weight = torch.clamp(weight, min=1e-6)
        out = (canvas / weight).unsqueeze(0)

        # уборка временных тайлов
        try:
            import shutil

            shutil.rmtree(tiles_abs_dir, ignore_errors=True)
        except Exception:
            pass
        return (out,)


NODE_CLASS_MAPPINGS = {
    NODE_DISTRIBUTOR: GPURaidDistributor,
    NODE_COLLECTOR: GPURaidCollector,
    NODE_TILED_UPSCALE: GPURaidTiledUpscale,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    NODE_DISTRIBUTOR: "GPU RAID Distributor (seed)",
    NODE_COLLECTOR: "GPU RAID Collector",
    NODE_TILED_UPSCALE: "GPU RAID Tiled Upscale",
}
