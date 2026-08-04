"""Тесты graph_rewrite (чистый Python, без ComfyUI). Запуск: tests/run_all.py"""

import copy

from gpu_raid.consts import INDEX_PH, PREFIX_PH, SAVE_NODE_ID, SEED_PH
from gpu_raid.graph_rewrite import (
    RewriteError,
    ancestors,
    apply_remap,
    build_tail,
    build_unit_template,
    classify_job_type,
    collect_upload_refs,
    descendants,
    extract_requirements,
    prepare_segment_template,
    render_segment,
    render_unit,
    splice_gpuraid,
    strip_annotation,
    validate_stripe,
)


def base_graph():
    return {
        "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "sdxl.safetensors"}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024, "batch_size": 1}},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "cat", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "bad", "clip": ["4", 1]}},
        "10": {"class_type": "GPURAID_Distributor",
               "inputs": {"seed": 100, "total_variants": 8, "min_vram_gb": 8.0}},
        "3": {"class_type": "KSampler", "inputs": {
            "seed": ["10", 0], "steps": 20, "cfg": 7.0, "sampler_name": "euler",
            "scheduler": "normal", "denoise": 1.0, "model": ["4", 0],
            "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]}},
        "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
        "11": {"class_type": "GPURAID_Collector", "inputs": {"images": ["8", 0], "job_id": ""}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["11", 0], "filename_prefix": "out"}},
    }


def test_ancestors_descendants():
    g = base_graph()
    anc = ancestors(g, "8")
    assert anc == {"8", "3", "4", "5", "6", "7", "10"}, anc
    desc = descendants(g, "11")
    assert desc == {"9"}, desc


def test_validate_ok():
    spec = validate_stripe(base_graph())
    assert spec["distributor"] == "10"
    assert spec["collector"] == "11"
    assert spec["source"] == ("8", 0)
    assert spec["base_seed"] == 100
    assert spec["total_variants"] == 8
    assert abs(spec["min_vram_gb"] - 8.0) < 1e-6
    assert spec["job_type"] == "image"


def test_validate_errors():
    g = base_graph()
    del g["10"]
    g["3"]["inputs"]["seed"] = 1
    try:
        validate_stripe(g)
        assert False, "нет Distributor — должно падать"
    except RewriteError:
        pass

    g2 = base_graph()
    g2["3"]["inputs"]["seed"] = 1  # Distributor есть, но не в ветке
    try:
        validate_stripe(g2)
        assert False, "Distributor вне ветки — должно падать"
    except RewriteError:
        pass

    g3 = base_graph()
    g3["11"]["inputs"]["images"] = None
    try:
        validate_stripe(g3)
        assert False, "Collector без images — должно падать"
    except RewriteError:
        pass


def test_unit_template_and_render():
    g = base_graph()
    spec = validate_stripe(g)
    template, uploads = build_unit_template(g, spec)
    assert set(template) == {"4", "5", "6", "7", "3", "8", SAVE_NODE_ID}, set(template)
    assert template["3"]["inputs"]["seed"] == SEED_PH
    assert template[SAVE_NODE_ID]["inputs"]["images"] == ["8", 0]
    assert template[SAVE_NODE_ID]["inputs"]["filename_prefix"] == PREFIX_PH
    assert uploads == []

    unit = render_unit(template, 105, 5, "gpuraid_tmp/j1/u0005")
    assert unit["3"]["inputs"]["seed"] == 105
    assert unit[SAVE_NODE_ID]["inputs"]["filename_prefix"] == "gpuraid_tmp/j1/u0005"
    # шаблон не мутирован
    assert template["3"]["inputs"]["seed"] == SEED_PH


def test_tail_simple():
    g = base_graph()
    spec = validate_stripe(g)
    tail = build_tail(g, spec, "jobX")
    assert set(tail) == {"11", "9"}, set(tail)
    assert tail["11"]["inputs"] == {"job_id": "jobX"}
    assert tail["9"]["inputs"]["images"] == ["11", 0]


def test_tail_pulls_dependencies():
    g = base_graph()
    g["14"] = {"class_type": "UpscaleModelLoader", "inputs": {"model_name": "4x.pth"}}
    g["13"] = {"class_type": "ImageUpscaleWithModel",
               "inputs": {"upscale_model": ["14", 0], "image": ["11", 0]}}
    g["15"] = {"class_type": "SaveImage", "inputs": {"images": ["13", 0], "filename_prefix": "up"}}
    spec = validate_stripe(g)
    tail = build_tail(g, spec, "jobY")
    assert set(tail) == {"11", "9", "13", "14", "15"}, set(tail)


def test_splice_offload():
    g = base_graph()
    spliced, warnings = splice_gpuraid(g)
    assert "10" not in spliced and "11" not in spliced
    assert spliced["9"]["inputs"]["images"] == ["8", 0]
    assert spliced["3"]["inputs"]["seed"] == 100
    assert warnings


def test_remap_and_requirements():
    g = base_graph()
    req = extract_requirements(g)
    assert req["models"]["checkpoints"] == {"sdxl.safetensors"}
    assert "KSampler" in req["classes"]

    apply_remap(g, {"checkpoints": {"sdxl.safetensors": "sdxl_worker.safetensors"}})
    assert g["4"]["inputs"]["ckpt_name"] == "sdxl_worker.safetensors"


def test_uploads_and_annotations():
    g = base_graph()
    g["5"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["12", 0], "vae": ["4", 2]}}
    g["3"]["inputs"]["latent_image"] = ["5", 0]
    g["12"] = {"class_type": "LoadImage", "inputs": {"image": "ref.png [input]"}}
    refs = collect_upload_refs(g)
    assert ("12", "image", "ref.png [input]") in refs
    assert strip_annotation("ref.png [input]") == ("ref.png", "input")
    assert strip_annotation("plain.png") == ("plain.png", "input")

    spec = validate_stripe(g)
    template, uploads = build_unit_template(g, spec)
    assert ("12", "image", "ref.png [input]") in uploads


