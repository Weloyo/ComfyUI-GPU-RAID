"""Тесты привязок «модель → рантайм» (placement, чистый модуль)."""

from gpu_raid import pipeline_split as ps
from gpu_raid import placement as pl

from test_pipeline_split import h3_graph


def wf_flat():
    """Плоский workflow: лоадеры с привязками в properties."""
    return {
        "nodes": [
            {"id": 6, "type": "UNETLoader",
             "properties": {"gpuraid_runtime": "platform:colab"},
             "widgets_values": ["h3_int8.safetensors", "default"]},
            {"id": 13, "type": "CLIPLoader",
             "properties": {"gpuraid_runtime": "platform:kaggle"}},
            {"id": 11, "type": "VAELoader",
             "properties": {"gpuraid_runtime": "local"}},
            {"id": 24, "type": "VAELoader", "properties": {}},
            {"id": 92, "type": "SaveVideo",
             "properties": {"gpuraid_runtime": "platform:colab"}},  # не лоадер — мимо
            {"id": 99, "type": "LoraLoader",
             "properties": {"gpuraid_runtime": "мусор"}},           # мусор -> ""
        ],
    }


def wf_subgraph():
    """Workflow как у шаблонов ComfyUI: лоадеры внутри subgraph-инстанса 105."""
    return {
        "nodes": [
            {"id": 92, "type": "SaveVideo", "properties": {}},
            {"id": 105, "type": "4c31-uuid", "properties": {}},
        ],
        "definitions": {"subgraphs": [{
            "id": "4c31-uuid",
            "nodes": [
                {"id": 6, "type": "UNETLoader",
                 "properties": {"gpuraid_runtime": "platform:colab"}},
                {"id": 13, "type": "CLIPLoader",
                 "properties": {"gpuraid_runtime": "platform:kaggle"}},
                {"id": 11, "type": "VAELoader",
                 "properties": {"gpuraid_runtime": "local"}},
                {"id": 14, "type": "SamplerCustomAdvanced", "properties": {}},
            ],
        }]},
    }


WORKERS = [
    {"id": "local", "name": "Локальная GPU", "platform": "", "state": "online",
     "enabled": True},
    {"id": "w-col", "name": "colab-0", "platform": "colab", "state": "online",
     "enabled": True},
    {"id": "w-kag", "name": "kaggle-0", "platform": "kaggle", "state": "offline",
     "enabled": True},
]


def test_normalize_and_labels():
    assert pl.normalize_assign(" local ") == "local"
    assert pl.normalize_assign("platform:colab") == "platform:colab"
    assert pl.normalize_assign("id:abc") == "id:abc"
    assert pl.normalize_assign("ерунда") == ""
    assert pl.normalize_assign(None) == ""
    assert pl.assign_label("platform:colab") == "Colab"
    assert pl.assign_label("local") == "локальная GPU"
    assert pl.assign_label("") == "авто"


def test_iter_workflow_nodes_flat_and_subgraph():
    ids = [i for i, _ in pl.iter_workflow_nodes(wf_flat())]
    assert "6" in ids and "92" in ids
    ids = dict(pl.iter_workflow_nodes(wf_subgraph()))
    # внутренние ноды получают id "<инстанс>:<внутренний>" — как в API-prompt'е
    assert "105:6" in ids and "105:13" in ids and "92" in ids
    assert "6" not in ids  # голого внутреннего id наружу нет


def test_iter_workflow_nodes_recursion_guard():
    wf = {
        "nodes": [{"id": 1, "type": "sg-a", "properties": {}}],
        "definitions": {"subgraphs": [
            {"id": "sg-a", "nodes": [{"id": 2, "type": "sg-b"}]},
            {"id": "sg-b", "nodes": [{"id": 3, "type": "sg-a"}]},  # цикл
        ]},
    }
    ids = [i for i, _ in pl.iter_workflow_nodes(wf)]
    assert "1" in ids and "1:2" in ids and "1:2:3" in ids
    assert len(ids) == len(set(ids))  # обход конечен и без дублей


