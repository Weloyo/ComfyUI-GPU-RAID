"""Подключения: одно место для всех внешних регистраций GPU RAID.

Идея: пользователь один раз проходит по списку провайдеров — кнопка ведёт
ровно на ту страницу, где берётся ключ, ключ вставляется в поле, расширение
тут же проверяет его на живом API и запоминает результат. Дальше ничего
переспрашивать не нужно.

Таблица PROVIDERS — чистые данные (её целиком отдаём фронтенду), сетевые
проверки лежат в check(). aiohttp, config и secrets импортируются лениво,
чтобы модуль оставался импортируемым в тестах без ComfyUI.

Секреты уходят в secrets.json и наружу НИКОГДА не возвращаются: UI видит
только «настроено / проверено / под каким аккаунтом». Результат последней
проверки хранится в settings["connections"][id] и переживает рестарт.
"""

import json
import logging
import sys
import time

from .consts import COLAB_NOTEBOOK_URL

log = logging.getLogger("gpu_raid")

HTTP_TIMEOUT_S = 20

PROVIDERS = (
    {
        "id": "llm",
        "title": "LLM — Сценарист",
        "why": "разбивает сюжет на сегменты и пишет промпты кадров",
        "optional": True,
        "fallback": "без LLM сюжет режется эвристикой по предложениям — "
                    "план будет, но промпты придётся причесать руками",
        "links": [
            {"label": "LM Studio — локально, без ключа", "url": "https://lmstudio.ai/"},
            {"label": "Ollama — локально, без ключа", "url": "https://ollama.com/"},
            {"label": "Ключ OpenRouter", "url": "https://openrouter.ai/keys"},
        ],
        "fields": [
            {"key": "base_url", "label": "base_url", "secret": False,
             "placeholder": "http://127.0.0.1:1234/v1 (LM Studio) · :11434/v1 (Ollama)"},
            {"key": "model", "label": "модель", "secret": False,
             "placeholder": "пусто = возьмём первую из списка при проверке"},
            {"key": "api_key", "label": "ключ", "secret": True,
             "placeholder": "локальным LLM не нужен"},
        ],
    },
    {
        "id": "github",
        "title": "GitHub — автоподключение воркеров",
        "why": "приватный gist, через который облачные воркеры сами сообщают "
               "мастеру свой адрес",
        "optional": True,
        "fallback": "без него connection string от Colab/Kaggle придётся "
                    "копировать в панель руками при каждом запуске",
        "links": [
            {"label": "Создать токен (scope gist уже проставлен)",
             "url": "https://github.com/settings/tokens/new"
                    "?scopes=gist&description=ComfyUI%20GPU%20RAID"},
            {"label": "Fine-grained токен (альтернатива: права только Gists)",
             "url": "https://github.com/settings/personal-access-tokens/new"},
        ],
        "fields": [
            {"key": "gh_token", "label": "токен", "secret": True,
             "placeholder": "ghp_… — нужен только доступ к Gists"},
            {"key": "gist_id", "label": "gist", "secret": False,
             "placeholder": "создастся кнопкой ниже — или вставьте id готового"},
        ],
        "actions": [
            {"id": "create_gist", "label": "Создать приватный gist",
             "title": "мастер создаст пустой приватный gist и сам пропишет его id"},
        ],
    },
    {
        "id": "kaggle",
        "title": "Kaggle — воркер по кнопке",
        "why": "запуск batch-кернела с 2×T4 прямо из панели",
        "optional": True,
        "fallback": "без него Kaggle-ноутбук запускается вручную",
        "links": [
            {"label": "Настройки Kaggle → секция API → «Create New Token»",
             "url": "https://www.kaggle.com/settings"},
        ],
        "steps": [
            "Пролистайте до секции API и нажмите «Create New Token».",
            "Новый Kaggle показывает строку KGAT_… в окне «API Token is now "
            "available» — скопируйте её (второй раз её не покажут). Старый "
            "скачивал файл kaggle.json — тогда откройте файл блокнотом и "
            "скопируйте строку целиком.",
            "Вставьте скопированное в поле «токен» ниже и впишите своё имя "
            "аккаунта Kaggle — из токена оно не читается, а без него не "
            "собрать адрес кернела.",
        ],
        "fields": [
            {"key": "username", "label": "аккаунт", "secret": False,
             "placeholder": "ваш логин на kaggle.com"},
            {"key": "kaggle_json", "label": "токен", "secret": True,
             "multiline": True,
             "placeholder": 'KGAT_… либо содержимое старого kaggle.json'},
        ],
        "actions": [
            {"id": "install_cli", "label": "Установить kaggle CLI",
             "title": "pip install kaggle в питон мастера (нужен для пуша кернела)"},
        ],
    },
    {
        "id": "huggingface",
        "title": "Hugging Face — загрузка моделей",
        "why": "скачивание моделей на воркеров, включая gated-репозитории",
        "optional": True,
        "fallback": "публичные модели качаются и без токена",
        "links": [
            {"label": "Создать read-токен",
             "url": "https://huggingface.co/settings/tokens/new"
                    "?tokenType=read&tokenName=ComfyUI+GPU+RAID"},
        ],
        "fields": [
            {"key": "hf_token", "label": "токен", "secret": True, "placeholder": "hf_…"},
        ],
    },
    {
        "id": "civitai",
        "title": "Civitai — загрузка моделей",
        "why": "скачивание чекпойнтов и LoRA с Civitai на воркеров",
        "optional": True,
        "fallback": "часть моделей Civitai отдаёт и без ключа",
        "links": [
            {"label": "Создать API-ключ (Account → API Keys)",
             "url": "https://civitai.com/user/account"},
        ],
        "fields": [
            {"key": "civitai_token", "label": "ключ", "secret": True, "placeholder": "…"},
        ],
    },
    {
        "id": "colab",
        "title": "Google Colab — воркер в ноутбуке",
        "why": "A100/L4 из подписки Colab как воркер GPU RAID",
        "optional": True,
        "fallback": "",
        "no_fields": True,
        "links": [
            {"label": "Открыть ноутбук воркера", "url": COLAB_NOTEBOOK_URL},
        ],
        "steps": [
            "Настройте GitHub выше — воркер подключится сам.",
            "В ноутбуке: 🔑 Secrets → добавьте GH_TOKEN (тот же токен) и "
            "включите доступ для ноутбука.",
            "Runtime → Run all. Воркер появится в списке за ~30 секунд.",
        ],
    },
)

