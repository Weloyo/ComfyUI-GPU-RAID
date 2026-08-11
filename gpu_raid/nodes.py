"""Ноды GPU RAID.

Две группы. «Рабочие» (Distributor/Collector/TiledUpscale/VideoSpec/Save-LoadBundle)
реально что-то считают и уезжают на воркеров. «Маркеры» (Story/Storyboard/
VideoSequence, LongVideo, Offload, Pipeline) — пульты управления мастером
прямо на канве: они ничего не вычисляют, весь их UI живёт во фронтенде
(web/lib/nodeui.js), а из любого графа перед отправкой они вырезаются
(graph_rewrite.strip_markers). Story/Storyboard/VideoSequence дополнительно
связаны коннектором GPURAID_PROJECT — по нему течёт только ссылка (label
проекта), сами данные остаются в манифесте на диске.
"""

import logging
import os
import queue as thread_queue
import shutil
import time
import uuid

log = logging.getLogger("gpu_raid")

from .consts import (ASPECTS, GPURAID_PROJECT_TYPE, GPURAID_RUNTIME_TYPE,
                     NODE_COLLECTOR, NODE_DISTRIBUTOR, NODE_LOAD_BUNDLE,
                     NODE_LONG_VIDEO, NODE_MODELS, NODE_OFFLOAD, NODE_PIPELINE,
                     NODE_SAVE_BUNDLE, NODE_STORY, NODE_STORYBOARD,
                     NODE_TILED_UPSCALE, NODE_VIDEO_SPEC, NODE_VIDEOSEQ, NODE_WORKERS,
                     SAVE_NODE_ID)
from . import results, storyplan


class AnyType(str):
    """Wildcard-«тип» для Save/LoadBundle: не равен ничему по __ne__ —
    фронтенд/валидатор пропускают соединение с любым типом (приём rgthree)."""

    def __ne__(self, _other):
        return False


ANY_TYPE = AnyType("*")


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

        try:
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
        finally:
            # Interrupt/OOM в local_compute не должны оставлять облако молотить
            # оставшиеся тайлы вхолостую: без отмены job штатно доберёт всю
            # очередь (upload+рендер+download на каждый), хотя читать результат
            # уже некому — тратится чужая GPU-квота и трафик туннеля впустую
            if job_id_remote:
                MANAGER.cancel_job_blocking(job_id_remote)
            shutil.rmtree(tiles_abs_dir, ignore_errors=True)

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

        # тайлы уже убраны в finally выше (тем же rmtree, ignore_errors=True —
        # безусловно, независимо от исхода цикла)
        return (out,)


class GPURaidVideoSpec:
    """Спецификация видео: длительность/аспект/разрешение/fps -> width/height/length.

    Универсальная нода (работает и на воркерах): подключите width/height/length
    к видео-ноде (для MiniMax H3 — вместо ComfyMathExpression из шаблона).
    snap=minimax_h3 выравнивает кадры по сетке 17k+5 и канву по правилам H3.
    Сценарист правит duration_s этой ноды при рендере каждого сегмента.
    """

    CATEGORY = "GPU RAID"
    RETURN_TYPES = ("INT", "INT", "INT", "INT", "FLOAT")
    RETURN_NAMES = ("width", "height", "length", "fps", "duration_s")
    FUNCTION = "run"
    DESCRIPTION = ("Длительность, соотношение сторон, разрешение и fps одним узлом. "
                   "length = кадры с учётом сетки модели (minimax_h3: 17k+5).")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "duration_s": ("FLOAT", {"default": 5.0, "min": 0.2, "max": 3600.0,
                                         "step": 0.1}),
                "fps": ("INT", {"default": 24, "min": 1, "max": 120}),
                "aspect": (list(ASPECTS), {"default": "16:9"}),
                "short_edge": ("INT", {"default": 768, "min": 64, "max": 4096,
                                       "step": 32}),
                "snap": (["minimax_h3", "none"], {"default": "minimax_h3"}),
            }
        }

    def run(self, duration_s, fps, aspect, short_edge, snap):
        width, height = storyplan.canvas(aspect, short_edge, snap)
        length = storyplan.align_frames(duration_s, fps, snap)
        return (int(width), int(height), int(length), int(fps), float(duration_s))


