"""Статические таблицы, идентификаторы и дефолты.

Чистый модуль: обязан импортироваться ВНЕ ComfyUI (тесты, authproxy) —
никаких импортов server/torch/folder_paths.
"""

VERSION = "0.1.0"
EXT_NAME = "comfyui-gpu-raid"

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
GPURAID_CLASSES = (NODE_DISTRIBUTOR, NODE_COLLECTOR, NODE_TILED_UPSCALE)

# --- плейсхолдеры юнит-шаблона ---
SEED_PH = "__GPURAID_SEED__"
INDEX_PH = "__GPURAID_INDEX__"
PREFIX_PH = "__GPURAID_PREFIX__"
SAVE_NODE_ID = "gpuraid_save"

# --- маркеры Long Video (заголовки нод, регистронезависимо) ---
LV_START = "GPURAID:START_IMAGE"
LV_END = "GPURAID:END_IMAGE"
LV_PROMPT = "GPURAID:PROMPT"
LV_OUT = "GPURAID:VIDEO_OUT"

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
    "timeouts": {
        "probe_s": 5,
        "image_startup_s": 240,
        "image_stall_s": 90,
        "video_startup_s": 600,
        "video_stall_s": 300,
        "hard_cap_s": 39600,
    },
}

# небольшой каталог проверенных URL для «Download to worker»
# (произвольные HF/Civitai URL тоже принимаются)
MODEL_CATALOG = [
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
