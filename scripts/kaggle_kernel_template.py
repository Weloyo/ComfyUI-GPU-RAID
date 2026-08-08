"""GPU RAID: Kaggle batch-воркер (шаблон; пушится мастером через kaggle CLI).

Плейсхолдеры {{...}} заполняет gpu_raid/kaggle_api.py при пуше. Секреты в код
НЕ вшиваются: кернел читает их из приватного датасета, который мастер создаёт
и подключает сам (Kaggle Secrets через API не привязать — только руками в
вебе, а нам нужен запуск одной кнопкой). Если датасета нет, остаётся запасной
путь через Kaggle Secrets (Add-ons → Secrets).

Завершение скрипта = завершение batch-сессии = квота не тратится: watchdog
выходит по sentinel'у от мастера или по MAX_SESSION_MIN.
"""

import json
import os
import subprocess
import sys

REPO_URL = "{{REPO_URL}}"
SECRET_DATASET = "{{SECRET_DATASET}}"
GIST_ID = "{{GIST_ID}}"
MODEL_PRESET = "{{MODEL_PRESET}}"
MAX_SESSION_MIN = float("{{MAX_SESSION_MIN}}" or 0)
NAME_PREFIX = "{{NAME_PREFIX}}"

WORK = "/kaggle/working"
SRC = os.path.join(WORK, "gpu-raid-src")


def _dataset_secrets():
    """Секреты из приватного датасета, подключённого мастером. {} если его нет."""
    if not SECRET_DATASET:
        return {}
    slug = SECRET_DATASET.split("/")[-1]
    path = os.path.join("/kaggle/input", slug, "gpuraid_secrets.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[secrets] датасет {SECRET_DATASET} не прочитан: {type(e).__name__}")
        return {}


_DS = _dataset_secrets()


def secret(name):
    """Сначала датасет (его подключает мастер), потом Kaggle Secrets вручную."""
    value = _DS.get(name)
    if value:
        return value
    try:
        from kaggle_secrets import UserSecretsClient
        return UserSecretsClient().get_secret(name)
    except Exception as e:
        print(f"[secrets] {name} не прочитан: {e}")
        return ""


if not os.path.isdir(SRC):
    subprocess.check_call(["git", "clone", "--depth", "1", REPO_URL, SRC])
sys.path.insert(0, os.path.join(SRC, "scripts"))
import worker_bootstrap as wb  # noqa: E402

token = wb.gen_token("")
info = wb.bring_up(
    gpuraid_src=SRC,
    comfy_dir=os.path.join(WORK, "ComfyUI"),
    token=token,
    gpus=(0,),
    base_port=8188,
    extra_args=("--force-fp16",),   # у T4 нет bf16; RAM-guard для H3 добавится сам
    use_datasets=True,
    hf_preset=MODEL_PRESET,
    hf_token=secret("HF_TOKEN"),
    name_prefix=NAME_PREFIX,
    gist_id=GIST_ID,
    gh_token=secret("GH_TOKEN"),
    max_session_min=MAX_SESSION_MIN,
)
wb.watchdog(info)
print("[worker] batch-сессия завершена")
