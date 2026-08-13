"""Тесты бутстрапа воркера (stdlib-скрипт, импортируется без ComfyUI).

Определение платформы уже дважды ломалось живьём в обе стороны, поэтому
сценарии здесь описывают именно реальные образы, а не идеальные.
"""

import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "scripts"))

import worker_bootstrap as wb  # noqa: E402

ENV_VARS = ("COLAB_RELEASE_TAG", "COLAB_GPU", "KAGGLE_KERNEL_RUN_TYPE",
            "KAGGLE_URL_BASE", "KAGGLE_DATA_PROXY_TOKEN", "KAGGLE_CONTAINER_NAME")


def _detect(dirs, env=None, colab_module=False):
    real_isdir, real_find = os.path.isdir, importlib.util.find_spec
    saved = {k: os.environ.get(k) for k in ENV_VARS}
    os.path.isdir = lambda p: p in dirs
    importlib.util.find_spec = (
        lambda m: object() if (m == "google.colab" and colab_module) else None)
    for k in ENV_VARS:
        os.environ.pop(k, None)
    os.environ.update(env or {})
    try:
        return wb.platform_detect()
    finally:
        os.path.isdir, importlib.util.find_spec = real_isdir, real_find
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_colab_despite_kaggle_dir():
    """В образе Colab есть /kaggle — из-за этого Colab считался Kaggle, и
    автостоп не звал runtime.unassign(), оставляя рантайм жечь квоту."""
    assert _detect({"/kaggle", "/content", "/content/sample_data"},
                   colab_module=True) == "colab"


def test_kaggle_despite_content_dir():
    """В образе Kaggle есть /content — после первой правки уже Kaggle стал
    считаться Colab, и каталоги воркера уехали в /content вместо /kaggle/tmp."""
    assert _detect({"/kaggle", "/kaggle/working", "/kaggle/input", "/content"}) == "kaggle"


def test_kaggle_batch_kernel_without_env_vars():
    """Живой batch-кернел Kaggle (2026-08-09): переменных KAGGLE_* нет, а модуль
    google.colab импортируется — Kaggle третий раз определился как Colab, увёл
    каталоги в /content и остался без обхода лимита 20 ГБ."""
    assert _detect({"/kaggle", "/kaggle/working", "/kaggle/input", "/content"},
                   colab_module=True) == "kaggle"


def test_kaggle_working_dir_beats_colab_env():
    """Живьём (2026-08-09): в кернеле Kaggle есть COLAB_*-переменные, и они
    перебивали всё остальное. Рабочий каталог Kaggle надёжнее любой env."""
    assert _detect({"/kaggle", "/kaggle/working"}, {"COLAB_RELEASE_TAG": "1"}) == "kaggle"
    assert _detect({"/kaggle", "/kaggle/working"}, {"COLAB_GPU": "1"},
                   colab_module=True) == "kaggle"


def test_env_wins_over_filesystem():
    # без рабочего каталога Kaggle переменные по-прежнему главнее каталогов
    assert _detect({"/kaggle"}, {"COLAB_RELEASE_TAG": "1"}) == "colab"
    assert _detect({"/content", "/content/drive"},
                   {"KAGGLE_KERNEL_RUN_TYPE": "Batch"}) == "kaggle"


def test_plain_machine_is_generic():
    assert _detect(set()) == "generic"
    assert _detect({"/home/user", "/tmp"}) == "generic"


def _with_dirs(dirs, fn):
    real_isdir, real_makedirs = os.path.isdir, os.makedirs
    os.path.isdir = lambda p: p in dirs
    os.makedirs = lambda *a, **kw: None
    try:
        return fn()
    finally:
        os.path.isdir, os.makedirs = real_isdir, real_makedirs


