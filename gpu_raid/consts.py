"""Статические таблицы, идентификаторы и дефолты.

Чистый модуль: обязан импортироваться ВНЕ ComfyUI (тесты, authproxy) —
никаких импортов server/torch/folder_paths.
"""

VERSION = "0.2.0"
EXT_NAME = "comfyui-gpu-raid"
REPO_URL = "https://github.com/Weloyo/ComfyUI-GPU-RAID"
COLAB_NOTEBOOK_URL = ("https://colab.research.google.com/github/Weloyo/"
                      "ComfyUI-GPU-RAID/blob/main/notebooks/colab_worker.ipynb")

# --- auth ---
TOKEN_HEADER = "X-GPURAID-Token"
TOKEN_ENV = "GPURAID_TOKEN"
STRICT_ENV = "GPURAID_AUTH_STRICT"   # "1" -> токен обязателен даже с loopback (для локальных тестов)
TOKEN_QUERY = "gpuraid_token"
TOKEN_COOKIE = "gpuraid_token"
FORWARD_HEADERS = ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP")

# --- имена классов нод (ключи NODE_CLASS_MAPPINGS) ---
NODE_DISTRIBUTOR = "GPURAID_Distributor"
NODE_COLLECTOR = "GPURAID_Collector"
NODE_TILED_UPSCALE = "GPURAID_TiledUpscale"
NODE_STORY_DIRECTOR = "GPURAID_StoryDirector"
NODE_LONG_VIDEO = "GPURAID_LongVideo"
NODE_OFFLOAD = "GPURAID_Offload"
NODE_PIPELINE = "GPURAID_Pipeline"
NODE_VIDEO_SPEC = "GPURAID_VideoSpec"       # НЕ в GPURAID_CLASSES: выполняется и на воркерах
NODE_SAVE_BUNDLE = "GPURAID_SaveBundle"     # тоже выполняются на воркерах (шардинг)
NODE_LOAD_BUNDLE = "GPURAID_LoadBundle"

# «Маркеры» — ноды-пульты мастера: ничего не вычисляют, живут только на канве и
# вырезаются из ЛЮБОГО графа перед отправкой куда бы то ни было. Сценарист
# отдаёт наружу текст сюжета, остальные вообще без выходов.
GPURAID_MARKER_CLASSES = (NODE_STORY_DIRECTOR, NODE_LONG_VIDEO, NODE_OFFLOAD,
                          NODE_PIPELINE)
GPURAID_CLASSES = (NODE_DISTRIBUTOR, NODE_COLLECTOR, NODE_TILED_UPSCALE
                   ) + GPURAID_MARKER_CLASSES

# --- плейсхолдеры юнит-шаблона ---
SEED_PH = "__GPURAID_SEED__"
INDEX_PH = "__GPURAID_INDEX__"
PREFIX_PH = "__GPURAID_PREFIX__"
SAVE_NODE_ID = "gpuraid_save"

# --- маркеры Long Video / Story (заголовки нод, регистронезависимо) ---
LV_START = "GPURAID:START_IMAGE"
LV_END = "GPURAID:END_IMAGE"
LV_PROMPT = "GPURAID:PROMPT"
LV_OUT = "GPURAID:VIDEO_OUT"
LV_KEYFRAME_OUT = "GPURAID:KEYFRAME_OUT"

# аспекты для VideoSpec/Сценариста
ASPECTS = ("16:9", "9:16", "1:1", "4:3", "3:4", "21:9")

# class_type -> {имя входа: папка моделей}
LOADER_TABLE = {
    "CheckpointLoaderSimple": {"ckpt_name": "checkpoints"},
    "CheckpointLoader": {"ckpt_name": "checkpoints"},
    "ImageOnlyCheckpointLoader": {"ckpt_name": "checkpoints"},
    "UNETLoader": {"unet_name": "diffusion_models"},
    "UnetLoaderGGUF": {"unet_name": "unet"},
    "UnetLoaderGGUFAdvanced": {"unet_name": "unet"},
    "VAELoader": {"vae_name": "vae"},
    "LoraLoader": {"lora_name": "loras"},
    "LoraLoaderModelOnly": {"lora_name": "loras"},
    "CLIPLoader": {"clip_name": "text_encoders"},
    "CLIPLoaderGGUF": {"clip_name": "text_encoders"},
    "DualCLIPLoader": {"clip_name1": "text_encoders", "clip_name2": "text_encoders"},
    "DualCLIPLoaderGGUF": {"clip_name1": "text_encoders", "clip_name2": "text_encoders"},
    "TripleCLIPLoader": {"clip_name1": "text_encoders", "clip_name2": "text_encoders",
                         "clip_name3": "text_encoders"},
    "QuadrupleCLIPLoader": {"clip_name1": "text_encoders", "clip_name2": "text_encoders",
                            "clip_name3": "text_encoders", "clip_name4": "text_encoders"},
    "ControlNetLoader": {"control_net_name": "controlnet"},
    "DiffControlNetLoader": {"control_net_name": "controlnet"},
    "UpscaleModelLoader": {"model_name": "upscale_models"},
    "StyleModelLoader": {"style_model_name": "style_models"},
    "CLIPVisionLoader": {"clip_name": "clip_vision"},
    "GLIGENLoader": {"gligen_name": "gligen"},
}

