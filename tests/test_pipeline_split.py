"""Тесты разбиения графа на острова (pipeline_split, чистый модуль)."""

from gpu_raid import pipeline_split as ps
from gpu_raid.graph_rewrite import RewriteError


def h3_graph():
    """Мок реального шаблона MiniMax H3 (структура и типы — как в живом графе)."""
    return {
        "6": {"class_type": "UNETLoader",
              "inputs": {"unet_name": "h3_int8.safetensors", "weight_dtype": "default"}},
        "13": {"class_type": "CLIPLoader",
               "inputs": {"clip_name": "qwen3vl.safetensors", "type": "minimax"}},
        "11": {"class_type": "VAELoader", "inputs": {"vae_name": "video_vae.safetensors"}},
        "24": {"class_type": "VAELoader", "inputs": {"vae_name": "audio_vae.safetensors"}},
        "111": {"class_type": "PrimitiveFloat", "inputs": {"value": 5.0}},
        "107": {"class_type": "ComfyMathExpression",
                "inputs": {"a": ["111", 0], "expression": "max(5, round(a*24))"}},
        "104": {"class_type": "MiniMaxH3ImageToVideo",
                "inputs": {"clip": ["13", 0], "vae": ["11", 0], "prompt": "p",
                           "width": 1344, "height": 768, "length": ["107", 0]}},
        "15": {"class_type": "RandomNoise", "inputs": {"noise_seed": 1}},
        "16": {"class_type": "BasicGuider",
               "inputs": {"model": ["6", 0], "conditioning": ["104", 0]}},
        "17": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "res_multistep"}},
        "9": {"class_type": "BasicScheduler",
              "inputs": {"model": ["6", 0], "scheduler": "simple", "steps": 20,
                         "denoise": 1.0}},
        "14": {"class_type": "SamplerCustomAdvanced",
               "inputs": {"noise": ["15", 0], "guider": ["16", 0], "sampler": ["17", 0],
                          "sigmas": ["9", 0], "latent_image": ["104", 1]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["14", 0], "vae": ["11", 0]}},
        "23": {"class_type": "VAEDecodeAudio",
               "inputs": {"samples": ["14", 0], "vae": ["24", 0]}},
        "91": {"class_type": "CreateVideo",
               "inputs": {"images": ["10", 0], "audio": ["23", 0], "fps": 24}},
        "92": {"class_type": "SaveVideo",
               "inputs": {"video": ["91", 0], "filename_prefix": "v", "format": "auto",
                          "codec": "auto"}},
    }


def _island_of(part, nid):
    return part["island_of"][nid]


def test_h3_partition_islands():
    part = ps.partition(h3_graph())
    isl = part["island_of"]
    # энкод-остров: 104 + примитивная math-цепочка
    assert isl["104"] == isl["107"]
    # семплинг-остров: guider/scheduler/sampler склеены несериализуемыми рёбрами
    assert isl["14"] == isl["16"] == isl["9"]
    assert isl["14"] != isl["104"]
    # декодеры — отдельные острова
    assert isl["10"] not in (isl["14"], isl["104"])
    assert isl["23"] not in (isl["14"], isl["104"], isl["10"])
    # CreateVideo+SaveVideo склеены VIDEO-ребром
    assert isl["91"] == isl["92"]
    assert len(part["islands"]) == 5


def test_h3_replication_of_video_vae():
    part = ps.partition(h3_graph())
    isl = part["island_of"]
    encode_isl = part["islands"][isl["104"]]
    decode_isl = part["islands"][isl["10"]]
    # видео-VAE (нода 11) реплицируется и в encode-, и в decode-остров
    assert "11" in encode_isl["replicated"]
    assert "11" in decode_isl["replicated"]
    # UNET/noise/sampler — реплики семплинг-острова
    sampler_isl = part["islands"][isl["14"]]
    assert {"6", "15", "17"} <= set(sampler_isl["replicated"])
    # источники не образуют собственных островов
    assert "6" not in isl and "111" not in isl


def test_h3_cuts_only_serializable():
    part = ps.partition(h3_graph())
    types = {c["type"] for c in part["cuts"]}
    assert types <= {"CONDITIONING", "LATENT", "IMAGE", "AUDIO"}, types
    # 104->16 (COND), 104->14 (LATENT), 14->10, 14->23 (LATENT), 10->91, 23->91
    assert len(part["cuts"]) == 6


