"""Привязка «модель → рантайм» из workflow и раскладка островов по привязкам.

Источник правды — properties.gpuraid_runtime у нод-лоадеров прямо на канве
(в том числе внутри subgraph'ов: фронтенд ComfyUI разворачивает их в prompt
с id вида "<инстанс>:<внутренний>", здесь строится тот же id). Форматы:

  ""             — не задано (остров разложит auto_place)
  "local"        — локальная GPU мастера
  "platform:X"   — любой воркер платформы X (colab / kaggle / …)
  "id:WID"       — конкретный воркер по id записи реестра

Правило раскладки: остров получает привязку самого «тяжёлого» из своих
назначенных лоадеров (вес = размер файлов модели). Так реплицированный
VAELoader, попавший и в encode-, и в decode-остров, не растаскивает
тяжёлую диффузию: у кого веса больше, тот и решает, а конфликт виден
в предупреждениях.

Чистый модуль: без ComfyUI-импортов, покрыт tests/test_placement.py.
"""

from .consts import LOADER_TABLE, RUNTIME_PROP

LOCAL_ASSIGN = "local"
_PREFIX_PLATFORM = "platform:"
_PREFIX_ID = "id:"

# человеческие имена привязок для сообщений и отчётов
PLATFORM_LABELS = {"colab": "Colab", "kaggle": "Kaggle", "generic": "облако"}


def normalize_assign(value):
    """Строка привязки из properties: мусор и незнакомые форматы -> ""."""
    s = str(value or "").strip()
    if not s:
        return ""
    if s == LOCAL_ASSIGN or s.startswith((_PREFIX_PLATFORM, _PREFIX_ID)):
        return s
    return ""


def assign_label(assign):
    """Читаемое имя привязки: platform:colab -> «Colab», id:x -> «воркер x»."""
    assign = normalize_assign(assign)
    if not assign:
        return "авто"
    if assign == LOCAL_ASSIGN:
        return "локальная GPU"
    if assign.startswith(_PREFIX_PLATFORM):
        plat = assign[len(_PREFIX_PLATFORM):]
        return PLATFORM_LABELS.get(plat, plat)
    return f"воркер {assign[len(_PREFIX_ID):]}"


def iter_workflow_nodes(workflow):
    """(api_id, node) по всем нодам workflow, включая вложенные subgraph'ы.

    api_id совпадает с id ноды в API-prompt'е: верхний уровень — "6",
    внутри инстанса subgraph — "105:6" (так же клеит фронтенд ComfyUI).
    Инстансов одного subgraph может быть несколько — обходим каждый со своим
    префиксом; защита от рекурсивных определений — по цепочке посещённых id.
    """
    wf = workflow if isinstance(workflow, dict) else {}
    defs = {}
    for sg in ((wf.get("definitions") or {}).get("subgraphs") or []):
        if isinstance(sg, dict) and sg.get("id"):
            defs[str(sg["id"])] = sg

    def walk(container, prefix, seen):
        for node in (container.get("nodes") or []):
            if not isinstance(node, dict) or node.get("id") is None:
                continue
            api_id = f"{prefix}{node['id']}"
            yield api_id, node
            sub = defs.get(str(node.get("type")))
            if sub is not None and str(sub["id"]) not in seen:
                yield from walk(sub, api_id + ":", seen | {str(sub["id"])})

    yield from walk(wf, "", frozenset())


def extract_assignments(workflow, loader_classes=None):
    """{api_id: привязка} для нод-лоадеров workflow с непустой привязкой."""
    classes = LOADER_TABLE if loader_classes is None else loader_classes
    out = {}
    for api_id, node in iter_workflow_nodes(workflow):
        if node.get("type") not in classes:
            continue
        assign = normalize_assign((node.get("properties") or {}).get(RUNTIME_PROP))
        if assign:
            out[api_id] = assign
    return out


def resolve_assignment(assign, workers, local_id="local"):
    """Привязка -> (worker_id | None, причина-если-не-вышло).

    workers: [{id, name, platform, state, enabled}] — ВСЕ записи реестра
    (включая офлайн: «есть, но не поднят» и «нет вовсе» — разные подсказки).
    """
    assign = normalize_assign(assign)
    if not assign:
        return None, "привязка не задана"
    if assign == LOCAL_ASSIGN:
        return local_id, ""
    if assign.startswith(_PREFIX_ID):
        wid = assign[len(_PREFIX_ID):]
        for w in workers:
            if w.get("id") != wid:
                continue
            if not w.get("enabled", True):
                return None, f"воркер «{w.get('name') or wid}» выключен"
            if w.get("state") != "online":
                return None, (f"воркер «{w.get('name') or wid}» не в сети — "
                              "запустите его рантайм")
            return wid, ""
        return None, f"воркер {wid} не найден (удалён из реестра?)"
    plat = assign[len(_PREFIX_PLATFORM):]
    label = PLATFORM_LABELS.get(plat, plat)
    cands = [w for w in workers
             if (w.get("platform") or "") == plat and w.get("enabled", True)]
    online = sorted((w for w in cands if w.get("state") == "online"),
                    key=lambda w: str(w.get("name") or w.get("id")))
    if online:
        return online[0]["id"], ""
    if cands:
        return None, f"воркер {label} есть, но не в сети — запустите его рантайм"
    return None, f"нет воркера {label} — запустите его рантайм"


def island_loaders(graph, island):
    """id нод-лоадеров острова (свои + реплики source-нод)."""
    out = []
    for nid in list(island.get("nodes") or []) + list(island.get("replicated") or []):
        node = graph.get(nid) or {}
        if node.get("class_type") in LOADER_TABLE:
            out.append(nid)
    return out


def _loader_title(graph, nid):
    node = graph.get(nid) or {}
    names = []
    for key in (LOADER_TABLE.get(node.get("class_type")) or {}):
        value = (node.get("inputs") or {}).get(key)
        if isinstance(value, str) and value:
            names.append(value)
    label = node.get("class_type") or nid
    return f"{label} ({', '.join(names)})" if names else str(label)


def place_islands(graph, part, assignments, loader_weights=None):
    """Раскладка островов по привязкам лоадеров.

    -> (placement {island_id: привязка}, decided_by {island_id: nid лоадера},
        warnings [str]).

    Острова без назначенных лоадеров в placement не попадают — их дозаполнит
    auto_place. Конфликт привязок внутри острова решается весом (кто тяжелее,
    тот и выбирает воркера) и фиксируется предупреждением.
    """
    weights = loader_weights or {}
    placement, decided_by, warnings = {}, {}, []
    for isl in part["islands"]:
        cand = []
        for nid in island_loaders(graph, isl):
            assign = assignments.get(nid)
            if assign:
                cand.append((float(weights.get(nid) or 0), str(nid), assign))
        if not cand:
            continue
        cand.sort(key=lambda t: (-t[0], t[1]))
        _, top_nid, top_assign = cand[0]
        placement[isl["id"]] = top_assign
        decided_by[isl["id"]] = top_nid
        losers = [(nid, a) for _, nid, a in cand if a != top_assign]
        if losers:
            detail = "; ".join(
                f"{_loader_title(graph, nid)} → {assign_label(a)}" for nid, a in losers)
            warnings.append(
                f"стадия {isl['id']}: модели склеены в один узел графа, но привязаны "
                f"к разным рантаймам ({detail}) — весь узел едет на "
                f"{assign_label(top_assign)} (решает самая тяжёлая модель: "
                f"{_loader_title(graph, top_nid)})")
    return placement, decided_by, warnings
