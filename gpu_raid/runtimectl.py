"""Управление рантаймами из нод-лоадеров: опции привязки, наличие моделей,
запуск/остановка воркеров и сценарии автостопа.

Панель больше не занимается железом: каждый лоадер на канве сам говорит, где
ему считаться (properties.gpuraid_runtime), показывает статус своего воркера
и умеет его поднять/погасить. Здесь — сетевая обвязка этих нод; чистые правила
привязки — в placement.py.
"""

import logging
import time

from . import distribute, events, kaggle_api, modelsrc, providers
from . import lifecycle_rules as rules
from . import placement as pm
from . import secrets as secret_store
from .consts import COLAB_NOTEBOOK_URL, FOLDER_ALIASES, LOADER_TABLE, REPO_URL
from .graph_rewrite import RewriteError
from .lifecycle import LIFECYCLE
from .workers import LOCAL_ID, REGISTRY

log = logging.getLogger("gpu_raid")

# платформы, которые предлагаем даже без единой записи в реестре:
# привязку задают ДО первого запуска рантайма
KNOWN_PLATFORMS = ("colab", "kaggle")

_LISTINGS = {}          # (worker_id, folder) -> (ts, [имена])
LISTINGS_TTL_S = 45.0


def worker_view(record):
    st = REGISTRY.status.get(record["id"], {})
    return {
        "id": record["id"], "name": record["name"],
        "platform": record.get("platform") or st.get("platform") or "",
        "kind": record.get("kind"),
        "state": "online" if record["id"] == LOCAL_ID else st.get("state", "unknown"),
        "enabled": bool(record.get("enabled", True)),
        "gpu": st.get("gpu", ""),
        "vram_total_gb": st.get("vram_total_gb"),
        "latency_ms": st.get("latency_ms"),
        "error": st.get("error", ""),
        "lifecycle": record.get("lifecycle") or {},
    }


def _views():
    return [worker_view(r) for r in REGISTRY.enabled_records()]


# ---------------------------------------------------------------------------
# опции привязки для дропдаунов лоадеров
# ---------------------------------------------------------------------------

def options():
    settings = REGISTRY.settings()
    lc = settings.get("lifecycle") or {}
    plat_overrides = lc.get("platform_overrides") or {}
    views = _views()
    out = []

    local = next((v for v in views if v["id"] == LOCAL_ID), None)
    if local:
        out.append({
            "value": "local", "label": "Локальная GPU",
            "platform": "local", "state": "online",
            "worker": local, "workers": [local],
            "can_start": False, "can_stop": False, "scenario": {},
        })

    platforms = {}
    for v in views:
        if v["id"] != LOCAL_ID and v["platform"]:
            platforms.setdefault(v["platform"], []).append(v)
    for plat in sorted(set(platforms) | set(KNOWN_PLATFORMS)):
        group = platforms.get(plat, [])
        wid, _reason = pm.resolve_assignment(f"platform:{plat}", views)
        resolved = next((v for v in group if v["id"] == wid), None)
        state = ("online" if resolved
                 else (group[0]["state"] if group else "none"))
        out.append({
            "value": f"platform:{plat}",
            "label": pm.PLATFORM_LABELS.get(plat, plat),
            "platform": plat, "state": state,
            "worker": resolved, "workers": group,
            "can_start": plat in KNOWN_PLATFORMS,
            "can_stop": bool(resolved),
            "scenario": plat_overrides.get(plat) or {},
        })

    for v in views:
        if v["id"] == LOCAL_ID or v["platform"]:
            continue
        out.append({
            "value": f"id:{v['id']}", "label": v["name"],
            "platform": "", "state": v["state"],
            "worker": v if v["state"] == "online" else None, "workers": [v],
            "can_start": False, "can_stop": v["state"] == "online",
            "scenario": v.get("lifecycle") or {},
        })

    return {
        "options": out,
        "loader_classes": {ct: dict(tab) for ct, tab in LOADER_TABLE.items()},
        "lifecycle_policy": lc.get("policy", "eco"),
        "idle_stop_min": lc.get("idle_stop_min", 10),
        "colab_notebook_url": COLAB_NOTEBOOK_URL,
    }


# ---------------------------------------------------------------------------
# наличие моделей на воркере привязки
# ---------------------------------------------------------------------------

