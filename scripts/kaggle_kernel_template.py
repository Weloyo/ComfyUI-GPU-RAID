"""GPU RAID: Kaggle batch-воркер (шаблон; пушится мастером через kaggle CLI).

Плейсхолдеры {{...}} заполняет gpu_raid/kaggle_api.py при пуше. Секреты в код
НЕ вшиваются: GH_TOKEN (gist-rendezvous) и HF_TOKEN кернел читает из Kaggle
Secrets (Add-ons → Secrets; включите оба для этого ноутбука).

Завершение скрипта = завершение batch-сессии = квота не тратится: watchdog
выходит по sentinel'у от мастера или по MAX_SESSION_MIN.
"""

import os
import subprocess
import sys

REPO_URL = "{{REPO_URL}}"
GIST_ID = "{{GIST_ID}}"
MODEL_PRESET = "{{MODEL_PRESET}}"
MAX_SESSION_MIN = float("{{MAX_SESSION_MIN}}" or 0)
NAME_PREFIX = "{{NAME_PREFIX}}"

WORK = "/kaggle/working"
SRC = os.path.join(WORK, "gpu-raid-src")


def secret(name):
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