def test_models_scratch_only_where_working_dir_is_capped():
    """Kaggle держит ComfyUI в /kaggle/working с лимитом 20 ГБ — один набор
    моделей его переполняет, поэтому веса уезжают на эфемерный диск."""
    assert _with_dirs({"/kaggle/working"},
                      lambda: wb.models_scratch("kaggle")) == "/kaggle/tmp/gpuraid_models"
    assert _with_dirs(set(), lambda: wb.models_scratch("colab")) == ""
    assert _with_dirs(set(), lambda: wb.models_scratch("generic")) == ""


def test_models_scratch_trusts_disk_over_label():
    """Ярлык платформы ошибался трижды. Если под ногами /kaggle/working —
    лимит 20 ГБ реален, как бы воркер себя ни называл."""
    assert _with_dirs({"/kaggle/working"},
                      lambda: wb.models_scratch("colab")) == "/kaggle/tmp/gpuraid_models"
    assert _with_dirs({"/kaggle/working"},
                      lambda: wb.scratch_args("colab"))[1] == "/kaggle/tmp/gpuraid_scratch/output"


def test_gen_token_is_random_and_urlsafe():
    a, b = wb.gen_token(""), wb.gen_token("")
    assert a != b and len(a) >= 16
    assert all(c.isalnum() or c in "-_" for c in a), a
    assert wb.gen_token("свой-токен") == "свой-токен"


