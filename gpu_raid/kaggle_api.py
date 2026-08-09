"""Автозапуск Kaggle-воркера: официальный kaggle CLI, batch-кернел (script).

Требуется: `pip install kaggle` на мастере + kaggle.json (панель → секреты;
кладётся в state_dir, CLI берёт его через KAGGLE_CONFIG_DIR). GH-токен для
gist-rendezvous в код кернела НЕ вшивается — кернел читает его из Kaggle
Secrets (Add-ons → Secrets, имя GH_TOKEN; опционально HF_TOKEN).

Кернел пушится как приватный python-script с GPU и интернетом: он клонирует
репо, скачивает модели пресета, поднимает воркера и публикует connection
string в gist — мастер подхватит его через rendezvous. Выход из скрипта
(watchdog увидел sentinel) завершает batch-сессию — квота перестаёт тратиться.
"""

import asyncio
import json
import os
import shutil
import tempfile
import time

from . import config
from . import secrets as secret_store

TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "kaggle_kernel_template.py",
)

KERNEL_TITLE = "gpu-raid-worker"
SECRET_DATASET_TITLE = "gpu-raid-secrets"
SECRET_FILE = "gpuraid_secrets.json"

# Просить ускоритель ЯВНО. По API Kaggle по умолчанию выдаёт NvidiaTeslaP100, а
# Pascal (sm_60) выпал из свежих сборок PyTorch, который ставится вместе с
# ComfyUI: воркер поднимается, качает модели и падает на первой же операции
# «CUDA error: no kernel image is available for execution on the device»
# (проверено живьём 2026-08-09). T4 — sm_75, поддерживается, и их дают две.
DEFAULT_ACCELERATOR = "NvidiaTeslaT4"


def _cli():
    from . import providers

    exe = providers.kaggle_cli_path()
    if not exe:
        raise RuntimeError(
            "kaggle CLI не найден: панель → «Подключения и ключи» → Kaggle → "
            "«Установить kaggle CLI»")
    return exe


def _env():
    env = dict(os.environ)
    # схемы взаимоисключающи: при сохранённом токене НЕ выставляем
    # KAGGLE_CONFIG_DIR, иначе поведение CLI зависит от порядка разрешения
    # кредов в его версии (мог бы подхватить старый kaggle.json)
    token = secret_store.get("kaggle_token")
    if token:
        env["KAGGLE_API_TOKEN"] = token             # новая схема: строка KGAT_…
    else:
        env["KAGGLE_CONFIG_DIR"] = config.state_dir()   # старая схема: kaggle.json
    return env


def username():
    from . import providers

    user = providers.kaggle_username()
    if not user:
        raise RuntimeError(
            "не указан аккаунт Kaggle (панель → «Подключения и ключи» → Kaggle)")
    return user


async def check_cli():
    """Дёргает CLI под сохранёнными кредами: (ok, вывод). Для проверки токена."""
    try:
        code, text = await _run("kernels", "list", "--mine", "--page-size", "1",
                                timeout=90)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return code == 0, text


def kernel_slug():
    return f"{username()}/{KERNEL_TITLE}"


def secret_dataset_slug():
    return f"{username()}/{SECRET_DATASET_TITLE}"


async def ensure_secret_dataset():
    """Приватный датасет с токенами для кернела — создаёт или обновляет версию.

    Зачем: Kaggle Secrets привязываются к ноутбуку ТОЛЬКО через веб-интерфейс,
    в kernel-metadata.json такого поля нет. Значит запуск «одной кнопкой» через
    них невозможен. Датасет же и создаётся, и подключается к кернелу по API —
    поэтому секреты едут им. Датасет приватный; токены в исходник кернела при
    этом не попадают (их не будет в истории версий кода).

    Возвращает slug или "" — если секретов нет, датасет не нужен.
    """
    from . import secrets as secret_store

    payload = {}
    for name, key in (("GH_TOKEN", "gh_token"), ("HF_TOKEN", "hf_token"),
                      ("CIVITAI_TOKEN", "civitai_token")):
        value = secret_store.get(key)
        if value:
            payload[name] = value
    if not payload:
        return ""

    slug = secret_dataset_slug()
    d = tempfile.mkdtemp(prefix="gpuraid_ds_")
    try:
        with open(os.path.join(d, SECRET_FILE), "w", encoding="utf-8") as f:
            json.dump(payload, f)
        with open(os.path.join(d, "dataset-metadata.json"), "w", encoding="utf-8") as f:
            json.dump({"id": slug, "title": SECRET_DATASET_TITLE,
                       "licenses": [{"name": "CC0-1.0"}]}, f, indent=2)
        # приватность по умолчанию: у `datasets create` флаг -u/--public делает
        # датасет публичным — здесь его быть не должно ни при каких условиях
        code, text = await _run("datasets", "create", "-p", d, "-r", "skip",
                                timeout=180)
        if code != 0:
            if "already exists" not in text.lower() and "409" not in text:
                raise RuntimeError(f"kaggle datasets create: {text[:400]}")
            code, text = await _run("datasets", "version", "-p", d, "-m",
                                    "gpu raid secrets update", "-r", "skip",
                                    timeout=180)
            if code != 0:
                raise RuntimeError(f"kaggle datasets version: {text[:400]}")
        await _wait_dataset_ready(slug)
    finally:
        shutil.rmtree(d, ignore_errors=True)
    return slug


