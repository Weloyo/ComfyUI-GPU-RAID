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


def test_env_wins_over_filesystem():
    assert _detect({"/kaggle", "/kaggle/working"}, {"COLAB_RELEASE_TAG": "1"}) == "colab"
    assert _detect({"/content", "/content/drive"},
                   {"KAGGLE_KERNEL_RUN_TYPE": "Batch"}) == "kaggle"


def test_plain_machine_is_generic():
    assert _detect(set()) == "generic"
    assert _detect({"/home/user", "/tmp"}) == "generic"


def test_gen_token_is_random_and_urlsafe():
    a, b = wb.gen_token(""), wb.gen_token("")
    assert a != b and len(a) >= 16
    assert all(c.isalnum() or c in "-_" for c in a), a
    assert wb.gen_token("свой-токен") == "свой-токен"