class GPURaidStory:
    """«История»: сюжет -> раскадровка по времени через LLM (или эвристику).

    Ничего не рендерит: нажатие Queue (или кнопка «План ▶» в теле ноды)
    перехватывается расширением — LLM разбивает сюжет на сегменты с таймингом
    (в пределах потолков max_segment_duration_s/max_total_duration_s — сколько
    длится каждый сегмент решает сам сценарист). model/system_prompt/
    temperature/max_tokens — «характер» этого конкретного сценариста (можно
    держать несколько нод Истории с разными характерами под разные сюжеты);
    base_url/ключ LLM — глобальное подключение, в панели. Раскадровка и
    Видеоряд подключаются коннектором GPURAID_PROJECT (или дропдауном
    «проект:» в их теле).
    """

    CATEGORY = "GPU RAID"
    RETURN_TYPES = (GPURAID_PROJECT_TYPE, "STRING")
    RETURN_NAMES = ("project", "story")
    FUNCTION = "run"
    DESCRIPTION = ("Опишите сюжет — Queue разберёт его на сегменты с таймингом "
                   "(first/last кадры + промпт + камера на сегмент, в пределах "
                   "потолков длительности). Раскадровка и рендер — в подключённых "
                   "нодах Раскадровка/Видеоряд.")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "story": ("STRING", {"multiline": True, "default": ""}),
                "label": ("STRING", {"default": "story"}),
                "segments_count": ("INT", {"default": 0, "min": 0, "max": 64,
                                           "tooltip": "0 = авто (решает LLM/эвристика)"}),
                "segment_duration_s": ("FLOAT", {"default": 5.0, "min": 0.5, "max": 60.0,
                                                 "step": 0.5,
                                                 "tooltip": "ориентир для LLM; фактическую "
                                                            "длительность каждого сегмента "
                                                            "решает сценарист в пределах "
                                                            "потолков ниже"}),
                "max_segment_duration_s": ("FLOAT", {"default": 15.0, "min": 0.5, "max": 60.0,
                                                     "step": 0.5,
                                                     "tooltip": "потолок на ОДИН сегмент "
                                                                "(H3 физически не считает "
                                                                "больше ~15с за раз)"}),
                "max_total_duration_s": ("FLOAT", {"default": 60.0, "min": 1.0, "max": 3600.0,
                                                   "step": 1.0,
                                                   "tooltip": "потолок на весь ролик"}),
                "fps": ("INT", {"default": 24, "min": 1, "max": 120}),
                "aspect": (list(ASPECTS), {"default": "16:9"}),
                "short_edge": ("INT", {"default": 768, "min": 64, "max": 4096,
                                       "step": 32}),
                "snap": (["minimax_h3", "none"], {"default": "minimax_h3"}),
                "use_llm": ("BOOLEAN", {"default": True}),
                "model": ("STRING", {"default": "",
                                     "tooltip": "имя модели у LLM-эндпоинта; пусто = "
                                                "дефолт из панели «Подключения и ключи»"}),
                "system_prompt": ("STRING", {"multiline": True, "default": "",
                                             "tooltip": "характер этого сценариста — жанр, "
                                                        "стиль подачи, ограничения"}),
                "temperature": ("FLOAT", {"default": 0.7, "min": 0.0, "max": 2.0, "step": 0.05}),
                "max_tokens": ("INT", {"default": 0, "min": 0, "max": 1000000,
                                       "tooltip": "0 = без ограничения (дефолт эндпоинта)"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "on_queue": ("BOOLEAN", {"default": True,
                                         "tooltip": "перехватывать Queue: нажатие составляет "
                                                    "план вместо локального прогона"}),
            }
        }

    def run(self, story, **_kw):
        return (str(story or ""), str(story or ""))


class GPURaidStoryboard:
    """«Раскадровка»: по плану Истории генерирует первый/последний кадр
    каждого сегмента — T2I, параллельно на всех GPU.

    Шаблон кадра — кнопка «Шаблон кадра из канвы» в теле ноды (свой
    T2I-workflow на канве, нода промпта с заголовком GPURAID:PROMPT + Save-нода);
    без него — встроенный дефолт (Z-Image). Непрерывность стиля между кадрами
    держится на style_bible (задаётся в теле ноды Истории, приклеивается к
    каждому промпту кадра при рендере) — единственный механизм пока
    (continuity_mode=style_only); img2img-цепочка кадров — в планах.
    """

    CATEGORY = "GPU RAID"
    RETURN_TYPES = (GPURAID_PROJECT_TYPE,)
    RETURN_NAMES = ("project",)
    FUNCTION = "run"
    DESCRIPTION = ("Кадры проекта (первый/последний на сегмент) — T2I, параллельно "
                   "на всех GPU. Лента кадров, правки, «Шаблон кадра из канвы» — "
                   "в теле ноды.")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "continuity_mode": (["style_only"], {"default": "style_only",
                                                      "tooltip": "как держать кадры визуально "
                                                                 "согласованными"}),
            },
            "optional": {
                "project": (GPURAID_PROJECT_TYPE, {}),
            },
        }

    def run(self, **_kw):
        return ("",)