def _drop_fresh_done_listings():
    """Закачка завершилась — инвентарь воркера устарел, кэш листингов долой."""
    now = time.time()
    for task in distribute.TASKS.values():
        if task.get("state") != "done":
            continue
        if now - task.get("finished", 0) > LISTINGS_TTL_S:
            continue
        wid = task.get("worker_id")
        for key in [k for k in _LISTINGS if k[0] == wid]:
            _LISTINGS.pop(key, None)


async def _listing(record, folder):
    """Имена файлов папки воркера (с синонимами), None = не удалось узнать."""
    names, failed = [], False
    for alias in FOLDER_ALIASES.get(folder, (folder,)):
        cached = _LISTINGS.get((record["id"], alias))
        if cached and time.time() - cached[0] < LISTINGS_TTL_S:
            names.extend(cached[1])
            continue
        try:
            got = (await distribute._worker_models(record, [alias])).get(alias, [])
        except Exception as e:
            log.debug("runtime listing %s/%s: %s", record["id"], alias, e)
            failed = True
            continue
        _LISTINGS[(record["id"], alias)] = (time.time(), got)
        names.extend(got)
    return None if (failed and not names) else names


async def status(items):
    """items: [{assign, folder, filename}] -> наличие/источник/закачка по каждому."""
    _drop_fresh_done_listings()
    views = _views()
    by_id = {v["id"]: v for v in views}
    resolved = {}
    prepared = []
    for item in items or []:
        assign = pm.normalize_assign(item.get("assign"))
        folder = str(item.get("folder") or "").strip()
        filename = str(item.get("filename") or "").strip()
        if assign not in resolved:
            resolved[assign] = pm.resolve_assignment(assign, views)
        wid, reason = resolved[assign]
        prepared.append((assign, folder, filename, wid, reason))

    listings = {}   # (wid, folder) -> [имена] | None
    for assign, folder, filename, wid, _reason in prepared:
        if not (wid and folder) or (wid, folder) in listings:
            continue
        record = REGISTRY.get(wid)
        listings[(wid, folder)] = await _listing(record, folder) if record else None

    out = []
    for assign, folder, filename, wid, reason in prepared:
        source = modelsrc.resolve(filename, folder)
        entry = {
            "assign": assign, "folder": folder, "filename": filename,
            "worker": by_id.get(wid), "reason": reason or "",
            "url": (source or {}).get("url", ""),
            "size_gb": (source or {}).get("size_gb"),
            "present": "unknown",
            "download": None,
        }
        have = listings.get((wid, folder))
        if have is not None and filename:
            remap = ((REGISTRY.get(wid) or {}).get("model_remap") or {})
            candidate = (remap.get(folder) or {}).get(filename) or filename
            flat = candidate.replace("\\", "/").split("/")[-1]
            have_flat = set(have) | {str(n).replace("\\", "/").split("/")[-1]
                                     for n in have}
            entry["present"] = ("have" if candidate in have_flat or flat in have_flat
                                else "missing")
        if wid:
            task = distribute.TASKS.get(f"{wid}|{modelsrc.key(folder, filename)}")
            if task:
                entry["download"] = {k: task.get(k) for k in
                                     ("state", "bytes_done", "bytes_total", "error")}
        out.append(entry)
    return {"items": out}


# ---------------------------------------------------------------------------
# запуск / остановка рантайма по привязке
# ---------------------------------------------------------------------------

async def _start_kaggle():
    settings = REGISTRY.settings()
    gist_id = (settings.get("rendezvous") or {}).get("gist_id", "")
    sv = secret_store.public_view()
    if not gist_id or not sv["has_gh_token"]:
        raise RewriteError("автозапуску Kaggle нужен gist-rendezvous: панель → "
                           "«Подключения и ключи» → GitHub (токен + «Создать "
                           "приватный gist»)")
    if not providers.configured("kaggle", settings, sv):
        raise RewriteError("токен Kaggle не сохранён: панель → «Подключения и "
                           "ключи» → Kaggle")
    if not providers.kaggle_cli_present():
        raise RewriteError("kaggle CLI не установлен: панель → «Подключения и "
                           "ключи» → Kaggle → «Установить kaggle CLI»")
    params = {
        "repo_url": REPO_URL, "gist_id": gist_id, "model_preset": "none",
        "max_session_min": (settings.get("lifecycle") or {}).get("budget_min") or 0,
        "name_prefix": "kaggle", "accelerator": kaggle_api.DEFAULT_ACCELERATOR,
    }
    try:
        result = await kaggle_api.push(params)
    except RuntimeError as e:
        raise RewriteError(str(e))
    events.toast("info", f"Kaggle-кернел «{result['kernel']}» запущен "
                         f"({result.get('accelerator', '')}) — воркер "
                         "зарегистрируется сам через несколько минут")
    return {"ok": True, "kernel": result.get("kernel"),
            "detail": "кернел пушится — воркер появится через несколько минут"}