PROVIDER_IDS = tuple(p["id"] for p in PROVIDERS)


def _by_id(pid):
    for p in PROVIDERS:
        if p["id"] == pid:
            return p
    raise KeyError(pid)


# ---------------------------------------------------------------------------
# состояние
# ---------------------------------------------------------------------------

def _secrets():
    from . import secrets as secret_store

    return secret_store


def _kaggle_creds():
    """(username, key) из сохранённого kaggle.json или (None, None)."""
    from . import config

    data = config.load_json(_secrets().kaggle_json_path(), None)
    if not isinstance(data, dict):
        return None, None
    return data.get("username") or None, data.get("key") or None


def kaggle_username(settings=None):
    """Имя аккаунта: из kaggle.json (старая схема) или из настроек (новая)."""
    user, _ = _kaggle_creds()
    if user:
        return user
    if settings is None:
        from .workers import REGISTRY

        settings = REGISTRY.settings()
    return str((settings.get("kaggle") or {}).get("username") or "")


TOKEN_PREFIX = "KGAT_"


def parse_kaggle_credentials(text):
    """Разбирает то, что дал Kaggle, — в двух форматах сразу.

    Старый: файл kaggle.json c {"username","key"} (CLI берёт его через
    KAGGLE_CONFIG_DIR). Новый (с 2026): одна строка KGAT_… для переменной
    KAGGLE_API_TOKEN, имени аккаунта в ней нет — его спрашиваем отдельно.

    Возвращает {"kind": "json"|"token", ...}. Чистая функция: пользователь
    вполне может вставить сюда что угодно.
    """
    # люди копируют по-разному: с кавычками, с пробелами, с хвостом строки
    text = str(text or "").strip().strip('"\'').strip()
    if not text:
        raise ValueError("пусто")
    if text.startswith(TOKEN_PREFIX):
        token = text.split()[0].strip().strip('"\'')
        if len(token) <= len(TOKEN_PREFIX):
            raise ValueError("токен обрезан — скопируйте строку KGAT_… целиком")
        return {"kind": "token", "token": token}
    if not text.lstrip().startswith("{"):
        raise ValueError(
            "не похоже ни на kaggle.json, ни на токен: вставьте либо строку "
            "KGAT_… из окна «API Token is now available», либо содержимое "
            "старого файла kaggle.json"
        )
    try:
        data = json.loads(text)
    except Exception:
        raise ValueError("это не JSON — вставьте содержимое файла kaggle.json целиком")
    if not isinstance(data, dict) or not data.get("username") or not data.get("key"):
        raise ValueError('в JSON нет полей "username" и "key"')
    return {"kind": "json", "username": str(data["username"]), "key": str(data["key"])}