class GPURaidVideoSequence:
    """«Видеоряд»: из пар кадров Раскадровки генерирует видео FLF2V-сегментами
    — параллельно на всех GPU.

    Шаблон сегмента — кнопка «Шаблон сегмента из канвы» в теле ноды
    (FLF2V-workflow на канве, GPURAID:START_IMAGE/END_IMAGE + Save-видео-нода,
    опционально GPURAID:STEPS для дешёвого черновика). prompt_format=minimax_h3
    строит промпт по официальному формату MiniMax H3 (Shot + выравнивание
    first/last кадра по секундам); raw — промпт сегмента как есть. Черновик и
    финал — разными кнопками в теле ноды.
    """

    CATEGORY = "GPU RAID"
    RETURN_TYPES = (GPURAID_PROJECT_TYPE,)
    RETURN_NAMES = ("project",)
    FUNCTION = "run"
    DESCRIPTION = ("Сегменты проекта — FLF2V между парами кадров Раскадровки, "
                   "параллельно на всех GPU. Черновик/финал, монтаж, экспорт — "
                   "в теле ноды.")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "prompt_format": (["minimax_h3", "raw"], {"default": "minimax_h3",
                                                          "tooltip": "minimax_h3 — официальный "
                                                                     "формат [Shot]+выравнивание "
                                                                     "кадров; raw — промпт "
                                                                     "сегмента как есть"}),
                "preview_short_edge": ("INT", {"default": 0, "min": 0, "max": 4096, "step": 32,
                                               "tooltip": "0 = автоматически (половина "
                                                          "разрешения финала)"}),
                "preview_steps": ("INT", {"default": 0, "min": 0, "max": 200,
                                          "tooltip": "0 = без override (нужна нода с "
                                                     "заголовком GPURAID:STEPS в шаблоне "
                                                     "сегмента, иначе игнорируется)"}),
            },
            "optional": {
                "project": (GPURAID_PROJECT_TYPE, {}),
            },
        }

    def run(self, **_kw):
        return ("",)


class GPURaidLongVideo:
    """«Длинное видео»: нода-проект без LLM — сегменты задаются руками.

    chain — каждый следующий сегмент продолжает последний кадр предыдущего
    (любая длина, последовательно); keyframes — готовые кадры из input-каталога
    попарно превращаются в сегменты FLF2V и считаются параллельно.
    Шаблон сегмента = текущий канвас (маркеры GPURAID:START_IMAGE и т.д.).
    """

    CATEGORY = "GPU RAID"
    RETURN_TYPES = ()
    FUNCTION = "run"
    DESCRIPTION = ("Сборка длинного видео из сегментов текущего workflow. "
                   "Промпты, порядок, тримы и перерендер — в теле ноды.")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "label": ("STRING", {"default": "myvideo",
                                     "tooltip": "имя проекта: output/gpuraid/<label>"}),
                "mode": (["chain", "keyframes"], {"default": "chain"}),
                "count": ("INT", {"default": 4, "min": 1, "max": 999,
                                  "tooltip": "сегментов (для chain)"}),
                "prompts": ("STRING", {"multiline": True, "default": "",
                                       "tooltip": "по промпту на строку; пусто = промпт из workflow"}),
                "keyframes": ("STRING", {"multiline": True, "default": "",
                                         "tooltip": "режим keyframes: имена файлов из input, "
                                                    "по одному на строку (минимум 2)"}),
                "seed": ("INT", {"default": 0, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "seed_policy": (["increment", "fixed", "random"], {"default": "increment"}),
                "crossfade_s": ("FLOAT", {"default": 0.0, "min": 0.0, "max": 10.0, "step": 0.1}),
                "on_queue": ("BOOLEAN", {"default": True,
                                         "tooltip": "перехватывать Queue: нажатие запускает "
                                                    "сборку проекта, а не локальный прогон"}),
            }
        }

    def run(self, **_kw):
        return ()