async def start(assign):
    assign = pm.normalize_assign(assign)
    wid, reason = pm.resolve_assignment(assign, _views())
    if wid:
        record = REGISTRY.get(wid)
        return {"ok": True, "already": True,
                "detail": f"воркер «{record['name']}» уже в сети"}
    if assign.startswith("platform:"):
        plat = assign.split(":", 1)[1]
        if plat == "kaggle":
            return await _start_kaggle()
        if plat == "colab":
            sv = secret_store.public_view()
            rd_ok = bool((REGISTRY.settings().get("rendezvous") or {}).get("gist_id")
                         and sv.get("has_gh_token"))
            detail = ("откроется ноутбук — Runtime → Run all, больше ничего: "
                      "конфиг читается из Colab Secrets, gist находится сам "
                      "(первый раз добавьте секреты GH_TOKEN и "
                      "I_USE_PAID_COLAB=true — см. карточку Colab в панели)")
            if not rd_ok:
                detail = ("откроется ноутбук — нажмите Run all. Внимание: "
                          "rendezvous не настроен (панель → «Подключения и ключи» "
                          "→ GitHub: токен + «Создать приватный gist»), воркер "
                          "не сможет зарегистрироваться сам")
            return {"ok": True, "open_url": COLAB_NOTEBOOK_URL, "detail": detail}
        raise RewriteError(f"платформа «{plat}» не умеет автозапуск — поднимите "
                           "воркера на его стороне и он появится сам")
    raise RewriteError(reason or "эту привязку нельзя запустить отсюда: воркера "
                                 "поднимают на его платформе")


async def stop(assign):
    wid, reason = pm.resolve_assignment(pm.normalize_assign(assign), _views())
    if not wid:
        raise RewriteError(reason or "воркер не найден")
    if wid == LOCAL_ID:
        raise RewriteError("локальный инстанс не останавливается")
    record = REGISTRY.get(wid)
    ok = await LIFECYCLE.stop_worker(record, "остановлено из ноды-лоадера")
    return {"stopped": ok, "worker": record["name"]}


# ---------------------------------------------------------------------------
# сценарий автостопа привязки
# ---------------------------------------------------------------------------

def _clean_scenario(raw):
    if not isinstance(raw, dict):
        return {}
    out = {}
    policy = str(raw.get("policy") or "").strip()
    if policy and policy != "inherit":
        if policy not in rules.POLICIES:
            raise RewriteError(f"неизвестная политика «{policy}»")
        out["policy"] = policy
    try:
        idle = int(raw.get("idle_stop_min") or 0)
    except (TypeError, ValueError):
        idle = 0
    if idle > 0:
        out["idle_stop_min"] = idle
    return out


async def set_scenario(assign, scenario):
    """Сценарий автостопа для привязки: платформа — в настройки (переживает
    перерождение сессий), конкретный воркер — в его запись."""
    assign = pm.normalize_assign(assign)
    scenario = _clean_scenario(scenario)
    if assign.startswith("platform:"):
        plat = assign.split(":", 1)[1]
        overrides = dict((REGISTRY.settings().get("lifecycle") or {})
                         .get("platform_overrides") or {})
        if scenario:
            overrides[plat] = scenario
        else:
            overrides.pop(plat, None)
        await REGISTRY.update_settings({"lifecycle": {"platform_overrides": overrides}})
        return {"ok": True, "platform": plat, "scenario": scenario}
    if assign.startswith("id:"):
        wid = assign.split(":", 1)[1]
        record = await REGISTRY.update(wid, {"lifecycle": scenario})
        if record is None:
            raise RewriteError("воркер не найден")
        return {"ok": True, "worker_id": wid, "scenario": scenario}
    raise RewriteError("сценарий автостопа применим к облачным привязкам "
                       "(платформа или конкретный воркер)")
