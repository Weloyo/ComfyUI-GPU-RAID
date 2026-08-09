"""Тесты config: атомарное состояние и санитайз имён файлов.

config импортирует ComfyUI-модуль folder_paths — подставляем заглушку до
импорта (интересует чистая логика, не ComfyUI).
"""

import sys
import types

# Заглушка общая на процесс (setdefault): держим её совместимой с той, что
# ставит test_downloads, — иначе кто зарегистрируется первым, тот и определит
# поведение get_folder_paths для обоих модулей.
_fake = types.ModuleType("folder_paths")
_fake.get_folder_paths = lambda folder: (
    [f"/comfy/models/{folder}"] if folder in ("diffusion_models", "vae", "text_encoders") else [])
sys.modules.setdefault("folder_paths", _fake)

from gpu_raid import config  # noqa: E402


def _rejected(name):
    try:
        config.safe_filename(name)
    except ValueError:
        return True
    return False


def test_safe_filename_keeps_plain_name():
    assert config.safe_filename("flux1-dev.safetensors") == "flux1-dev.safetensors"


def test_safe_filename_strips_posix_dirs():
    # '../custom_nodes/evil.py' от вредоносного воркера не должен вылезти вверх
    assert config.safe_filename("../../custom_nodes/evil.py") == "evil.py"
    assert config.safe_filename("/abs/path/model.safetensors") == "model.safetensors"


def test_safe_filename_strips_windows_dirs():
    # мастер на Windows: разделитель другой ОС тоже режем (basename бы не спас)
    assert config.safe_filename(r"..\..\startup\x.bat") == "x.bat"
    assert config.safe_filename(r"C:\Windows\evil.dll") == "evil.dll"


def test_safe_filename_rejects_traversal_only_names():
    assert _rejected("..")
    assert _rejected(".")
    assert _rejected("")
    assert _rejected("   ")
    assert _rejected("../..")      # basename '..' после срезки каталогов


def test_safe_filename_allows_leading_dots_in_real_name():
    # файл, буквально названный '..foo' — это имя, а не выход вверх
    assert config.safe_filename("..foo.safetensors") == "..foo.safetensors"