class GPURaidOffload:
    """«Выполнить на воркере»: весь текущий workflow целиком уезжает на одну машину.

    Маркер-нода: выбор воркера и кнопка запуска живут в теле ноды. Из графа,
    уезжающего на воркера, нода вырезается.
    """

    CATEGORY = "GPU RAID"
    RETURN_TYPES = ()
    FUNCTION = "run"
    DESCRIPTION = ("Весь workflow считает выбранный воркер, локальная GPU свободна; "
                   "результаты возвращаются в output/gpuraid/<label>_<время>/.")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "worker": ("STRING", {"default": "",
                                      "tooltip": "id воркера; выбирается списком в теле ноды"}),
                "label": ("STRING", {"default": "offload"}),
                "on_queue": ("BOOLEAN", {"default": True,
                                         "tooltip": "перехватывать Queue: нажатие отправляет "
                                                    "workflow на воркера"}),
            }
        }

    def run(self, **_kw):
        return ()


class GPURaidPipeline:
    """«Конвейер»: один workflow режется на стадии, стадии считают разные воркеры.

    Маркер-нода: кнопка «Анализ», раскладка стадий по воркерам и запуск живут в
    теле ноды; выбранная раскладка хранится в свойствах ноды и сохраняется
    вместе с workflow.
    """

    CATEGORY = "GPU RAID"
    RETURN_TYPES = ()
    FUNCTION = "run"
    DESCRIPTION = ("Для моделей, которые не влезают целиком ни в один GPU: энкодер / "
                   "диффузия / VAE разъезжаются по воркерам, промежуточные тензоры "
                   "едут бандлами. Спец-ноды в графе не нужны.")

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "label": ("STRING", {"default": "pipeline"}),
                "on_queue": ("BOOLEAN", {"default": True,
                                         "tooltip": "перехватывать Queue: нажатие запускает "
                                                    "конвейер по сохранённой раскладке"}),
            }
        }

    def run(self, **_kw):
        return ()


class GPURaidWorkers:
    """«Воркеры»: парк машин и задания — прямо на канве.

    Маркер-нода: список воркеров со статусами, добавление по connection string,
    быстрый запуск Colab/Kaggle, глобальная политика автостопа и активные
    задания. Один выход «воркеры» (шина GPURAID_RUNTIME): один и тот же провод
    тянется от него к каждой ноде-лоадеру — после подключения в лоадере
    открывается список доступных воркеров, и привязка «модель → рантайм»
    выбирается там (properties.gpuraid_runtime). Данные по проводу не текут —
    из графов на исполнение такие линки вычищает strip_markers.
    """

    CATEGORY = "GPU RAID"
    RETURN_TYPES = (GPURAID_RUNTIME_TYPE,)
    RETURN_NAMES = ("воркеры",)
    FUNCTION = "run"
    DESCRIPTION = ("Парк GPU: кто в сети, добавление воркеров, автостоп и задания. "
                   "Выход «воркеры» подключается ко всем нодам-лоадерам — после "
                   "этого в лоадере появляется список доступных воркеров. "
                   "Секреты и ключи — в боковой панели.")

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    def run(self):
        # нода — маркер, из исполняемых графов вырезается
        return ("gpuraid",)


class GPURaidModels:
    """«Модели на воркерах»: сверка текущего графа с инвентарём и рассылка.

    Модели выбираются как обычно — в лоадерах на канве. Эта нода только
    показывает, у кого из воркеров нужных файлов нет, и запускает закачку:
    каждый воркер тянет файл сам с публичной ссылки из библиотеки источников
    (кнопка 🔗 в runtime-блоке лоадера), мимо канала мастера.
    """

    CATEGORY = "GPU RAID"
    RETURN_TYPES = ()
    FUNCTION = "run"
    DESCRIPTION = ("Матрица «модель × воркер» для текущего workflow и кнопка "
                   "разослать недостающее. Воркеры качают напрямую с источника.")

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    def run(self):
        return ()


