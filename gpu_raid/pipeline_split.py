"""Разбиение API-графа на «острова» по компонентам моделей (шардинг).

Правила:
  * source-ноды (все входы — виджеты: лоадеры моделей, RandomNoise,
    KSamplerSelect, примитивы) РЕПЛИЦИРУЮТСЯ в каждый остров-потребитель —
    так видео-VAELoader H3 оказывается и в encode-, и в decode-острове;
  * рёбра несериализуемых типов (MODEL/CLIP/VAE/GUIDER/SIGMAS/NOISE/VIDEO/
    неизвестные) склеивают ноды в один остров — их нельзя переслать по сети;
  * примитивные рёбра (INT/FLOAT/STRING/BOOLEAN) тоже склеивают: гонять int
    файлом глупо (принудительный разрез возможен через forced_cuts);
  * рёбра LATENT/CONDITIONING/IMAGE/AUDIO/MASK — кандидаты в разрезы: между
    островами превращаются в пары SaveBundle/LoadBundle.

Циклы после стягивания (возможны в DAG) сливаются по SCC с предупреждением.
Чистый модуль: без ComfyUI-импортов, тестируется в tests/test_pipeline_split.py.
"""

import copy

from .consts import LOADER_TABLE, NODE_VIDEO_SPEC
from .graph_rewrite import RewriteError, is_link

CUTTABLE = ("LATENT", "CONDITIONING", "IMAGE", "AUDIO", "MASK")
PRIMITIVE = ("INT", "FLOAT", "STRING", "BOOLEAN")

# грубые оценки размеров бандлов по типу (МБ) — для предупреждений и placement
CUT_SIZE_MB = {"LATENT": 64, "CONDITIONING": 48, "IMAGE": 1500, "AUDIO": 32, "MASK": 8}
TUNNEL_LIMIT_MB = 95   # cloudflared ~100 МБ на запрос

# минимальная таблица типов выходов ядерных классов: используется в тестах и
# как fallback; в бою pipeline.analyze() строит таблицу из NODE_CLASS_MAPPINGS
DEFAULT_TYPE_TABLE = {
    "CheckpointLoaderSimple": ("MODEL", "CLIP", "VAE"),
    "UNETLoader": ("MODEL",),
    "CLIPLoader": ("CLIP",),
    "VAELoader": ("VAE",),
    "LoraLoader": ("MODEL", "CLIP"),
    "CLIPTextEncode": ("CONDITIONING",),
    "KSampler": ("LATENT",),
    "KSamplerSelect": ("SAMPLER",),
    "SamplerCustomAdvanced": ("LATENT", "LATENT"),
    "BasicGuider": ("GUIDER",),
    "BasicScheduler": ("SIGMAS",),
    "RandomNoise": ("NOISE",),
    "VAEDecode": ("IMAGE",),
    "VAEDecodeAudio": ("AUDIO",),
    "VAEEncode": ("LATENT",),
    "EmptyLatentImage": ("LATENT",),
    "EmptyImage": ("IMAGE",),
    "LoadImage": ("IMAGE", "MASK"),
    "ImageInvert": ("IMAGE",),
    "ImageScale": ("IMAGE",),
    "ImageBatch": ("IMAGE",),
    "CreateVideo": ("VIDEO",),
    "SaveVideo": (),
    "SaveImage": (),
    "PrimitiveFloat": ("FLOAT",),
    "PrimitiveInt": ("INT",),
    "PrimitiveString": ("STRING",),
    "MiniMaxH3ImageToVideo": ("CONDITIONING", "LATENT"),
    "ComfyMathExpression": ("INT", "FLOAT"),
    NODE_VIDEO_SPEC: ("INT", "INT", "INT", "INT", "FLOAT"),
}

OUTPUT_HINT_CLASSES = ("SaveImage", "SaveVideo", "VHS_VideoCombine", "SaveWEBM",
                       "SaveAnimatedWEBP", "SaveAnimatedPNG", "SaveAudio", "SaveLatent")


def _is_source(node):
    return all(not is_link(v) for v in (node.get("inputs") or {}).values())


