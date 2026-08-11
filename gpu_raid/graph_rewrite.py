"""Перезапись API-графов ComfyUI: юниты страйпинга, хвост, offload, Long Video.

Чистый модуль (без ComfyUI-импортов) — покрыт тестами tests/test_graph_rewrite.py.

API-формат графа: {node_id(str): {"class_type": str, "inputs": {key: value}, "_meta": {"title": str}}}
Ссылка на выход другой ноды кодируется списком [source_node_id, output_slot].
Массивные значения виджетов могут приходить как {"__value__": [...]} — это НЕ ссылка.
"""

import copy

from .consts import (
    GPURAID_CLASSES,
    GPURAID_MARKER_CLASSES,
    INDEX_PH,
    LOADER_TABLE,
    LV_END,
    LV_KEYFRAME_OUT,
    LV_OUT,
    LV_PROMPT,
    LV_START,
    LV_STEPS,
    NODE_COLLECTOR,
    NODE_DISTRIBUTOR,
    NODE_STORY,
    NODE_TILED_UPSCALE,
    NODE_VIDEO_SPEC,
    PREFIX_PH,
    SAVE_NODE_ID,
    SEED_KEYS,
    SEED_PH,
    STEPS_KEYS,
    TEXT_KEYS,
    UPLOAD_TABLE,
    VIDEO_HINTS,
    VIDEO_OUT_CLASSES,
)


class RewriteError(Exception):
    """Ошибка валидации/перезаписи графа. Текст показывается пользователю."""


def is_link(value):
    return (
        isinstance(value, list)
        and len(value) == 2
        and isinstance(value[0], (str, int))
        and isinstance(value[1], int)
    )


def iter_links(node):
    for key, value in node.get("inputs", {}).items():
        if is_link(value):
            yield key, str(value[0]), value[1]


def find_by_class(graph, class_type):
    return [nid for nid, node in graph.items() if node.get("class_type") == class_type]


def ancestors(graph, start_id):
    """Множество id предков start_id, включая его самого."""
    seen = set()
    stack = [str(start_id)]
    while stack:
        nid = stack.pop()
        if nid in seen or nid not in graph:
            continue
        seen.add(nid)
        for _, src, _ in iter_links(graph[nid]):
            stack.append(src)
    return seen


def descendants(graph, start_id):
    """Множество id потомков start_id (достижимых по ссылкам ОТ него), без него самого."""
    start_id = str(start_id)
    consumers = {}
    for nid, node in graph.items():
        for _, src, _ in iter_links(node):
            consumers.setdefault(src, set()).add(nid)
    seen = set()
    stack = [start_id]
    while stack:
        nid = stack.pop()
        for consumer in consumers.get(nid, ()):
            if consumer not in seen:
                seen.add(consumer)
                stack.append(consumer)
    return seen


def _replace_links_to(graph, node_id, slot_values):
    """Заменяет во всём графе ссылки [node_id, slot] на литерал slot_values[slot]."""
    node_id = str(node_id)
    for node in graph.values():
        inputs = node.get("inputs", {})
        for key, value in list(inputs.items()):
            if is_link(value) and str(value[0]) == node_id and value[1] in slot_values:
                inputs[key] = slot_values[value[1]]


def _literal_int(node, key, what):
    value = node.get("inputs", {}).get(key)
    if is_link(value):
        raise RewriteError(f"{what}: значение должно быть виджетом, а не входом-ссылкой")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise RewriteError(f"{what}: не удалось прочитать число (получено {value!r})")


