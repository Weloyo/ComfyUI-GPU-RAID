"""Бандл — контейнер промежуточных тензоров для шардинга (Save/LoadBundle).

Формат: один safetensors-файл. Тензоры лежат плоско под ключами "t0..tN"
(CPU, contiguous, dtype сохраняется); структура — в metadata:
  {"gpuraid": "bundle/1", "type": "LATENT|CONDITIONING|IMAGE|AUDIO|...",
   "tree": "<json>"}
В дереве:
  {"__t__": "t3"}                    — торч-тензор
  {"__nt__": [{"__t__": ...}, ...]}  — comfy NestedTensor (H3 AV-латент)
Остальное — обычные JSON-типы. CONDITIONING (list[[Tensor, dict]]) с
тензорами внутри dict (например minimax_keyframes у H3) обходится рекурсией.

comfy.* импортируется лениво и только на decode NestedTensor — модуль
тестируем вне ComfyUI (нужен только torch+safetensors).
"""

import json
import os


def _is_nested(x):
    return type(x).__name__ == "NestedTensor" and hasattr(x, "unbind")


def _make_nested(tensors):
    import comfy.nested_tensor
    return comfy.nested_tensor.NestedTensor(tuple(tensors))


def encode(payload):
    """payload -> (tensors: {name: Tensor}, tree). ValueError на чужих объектах."""
    import torch

    tensors = {}

    def grab(t):
        name = f"t{len(tensors)}"
        tensors[name] = t.detach().to("cpu").contiguous()
        return name

    def enc(x, path):
        if isinstance(x, torch.Tensor):
            return {"__t__": grab(x)}
        if _is_nested(x):
            return {"__nt__": [{"__t__": grab(t)} for t in x.unbind()]}
        if isinstance(x, dict):
            return {str(k): enc(v, f"{path}.{k}") for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return [enc(v, f"{path}[{i}]") for i, v in enumerate(x)]
        if x is None or isinstance(x, (bool, int, float, str)):
            return x
        raise ValueError(
            f"Несериализуемый объект в бандле: {type(x).__name__} (путь {path}). "
            "Это ребро графа нельзя разрезать — исключите его из разрезов."
        )

    tree = enc(payload, "$")
    return tensors, tree


def decode(tensors, tree, make_nested=None):
    make_nested = make_nested or _make_nested

    def dec(x):
        if isinstance(x, dict):
            keys = set(x.keys())
            if keys == {"__t__"}:
                return tensors[x["__t__"]]
            if keys == {"__nt__"}:
                return make_nested([dec(item) for item in x["__nt__"]])
            return {k: dec(v) for k, v in x.items()}
        if isinstance(x, list):
            return [dec(v) for v in x]
        return x

    return dec(tree)


def save_bundle(path, payload, data_type=""):
    import safetensors.torch

    tensors, tree = encode(payload)
    meta = {"gpuraid": "bundle/1", "type": str(data_type or ""),
            "tree": json.dumps(tree, ensure_ascii=False)}
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    safetensors.torch.save_file(tensors, path, metadata=meta)
    return {"tensors": len(tensors), "bytes": os.path.getsize(path)}


def load_bundle(path, make_nested=None):
    """-> (payload, data_type)."""
    import safetensors

    with safetensors.safe_open(path, framework="pt", device="cpu") as f:
        meta = f.metadata() or {}
        if meta.get("gpuraid") != "bundle/1":
            raise ValueError(f"{os.path.basename(path)}: не GPU RAID бандл")
        tensors = {k: f.get_tensor(k) for k in f.keys()}
    tree = json.loads(meta.get("tree") or "null")
    return decode(tensors, tree, make_nested=make_nested), meta.get("type", "")