# совместимость со старым именем (и старыми тестами)
def parse_kaggle_json(text):
    creds = parse_kaggle_credentials(text)
    if creds["kind"] != "json":
        raise ValueError("это токен новой схемы, а не kaggle.json")
    return {"username": creds["username"], "key": creds["key"]}


def kaggle_cli_path():
    """Путь к kaggle CLI.

    По PATH искать бесполезно: pip кладёт его в Scripts/ питона мастера, а у
    портабла эта папка в PATH процесса не попадает. Поэтому смотрим рядом с
    интерпретатором, которым нас запустили.
    """
    import os
    import shutil
    import sysconfig

    found = shutil.which("kaggle")
    if found:
        return found
    names = ("kaggle.exe", "kaggle") if sys.platform == "win32" else ("kaggle",)
    dirs = [sysconfig.get_path("scripts"),
            os.path.join(os.path.dirname(sys.executable), "Scripts"),
            os.path.join(os.path.dirname(sys.executable), "bin")]
    for d in dirs:
        for name in names:
            path = os.path.join(d or "", name)
            if os.path.isfile(path):
                return path
    return ""


def kaggle_cli_present():
    return bool(kaggle_cli_path())


def configured(pid, settings, secrets_view):
    """Заполнено ли главное поле провайдера (без сетевой проверки)."""
    if pid == "llm":
        return bool((settings.get("llm") or {}).get("base_url"))
    if pid == "github":
        return bool(secrets_view.get("has_gh_token"))
    if pid == "kaggle":
        return bool(secrets_view.get("has_kaggle_json")
                    or secrets_view.get("has_kaggle_token"))
    if pid == "huggingface":
        return bool(secrets_view.get("has_hf_token"))
    if pid == "civitai":
        return bool(secrets_view.get("has_civitai_token"))
    if pid == "colab":
        return bool(secrets_view.get("has_gh_token"))
    return False


def status_view(settings, secrets_view):
    """Полный снимок для панели: таблица провайдеров + что уже настроено."""
    checks = settings.get("connections") or {}
    llm = settings.get("llm") or {}
    rdv = settings.get("rendezvous") or {}
    values = {
        "llm": {"base_url": llm.get("base_url", ""), "model": llm.get("model", "")},
        "github": {"gist_id": rdv.get("gist_id", "")},
        "kaggle": {"username": kaggle_username(settings)},
    }
    out = []
    for p in PROVIDERS:
        pid = p["id"]
        out.append({
            **p,
            "configured": configured(pid, settings, secrets_view),
            "values": values.get(pid, {}),
            "check": checks.get(pid) or {},
        })
    extra = {"kaggle_cli": kaggle_cli_present(), "python": sys.executable}
    return {"providers": out, "extra": extra}


async def _remember(pid, ok, detail, pending=""):
    """pending — id действия, которого не хватает до готовности (не ошибка!)."""
    from .workers import REGISTRY

    result = {"ok": bool(ok), "detail": str(detail or ""), "ts": int(time.time()),
              "pending": str(pending or "")}
    current = dict((REGISTRY.settings().get("connections") or {}))
    current[pid] = result
    await REGISTRY.update_settings({"connections": current})
    return result


# ---------------------------------------------------------------------------
# сохранение
# ---------------------------------------------------------------------------

