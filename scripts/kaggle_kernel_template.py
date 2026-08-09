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
    """Секреты из приватного датасета, подключённого мастером. {} если его нет.

    Имя каталога монтирования Kaggle выводит из слага датасета, но полагаться
    на это нельзя (живьём: FileNotFoundError на ожидаемом пути, и воркер час
    простоял неопубликованным). Поэтому: сначала ожидаемый путь, затем поиск
    файла по всему /kaggle/input, и в любом случае печатаем, что смонтировано.
    """
    if not SECRET_DATASET:
        return {}
    name = "gpuraid_secrets.json"
    slug = SECRET_DATASET.split("/")[-1]
    candidates = [os.path.join("/kaggle/input", slug, name)]
    try:
        for root, _dirs, files in os.walk("/kaggle/input"):
            if name in files:
                candidates.append(os.path.join(root, name))
    except Exception:
        pass
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            print(f"[secrets] прочитаны из {path}: {sorted(data)}")
            return data
        except FileNotFoundError:
            continue
        except Exception as e:
            print(f"[secrets] {path}: {type(e).__name__}: {e}")
    try:
        mounted = sorted(os.listdir("/kaggle/input"))
    except Exception as e:
        mounted = f"<{type(e).__name__}>"
    print(f"[secrets] датасет {SECRET_DATASET} не найден. Смонтировано: {mounted}")
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


def _gpus():
    """Сколько инстансов поднимать: по одному на видеокарту.

    Kaggle с ускорителем NvidiaTeslaT4 даёт ДВЕ карты — это два воркера в
    RAID с одной сессии. Исключение — H3: 40 ГБ весов при ~29 ГБ RAM сессии
    два инстанса не тянут (живьём убивало сессию по OOM, «status 42»).
    """
    if MODEL_PRESET == "minimax_h3":
        return (0,)
    try:
        import torch

        count = int(torch.cuda.device_count())
    except Exception as e:
        print(f"[gpu] не смог посчитать карты ({type(e).__name__}) — беру одну")
        count = 1
    count = max(1, count)
    print(f"[gpu] карт видно: {count}")
    return tuple(range(count))


if not os.path.isdir(SRC):
    subprocess.check_call(["git", "clone", "--depth", "1", REPO_URL, SRC])
sys.path.insert(0, os.path.join(SRC, "scripts"))
import worker_bootstrap as wb  # noqa: E402

token = wb.gen_token("")
info = wb.bring_up(
    gpuraid_src=SRC,
    comfy_dir=os.path.join(WORK, "ComfyUI"),
    token=token,
    gpus=_gpus(),
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