def strip_markers(graph, in_place=False):
    """Убирает ноды-пульты мастера (История/Раскадровка/Видеоряд, Длинное
    видео, Offload, Конвейер).

    Они ничего не вычисляют и не должны попадать ни в шаблоны сегментов/кадров,
    ни на воркеров — только на канвас пользователя. У Истории два выхода
    (slot 0 — GPURAID_PROJECT, коннектится только к другим маркерам, которые
    в этом же проходе и вырезаются — литерализовать нечего; slot 1 — STRING
    текста сюжета: он литерализуется у потребителей).
    """
    g = graph if in_place else copy.deepcopy(graph)
    removed = set()
    for ct in GPURAID_MARKER_CLASSES:
        for nid in find_by_class(g, ct):
            if ct == NODE_STORY:
                story = g[nid].get("inputs", {}).get("story", "")
                if is_link(story):
                    story = ""
                _replace_links_to(g, nid, {1: str(story or "")})
            g.pop(nid)
            removed.add(str(nid))
    if removed:
        # провода от вырезанных маркеров (например, GPURAID_RUNTIME от Воркеров
        # к лоадерам — привязка «модель → рантайм») данных не несут; оставить
        # ссылку — значит уронить partition()/валидацию на отсутствующей ноде
        for node in g.values():
            inputs = node.get("inputs", {})
            for key in [k for k, v in inputs.items()
                        if is_link(v) and str(v[0]) in removed]:
                inputs.pop(key)
    return g


def markers_in(graph):
    """Список class_type присутствующих в графе нод-пультов (для предупреждений)."""
    return [ct for ct in GPURAID_MARKER_CLASSES if find_by_class(graph, ct)]


# ---------------------------------------------------------------------------
# Stripe
# ---------------------------------------------------------------------------

def validate_stripe(graph):
    """Проверяет граф для страйпинга, возвращает spec.

    spec: {distributor, collector, source: (id, slot), base_seed, total_variants,
           min_vram_gb, job_type}
    """
    dists = find_by_class(graph, NODE_DISTRIBUTOR)
    colls = find_by_class(graph, NODE_COLLECTOR)
    if len(dists) != 1 or len(colls) != 1:
        raise RewriteError(
            "Нужно ровно по одной ноде GPU RAID Distributor и Collector "
            f"(найдено: {len(dists)} / {len(colls)})"
        )
    d_id, c_id = dists[0], colls[0]
    images = graph[c_id].get("inputs", {}).get("images")
    if not is_link(images):
        raise RewriteError("Вход images ноды Collector должен быть подключён к изображениям (после VAEDecode)")
    source = (str(images[0]), images[1])

    branch = ancestors(graph, source[0])
    if d_id not in branch:
        raise RewriteError(
            "Distributor должен участвовать в ветке генерации: подключите его выход seed к KSampler.seed"
        )

    d_node = graph[d_id]
    base_seed = _literal_int(d_node, "seed", "Distributor.seed")
    total = _literal_int(d_node, "total_variants", "Distributor.total_variants")
    if total < 1:
        raise RewriteError("total_variants должен быть >= 1")
    try:
        min_vram = float(d_node.get("inputs", {}).get("min_vram_gb", 0) or 0)
    except (TypeError, ValueError):
        min_vram = 0.0

    return {
        "distributor": d_id,
        "collector": c_id,
        "source": source,
        "base_seed": base_seed,
        "total_variants": total,
        "min_vram_gb": min_vram,
        "job_type": classify_job_type(graph),
    }


def build_unit_template(graph, spec):
    """Строит юнит-шаблон (замыкание предков источника + SaveImage) и список файловых входов.

    Возвращает (template, uploads), uploads = [(node_id, input_key, value)].
    В шаблоне сид/индекс/префикс заменены плейсхолдерами; нод GPURAID в нём нет.
    """
    d_id = spec["distributor"]
    src_id, src_slot = spec["source"]
    keep = ancestors(graph, src_id)
    template = {nid: copy.deepcopy(graph[nid]) for nid in keep}
    _replace_links_to(template, d_id, {0: SEED_PH, 1: INDEX_PH})
    template.pop(d_id, None)

    for nid, node in template.items():
        if node.get("class_type") in GPURAID_CLASSES:
            raise RewriteError(
                f"Нода {node.get('class_type')} (id {nid}) не может находиться внутри распределяемой ветки"
            )

    template[SAVE_NODE_ID] = {
        "class_type": "SaveImage",
        "inputs": {"images": [src_id, src_slot], "filename_prefix": PREFIX_PH},
    }
    return template, collect_upload_refs(template)