def test_extract_assignments():
    a = pl.extract_assignments(wf_flat())
    assert a == {"6": "platform:colab", "13": "platform:kaggle", "11": "local"}
    a = pl.extract_assignments(wf_subgraph())
    assert a == {"105:6": "platform:colab", "105:13": "platform:kaggle",
                 "105:11": "local"}


def test_resolve_assignment():
    wid, why = pl.resolve_assignment("local", WORKERS)
    assert wid == "local" and not why
    wid, why = pl.resolve_assignment("platform:colab", WORKERS)
    assert wid == "w-col"
    wid, why = pl.resolve_assignment("platform:kaggle", WORKERS)
    assert wid is None and "не в сети" in why
    wid, why = pl.resolve_assignment("platform:runpod", WORKERS)
    assert wid is None and "нет воркера" in why
    wid, why = pl.resolve_assignment("id:w-col", WORKERS)
    assert wid == "w-col"
    wid, why = pl.resolve_assignment("id:w-kag", WORKERS)
    assert wid is None and "не в сети" in why
    wid, why = pl.resolve_assignment("id:gone", WORKERS)
    assert wid is None and "не найден" in why
    wid, why = pl.resolve_assignment("", WORKERS)
    assert wid is None


def test_resolve_platform_prefers_deterministic_online():
    workers = WORKERS + [{"id": "w-col2", "name": "colab-1", "platform": "colab",
                          "state": "online", "enabled": True}]
    wid, _ = pl.resolve_assignment("platform:colab", workers)
    assert wid == "w-col"  # сортировка по имени: colab-0 раньше colab-1


def test_place_islands_h3_three_gpu_plan():
    """План из вики: диффузия → Colab, энкодер → Kaggle, декод → локальная."""
    graph = h3_graph()
    part = ps.partition(graph)
    assignments = {"6": "platform:colab", "13": "platform:kaggle", "11": "local",
                   "24": "local"}
    weights = {"6": 20 * 2**30, "13": 15 * 2**30, "11": 5 * 2**30, "24": 1 * 2**30}
    placement, decided, warnings = pl.place_islands(graph, part, assignments, weights)
    iof = part["island_of"]
    assert placement[iof["14"]] == "platform:colab"      # семплинг: решает UNET
    assert placement[iof["104"]] == "platform:kaggle"    # энкод: CLIP тяжелее VAE
    assert placement[iof["10"]] == "local"               # видео-декод: только VAE
    assert placement[iof["23"]] == "local"               # аудио-декод
    assert iof["91"] not in placement                    # CreateVideo: лоадеров нет
    # конфликт в энкод-острове (CLIP kaggle против VAE local) виден предупреждением
    assert any("kaggle" in w.lower() or "Kaggle" in w for w in warnings)
    assert decided[iof["104"]] == "13"


def test_place_islands_no_assignments():
    graph = h3_graph()
    part = ps.partition(graph)
    placement, decided, warnings = pl.place_islands(graph, part, {}, {})
    assert placement == {} and decided == {} and warnings == []


def test_auto_place_respects_preplaced():
    graph = h3_graph()
    part = ps.partition(graph)
    iof = part["island_of"]
    workers = [{"id": "local", "vram_gb": 12}, {"id": "w-col", "vram_gb": 16},
               {"id": "w-kag", "vram_gb": 16}]
    pre = {iof["14"]: "w-col", iof["104"]: "w-kag"}
    placement = ps.auto_place(graph, part, workers, {}, preplaced=pre)
    assert placement[iof["14"]] == "w-col"
    assert placement[iof["104"]] == "w-kag"
    # выходной остров по-прежнему прижат к локальной
    assert placement[iof["91"]] == "local"
    assert set(placement) == {isl["id"] for isl in part["islands"]}