def _edges(graph, table, warnings):
    edges = []
    for dst, node in graph.items():
        for key, val in (node.get("inputs") or {}).items():
            if not is_link(val):
                continue
            src, slot = str(val[0]), int(val[1])
            if src not in graph:
                raise RewriteError(f"Нода {dst}: вход {key} ссылается на отсутствующую {src}")
            types = table.get(graph[src].get("class_type"))
            etype = types[slot] if types and slot < len(types) else None
            if etype is None:
                warnings.append(
                    f"{graph[src].get('class_type')} (нода {src}): тип выхода {slot} "
                    "неизвестен — ребро считаю неразрезаемым")
            edges.append({"src": src, "slot": slot, "dst": dst, "key": key,
                          "type": etype})
    return edges


def partition(graph, type_table=None, forced_cuts=None):
    """-> {"islands", "island_of", "cuts", "sources", "warnings"}.

    islands: [{"id", "nodes": [nid...], "replicated": [nid...]}] в топопорядке.
    cuts: [{"src","slot","dst","key","type","src_island","dst_island"}].
    forced_cuts: [(dst_nid, input_key)] — разрезать примитивное ребро.
    """
    warnings = []
    table = dict(DEFAULT_TYPE_TABLE)
    table.update(type_table or {})
    forced = {(str(d), str(k)) for d, k in (forced_cuts or [])}

    sources = {nid for nid, node in graph.items() if _is_source(node)}
    regular = [nid for nid in graph if nid not in sources]
    if not regular:
        raise RewriteError("В графе нет вычислимых нод для шардинга")

    parent = {nid: nid for nid in regular}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    edges = _edges(graph, table, warnings)
    for e in edges:
        if e["src"] in sources:
            continue
        cuttable = e["type"] in CUTTABLE
        primitive = e["type"] in PRIMITIVE
        if (e["dst"], e["key"]) in forced:
            if cuttable or primitive:
                continue  # принудительный разрез
            warnings.append(f"forced cut {e['dst']}.{e['key']}: тип {e['type']} "
                            "несериализуем — игнорирую")
        if not cuttable:
            union(e["src"], e["dst"])

    # SCC-слияние: если стянутый граф островов зациклился — склеиваем цикл
    for _ in range(len(regular)):
        cyc = _find_island_cycle(graph, edges, sources, find, forced)
        if not cyc:
            break
        warnings.append("разрезы образуют цикл между островами — сливаю их в один")
        first = cyc[0]
        for other in cyc[1:]:
            union(first, other)

    roots = {}
    for nid in regular:
        roots.setdefault(find(nid), []).append(nid)

    # топопорядок островов по разрезам
    island_ids = _topo_islands(graph, edges, sources, find, roots, forced)
    island_of = {}
    islands = []
    for idx, root in enumerate(island_ids):
        nodes = sorted(roots[root])
        for nid in nodes:
            island_of[nid] = idx
        islands.append({"id": idx, "nodes": nodes, "replicated": []})

    # реплики source-нод: в каждый остров, где есть потребители
    for e in edges:
        if e["src"] in sources and e["dst"] not in sources:
            isl = islands[island_of[e["dst"]]]
            if e["src"] not in isl["replicated"]:
                isl["replicated"].append(e["src"])
    for isl in islands:
        isl["replicated"].sort()

    cuts = []
    for e in edges:
        if e["src"] in sources or e["dst"] in sources:
            continue
        si, di = island_of[e["src"]], island_of[e["dst"]]
        if si != di:
            cuts.append({**e, "src_island": si, "dst_island": di})

    return {"islands": islands, "island_of": island_of, "cuts": cuts,
            "sources": sorted(sources), "warnings": warnings}


def _island_edges(graph, edges, sources, find, forced):
    """Рёбра между корнями островов (только разрезаемые)."""
    out = set()
    for e in edges:
        if e["src"] in sources or e["dst"] in sources:
            continue
        ra, rb = find(e["src"]), find(e["dst"])
        if ra != rb:
            out.add((ra, rb))
    return out


def _find_island_cycle(graph, edges, sources, find, forced):
    adj = {}
    for a, b in _island_edges(graph, edges, sources, find, forced):
        adj.setdefault(a, set()).add(b)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}
    stack_path = []

    def dfs(u):
        color[u] = GRAY
        stack_path.append(u)
        for v in adj.get(u, ()):
            c = color.get(v, WHITE)
            if c == GRAY:
                return stack_path[stack_path.index(v):]
            if c == WHITE:
                cyc = dfs(v)
                if cyc:
                    return cyc
        color[u] = BLACK
        stack_path.pop()
        return None

    for u in list(adj):
        if color.get(u, WHITE) == WHITE:
            cyc = dfs(u)
            if cyc:
                return cyc
    return None