async def save(pid, payload):
    """Раскладывает присланные поля по settings и secrets.json."""
    from .workers import REGISTRY

    _by_id(pid)
    payload = payload or {}
    sec, settings_patch = {}, {}

    def given(key):
        # пустая строка = «не трогать»; стереть секрет можно значением null
        return key in payload and payload[key] is not None

    if pid == "llm":
        llm = {}
        if given("base_url"):
            llm["base_url"] = str(payload["base_url"]).strip().rstrip("/")
        if given("model"):
            llm["model"] = str(payload["model"]).strip()
        if llm:
            settings_patch["llm"] = llm
        if given("api_key") and str(payload["api_key"]).strip():
            sec["llm_api_key"] = str(payload["api_key"]).strip()
    elif pid == "github":
        if given("gh_token") and str(payload["gh_token"]).strip():
            sec["gh_token"] = str(payload["gh_token"]).strip()
        if given("gist_id"):
            settings_patch["rendezvous"] = {"gist_id": _gist_id(payload["gist_id"])}
    elif pid == "kaggle":
        if given("username"):
            settings_patch["kaggle"] = {"username": str(payload["username"]).strip()}
        if given("kaggle_json") and str(payload["kaggle_json"]).strip():
            creds = parse_kaggle_credentials(payload["kaggle_json"])
            if creds["kind"] == "json":
                _secrets().save_kaggle_json(json.dumps(
                    {"username": creds["username"], "key": creds["key"]}))
                settings_patch.setdefault("kaggle", {})["username"] = creds["username"]
            else:
                sec["kaggle_token"] = creds["token"]
    elif pid == "huggingface":
        if given("hf_token") and str(payload["hf_token"]).strip():
            sec["hf_token"] = str(payload["hf_token"]).strip()
    elif pid == "civitai":
        if given("civitai_token") and str(payload["civitai_token"]).strip():
            sec["civitai_token"] = str(payload["civitai_token"]).strip()

    if sec:
        _secrets().save(sec)
    if settings_patch:
        await REGISTRY.update_settings(settings_patch)


# provider -> ключи в secrets.json (kaggle хранится отдельным файлом)
SECRET_KEYS = {
    "llm": ("llm_api_key",),
    "github": ("gh_token",),
    "kaggle": ("kaggle_token",),
    "huggingface": ("hf_token",),
    "civitai": ("civitai_token",),
}


async def forget(pid):
    """Стирает сохранённые ключи провайдера — «выйти из аккаунта»."""
    from .workers import REGISTRY

    _by_id(pid)
    keys = SECRET_KEYS.get(pid, ())
    if keys:
        _secrets().save({k: "" for k in keys})   # пустое значение удаляет секрет
    if pid == "kaggle":
        _secrets().save_kaggle_json("")
        await REGISTRY.update_settings({"kaggle": {"username": ""}})
    if pid == "github":
        await REGISTRY.update_settings({"rendezvous": {"gist_id": ""}})
    if pid == "llm":
        await REGISTRY.update_settings({"llm": {"base_url": "", "model": ""}})
    return await _remember(pid, False, "сброшено")


def _gist_id(value):
    """Из ссылки на gist достаём id — пользователи копируют URL целиком."""
    value = str(value or "").strip().rstrip("/")
    if "/" in value:
        value = value.rsplit("/", 1)[-1]
    return value.split("?")[0].split("#")[0]


# ---------------------------------------------------------------------------
# проверки на живых API
# ---------------------------------------------------------------------------

async def check(pid):
    """Дёргает провайдера и запоминает результат. Возвращает {ok, detail, extra}."""
    fn = {
        "llm": _check_llm,
        "github": _check_github,
        "kaggle": _check_kaggle,
        "huggingface": _check_hf,
        "civitai": _check_civitai,
        "colab": _check_colab,
    }[_by_id(pid)["id"]]
    try:
        ok, detail, extra = await fn()
    except Exception as e:
        ok, extra = False, {}
        if isinstance(e, TimeoutError):   # aiohttp кидает его же без текста
            detail = f"сервис не ответил за {HTTP_TIMEOUT_S} с — проверьте сеть или прокси"
        else:
            detail = str(e).strip() or type(e).__name__
    # «ключ верный, но остался шаг» — это не провал: помечаем pending, чтобы UI
    # не пугал красным там, где надо просто нажать соседнюю кнопку
    pending = extra.pop("pending", "") if isinstance(extra, dict) else ""
    stored = await _remember(pid, ok, detail, pending)
    # extra (например, список моделей) живёт только в ответе: settings хранит
    # компактный результат, иначе мутация сохранённого объекта утечёт на диск
    return {**stored, "extra": extra}


