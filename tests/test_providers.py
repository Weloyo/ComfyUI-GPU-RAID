"""Тесты чистой части providers (таблица подключений и разбор ввода).

Сетевые проверки здесь не трогаем — модуль специально импортируется без
ComfyUI (config/secrets/aiohttp внутри функций).
"""

from gpu_raid import providers


def test_table_is_sane():
    ids = [p["id"] for p in providers.PROVIDERS]
    assert len(ids) == len(set(ids)), "id провайдеров должны быть уникальны"
    assert providers.PROVIDER_IDS == tuple(ids)
    for p in providers.PROVIDERS:
        assert p["title"] and p["why"], p["id"]
        for link in p.get("links", []):
            assert link["label"] and link["url"].startswith("https://"), p["id"]
        for f in p.get("fields", []):
            assert f["key"] and f["label"] and "secret" in f, p["id"]
        for a in p.get("actions", []):
            assert a["id"] and a["label"], p["id"]
    # провайдер без полей обязан объяснять, что делать руками
    for p in providers.PROVIDERS:
        if p.get("no_fields"):
            assert p.get("steps"), p["id"]


def test_actions_have_handlers():
    # роуты знают ровно эти пары (pid, action) — держим таблицу в синхроне
    known = {("github", "create_gist"), ("kaggle", "install_cli")}
    declared = {(p["id"], a["id"]) for p in providers.PROVIDERS
                for a in p.get("actions", [])}
    assert declared == known, declared


def test_every_secret_field_can_be_forgotten():
    """Что сохраняем — то и умеем стирать, иначе ключ не выкинуть из панели."""
    for p in providers.PROVIDERS:
        secret_fields = [f for f in p.get("fields", []) if f.get("secret")]
        if not secret_fields:
            continue
        assert p["id"] in providers.SECRET_KEYS or p["id"] == "kaggle", p["id"]


def test_parse_kaggle_json_ok():
    creds = providers.parse_kaggle_json('{"username": "weloyo", "key": "abc123"}')
    assert creds == {"username": "weloyo", "key": "abc123"}


def test_parse_kaggle_json_errors():
    for bad, hint in [("", "пусто"), ("не json", "JSON"),
                      ('{"username": "x"}', "username"),
                      ('{"key": "x"}', "username"),
                      ('["x"]', "username")]:
        try:
            providers.parse_kaggle_json(bad)
            assert False, f"должно падать: {bad!r}"
        except ValueError as e:
            assert hint in str(e), (bad, str(e))


def test_gist_id_from_url_or_raw():
    raw = "1a2b3c4d5e"
    assert providers._gist_id(raw) == raw
    assert providers._gist_id(f"https://gist.github.com/Weloyo/{raw}") == raw
    assert providers._gist_id(f"https://gist.github.com/Weloyo/{raw}/") == raw
    assert providers._gist_id(f"https://gist.github.com/Weloyo/{raw}?foo=1") == raw
    assert providers._gist_id(f"  {raw}#file  ") == raw
    assert providers._gist_id("") == ""


def test_configured_flags():
    settings = {"llm": {"base_url": "http://127.0.0.1:1234/v1"}}
    view = {"has_gh_token": True, "has_kaggle_json": False,
            "has_hf_token": False, "has_civitai_token": False}
    assert providers.configured("llm", settings, view) is True
    assert providers.configured("github", settings, view) is True
    assert providers.configured("colab", settings, view) is True   # зависит от GH
    assert providers.configured("kaggle", settings, view) is False
    assert providers.configured("huggingface", settings, view) is False
    assert providers.configured("civitai", settings, view) is False
    assert providers.configured("llm", {}, view) is False


def test_status_view_hides_secrets():
    orig = providers._kaggle_creds
    providers._kaggle_creds = lambda: ("weloyo", "секрет")
    try:
        view = providers.status_view(
            {"llm": {"base_url": "http://x/v1", "model": "m"},
             "rendezvous": {"gist_id": "gid"},
             "connections": {"llm": {"ok": True, "detail": "3 модели", "ts": 5}}},
            {"has_llm_key": True, "has_gh_token": False, "has_kaggle_json": True,
             "has_hf_token": False, "has_civitai_token": False})
    finally:
        providers._kaggle_creds = orig

    dumped = repr(view)
    assert "секрет" not in dumped, "ключ Kaggle не должен утекать в UI"
    by_id = {p["id"]: p for p in view["providers"]}
    assert by_id["llm"]["values"] == {"base_url": "http://x/v1", "model": "m"}
    assert by_id["llm"]["check"] == {"ok": True, "detail": "3 модели", "ts": 5}
    assert by_id["kaggle"]["values"]["username"] == "weloyo"
    assert by_id["github"]["configured"] is False
    assert "kaggle_cli" in view["extra"]