def _topo_islands(graph, edges, sources, find, roots, forced):
    pairs = _island_edges(graph, edges, sources, find, forced)
    indeg = {r: 0 for r in roots}
    adj = {r: set() for r in roots}
    for a, b in pairs:
        if b not in adj[a]:
            adj[a].add(b)
            indeg[b] += 1
    ready = sorted([r for r, d in indeg.items() if d == 0])
    order = []
    while ready:
        r = ready.pop(0)
        order.append(r)
        for v in sorted(adj[r]):
            indeg[v] -= 1
            if indeg[v] == 0:
                ready.append(v)
        ready.sort()
    if len(order) != len(roots):
        raise RewriteError("Не удалось топологически упорядочить острова (цикл?)")
    return order


# ---------------------------------------------------------------------------
# оценки и авторазмещение
# ---------------------------------------------------------------------------

def island_models(graph, island):
    """{folder: set(names)} по лоадерам острова (вместе с репликами)."""
    models = {}
    for nid in list(island["nodes"]) + list(island["replicated"]):
        node = graph.get(nid) or {}
        tab = LOADER_TABLE.get(node.get("class_type"))
        if not tab:
            continue
        for key, folder in tab.items():
            value = (node.get("inputs") or {}).get(key)
            if isinstance(value, str) and value:
                models.setdefault(folder, set()).add(value)
    return models


def estimate_island_vram_gb(graph, island, model_sizes):
    """model_sizes: {(folder, name): bytes}. Веса ×1.25 на активации."""
    total = 0
    for folder, names in island_models(graph, island).items():
        for name in names:
            total += model_sizes.get((folder, name), 0)
    return round(total * 1.25 / (1024 ** 3), 1)


def estimate_cut_mb(cut):
    return CUT_SIZE_MB.get(cut.get("type"), 64)


def output_island_ids(graph, part):
    out = set()
    for nid, node in graph.items():
        if node.get("class_type") in OUTPUT_HINT_CLASSES and nid in part["island_of"]:
            out.add(part["island_of"][nid])
    return out


def auto_place(graph, part, workers, model_sizes=None, pin_output_to="local"):
    """workers: [{"id", "vram_gb"}] -> {island_id: worker_id}.

    Жадно: остров с выходными нодами — на pin_output_to (IMAGE после VAEDecode
    через туннель — гигабайты); остальные по убыванию VRAM-оценки на самый
    свободный подходящий GPU, с тяготением к воркеру соседних островов.
    """
    model_sizes = model_sizes or {}
    placement = {}
    budget = {w["id"]: float(w.get("vram_gb") or 0) for w in workers}
    if not budget:
        raise RewriteError("Нет воркеров для размещения")

    outputs = output_island_ids(graph, part)
    if pin_output_to in budget:
        for i in outputs:
            placement[i] = pin_output_to

    est = {isl["id"]: estimate_island_vram_gb(graph, isl, model_sizes)
           for isl in part["islands"]}
    for i, wid in placement.items():
        budget[wid] -= est[i]

    neighbors = {}
    for cut in part["cuts"]:
        mb = estimate_cut_mb(cut)
        neighbors.setdefault(cut["src_island"], {}).setdefault(cut["dst_island"], 0)
        neighbors[cut["src_island"]][cut["dst_island"]] += mb
        neighbors.setdefault(cut["dst_island"], {}).setdefault(cut["src_island"], 0)
        neighbors[cut["dst_island"]][cut["src_island"]] += mb

    for isl_id, _need in sorted(((i, e) for i, e in est.items() if i not in placement),
                                key=lambda t: -t[1]):
        need = est[isl_id]
        best, best_score = None, None
        for wid, free in budget.items():
            fits = free >= need
            affinity = sum(mb for nb, mb in neighbors.get(isl_id, {}).items()
                           if placement.get(nb) == wid)
            score = (1 if fits else 0, affinity, free)
            if best_score is None or score > best_score:
                best, best_score = wid, score
        placement[isl_id] = best
        budget[best] -= need
    return placement


# ---------------------------------------------------------------------------
# сборка графов стадий
# ---------------------------------------------------------------------------