def _session():
    import aiohttp

    return aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_S),
        headers={"User-Agent": "comfyui-gpu-raid"},
    )


async def _check_llm():
    from .workers import REGISTRY

    llm = REGISTRY.settings().get("llm") or {}
    base = str(llm.get("base_url") or "").strip().rstrip("/")
    if not base:
        return False, "не задан base_url", {}
    key = _secrets().get("llm_api_key")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with _session() as s:
        async with s.get(f"{base}/models", headers=headers) as r:
            if r.status == 401:
                return False, "401 — нужен или неверен API-ключ", {}
            if r.status != 200:
                return False, f"HTTP {r.status}", {}
            data = await r.json(content_type=None)
    models = [m.get("id") for m in (data or {}).get("data") or [] if m.get("id")]
    cur = str(llm.get("model") or "").strip()
    if not cur and models:
        await REGISTRY.update_settings({"llm": {"model": models[0]}})
        cur = models[0]
    detail = f"доступно моделей: {len(models)}" + (f" · выбрана {cur}" if cur else "")
    if cur and models and cur not in models:
        detail += " · такой модели на сервере нет"
    return True, detail, {"models": models[:100]}


async def _github_headers():
    token = _secrets().get("gh_token")
    if not token:
        return None
    return {"Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json"}


async def _check_github():
    from .workers import REGISTRY

    headers = await _github_headers()
    if not headers:
        return False, "нет токена", {}
    async with _session() as s:
        async with s.get("https://api.github.com/user", headers=headers) as r:
            if r.status == 401:
                return False, "401 — токен неверен или отозван", {}
            if r.status != 200:
                return False, f"HTTP {r.status}", {}
            login = (await r.json()).get("login") or "?"
        gist_id = str((REGISTRY.settings().get("rendezvous") or {}).get("gist_id") or "")
        if not gist_id:
            return (False, f"токен работает (аккаунт {login}) — остался один шаг: "
                    "нажмите «Создать приватный gist»",
                    {"login": login, "pending": "create_gist"})
        async with s.get(f"https://api.github.com/gists/{gist_id}",
                         headers=headers) as r:
            if r.status == 404:
                return (False, f"аккаунт {login}: gist {gist_id} не найден — "
                        "создайте новый кнопкой ниже",
                        {"login": login, "pending": "create_gist"})
            if r.status != 200:
                return False, f"аккаунт {login}: gist HTTP {r.status}", {"login": login}
    return True, f"аккаунт {login} · gist {gist_id}", {"login": login}


async def create_gist():
    """Создаёт приватный gist под rendezvous и сразу прописывает его id.

    Идемпотентна: если рабочий gist уже прописан — возвращает его, а не плодит
    пустые гисты в аккаунте на каждое нажатие кнопки.
    """
    from .workers import REGISTRY

    headers = await _github_headers()
    if not headers:
        raise RuntimeError("сначала сохраните GitHub-токен")

    current = str((REGISTRY.settings().get("rendezvous") or {}).get("gist_id") or "")
    if current:
        async with _session() as s:
            async with s.get(f"https://api.github.com/gists/{current}",
                             headers=headers) as r:
                if r.status == 200:
                    data = await r.json()
                    return {"gist_id": current, "reused": True,
                            "html_url": data.get("html_url", "")}

    body = {
        "description": "ComfyUI GPU RAID — rendezvous (воркеры публикуют сюда адреса)",
        "public": False,
        "files": {"README.md": {"content":
                                "Служебный gist ComfyUI GPU RAID.\n"
                                "Воркеры кладут сюда файлы w_<session>.json.\n"}},
    }
    async with _session() as s:
        async with s.post("https://api.github.com/gists", headers=headers,
                          json=body) as r:
            if r.status == 401:
                raise RuntimeError("401 — токен неверен")
            if r.status == 403:
                raise RuntimeError("403 — у токена нет права на Gists")
            if r.status not in (200, 201):
                raise RuntimeError(f"GitHub HTTP {r.status}: {(await r.text())[:200]}")
            data = await r.json()
    gist_id = data.get("id")
    if not gist_id:
        raise RuntimeError("GitHub не вернул id гиста")
    await REGISTRY.update_settings({"rendezvous": {"gist_id": gist_id}})
    return {"gist_id": gist_id, "html_url": data.get("html_url", "")}