def render_unit(template, seed, index, prefix):
    """Подставляет литералы в копию шаблона."""
    unit = copy.deepcopy(template)
    for node in unit.values():
        inputs = node.get("inputs", {})
        for key, value in list(inputs.items()):
            if value == SEED_PH:
                inputs[key] = int(seed)
            elif value == INDEX_PH:
                inputs[key] = int(index)
            elif value == PREFIX_PH:
                inputs[key] = prefix
    return unit


def build_tail(graph, spec, job_id):
    """Хвост: Collector (+все потомки) с дозакрытием зависимостей; Distributor литерализован."""
    c_id = spec["collector"]
    d_id = spec["distributor"]
    keep = {c_id} | descendants(graph, c_id)

    changed = True
    while changed:
        changed = False
        for nid in list(keep):
            if nid == c_id:
                continue  # входы Collector заменяются на job_id — его старая ссылка images не тянет ветку
            for _, src, _ in iter_links(graph[nid]):
                if src not in keep and src in graph:
                    keep.add(src)
                    changed = True

    tail = {nid: copy.deepcopy(graph[nid]) for nid in keep}
    collector = tail[c_id]
    collector["inputs"] = {"job_id": job_id}

    if d_id in tail:
        _replace_links_to(tail, d_id, {0: spec["base_seed"], 1: 0})
        tail.pop(d_id, None)
    return tail


# ---------------------------------------------------------------------------
# Offload
# ---------------------------------------------------------------------------

def splice_gpuraid(graph):
    """Убирает GPURAID-ноды из графа для целикового выполнения на стоковом воркере.

    Возвращает (graph_copy, warnings: list[str]).
    """
    g = copy.deepcopy(graph)
    warnings = []

    for c_id in find_by_class(g, NODE_COLLECTOR):
        images = g[c_id].get("inputs", {}).get("images")
        if is_link(images):
            # потребители выхода Collector переключаются напрямую на источник images
            _replace_links_to(g, c_id, {0: images})
        else:
            warnings.append("Collector без входа images удалён — потребители отключены")
            _replace_links_to(g, c_id, {0: None})
        g.pop(c_id)

    for d_id in find_by_class(g, NODE_DISTRIBUTOR):
        seed = g[d_id].get("inputs", {}).get("seed", 0)
        if is_link(seed):
            seed = 0
        _replace_links_to(g, d_id, {0: int(seed or 0), 1: 0})
        g.pop(d_id)
        warnings.append("Distributor заменён литеральным сидом")

    for t_id in find_by_class(g, NODE_TILED_UPSCALE):
        image = g[t_id].get("inputs", {}).get("image")
        if is_link(image):
            _replace_links_to(g, t_id, {0: image})
            warnings.append("GPU RAID Tiled Upscale пропущен на воркере (изображение передано без апскейла)")
        g.pop(t_id)

    present = markers_in(g)
    if present:
        strip_markers(g, in_place=True)
        warnings.append("Ноды-пульты мастера вырезаны: " + ", ".join(present))

    # подчистка повисших ссылок (потребители удалённых нод с value None)
    for nid, node in g.items():
        for key, value in list(node.get("inputs", {}).items()):
            if value is None:
                raise RewriteError(
                    f"После удаления GPURAID-нод у ноды {nid} повис вход {key} — "
                    "уберите Collector без images из workflow"
                )
    return g, warnings


# ---------------------------------------------------------------------------
# Общие утилиты
# ---------------------------------------------------------------------------

def classify_job_type(graph):
    for node in graph.values():
        ct = str(node.get("class_type", "")).lower()
        if any(h in ct for h in VIDEO_HINTS):
            return "video"
    return "image"


def collect_upload_refs(graph):
    """[(node_id, input_key, value)] для входов-файлов из UPLOAD_TABLE."""
    refs = []
    for nid, node in graph.items():
        keys = UPLOAD_TABLE.get(node.get("class_type"))
        if not keys:
            continue
        for key in keys:
            value = node.get("inputs", {}).get(key)
            if isinstance(value, str) and value:
                refs.append((nid, key, value))
    return refs