def test_classify():
    assert classify_job_type(base_graph()) == "image"
    g = base_graph()
    g["30"] = {"class_type": "VHS_VideoCombine", "inputs": {}}
    assert classify_job_type(g) == "video"


def lv_graph():
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": "start.png"},
              "_meta": {"title": "GPURAID:START_IMAGE"}},
        "2": {"class_type": "LoadImage", "inputs": {"image": "end.png"},
              "_meta": {"title": "GPURAID:END_IMAGE"}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "hello"},
              "_meta": {"title": "GPURAID:PROMPT"}},
        "3": {"class_type": "WanImageToVideo",
              "inputs": {"start": ["1", 0], "end": ["2", 0], "seed": 5, "cond": ["5", 0]}},
        "4": {"class_type": "VHS_VideoCombine",
              "inputs": {"images": ["3", 0], "frame_rate": 16,
                         "filename_prefix": "vhs", "save_output": False},
              "_meta": {"title": "GPURAID:VIDEO_OUT"}},
    }


def test_longvideo_template():
    spec = prepare_segment_template(lv_graph())
    assert spec["start"] == "1" and spec["end"] == "2"
    assert spec["prompt"] == "5" and spec["prompt_key"] == "text"
    assert spec["out"] == "4"

    seg = render_segment(spec, "s0.png", "s1.png", "новый промпт", 42, "gpuraid_tmp/j/s000")
    assert seg["1"]["inputs"]["image"] == "s0.png"
    assert seg["2"]["inputs"]["image"] == "s1.png"
    assert seg["5"]["inputs"]["text"] == "новый промпт"
    assert seg["3"]["inputs"]["seed"] == 42
    assert seg["4"]["inputs"]["filename_prefix"] == "gpuraid_tmp/j/s000"
    assert seg["4"]["inputs"]["save_output"] is True
    # шаблон не мутирован
    assert spec["template"]["4"]["inputs"]["save_output"] is False


def test_longvideo_autodetect_and_errors():
    g = lv_graph()
    del g["2"]["_meta"]
    del g["4"]["_meta"]
    g["3"]["inputs"].pop("end")
    spec = prepare_segment_template(g)  # start по титулу, out по классу
    assert spec["out"] == "4"

    g2 = lv_graph()
    g2["99"] = {"class_type": "GPURAID_Distributor", "inputs": {"seed": 1, "total_variants": 2}}
    try:
        prepare_segment_template(g2)
        assert False, "Distributor в шаблоне LV — должно падать"
    except RewriteError:
        pass
