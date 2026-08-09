"""Тесты серверной закачки моделей (роль воркера).

Модуль зависит от ComfyUI (`folder_paths`), поэтому подставляем заглушку до
импорта — интересует не ComfyUI, а решение «куда лить файл»: на Kaggle рабочий
каталог воркера ограничен 20 ГБ, и один набор моделей его переполняет.
"""

import os
import shutil
import sys
import types

_fake = types.ModuleType("folder_paths")
_fake.get_folder_paths = lambda folder: (
    [f"/comfy/models/{folder}"] if folder in ("diffusion_models", "vae", "text_encoders") else [])
sys.modules.setdefault("folder_paths", _fake)

from gpu_raid import downloads  # noqa: E402


def _no_makedirs(fn):
    """Тесты не должны создавать каталоги вроде D:\\comfy\\models."""
    def wrapper(*a, **kw):
        real = os.makedirs
        os.makedirs = lambda *args, **kwargs: None
        try:
            return fn(*a, **kw)
        finally:
            os.makedirs = real
    return wrapper


def _dirs(folder, scratch=None):
    saved = os.environ.get("GPURAID_MODELS_DIR")
    if scratch is None:
        os.environ.pop("GPURAID_MODELS_DIR", None)
    else:
        os.environ["GPURAID_MODELS_DIR"] = scratch
    try:
        return _no_makedirs(downloads._dirs)(folder)
    finally:
        os.environ.pop("GPURAID_MODELS_DIR", None)
        if saved is not None:
            os.environ["GPURAID_MODELS_DIR"] = saved


def test_dirs_default_is_models_folder():
    store, link = _dirs("vae")
    assert store == "/comfy/models/vae"
    assert link is None       # symlink не нужен: файл и так на месте


def test_dirs_scratch_keeps_symlink_target():
    store, link = _dirs("diffusion_models", "/kaggle/tmp/gpuraid_models")
    assert store == os.path.join("/kaggle/tmp/gpuraid_models", "diffusion_models")
    assert link == "/comfy/models/diffusion_models"


def test_dirs_empty_scratch_ignored():
    # пустая переменная приезжает от bootstrap на не-Kaggle платформах
    assert _dirs("vae", "   ")[1] is None


def test_dirs_unknown_folder():
    try:
        _dirs("не-такая-папка")
    except ValueError:
        return
    raise AssertionError("неизвестная папка моделей должна давать ValueError")


def test_check_space_refuses_before_download():
    real = shutil.disk_usage
    shutil.disk_usage = lambda p: types.SimpleNamespace(total=0, used=0, free=5 << 30)
    try:
        downloads._check_space("/x", 3 << 30)          # 3 ГБ в 5 свободных — можно
        try:
            downloads._check_space("/x", 19 << 30)     # 19 ГБ в 5 — нельзя
        except RuntimeError as e:
            assert "не хватит места" in str(e), e
        else:
            raise AssertionError("должно было отказать до закачки")
        # запас: 4.5 ГБ в 5 свободных — впритык, значит отказ
        try:
            downloads._check_space("/x", int(4.5 * (1 << 30)))
        except RuntimeError:
            pass
        else:
            raise AssertionError("запас FREE_SPACE_MARGIN не учтён")
    finally:
        shutil.disk_usage = real


def test_check_space_silent_without_content_length():
    downloads._check_space("/nonexistent", 0)   # размер неизвестен — не мешаем


def test_publish_reports_broken_symlink():
    real_symlink, real_lexists = os.symlink, os.path.lexists
    os.path.lexists = lambda p: False
    os.symlink = lambda *a: (_ for _ in ()).throw(OSError("Operation not permitted"))
    try:
        downloads._publish("/kaggle/tmp/gpuraid_models/vae/ae.safetensors",
                           "/comfy/models/vae", "ae.safetensors")
    except RuntimeError as e:
        assert "GPURAID_MODELS_DIR" in str(e), e
    else:
        raise AssertionError("невозможность симлинка нельзя проглатывать: "
                             "модель есть на диске, но ComfyUI её не увидит")
    finally:
        os.symlink, os.path.lexists = real_symlink, real_lexists


def test_publish_noop_without_link_dir():
    downloads._publish("/comfy/models/vae/ae.safetensors", None, "ae.safetensors")


def test_part_size_of_missing_file_is_zero():
    """С этого числа начинается Range-докачка: у несуществующего .part — ноль,
    иначе первая же попытка запросила бы у сервера мусорный диапазон."""
    assert downloads._part_size("/нет/такого/файла.part") == 0


def test_retry_budget_is_meaningful():
    """Живьём HF оборвал 11 ГБ дважды подряд (на 25% и 62%). Одна попытка —
    это лотерея, поэтому их несколько с растущей паузой."""
    assert downloads.ATTEMPTS >= 3
    assert downloads.RETRY_PAUSE_S > 0
