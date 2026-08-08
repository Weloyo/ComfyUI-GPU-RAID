"""Тесты бандл-контейнера (нужны torch+safetensors, ComfyUI не нужен)."""

import os
import tempfile

import torch

from gpu_raid import bundle


class NestedTensor:
    """Локальный двойник comfy.nested_tensor.NestedTensor (duck-type по имени)."""

    def __init__(self, tensors):
        self.tensors = list(tensors)

    def unbind(self):
        return self.tensors


def _tmp(name):
    return os.path.join(tempfile.mkdtemp(prefix="gpuraid_bundle_"), name)


def test_latent_roundtrip_fp16():
    lat = {"samples": torch.randn(1, 4, 8, 8, dtype=torch.float16)}
    path = _tmp("l.safetensors")
    stats = bundle.save_bundle(path, lat, "LATENT")
    assert stats["tensors"] == 1 and stats["bytes"] > 0
    out, dtype = bundle.load_bundle(path)
    assert dtype == "LATENT"
    assert torch.equal(out["samples"], lat["samples"])
    assert out["samples"].dtype == torch.float16


def test_conditioning_roundtrip_with_nested_dicts():
    cond = [[
        torch.randn(1, 7, 16),
        {
            "pooled_output": torch.randn(1, 12, dtype=torch.bfloat16),
            "minimax_keyframes": [
                {"resolved_frame_index": 0, "latent": torch.randn(1, 3, 2, 2)},
                {"resolved_frame_index": 123, "latent": torch.randn(1, 3, 2, 2)},
            ],
            "minimax_frame_count": 124,
            "start_percent": 0.0,
        },
    ]]
    path = _tmp("c.safetensors")
    bundle.save_bundle(path, cond, "CONDITIONING")
    out, dtype = bundle.load_bundle(path)
    assert dtype == "CONDITIONING"
    assert torch.equal(out[0][0], cond[0][0])
    meta = out[0][1]
    assert meta["pooled_output"].dtype == torch.bfloat16
    assert torch.equal(meta["pooled_output"], cond[0][1]["pooled_output"])
    assert meta["minimax_frame_count"] == 124
    assert meta["minimax_keyframes"][1]["resolved_frame_index"] == 123
    assert torch.equal(meta["minimax_keyframes"][1]["latent"],
                       cond[0][1]["minimax_keyframes"][1]["latent"])


def test_nested_tensor_roundtrip():
    video = torch.randn(1, 24, 2, 4, 4, dtype=torch.float16)
    audio = torch.randn(1, 32, 2, 40, dtype=torch.float16)
    lat = {"samples": NestedTensor((video, audio))}
    path = _tmp("nt.safetensors")
    stats = bundle.save_bundle(path, lat, "LATENT")
    assert stats["tensors"] == 2
    out, _ = bundle.load_bundle(path, make_nested=NestedTensor)
    nt = out["samples"]
    assert isinstance(nt, NestedTensor)
    parts = nt.unbind()
    assert torch.equal(parts[0], video) and torch.equal(parts[1], audio)


def test_primitives_and_none():
    payload = {"a": 1, "b": 2.5, "c": "текст", "d": True, "e": None, "f": [1, "x"]}
    path = _tmp("p.safetensors")
    bundle.save_bundle(path, payload, "MISC")
    out, _ = bundle.load_bundle(path)
    assert out == payload


def test_unserializable_raises_with_path():
    class Alien:
        pass

    try:
        bundle.encode({"deep": [{"x": Alien()}]})
    except ValueError as e:
        assert "Alien" in str(e) and "$.deep[0].x" in str(e), e
    else:
        raise AssertionError("ожидали ValueError")


def test_not_a_bundle_rejected():
    import safetensors.torch
    path = _tmp("alien.safetensors")
    safetensors.torch.save_file({"t": torch.zeros(1)}, path)
    try:
        bundle.load_bundle(path)
    except ValueError:
        pass
    else:
        raise AssertionError("чужой safetensors должен отклоняться")