def strip_annotation(name):
    """'img.png [input]' -> ('img.png', 'input'); без аннотации -> (name, 'input')."""
    if isinstance(name, str) and name.endswith("]") and " [" in name:
        base, _, ann = name.rpartition(" [")
        return base, ann[:-1]
    return name, "input"


def rewrite_upload_refs(graph, mapping):
    """Заменяет значения файловых входов по mapping {(node_id, key): new_value}."""
    for (nid, key), new_value in mapping.items():
        node = graph.get(str(nid))
        if node is not None and key in node.get("inputs", {}):
            node["inputs"][key] = new_value


def apply_remap(graph, model_remap):
    """Переименование моделей per-worker: {folder: {master_name: worker_name}}.

    Замена только в пределах того же класса лоадера (кросс-лоадерная замена невыразима).
    """
    if not model_remap:
        return graph
    for node in graph.values():
        table = LOADER_TABLE.get(node.get("class_type"))
        if not table:
            continue
        inputs = node.get("inputs", {})
        for key, folder in table.items():
            value = inputs.get(key)
            if isinstance(value, str):
                new = model_remap.get(folder, {}).get(value)
                if new:
                    inputs[key] = new
    return graph


def extract_requirements(graph):
    """{classes: set, models: {folder: set(names)}, files: [(nid, key, value)]}"""
    classes = set()
    models = {}
    for node in graph.values():
        ct = node.get("class_type")
        if not ct:
            continue
        classes.add(ct)
        table = LOADER_TABLE.get(ct)
        if table:
            for key, folder in table.items():
                value = node.get("inputs", {}).get(key)
                if isinstance(value, str) and value:
                    models.setdefault(folder, set()).add(value)
    return {
        "classes": classes,
        "models": models,
        "files": collect_upload_refs(graph),
    }


# ---------------------------------------------------------------------------
# Long Video
# ---------------------------------------------------------------------------

def _find_titled(graph, title):
    title = title.upper()
    hits = [
        nid
        for nid, node in graph.items()
        if str(node.get("_meta", {}).get("title", "")).strip().upper() == title
    ]
    if len(hits) > 1:
        raise RewriteError(f"Несколько нод с заголовком {title} — оставьте одну")
    return hits[0] if hits else None