def test_auto_place_pins_output_local_and_big_island_to_big_gpu():
    g = h3_graph()
    part = ps.partition(g)
    sizes = {
        ("diffusion_models", "h3_int8.safetensors"): 20 * 1024 ** 3,
        ("text_encoders", "qwen3vl.safetensors"): 15 * 1024 ** 3,
        ("vae", "video_vae.safetensors"): 5 * 1024 ** 3,
        ("vae", "audio_vae.safetensors"): 1 * 1024 ** 3,
    }
    workers = [{"id": "local", "vram_gb": 12}, {"id": "colab", "vram_gb": 80}]
    placement = ps.auto_place(g, part, workers, sizes)
    isl = part["island_of"]
    assert placement[isl["92"]] == "local"            # выходной остров прибит к local
    assert placement[isl["14"]] == "colab"            # UNET 20 ГБ не лезет в 12 ГБ
    assert placement[isl["104"]] == "colab"           # CLIP 15 ГБ + VAE — тоже colab


def test_build_stage_graphs_merges_colocated_and_dedups_bundles():
    g = h3_graph()
    part = ps.partition(g)
    isl = part["island_of"]
    placement = {isl["104"]: "colab", isl["14"]: "colab",
                 isl["10"]: "local", isl["23"]: "local", isl["91"]: "local"}
    stages = ps.build_stage_graphs(g, part, placement, "jobX")
    assert len(stages) == 2, [s["island_ids"] for s in stages]
    colab = next(s for s in stages if s["worker_id"] == "colab")
    local = next(s for s in stages if s["worker_id"] == "local")
    # внутри colab-стадии 104->16/104->14 остались прямыми связями
    assert colab["graph"]["16"]["inputs"]["conditioning"] == ["104", 0]
    assert not colab["in_bundles"]
    # один бандл на (14, slot 0) — оба декодера едят один файл
    assert len(colab["out_bundles"]) == 1
    name = list(colab["out_bundles"].values())[0]
    assert name == "gpuraid_bundle/jobX/e14_0.safetensors"
    assert local["in_bundles"] == [name]
    lb_ids = [nid for nid, n in local["graph"].items()
              if n["class_type"] == "GPURAID_LoadBundle"]
    assert len(lb_ids) == 1
    assert local["graph"]["10"]["inputs"]["samples"] == [lb_ids[0], 0]
    assert local["graph"]["23"]["inputs"]["samples"] == [lb_ids[0], 0]
    assert local["deps"] == [colab["index"]]
    # реплики загрузчиков присутствуют в обеих стадиях
    assert "11" in colab["graph"] and "11" in local["graph"]
    # save_output-ноды финальной стадии не потерялись
    assert "92" in local["graph"]


def test_island_cycle_merged_with_warning():
    table = {"S": ("INT",), "A": ("IMAGE", "GLUE"), "B": ("GLUE",),
             "C": ("IMAGE",), "D": ()}
    g = {
        "s": {"class_type": "S", "inputs": {"v": 1}},
        "a": {"class_type": "A", "inputs": {"x": ["s", 0]}},
        "b": {"class_type": "B", "inputs": {"img": ["a", 0]}},
        "c": {"class_type": "C", "inputs": {"x": ["b", 0]}},
        "d": {"class_type": "D", "inputs": {"img": ["c", 0], "g": ["a", 1]}},
    }
    part = ps.partition(g, table)
    assert len(part["islands"]) == 1
    assert not part["cuts"]
    assert any("цикл" in w for w in part["warnings"])


def test_unknown_type_glues_with_warning():
    g = {
        "s": {"class_type": "EmptyImage",
              "inputs": {"width": 8, "height": 8, "batch_size": 1, "color": 0}},
        "a": {"class_type": "Неведомая", "inputs": {"image": ["s", 0]}},
        "b": {"class_type": "ImageInvert", "inputs": {"image": ["a", 0]}},
    }
    part = ps.partition(g)
    assert part["island_of"]["a"] == part["island_of"]["b"]
    assert any("неизвестен" in w for w in part["warnings"])


def test_forced_cut_on_primitive():
    table = {"S": ("INT",), "A": ("INT",), "B": ()}
    g = {
        "s": {"class_type": "S", "inputs": {"v": 1}},
        "a": {"class_type": "A", "inputs": {"x": ["s", 0]}},
        "b": {"class_type": "B", "inputs": {"n": ["a", 0]}},
    }
    part = ps.partition(g, table)
    assert part["island_of"]["a"] == part["island_of"]["b"]   # INT склеивает
    part2 = ps.partition(g, table, forced_cuts=[("b", "n")])
    assert part2["island_of"]["a"] != part2["island_of"]["b"]  # принудительный разрез


def test_no_computable_nodes():
    try:
        ps.partition({"s": {"class_type": "S", "inputs": {"v": 1}}})
    except RewriteError:
        pass
    else:
        raise AssertionError("граф из одних source-нод должен отклоняться")