async def _check_kaggle():
    """Две схемы: старый kaggle.json (проверяем по HTTP) и новый токен KGAT_…

    У нового токена единственный документированный потребитель — сам kaggle CLI
    (переменная KAGGLE_API_TOKEN), поэтому и проверяем им же: он нам всё равно
    нужен, чтобы пушить кернел.
    """
    from . import kaggle_api

    legacy_user, legacy_key = _kaggle_creds()
    token = _secrets().get("kaggle_token")
    if not legacy_key and not token:
        return False, "токен не сохранён", {}
    user = kaggle_username()
    if not user:
        return (False, "впишите имя аккаунта Kaggle — из токена KGAT_… оно не "
                "читается, а без него не собрать адрес кернела", {})
    cli = kaggle_cli_present()

    if legacy_key:
        import aiohttp

        async with _session() as s:
            async with s.get("https://www.kaggle.com/api/v1/kernels/list",
                             params={"mine": "true", "pageSize": "1"},
                             auth=aiohttp.BasicAuth(legacy_user, legacy_key)) as r:
                if r.status in (401, 403):
                    return False, f"{r.status} — ключ неверен или отозван", {}
                if r.status != 200:
                    return False, f"HTTP {r.status}", {}
        if not cli:
            return (False, f"ключ работает (аккаунт {user}) — остался один шаг: "
                    "нажмите «Установить kaggle CLI»",
                    {"username": user, "cli": False, "pending": "install_cli"})
        return True, f"аккаунт {user} (схема kaggle.json)", {"username": user, "cli": True}

    if not cli:
        return (False, "токен сохранён — остался один шаг: «Установить kaggle CLI». "
                "Новая схема Kaggle проверяется только через него",
                {"username": user, "cli": False, "pending": "install_cli"})
    ok, text = await kaggle_api.check_cli()
    if not ok:
        return False, f"kaggle CLI не принял токен: {text[:200]}", {"username": user}
    return True, f"аккаунт {user} (токен KGAT)", {"username": user, "cli": True}


async def install_kaggle_cli():
    """pip install kaggle в питон мастера — иначе пуш кернела невозможен."""
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "kaggle",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("pip: таймаут (5 минут)")
    text = (out or b"").decode(errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"pip вернул {proc.returncode}: {text[-400:]}")
    return {"ok": True, "cli": kaggle_cli_present(), "log": text[-400:]}


async def _check_hf():
    token = _secrets().get("hf_token")
    if not token:
        return False, "нет токена", {}
    async with _session() as s:
        async with s.get("https://huggingface.co/api/whoami-v2",
                         headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status == 401:
                return False, "401 — токен неверен", {}
            if r.status != 200:
                return False, f"HTTP {r.status}", {}
            data = await r.json()
    return True, f"аккаунт {data.get('name') or '?'}", {}


async def _check_civitai():
    token = _secrets().get("civitai_token")
    if not token:
        return False, "нет ключа", {}
    async with _session() as s:
        async with s.get("https://civitai.com/api/v1/models",
                         params={"limit": "1"},
                         headers={"Authorization": f"Bearer {token}"}) as r:
            if r.status in (401, 403):
                return False, f"{r.status} — ключ неверен", {}
            if r.status != 200:
                return False, f"HTTP {r.status}", {}
    return True, "ключ принят", {}


async def _check_colab():
    """У Colab нет API — проверяем то, от чего он зависит."""
    from .workers import REGISTRY

    if not _secrets().get("gh_token"):
        return False, "нужен GitHub-токен выше", {}
    if not (REGISTRY.settings().get("rendezvous") or {}).get("gist_id"):
        return False, "нужен gist (кнопка в блоке GitHub)", {}
    cloud = [w for w in REGISTRY.records(include_local=False)
             if (w.get("platform") or "") == "colab"]
    if cloud:
        return True, f"воркеров Colab в реестре: {len(cloud)}", {}
    return True, "всё готово — запустите ноутбук (Run all)", {}