def prepare_segment_template(graph):
    """Валидация шаблона сегмента Long Video. Возвращает spec.

    Маркировка нод по заголовкам (Title в контекстном меню ноды):
      GPURAID:START_IMAGE — LoadImage стартового кадра (обязательно)
      GPURAID:END_IMAGE   — LoadImage конечного кадра (для режима keyframes)
      GPURAID:PROMPT      — нода с текстовым виджетом (опционально)
      GPURAID:VIDEO_OUT   — нода сохранения видео (иначе автоопределение)
    """
    # ноды-пульты (Сценарист, Длинное видео, Offload, Конвейер) живут на той же
    # канве, что и шаблон, — просто вырезаем их
    g = strip_markers(graph)
    for ct in (NODE_DISTRIBUTOR, NODE_COLLECTOR):
        if find_by_class(g, ct):
            raise RewriteError(
                "Уберите ноды Distributor/Collector из шаблона сегмента: "
                "страйпинг и сборка длинного видео — разные режимы"
            )

    start_id = _find_titled(g, LV_START)
    if start_id is None:
        loaders = find_by_class(g, "LoadImage")
        if len(loaders) == 1:
            start_id = loaders[0]
        else:
            raise RewriteError(
                'Пометьте LoadImage стартового кадра заголовком "GPURAID:START_IMAGE" '
                "(правый клик по ноде → Title)"
            )
    if g[start_id].get("class_type") not in UPLOAD_TABLE:
        raise RewriteError("GPURAID:START_IMAGE должен быть нодой LoadImage")

    end_id = _find_titled(g, LV_END)
    if end_id is not None and g[end_id].get("class_type") not in UPLOAD_TABLE:
        raise RewriteError("GPURAID:END_IMAGE должен быть нодой LoadImage")
    if end_id == start_id:
        raise RewriteError("START_IMAGE и END_IMAGE — разные ноды LoadImage")

    prompt_id = _find_titled(g, LV_PROMPT)
    prompt_key = None
    if prompt_id is not None:
        inputs = g[prompt_id].get("inputs", {})
        for key in TEXT_KEYS:
            if key in inputs and isinstance(inputs[key], str):
                prompt_key = key
                break
        if prompt_key is None:
            for key, value in inputs.items():
                if isinstance(value, str) and not is_link(value):
                    prompt_key = key
                    break
        if prompt_key is None:
            raise RewriteError("У ноды GPURAID:PROMPT нет текстового виджета")

    steps_id = _find_titled(g, LV_STEPS)
    steps_key = None
    if steps_id is not None:
        inputs = g[steps_id].get("inputs", {})
        for key in STEPS_KEYS:
            if key in inputs and not is_link(inputs[key]):
                steps_key = key
                break
        if steps_key is None:
            raise RewriteError("У ноды GPURAID:STEPS нет числового виджета steps")

    out_id = _find_titled(g, LV_OUT)
    if out_id is None:
        outs = [nid for nid in g if g[nid].get("class_type") in VIDEO_OUT_CLASSES]
        if len(outs) == 1:
            out_id = outs[0]
        else:
            raise RewriteError(
                'Пометьте ноду сохранения видео заголовком "GPURAID:VIDEO_OUT" '
                f"(найдено кандидатов: {len(outs)})"
            )

    return {
        "template": g,
        "start": start_id,
        "start_key": UPLOAD_TABLE[g[start_id]["class_type"]][0],
        "end": end_id,
        "end_key": UPLOAD_TABLE[g[end_id]["class_type"]][0] if end_id else None,
        "prompt": prompt_id,
        "prompt_key": prompt_key,
        "steps": steps_id,
        "steps_key": steps_key,
        "out": out_id,
        "job_type": "video",
    }


def _apply_seed(g, seed):
    """Перебивает все НЕсвязные seed/noise_seed виджеты графа одним значением.

    Осознанно blanket: шаблоны сегментов/кадров одно-семплерные; workflow с
    несколькими намеренно разными сидами Сценаристу не подходит.
    """
    if seed is None:
        return
    for node in g.values():
        inputs = node.get("inputs", {})
        for key in SEED_KEYS:
            if key in inputs and not is_link(inputs[key]):
                try:
                    int(inputs[key])
                except (TypeError, ValueError):
                    continue
                inputs[key] = int(seed)


VIDEO_SPEC_KEYS = ("duration_s", "fps", "aspect", "short_edge", "snap")


def apply_videospec_overrides(g, overrides):
    """Правит виджеты нод GPURAID_VideoSpec (длительность/аспект/fps и т.п.).

    Только literal-виджеты; чужие ноды не трогаются — никаких blanket-перезаписей
    ширины/высоты по графу.
    """
    if not overrides:
        return
    for nid in find_by_class(g, NODE_VIDEO_SPEC):
        inputs = g[nid].setdefault("inputs", {})
        for key in VIDEO_SPEC_KEYS:
            if key in overrides and overrides[key] is not None \
                    and not is_link(inputs.get(key)):
                inputs[key] = overrides[key]


def render_segment(spec, start_image, end_image, prompt, seed, prefix, overrides=None):
    """Собирает граф одного сегмента из шаблона spec."""
    g = copy.deepcopy(spec["template"])
    g[spec["start"]]["inputs"][spec["start_key"]] = start_image
    if spec["end"] is not None and end_image:
        g[spec["end"]]["inputs"][spec["end_key"]] = end_image
    if spec["prompt"] is not None and prompt is not None:
        g[spec["prompt"]]["inputs"][spec["prompt_key"]] = prompt

    _apply_seed(g, seed)
    apply_videospec_overrides(g, overrides)
    if spec.get("steps") is not None and overrides and overrides.get("steps") is not None:
        g[spec["steps"]]["inputs"][spec["steps_key"]] = int(overrides["steps"])

    out = g[spec["out"]]
    if "filename_prefix" in out.get("inputs", {}):
        out["inputs"]["filename_prefix"] = prefix
    if "save_output" in out.get("inputs", {}):
        out["inputs"]["save_output"] = True
    return g