async def _wait_dataset_ready(slug, timeout_s=180):
    """Ждём, пока Kaggle обработает версию датасета.

    Обработка асинхронная: если запушить кернел сразу, датасет к его старту ещё
    не смонтируется, и кернел не увидит секретов (живьём: FileNotFoundError на
    /kaggle/input/... и воркер не смог опубликовать адрес в гисте).
    """
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        code, text = await _run("datasets", "status", slug, timeout=60)
        last = (text or "").strip()
        if code == 0 and "ready" in last.lower():
            return True
        if "error" in last.lower():
            raise RuntimeError(f"датасет секретов не обработан: {last[:200]}")
        await asyncio.sleep(5)
    raise RuntimeError(
        f"датасет секретов не готов за {timeout_s}с (последний статус: {last[:120]})")


def build_kernel_dir(params):
    """params: repo_url, gist_id, model_preset, max_session_min, name_prefix,
    secret_dataset (slug приватного датасета с токенами или "")."""
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        src = f.read()
    for key, value in params.items():
        src = src.replace("{{" + key.upper() + "}}", str(value))
    d = tempfile.mkdtemp(prefix="gpuraid_kaggle_")
    with open(os.path.join(d, "worker.py"), "w", encoding="utf-8") as f:
        f.write(src)
    secret_ds = str(params.get("secret_dataset") or "").strip()
    meta = {
        "id": kernel_slug(),
        "title": KERNEL_TITLE,
        "code_file": "worker.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": True,
        "enable_internet": True,
        "dataset_sources": [secret_ds] if secret_ds else [],
        "competition_sources": [],
        "kernel_sources": [],
        "model_sources": [],
    }
    with open(os.path.join(d, "kernel-metadata.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    return d


async def _run(*args, timeout=120):
    proc = await asyncio.create_subprocess_exec(
        _cli(), *args, env=_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("kaggle CLI: таймаут")
    text = (out or b"").decode(errors="replace").strip()
    return proc.returncode, text


async def push(params):
    """Пушит и запускает batch-кернел. Возвращает {kernel, log, secret_dataset}.

    Перед пушем поднимает приватный датасет с токенами и подключает его к
    кернелу — иначе воркер не сможет опубликовать свой адрес в гисте, а мы
    вернулись бы к ручному заходу в веб-интерфейс Kaggle за Secrets.
    """
    params = dict(params)
    params["secret_dataset"] = await ensure_secret_dataset()
    accelerator = str(params.get("accelerator") or DEFAULT_ACCELERATOR).strip()
    kdir = build_kernel_dir(params)
    try:
        code, text = await _run("kernels", "push", "-p", kdir,
                                "--accelerator", accelerator, timeout=180)
    finally:
        shutil.rmtree(kdir, ignore_errors=True)
    if code != 0:
        raise RuntimeError(f"kaggle push: {text[:500]}")
    return {"kernel": kernel_slug(), "log": text[:500], "accelerator": accelerator,
            "secret_dataset": params.get("secret_dataset", "")}


async def status():
    code, text = await _run("kernels", "status", kernel_slug(), timeout=60)
    if code != 0:
        raise RuntimeError(f"kaggle status: {text[:300]}")
    return {"kernel": kernel_slug(), "raw": text[:300]}
