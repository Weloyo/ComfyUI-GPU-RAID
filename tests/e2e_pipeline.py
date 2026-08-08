"""E2E конвейера (шардинг) на CPU: стадия 1 на воркере -> бандл IMAGE ->
стадия 2 на мастере; результат попиксельно равен одномашинному прогону.

Граф: EmptyImage(источник, реплицируется) -> ImageInvert -> ImageInvert ->
SaveImage. Разрез по IMAGE-ребру между инвертами.

Запуск:
  D:\\ComfyUI_windows_portable\\python_embeded\\python.exe tests\\e2e_pipeline.py [COMFY_DIR]
"""

import glob
import os
import shutil
import sys
import tempfile
import time

if len(sys.argv) > 1:
    os.environ["GPURAID_E2E_COMFY"] = sys.argv[1]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_common import (COMFY, register_worker, req, spawn, wait_ready)  # noqa: E402

TOKEN = "e2etoken"
A = "http://127.0.0.1:8189"

GRAPH = {
    "1": {"class_type": "EmptyImage",
          "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": 4080}},
    "2": {"class_type": "ImageInvert", "inputs": {"image": ["1", 0]}},
    "3": {"class_type": "ImageInvert", "inputs": {"image": ["2", 0]}},
    "4": {"class_type": "SaveImage",
          "inputs": {"images": ["3", 0], "filename_prefix": "gpuraid_pipe"}},
}

REF_GRAPH = {
    "1": GRAPH["1"],
    "2": GRAPH["2"],
    "3": GRAPH["3"],
    "4": {"class_type": "SaveImage",
          "inputs": {"images": ["3", 0], "filename_prefix": "gpuraid_piperef"}},
}


def wait_job(master, job_id, timeout=240):
    deadline = time.time() + timeout
    snap = None
    while time.time() < deadline:
        _, snap = req("GET", f"{master}/gpuraid/jobs/{job_id}")
        if snap["state"] in ("COMPLETE", "PARTIAL", "FAILED", "CANCELLED"):
            return snap
        time.sleep(2)
    raise SystemExit(f"job {job_id} не завершился: {snap}")


def pixels(path):
    from PIL import Image
    with Image.open(path) as im:
        return im.convert("RGB").tobytes()


def main():
    procs = []
    users_tmp = tempfile.mkdtemp(prefix="gpuraid_e2e_pipe_")
    out_root = os.path.join(COMFY, "ComfyUI", "output")
    try:
        print("[1] мастер :8189 + воркер :8190 (CPU)…")
        procs.append(spawn(8189, {}, os.path.join(users_tmp, "master"),
                           log_name="e2e_pipe_8189.log"))
        procs.append(spawn(8190, {"GPURAID_TOKEN": TOKEN, "GPURAID_AUTH_STRICT": "1"},
                           os.path.join(users_tmp, "worker"),
                           log_name="e2e_pipe_8190.log"))
        wait_ready(A)
        wait_ready("http://127.0.0.1:8190", headers={"X-GPURAID-Token": TOKEN})
        wid = register_worker(A, TOKEN, 8190, "e2e-w")

        print("[2] анализ графа…")
        _, r = req("POST", A + "/gpuraid/pipeline/analyze", {"graph": GRAPH})
        islands = r["islands"]
        assert len(islands) == 3, [i["classes"] for i in islands]
        cuts = r["cuts"]
        assert all(c["type"] == "IMAGE" for c in cuts), cuts
        isl_by_class = {}
        for isl in islands:
            for cls in isl["classes"]:
                isl_by_class.setdefault(cls, isl["id"])
        save_isl = isl_by_class["SaveImage"]
        first_inv = min(i["id"] for i in islands if "ImageInvert" in i["classes"])
        print(f"  острова: {[i['classes'] for i in islands]}")

        print("[3] запуск конвейера: первый инверт на воркере, остальное на мастере…")
        placement = {str(i["id"]): "local" for i in islands}
        placement[str(first_inv)] = wid
        _, r = req("POST", A + "/gpuraid/pipeline/start",
                   {"graph": GRAPH, "placement": placement, "label": "e2epipe",
                    "client_id": "e2e"})
        assert r["stages"] == 2, r
        snap = wait_job(A, r["job_id"])
        assert snap["state"] == "COMPLETE", snap
        assert set(snap["per_worker"]) == {"local", wid}, snap["per_worker"]
        print(f"  {snap['state']}, per_worker {snap['per_worker']}")

        outdir = glob.glob(os.path.join(out_root, "gpuraid", "e2epipe_*"))
        outdir.sort(key=os.path.getmtime)
        assert outdir, "нет каталога доставки"
        pipe_files = glob.glob(os.path.join(outdir[-1], "gpuraid_pipe_*.png"))
        assert pipe_files, f"нет выходного файла в {outdir[-1]}"

        print("[4] эталонный одномашинный прогон того же графа…")
        _, r = req("POST", A + "/prompt", {"prompt": REF_GRAPH, "client_id": "e2e"})
        ref_pid = r["prompt_id"]
        deadline = time.time() + 120
        ref_file = None
        while time.time() < deadline and not ref_file:
            time.sleep(2)
            _, hist = req("GET", f"{A}/history/{ref_pid}")
            entry = hist.get(ref_pid) or {}
            for node_out in (entry.get("outputs") or {}).values():
                for item in node_out.get("images", []) or []:
                    sub = item.get("subfolder") or ""
                    ref_file = os.path.join(out_root, sub, item["filename"])
        assert ref_file and os.path.isfile(ref_file), "эталонный прогон без файла"

        print("[5] сравнение пикселей…")
        assert pixels(pipe_files[0]) == pixels(ref_file), \
            "конвейер дал другой результат, чем одномашинный прогон"
        print("  идентичны")

        print("[6] бандлы вычищены из input мастера…")
        leftovers = glob.glob(os.path.join(COMFY, "ComfyUI", "input",
                                           "gpuraid_bundle", "*"))
        assert not leftovers, f"остались бандлы: {leftovers}"
        print("  ok")

        print("\nE2E PIPELINE OK — шардинг, бандлы и сборка работают.")
        return 0
    finally:
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        time.sleep(2)
        shutil.rmtree(users_tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
