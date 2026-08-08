"""Хранилище секретов (LLM-ключ, GitHub-токен, HF/Civitai-токены, kaggle.json).

Отдельный файл <state_dir>/secrets.json — НЕ workers.json: значения секретов
никогда не попадают в GET-ответы (public_view отдаёт только булевы has_*),
в WS-события, логи и манифесты. kaggle.json хранится отдельным файлом в том
же каталоге (его ждёт официальный kaggle CLI через KAGGLE_CONFIG_DIR).

chmod 0600 — best-effort: на Windows прав POSIX нет, файл защищён тем, что
лежит в профиле пользователя.
"""

import os

from . import config

KEYS = ("llm_api_key", "gh_token", "hf_token", "civitai_token")


def secrets_path():
    return os.path.join(config.state_dir(), "secrets.json")


def kaggle_json_path():
    return os.path.join(config.state_dir(), "kaggle.json")


def _chmod_private(path):
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def load():
    return config.load_json(secrets_path(), {}) or {}


def save(patch):
    """Обновляет только известные ключи; пустое значение удаляет секрет."""
    data = load()
    for key, value in (patch or {}).items():
        if key not in KEYS:
            continue
        value = str(value or "").strip()
        if value:
            data[key] = value
        else:
            data.pop(key, None)
    config.save_json_atomic(secrets_path(), data)
    _chmod_private(secrets_path())
    return data


def get(key, default=""):
    return load().get(key, default)


def save_kaggle_json(text):
    text = str(text or "").strip()
    if not text:
        try:
            os.remove(kaggle_json_path())
        except OSError:
            pass
        return
    with open(kaggle_json_path(), "w", encoding="utf-8") as f:
        f.write(text)
    _chmod_private(kaggle_json_path())


def public_view():
    data = load()
    return {
        "has_llm_key": bool(data.get("llm_api_key")),
        "has_gh_token": bool(data.get("gh_token")),
        "has_hf_token": bool(data.get("hf_token")),
        "has_civitai_token": bool(data.get("civitai_token")),
        "has_kaggle_json": os.path.isfile(kaggle_json_path()),
    }