class GPURaidSaveBundle:
    """Сохраняет ЛЮБОЕ промежуточное значение (LATENT/CONDITIONING/IMAGE/AUDIO)
    в бандл-файл. Граница стадии конвейера — мастер вставляет её автоматически
    при шардинге; руками нода тоже работает."""

    CATEGORY = "GPU RAID"
    RETURN_TYPES = ()
    OUTPUT_NODE = True
    FUNCTION = "run"
    DESCRIPTION = "Пишет значение в output/<prefix>_NNNNN_.safetensors (бандл GPU RAID)."

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "value": (ANY_TYPE,),
                "filename_prefix": ("STRING", {"default": "gpuraid_bundle/b"}),
            },
            "optional": {
                "data_type": ("STRING", {"default": ""}),
            },
        }

    def run(self, value, filename_prefix, data_type=""):
        import folder_paths

        from . import bundle as bundle_mod

        full_out, filename, counter, subfolder, _ = folder_paths.get_save_image_path(
            filename_prefix, folder_paths.get_output_directory())
        name = f"{filename}_{counter:05}_.safetensors"
        stats = bundle_mod.save_bundle(os.path.join(full_out, name), value, data_type)
        log.info("bundle saved: %s/%s (%d т., %.1f МБ)", subfolder, name,
                 stats["tensors"], stats["bytes"] / 1e6)
        return {"ui": {"gpuraid_bundles": [{
            "filename": name, "subfolder": subfolder, "type": "output",
            "bytes": stats["bytes"],
        }]}}


class GPURaidLoadBundle:
    """Загружает бандл (см. SaveBundle) и отдаёт значение дальше по графу."""

    CATEGORY = "GPU RAID"
    RETURN_TYPES = (ANY_TYPE,)
    RETURN_NAMES = ("value",)
    FUNCTION = "run"
    DESCRIPTION = "Путь относительно input-каталога (файл приезжает от мастера автоматически)."

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"bundle": ("STRING", {"default": ""})}}

    def run(self, bundle):
        import folder_paths

        from . import bundle as bundle_mod

        path = folder_paths.get_annotated_filepath(bundle)
        if not path or not os.path.isfile(path):
            raise RuntimeError(f"GPU RAID LoadBundle: файл не найден: {bundle}")
        payload, _dtype = bundle_mod.load_bundle(path)
        return (payload,)

    @classmethod
    def IS_CHANGED(cls, bundle):
        import folder_paths

        path = folder_paths.get_annotated_filepath(bundle)
        try:
            st = os.stat(path)
            return f"{st.st_size}:{st.st_mtime_ns}"
        except (OSError, TypeError):
            return float("nan")

    @classmethod
    def VALIDATE_INPUTS(cls, bundle):
        # приняв параметр по имени, отключаем стандартную combo-валидацию;
        # существование проверяем сами (файл мог только что приехать от мастера)
        if not str(bundle or "").strip():
            return "укажите файл бандла"
        return True


NODE_CLASS_MAPPINGS = {
    NODE_DISTRIBUTOR: GPURaidDistributor,
    NODE_COLLECTOR: GPURaidCollector,
    NODE_TILED_UPSCALE: GPURaidTiledUpscale,
    NODE_VIDEO_SPEC: GPURaidVideoSpec,
    NODE_STORY: GPURaidStory,
    NODE_STORYBOARD: GPURaidStoryboard,
    NODE_VIDEOSEQ: GPURaidVideoSequence,
    NODE_LONG_VIDEO: GPURaidLongVideo,
    NODE_OFFLOAD: GPURaidOffload,
    NODE_PIPELINE: GPURaidPipeline,
    NODE_MODELS: GPURaidModels,
    NODE_WORKERS: GPURaidWorkers,
    NODE_SAVE_BUNDLE: GPURaidSaveBundle,
    NODE_LOAD_BUNDLE: GPURaidLoadBundle,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    NODE_DISTRIBUTOR: "GPU RAID Distributor (seed)",
    NODE_COLLECTOR: "GPU RAID Collector",
    NODE_TILED_UPSCALE: "GPU RAID Tiled Upscale",
    NODE_VIDEO_SPEC: "GPU RAID Видео-спека",
    NODE_STORY: "GPU RAID История",
    NODE_STORYBOARD: "GPU RAID Раскадровка",
    NODE_VIDEOSEQ: "GPU RAID Видеоряд",
    NODE_LONG_VIDEO: "GPU RAID Длинное видео",
    NODE_OFFLOAD: "GPU RAID Выполнить на воркере",
    NODE_PIPELINE: "GPU RAID Конвейер (шардинг)",
    NODE_MODELS: "GPU RAID Модели на воркерах",
    NODE_WORKERS: "GPU RAID Воркеры",
    NODE_SAVE_BUNDLE: "GPU RAID Save Bundle (шардинг)",
    NODE_LOAD_BUNDLE: "GPU RAID Load Bundle (шардинг)",
}