# папка -> список папок-синонимов при проверке инвентаря воркера
FOLDER_ALIASES = {
    "text_encoders": ("text_encoders", "clip"),
    "diffusion_models": ("diffusion_models", "unet"),
    "unet": ("unet", "diffusion_models"),
}

# class_type -> входы, ссылающиеся на файл из input-каталога (нужен upload на воркера)
UPLOAD_TABLE = {
    "LoadImage": ("image",),
    "LoadImageMask": ("image",),
    "LoadImageOutput": ("image",),
    "VHS_LoadVideo": ("video",),
    "LoadAudio": ("audio",),
    NODE_LOAD_BUNDLE: ("bundle",),
}

# классы, чьи выходные файлы забираем при offload/longvideo как «видео-выход»
VIDEO_OUT_CLASSES = (
    "VHS_VideoCombine", "SaveVideo", "SaveWEBM", "SaveAnimatedWEBP", "SaveAnimatedPNG",
)

# подстроки class_type (lower), по которым job классифицируется как video (таймауты)
VIDEO_HINTS = (
    "wan", "vhs_", "ltx", "video", "mochi", "cogvideo", "hunyuan",
    "svd", "animatediff", "cosmos",
)

# какие ключи считаем текстом промпта / сидом при инъекции в Long Video
TEXT_KEYS = ("text", "prompt", "positive_prompt", "string", "value", "caption")
SEED_KEYS = ("seed", "noise_seed")

DEFAULT_SETTINGS = {
    "max_retries": 2,
    "keep_last_jobs": 5,
    "heartbeat_s": 15,
    "free_after_job": False,
    "timeouts": {
        "probe_s": 5,
        "image_startup_s": 240,
        "image_stall_s": 90,
        "video_startup_s": 600,
        "video_stall_s": 300,
        "hard_cap_s": 39600,
    },
    # политика питания облачных воркеров: keep | eco | instant | local_only
    "lifecycle": {
        "policy": "eco",
        "idle_stop_min": 10,
        "budget_min": 0,           # 0 = без потолка длительности сессии
        "auto_start_kaggle": False,
    },
    # OpenAI-совместимый endpoint для «Сценариста» (ключ — в secrets.json)
    "llm": {
        "base_url": "",
        "model": "",
        "temperature": 0.7,
    },
    # авторегистрация воркеров через приватный GitHub Gist
    "rendezvous": {
        "gist_id": "",
        "poll_s": 30,
    },
}

# небольшой каталог проверенных URL для «Download to worker»
# (произвольные HF/Civitai URL тоже принимаются)
MODEL_CATALOG = [
    {
        "name": "MiniMax H3 diffusion (int8)",
        "filename": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "folder": "diffusion_models",
        "size_gb": 19.5,
        "url": "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors",
    },
    {
        "name": "MiniMax H3 text encoder (Qwen3-VL nvfp4)",
        "filename": "qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
        "folder": "text_encoders",
        "size_gb": 14.6,
        "url": "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors",
    },
    {
        "name": "MiniMax H3 video VAE (fp16)",
        "filename": "minimax_h3_video_vae_fp16.safetensors",
        "folder": "vae",
        "size_gb": 4.9,
        "url": "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_fp16.safetensors",
    },
    {
        "name": "MiniMax H3 audio VAE (fp32)",
        "filename": "minimax_h3_audio_vae_fp32.safetensors",
        "folder": "vae",
        "size_gb": 0.6,
        "url": "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_audio_vae_fp32.safetensors",
    },
    {
        "name": "SDXL Base 1.0",
        "filename": "sd_xl_base_1.0.safetensors",
        "folder": "checkpoints",
        "size_gb": 6.9,
        "url": "https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors",
    },
    {
        "name": "Wan 2.1 VAE",
        "filename": "wan_2.1_vae.safetensors",
        "folder": "vae",
        "size_gb": 0.25,
        "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors",
    },
    {
        "name": "UMT5-XXL fp8 (Wan text encoder)",
        "filename": "umt5_xxl_fp8_e4m3fn_scaled.safetensors",
        "folder": "text_encoders",
        "size_gb": 6.7,
        "url": "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors",
    },
]