# ---------------------------------------------------------------------------
# Story: шаблон ключевого кадра (T2I) и извлечение Сценариста
# ---------------------------------------------------------------------------

def prepare_keyframe_template(graph):
    """Валидация T2I-шаблона ключевого кадра Сценариста.

    Маркеры: GPURAID:PROMPT (нода с текстовым виджетом; без маркера —
    единственный CLIPTextEncode с литеральным text), GPURAID:KEYFRAME_OUT
    (SaveImage; без маркера — единственный SaveImage). Save-нода заменяется
    синтетической с фиксированным id (как в stripe) и PREFIX_PH.
    """
    g = strip_markers(graph)
    for nid, node in g.items():
        if node.get("class_type") in GPURAID_CLASSES:
            raise RewriteError(
                f"Уберите ноду {node.get('class_type')} из шаблона ключевого кадра")

    prompt_id = _find_titled(g, LV_PROMPT)
    if prompt_id is None:
        cands = [nid for nid in find_by_class(g, "CLIPTextEncode")
                 if isinstance(g[nid].get("inputs", {}).get("text"), str)]
        if len(cands) == 1:
            prompt_id = cands[0]
        else:
            raise RewriteError(
                'Пометьте ноду промпта кадра заголовком "GPURAID:PROMPT" '
                f"(кандидатов CLIPTextEncode: {len(cands)})"
            )
    prompt_key = None
    inputs = g[prompt_id].get("inputs", {})
    for key in TEXT_KEYS:
        if key in inputs and isinstance(inputs[key], str):
            prompt_key = key
            break
    if prompt_key is None:
        for key, value in inputs.items():
            if isinstance(value, str) and not is_link(value):
                prompt_key = key
                break
    if prompt_key is None:
        raise RewriteError("У ноды GPURAID:PROMPT нет текстового виджета")

    out_id = _find_titled(g, LV_KEYFRAME_OUT)
    if out_id is None:
        outs = find_by_class(g, "SaveImage") + find_by_class(g, "PreviewImage")
        if len(outs) == 1:
            out_id = outs[0]
        else:
            raise RewriteError(
                'Пометьте Save/Preview-ноду кадра заголовком "GPURAID:KEYFRAME_OUT" '
                f"(найдено кандидатов: {len(outs)})"
            )
    images_in = g[out_id].get("inputs", {}).get("images")
    if not is_link(images_in):
        raise RewriteError("У ноды вывода кадра не подключён вход images")
    g.pop(out_id)
    g[SAVE_NODE_ID] = {
        "class_type": "SaveImage",
        "inputs": {"images": images_in, "filename_prefix": PREFIX_PH},
    }
    return {"template": g, "prompt": prompt_id, "prompt_key": prompt_key,
            "job_type": "image"}


def render_keyframe(spec, prompt, seed, width, height, prefix):
    """Граф одного ключевого кадра: промпт + сид + размер канвы + префикс.

    Размер пишется в Empty*Latent*-ноды (EmptyLatentImage, EmptySD3LatentImage…)
    — кадр обязан рендериться ровно в WxH сегмента, иначе H3 скомпонует
    first_frame (stretch) и last_frame (cover) по-разному и стык будет виден.
    """
    g = copy.deepcopy(spec["template"])
    g[spec["prompt"]]["inputs"][spec["prompt_key"]] = prompt
    _apply_seed(g, seed)
    for node in g.values():
        ct = str(node.get("class_type", "")).lower()
        if "empty" in ct and "latent" in ct:
            inputs = node.get("inputs", {})
            if "width" in inputs and not is_link(inputs["width"]):
                inputs["width"] = int(width)
            if "height" in inputs and not is_link(inputs["height"]):
                inputs["height"] = int(height)
    g[SAVE_NODE_ID]["inputs"]["filename_prefix"] = prefix
    return g