def build_stage_graphs(graph, part, placement, job_id,
                       load_class="GPURAID_LoadBundle",
                       save_class="GPURAID_SaveBundle"):
    """-> stages: [{"index", "worker_id", "island_ids", "graph",
                    "in_bundles": [name], "out_bundles": {node_id: name},
                    "deps": [stage_index]}]

    Острова одного воркера сливаются в одну стадию, если это не создаёт цикла
    через чужие стадии. Один бандл на (src, slot) — общий для всех потребителей.
    """
    islands = part["islands"]
    for isl in islands:
        if isl["id"] not in placement:
            raise RewriteError(f"Остров {isl['id']} не размещён")

    # группировка: жадно сливаем колокализованные острова, если стадийный DAG
    # остаётся ацикличным (иначе A+C -> B -> A+C дал бы взаимную блокировку)
    group_of = {isl["id"]: isl["id"] for isl in islands}

    def acyclic(gof):
        groups = set(gof.values())
        adj = {g: set() for g in groups}
        for cut in part["cuts"]:
            a, b = gof[cut["src_island"]], gof[cut["dst_island"]]
            if a != b:
                adj[a].add(b)
        indeg = {g: 0 for g in groups}
        for a in adj:
            for b in adj[a]:
                indeg[b] += 1
        ready = [g for g in groups if indeg[g] == 0]
        seen = 0
        while ready:
            g = ready.pop()
            seen += 1
            for b in adj[g]:
                indeg[b] -= 1
                if indeg[b] == 0:
                    ready.append(b)
        return seen == len(groups)

    ids = [isl["id"] for isl in islands]
    changed = True
    while changed:
        changed = False
        for i in ids:
            for j in ids:
                if i >= j or group_of[i] == group_of[j] or placement[i] != placement[j]:
                    continue
                trial = dict(group_of)
                a, b = trial[i], trial[j]
                for k, g in trial.items():
                    if g == a:
                        trial[k] = b
                if acyclic(trial):
                    group_of = trial
                    changed = True

    groups = {}
    for isl_id, g in group_of.items():
        groups.setdefault(g, []).append(isl_id)

    # один бандл на (src, slot)
    bundle_name = {}
    for cut in part["cuts"]:
        if group_of[cut["src_island"]] == group_of[cut["dst_island"]]:
            continue
        key = (cut["src"], cut["slot"])
        if key not in bundle_name:
            bundle_name[key] = f"gpuraid_bundle/{job_id}/e{cut['src']}_{cut['slot']}.safetensors"

    stages = []
    group_order = sorted(groups, key=lambda g: min(groups[g]))
    group_stage_idx = {g: i for i, g in enumerate(group_order)}
    for g in group_order:
        isl_ids = sorted(groups[g])
        nodes = set()
        for isl_id in isl_ids:
            isl = islands[isl_id]
            nodes.update(isl["nodes"])
            nodes.update(isl["replicated"])
        sub = {nid: copy.deepcopy(graph[nid]) for nid in nodes}

        in_bundles, out_bundles, deps = [], {}, set()
        for cut in part["cuts"]:
            sg, dg = group_of[cut["src_island"]], group_of[cut["dst_island"]]
            if sg == dg:
                continue
            key = (cut["src"], cut["slot"])
            name = bundle_name[key]
            if dg == g:
                lb_id = f"gpuraid_lb_{cut['src']}_{cut['slot']}"
                if lb_id not in sub:
                    sub[lb_id] = {"class_type": load_class,
                                  "inputs": {"bundle": name}}
                sub[cut["dst"]]["inputs"][cut["key"]] = [lb_id, 0]
                if name not in in_bundles:
                    in_bundles.append(name)
                deps.add(sg)
            if sg == g:
                sb_id = f"gpuraid_sb_{cut['src']}_{cut['slot']}"
                if sb_id not in sub:
                    prefix = name[:-len(".safetensors")]
                    sub[sb_id] = {"class_type": save_class,
                                  "inputs": {"value": [cut["src"], cut["slot"]],
                                             "filename_prefix": prefix}}
                    out_bundles[sb_id] = name
        stages.append({
            "index": group_stage_idx[g],
            "worker_id": placement[isl_ids[0]],
            "island_ids": isl_ids,
            "graph": sub,
            "in_bundles": in_bundles,
            "out_bundles": out_bundles,
            "deps": sorted(group_stage_idx[d] for d in deps),
        })
    return stages
