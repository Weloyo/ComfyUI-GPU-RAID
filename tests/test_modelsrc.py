"""Тесты библиотеки источников моделей (чистая часть, без ComfyUI)."""

from gpu_raid import modelsrc


def test_hf_blob_becomes_resolve():
    # самая частая ошибка: скопирована страница файла, по ней приедет HTML
    blob = ("https://huggingface.co/Comfy-Org/Z-Image_ComfyUI/blob/main/"
            "split_files/diffusion_models/z_image_turbo_bf16.safetensors")
    assert modelsrc.normalize_url(blob) == blob.replace("/blob/", "/resolve/")


def test_normalize_keeps_good_urls():
    for url in ["https://huggingface.co/a/b/resolve/main/m.safetensors",
                "https://civitai.com/api/download/models/12345",
                "https://example.com/models/m.gguf"]:
        assert modelsrc.normalize_url(url) == url
    assert modelsrc.normalize_url("  ") == ""
    assert modelsrc.normalize_url(None) == ""


def test_guess_filename():
    assert modelsrc.guess_filename(
        "https://x/y/model.safetensors?download=true") == "model.safetensors"
    assert modelsrc.guess_filename("https://x/y/weights.gguf") == "weights.gguf"
    # страница, а не файл — имя не выдумываем
    assert modelsrc.guess_filename("https://civitai.com/models/123") == ""
    assert modelsrc.guess_filename("") == ""


def test_url_warning_catches_pages_and_local_paths():
    assert modelsrc.url_warning("") == "пустая ссылка"
    assert "публичная" in modelsrc.url_warning(r"D:\models\m.safetensors")
    assert "Civitai" in modelsrc.url_warning("https://civitai.com/models/1234567")
    assert "resolve" in modelsrc.url_warning(
        "https://huggingface.co/a/b/blob/main/m.safetensors")
    assert modelsrc.url_warning(
        "https://huggingface.co/a/b/resolve/main/m.safetensors") == ""
    assert modelsrc.url_warning("https://civitai.com/api/download/models/1") == ""


def test_key_is_stable():
    assert modelsrc.key("vae", "a.safetensors") == "vae/a.safetensors"
    assert modelsrc.key(" vae ", " a.safetensors ") == "vae/a.safetensors"


def test_builtin_catalog_shape():
    entries = modelsrc.builtin()
    assert entries, "встроенный каталог не должен быть пустым"
    for e in entries:
        assert e["filename"] and e["folder"] and e["url"].startswith("https://")
        assert e["builtin"] is True
        # встроенные ссылки обязаны быть прямыми, иначе воркер скачает HTML
        assert modelsrc.url_warning(e["url"]) == "", e["filename"]
