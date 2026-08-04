"""Тесты parity-сверки."""

from gpu_raid import parity
from gpu_raid.graph_rewrite import extract_requirements


def graph():
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "modelA.safetensors"}},
        "2": {"class_type": "LoraLoader", "inputs": {"lora_name": "styleB.safetensors",
                                                     "model": ["1", 0], "clip": ["1", 1]}},
        "3": {"class_type": "UNETLoader", "inputs": {"unet_name": "wan_fp8.safetensors"}},
        "4": {"class_type": "KSampler", "inputs": {"model": ["1", 0]}},
    }


def test_green():
    req = extract_requirements(graph())
    report = parity.check(
        req,
        worker_classes={"CheckpointLoaderSimple", "LoraLoader", "UNETLoader", "KSampler"},
        worker_models={
            "checkpoints": ["modelA.safetensors"],
            "loras": ["styleB.safetensors"],
            "diffusion_models": ["wan_fp8.safetensors"],
        },
    )
    assert report["level"] == parity.GREEN, report


def test_yellow_with_suggestion():
    req = extract_requirements(graph())
    report = parity.check(
        req,
        worker_classes={"CheckpointLoaderSimple", "LoraLoader", "UNETLoader", "KSampler"},
        worker_models={
            "checkpoints": ["modelA_v2.safetensors"],
            "loras": ["styleB.safetensors"],
            "diffusion_models": ["wan_fp8.safetensors"],
        },
    )
    assert report["level"] == parity.YELLOW
    assert "modelA.safetensors" in report["missing_models"]["checkpoints"]
    assert report["suggestions"]["modelA.safetensors"] == ["modelA_v2.safetensors"]


def test_red_missing_class():
    req = extract_requirements(graph())
    report = parity.check(req, worker_classes={"KSampler"}, worker_models={})
    assert report["level"] == parity.RED
    assert "CheckpointLoaderSimple" in report["missing_classes"]


def test_remap_applied():
    req = extract_requirements(graph())
    report = parity.check(
        req,
        worker_classes={"CheckpointLoaderSimple", "LoraLoader", "UNETLoader", "KSampler"},
        worker_models={
            "checkpoints": ["worker_model.safetensors"],
            "loras": ["styleB.safetensors"],
            "diffusion_models": ["wan_fp8.safetensors"],
        },
        model_remap={"checkpoints": {"modelA.safetensors": "worker_model.safetensors"}},
    )
    assert report["level"] == parity.GREEN
    assert report["remap_applied"]["modelA.safetensors"] == "worker_model.safetensors"


def test_folder_aliases():
    req = extract_requirements(graph())
    folders = parity.folders_to_query(req)
    assert "diffusion_models" in folders and "unet" in folders
    merged = parity.merge_folder_listings(req, {
        "checkpoints": ["modelA.safetensors"],
        "loras": ["styleB.safetensors"],
        "diffusion_models": [],
        "unet": ["wan_fp8.safetensors"],   # воркер положил в unet
    })
    assert "wan_fp8.safetensors" in merged["diffusion_models"]

    report = parity.check(req, {"CheckpointLoaderSimple", "LoraLoader", "UNETLoader", "KSampler"},
                          merged)
    assert report["level"] == parity.GREEN, report


def test_subfolder_basenames():
    req = extract_requirements(graph())
    report = parity.check(
        req,
        worker_classes={"CheckpointLoaderSimple", "LoraLoader", "UNETLoader", "KSampler"},
        worker_models={
            "checkpoints": ["sdxl/modelA.safetensors"],   # у воркера в подпапке
            "loras": ["styleB.safetensors"],
            "diffusion_models": ["wan_fp8.safetensors"],
        },
    )
    assert report["level"] == parity.GREEN, report
