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

from . import config
from . import secrets as secret_store

TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "kaggle_kernel_template.py",
)

KERNEL_TITLE = "gpu-raid-worker"


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
    env["KAGGLE_CONFIG_DIR"] = config.state_dir()   # старая схема: kaggle.json
    token = secret_store.get("kaggle_token")
    if token:
        env["KAGGLE_API_TOKEN"] = token             # новая схема: строка KGAT_…
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


def build_kernel_dir(params):
    """params: repo_url, gist_id, model_preset, max_session_min, name_prefix."""
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        src = f.read()
    for key, value in params.items():
        src = src.replace("{{" + key.upper() + "}}", str(value))
    d = tempfile.mkdtemp(prefix="gpuraid_kaggle_")
    with open(os.path.join(d, "worker.py"), "w", encoding="utf-8") as f:
        f.write(src)
    meta = {
        "id": kernel_slug(),
        "title": KERNEL_TITLE,
        "code_file": "worker.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": True,
        "enable_internet": True,
        "dataset_sources": [],
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
    """Пушит и запускает batch-кернел. Возвращает {kernel, log}."""
    kdir = build_kernel_dir(params)
    try:
        code, text = await _run("kernels", "push", "-p", kdir, timeout=180)
    finally:
        shutil.rmtree(kdir, ignore_errors=True)
    if code != 0:
        raise RuntimeError(f"kaggle push: {text[:500]}")
    return {"kernel": kernel_slug(), "log": text[:500]}


async def status():
    code, text = await _run("kernels", "status", kernel_slug(), timeout=60)
    if code != 0:
        raise RuntimeError(f"kaggle status: {text[:300]}")
    return {"kernel": kernel_slug(), "raw": text[:300]}
