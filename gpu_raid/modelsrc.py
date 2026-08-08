"""Библиотека источников моделей: имя файла -> публичная ссылка.

Воркеры качают модели НАПРЯМУЮ с источника (HF/Civitai/любой https), минуя
канал мастера. Это принципиально: 40 ГБ через домашний исходящий канал — часы,
а с HF на облачную машину — минуты. Поэтому источник обязан быть публично
достижим; локальные файлы без ссылки раздать нельзя.

Библиотека переживает рестарты (<state_dir>/models.json) и накрывает встроенный
MODEL_CATALOG: вписал ссылку один раз — расширение больше не спрашивает.

Чистый модуль (config импортируется лениво) — покрыт тестами.
"""

import re

from .consts import MODEL_CATALOG

FILE_EXT = (".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".gguf", ".onnx",
            ".sft", ".vae")

# https://huggingface.co/<repo>/blob/<rev>/<path>  ->  .../resolve/<rev>/<path>
_HF_BLOB = re.compile(r"^(https://huggingface\.co/.+?)/blob/(.+)$")


def normalize_url(url):
    """Приводит ссылку к прямой, скачиваемой.

    Самая частая ошибка — скопировать адрес страницы файла на HF (…/blob/…):
    по нему приедет HTML вместо весов. Чиним молча.
    """
    url = str(url or "").strip()
    if not url:
        return ""
    m = _HF_BLOB.match(url)
    if m:
        url = f"{m.group(1)}/resolve/{m.group(2)}"
    return url


def guess_filename(url):
    """Имя файла из ссылки (без query) — чтобы не заставлять вбивать руками."""
    path = str(url or "").split("?")[0].split("#")[0].rstrip("/")
    name = path.rsplit("/", 1)[-1] if "/" in path else path
    return name if name.lower().endswith(FILE_EXT) else ""


def url_warning(url):
    """Человеческое предупреждение, если ссылка непохожа на файл. '' = всё ок."""
    url = str(url or "").strip()
    if not url:
        return "пустая ссылка"
    if not url.startswith(("http://", "https://")):
        return ("нужна публичная http(s)-ссылка: воркеры качают напрямую с "
                "источника, локальные пути им недоступны")
    low = url.split("?")[0].lower()
    if re.match(r"^https://civitai\.com/models/\d+", low):
        return ("это адрес страницы Civitai, а не файла — нужна ссылка вида "
                "https://civitai.com/api/download/models/<versionId> "
                "(кнопка Download → «Copy link»)")
    if "huggingface.co" in low and "/resolve/" not in low:
        return ("на Hugging Face нужна ссылка «Download» (…/resolve/…), а не "
                "адрес страницы файла")
    if "civitai.com/api/download/" in low:
        return ""      # Civitai отдаёт файл без расширения в пути — это нормально
    if not low.endswith(FILE_EXT):
        return "ссылка не заканчивается именем файла модели — проверьте, что она прямая"
    return ""


# ---------------------------------------------------------------------------
# хранилище
# ---------------------------------------------------------------------------

def _path():
    from . import config

    import os
    return os.path.join(config.state_dir(), "models.json")


def _load_user():
    from . import config

    data = config.load_json(_path(), None) or {}
    entries = data.get("models") if isinstance(data, dict) else None
    return [e for e in (entries or []) if isinstance(e, dict) and e.get("filename")]


def _save_user(entries):
    from . import config

    config.save_json_atomic(_path(), {"models": entries})


def key(folder, filename):
    return f"{str(folder or '').strip()}/{str(filename or '').strip()}"


def builtin():
    """Встроенный каталог как записи библиотеки (перекрываются пользовательскими)."""
    out = []
    for item in MODEL_CATALOG:
        out.append({
            "filename": item["filename"], "folder": item["folder"],
            "url": item["url"], "size_gb": item.get("size_gb"),
            "note": item.get("name", ""), "builtin": True,
        })
    return out


def catalog():
    """Полный список источников: встроенные + пользовательские (те главнее)."""
    merged = {}
    for entry in builtin():
        merged[key(entry["folder"], entry["filename"])] = entry
    for entry in _load_user():
        entry = dict(entry, builtin=False)
        merged[key(entry.get("folder"), entry["filename"])] = entry
    return sorted(merged.values(), key=lambda e: (e.get("folder") or "", e["filename"]))


def resolve(filename, folder=""):
    """Источник для файла: сперва точное совпадение папки, потом по имени."""
    filename = str(filename or "").strip()
    if not filename:
        return None
    entries = catalog()
    if folder:
        for e in entries:
            if e["filename"] == filename and e.get("folder") == folder:
                return e
    for e in entries:
        if e["filename"] == filename:
            return e
    return None


def upsert(entry):
    """Добавляет/обновляет пользовательский источник. Возвращает нормализованный."""
    filename = str(entry.get("filename") or "").strip()
    folder = str(entry.get("folder") or "").strip()
    url = normalize_url(entry.get("url"))
    if not url:
        raise ValueError("нужна ссылка на файл модели")
    if not filename:
        filename = guess_filename(url)
    if not filename:
        raise ValueError("не удалось определить имя файла — впишите его вручную")
    if not folder:
        raise ValueError("укажите папку моделей (diffusion_models, vae, …)")
    record = {"filename": filename, "folder": folder, "url": url,
              "note": str(entry.get("note") or "").strip(),
              "size_gb": entry.get("size_gb")}
    entries = [e for e in _load_user()
               if key(e.get("folder"), e.get("filename")) != key(folder, filename)]
    entries.append(record)
    _save_user(entries)
    return dict(record, builtin=False)


def remove(folder, filename):
    """Удаляет пользовательский источник (встроенные не трогает)."""
    entries = _load_user()
    rest = [e for e in entries
            if key(e.get("folder"), e.get("filename")) != key(folder, filename)]
    if len(rest) == len(entries):
        return False
    _save_user(rest)
    return True
