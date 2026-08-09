"""Пути и атомарное чтение/запись JSON-состояния (workers.json и пр.)."""

import json
import os

import folder_paths

from .consts import DEFAULT_SETTINGS


def state_dir():
    d = os.path.join(folder_paths.get_user_directory(), "default", "gpu-raid")
    os.makedirs(d, exist_ok=True)
    return d


def workers_path():
    return os.path.join(state_dir(), "workers.json")


def temp_base():
    """Каталог приёма результатов юнитов (под output — виден через /view)."""
    d = os.path.join(folder_paths.get_output_directory(), "gpuraid_tmp")
    os.makedirs(d, exist_ok=True)
    return d


def deliver_base():
    """Каталог доставок offload/longvideo: output/gpuraid/."""
    d = os.path.join(folder_paths.get_output_directory(), "gpuraid")
    os.makedirs(d, exist_ok=True)
    return d


def input_dir():
    return folder_paths.get_input_directory()


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except Exception:
        # битый файл не должен валить сервер: откладываем в .bad и стартуем с дефолта
        try:
            os.replace(path, path + ".bad")
        except OSError:
            pass
        return default


def save_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def merged_settings(stored):
    out = json.loads(json.dumps(DEFAULT_SETTINGS))
    for key, value in (stored or {}).items():
        # вложенные блоки (timeouts/lifecycle/llm/rendezvous) сливаем поверх
        # дефолтов, чтобы частично сохранённый блок не терял остальные ключи
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def sanitize_name(name, fallback="job"):
    keep = "".join(c if c.isalnum() or c in "-_." else "_" for c in str(name).strip())
    return keep[:60] or fallback


def safe_filename(name):
    """Одно имя файла без разделителей пути — защита от path traversal.

    Имя приходит из недоверенного источника (тело запроса к мастеру, поле
    filename из /history воркера): срезаем каталоги и оставляем базовое имя,
    отклоняя пустое/'.'/'..'. Разделители обеих ОС режем всегда — имена
    моделей и результатов их не содержат, а мастер и воркеры бывают на разных
    платформах, так что basename одной ОС не спасает от разделителя другой.
    """
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1].strip()
    if base in ("", ".", ".."):
        raise ValueError(f"недопустимое имя файла: {name!r}")
    return base