def test_copy_fast_chunked_matches_source():
    """Параллельная почанковая копия (ускорение Drive-кэша) обязана быть
    байт-в-байт: границы диапазонов не кратны блоку и размеру файла."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src, dst = os.path.join(td, "src.bin"), os.path.join(td, "dst.bin")
        data = os.urandom(3 * 2**20 + 13)  # нарочно некруглый размер
        with open(src, "wb") as f:
            f.write(data)
        wb._copy_fast(src, dst, workers=4, block=256 * 1024)
        assert open(dst, "rb").read() == data
        assert not os.path.exists(dst + ".part")


def test_copy_fast_small_file_plain_branch():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        src, dst = os.path.join(td, "s"), os.path.join(td, "d")
        with open(src, "wb") as f:
            f.write(b"x" * 1024)
        wb._copy_fast(src, dst)  # size < 4*block -> обычный copy
        assert open(dst, "rb").read() == b"x" * 1024


def test_hf_plan_existing_then_drive_then_hf():
    """skip: файл уже в models/ (датасет/Drive-линк закрыл позицию);
    drive: есть в кэше; hf: больше негде."""
    import tempfile
    items = [("r", "vae/a.safetensors", "vae"),
             ("r", "vae/b.safetensors", "vae"),
             ("r", "vae/c.safetensors", "vae")]
    with tempfile.TemporaryDirectory() as td:
        comfy, cache = os.path.join(td, "comfy"), os.path.join(td, "cache")
        os.makedirs(os.path.join(comfy, "models", "vae"))
        os.makedirs(os.path.join(cache, "vae"))
        open(os.path.join(comfy, "models", "vae", "a.safetensors"), "wb").write(b"x")
        open(os.path.join(cache, "vae", "b.safetensors"), "wb").write(b"y")
        open(os.path.join(cache, "vae", "c.safetensors"), "wb")  # 0 байт = обрезок
        plan = wb._hf_plan(items, comfy, cache)
        assert [p["source"] for p in plan] == ["skip", "drive", "hf"]
        # без Drive-кэша качаем всё, чего нет на месте
        plan = wb._hf_plan(items, comfy, None)
        assert [p["source"] for p in plan] == ["skip", "hf", "hf"]


class _PipCalls:
    """Подмена wb.sh: считает pip-вызовы, ничего не запускает."""

    def __init__(self):
        self.calls = []

    def __call__(self, cmd, check=True, **kw):
        self.calls.append(cmd)
        class Res:
            returncode = 0
        return Res()


def test_pip_stamp_skips_unchanged_requirements():
    """Ячейка Colab зовёт install_comfy дважды (сама + внутри bring_up), и
    каждый pip-резолв стоил десятки секунд. Штамп: повторный вызов по тем же
    requirements — бесплатный, изменение файла снова запускает pip."""
    import tempfile
    real_sh = wb.sh
    fake = _PipCalls()
    wb.sh = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            req = os.path.join(td, "requirements.txt")
            open(req, "w").write("einops\n")
            assert wb._pip_install_reqs([req], td, "t") is not None
            assert wb._pip_install_reqs([req], td, "t") is None
            assert len(fake.calls) == 1
            open(req, "w").write("einops\nnumpy\n")
            assert wb._pip_install_reqs([req], td, "t") is not None
            assert len(fake.calls) == 2
            # несуществующие requirements не считаются и не ломают штамп
            assert wb._pip_install_reqs([os.path.join(td, "нет.txt")], td, "t2") is None
    finally:
        wb.sh = real_sh


def test_install_comfy_twice_runs_pip_once():
    import tempfile
    real_sh = wb.sh
    fake = _PipCalls()
    wb.sh = fake
    try:
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "comfy"))  # клон уже есть
            open(os.path.join(td, "requirements.txt"), "w").write("aiohttp\n")
            wb.install_comfy(td)
            wb.install_comfy(td)
            pip_calls = [c for c in fake.calls if "pip" in c]
            assert len(pip_calls) == 1, fake.calls
    finally:
        wb.sh = real_sh


def test_hf_writeback_saves_only_fresh_downloads():
    """В Drive-кэш уезжает только скачанное С HF в этой сессии (как и до
    ускорения), зато фоном; нулевой обрезок в кэше перезаписывается."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        local, cache = os.path.join(td, "m"), os.path.join(td, "cache")
        os.makedirs(local)
        fresh = os.path.join(local, "fresh.bin")
        open(fresh, "wb").write(b"fresh")
        skipped = os.path.join(local, "old.bin")
        open(skipped, "wb").write(b"old")
        stub = os.path.join(cache, "vae")
        os.makedirs(stub)
        open(os.path.join(stub, "husk.bin"), "wb")  # 0 байт от прошлого обрыва
        husk_local = os.path.join(local, "husk.bin")
        open(husk_local, "wb").write(b"healed")
        plan = [
            {"filename": "fresh.bin", "dst": fresh, "fetched_from_hf": True,
             "drive_path": os.path.join(cache, "vae", "fresh.bin")},
            {"filename": "old.bin", "dst": skipped, "source": "skip",
             "drive_path": os.path.join(cache, "vae", "old.bin")},
            {"filename": "husk.bin", "dst": husk_local, "fetched_from_hf": True,
             "drive_path": os.path.join(cache, "vae", "husk.bin")},
        ]
        t = wb._hf_writeback_async(plan)
        assert t is not None
        t.join(timeout=30)
        assert open(os.path.join(cache, "vae", "fresh.bin"), "rb").read() == b"fresh"
        assert open(os.path.join(cache, "vae", "husk.bin"), "rb").read() == b"healed"
        assert not os.path.exists(os.path.join(cache, "vae", "old.bin"))
        assert wb._hf_writeback_async([plan[1]]) is None


def test_pick_rendezvous_gist():
    mk = lambda i, d, ts: {"id": i, "description": d, "updated_at": ts}
    gists = [
        mk("aaa", "мой дневник", "2026-01-01T00:00:00Z"),
        mk("bbb", "ComfyUI GPU RAID — rendezvous (воркеры публикуют сюда адреса)",
           "2026-01-02T00:00:00Z"),
        mk("ccc", "ComfyUI GPU RAID — rendezvous (воркеры публикуют сюда адреса)",
           "2026-03-01T00:00:00Z"),
        {"description": "ComfyUI GPU RAID", "updated_at": "2026-04-01T00:00:00Z"},
    ]
    # самый свежий из подходящих; записи без id пропускаются
    assert wb.pick_rendezvous_gist(gists) == "ccc"
    assert wb.pick_rendezvous_gist([mk("x", "чужой gist", "2026-01-01")]) == ""
    assert wb.pick_rendezvous_gist([]) == ""
    assert wb.pick_rendezvous_gist(None) == ""
