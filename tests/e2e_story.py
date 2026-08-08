"""E2E Сценариста на CPU: план (эвристика) -> кадры параллельно -> сегменты
FLF2V-шаблоном -> склейка -> правка промпта + перерендер сегмента.

Без моделей: кадры = EmptyImage, сегмент = ImageBatch двух кадров -> CreateVideo
-> SaveVideo. Два инстанса: мастер :8189 (без токена) + воркер :8190 (токен).

Запуск:
  D:\\ComfyUI_windows_portable\\python_embeded\\python.exe tests\\e2e_story.py [COMFY_DIR]
"""

import os
import shutil
import sys
import tempfile
import time

if len(sys.argv) > 1:
    os.environ["GPURAID_E2E_COMFY"] = sys.argv[1]

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from e2e_common import (COMFY, register_worker, req, spawn, wait_manifest_state,
                        wait_ready)  # noqa: E402

TOKEN = "e2etoken"
A = "http://127.0.0.1:8189"
LABEL = "e2estory"

# шаблон сегмента: FLF2V-имитация — батч из start+end кадров -> видео
SEG_TEMPLATE = {
    "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"},
          "_meta": {"title": "GPURAID:START_IMAGE"}},
    "2": {"class_type": "LoadImage", "inputs": {"image": "y.png"},
          "_meta": {"title": "GPURAID:END_IMAGE"}},
    "3": {"class_type": "ImageBatch", "inputs": {"image1": ["1", 0], "image2": ["2", 0]}},
    "7": {"class_type": "PrimitiveString", "inputs": {"value": ""},
          "_meta": {"title": "GPURAID:PROMPT"}},
    "4": {"class_type": "CreateVideo", "inputs": {"images": ["3", 0], "fps": 8.0}},
    "5": {"class_type": "SaveVideo",
          "inputs": {"video": ["4", 0], "filename_prefix": "video/seg",
                     "format": "auto", "codec": "auto"},
          "_meta": {"title": "GPURAID:VIDEO_OUT"}},
}

# шаблон ключевого кадра: чистый CPU, промпт через PrimitiveString
KF_TEMPLATE = {
    "1": {"class_type": "EmptyImage",
          "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": 128}},
    "2": {"class_type": "PrimitiveString", "inputs": {"value": ""},
          "_meta": {"title": "GPURAID:PROMPT"}},
    "3": {"class_type": "SaveImage", "inputs": {"images": ["1", 0], "filename_prefix": "kf"},
          "_meta": {"title": "GPURAID:KEYFRAME_OUT"}},
}

STORY = ("Рассвет над морем, лодка у пирса. Лодка отходит от берега.\n"
         "Шторм настигает героев, волны бьют в борт.")


def main():
    procs = []
    users_tmp = tempfile.mkdtemp(prefix="gpuraid_e2e_story_")
    input_dir = os.path.join(COMFY, "ComfyUI", "input")
    out_dir = os.path.join(COMFY, "ComfyUI", "output", "gpuraid", LABEL)
    kf_dir = os.path.join(input_dir, "gpuraid_story", LABEL)
    # чистый старт: прошлые прогоны не должны влиять (уникализация имени и т.д.)
    shutil.rmtree(out_dir, ignore_errors=True)
    shutil.rmtree(kf_dir, ignore_errors=True)
    try:
        print("[1] мастер :8189 + воркер :8190 (CPU)…")
        procs.append(spawn(8189, {}, os.path.join(users_tmp, "master"),
                           log_name="e2e_story_8189.log"))
        procs.append(spawn(8190, {"GPURAID_TOKEN": TOKEN, "GPURAID_AUTH_STRICT": "1"},
                           os.path.join(users_tmp, "worker"),
                           log_name="e2e_story_8190.log"))
        wait_ready(A)
        wait_ready("http://127.0.0.1:8190", headers={"X-GPURAID-Token": TOKEN})
        register_worker(A, TOKEN, 8190, "e2e-w")

        print("[2] план (эвристика, без LLM)…")
        _, r = req("POST", A + "/gpuraid/story/plan", {
            "graph": SEG_TEMPLATE,
            "keyframe_graph": KF_TEMPLATE,
            "params": {"story": STORY, "label": LABEL, "segments_count": 2,
                       "segment_duration_s": 1.0, "aspect": "1:1", "snap": "none",
                       "fps": 8, "use_llm": False, "seed": 7},
            "client_id": "e2e",
        })
        label = r["label"]
        m = r["manifest"]
        assert label == LABEL, f"имя проекта {label} (ожидали {LABEL} — каталог не вычищен?)"
        assert m["state"] == "draft" and len(m["segments"]) == 2, m["state"]
        assert len(m["keyframes"]) == 3, "N+1 кадров"
        assert m["segments"][0]["start_image"].endswith("key_000.png")
        print(f"  план: {len(m['segments'])} сегментов, {len(m['keyframes'])} кадров")

        print("[3] правка промпта кадра до рендера…")
        _, _ = req("PATCH", f"{A}/gpuraid/story/{label}/keyframes/1",
                   {"prompt": "отредактированный кадр", "seed": 555})
        _, m = req("GET", f"{A}/gpuraid/longvideo/{label}")
        assert m["keyframes"][1]["prompt"] == "отредактированный кадр"
        assert m["keyframes"][1]["seed"] == 555
        print("  ok")

        print("[4] рендер ключевых кадров (3 шт, параллельно)…")
        _, r = req("POST", f"{A}/gpuraid/story/{label}/keyframes/render",
                   {"client_id": "e2e"})
        m = wait_manifest_state(A, label, ("kf_done", "kf_partial"), timeout=180)
        assert m["state"] == "kf_done", (m["state"], [k for k in m["keyframes"]])
        for k in m["keyframes"]:
            path = os.path.join(kf_dir, k["file"])
            assert os.path.isfile(path), f"нет файла кадра {path}"
        workers_used = {k.get("worker") for k in m["keyframes"]}
        print(f"  кадры готовы, воркеры: {sorted(w or '?' for w in workers_used)}")

        print("[5] рендер сегментов (2 шт, параллельно)…")
        _, r = req("POST", f"{A}/gpuraid/story/{label}/render", {"client_id": "e2e"})
        m = wait_manifest_state(A, label, ("done", "partial", "failed"), timeout=240)
        assert m["state"] == "done", (m["state"], [s.get("error") for s in m["segments"]])
        for s in m["segments"]:
            path = os.path.join(out_dir, s["file"])
            assert os.path.isfile(path), f"нет файла сегмента {path}"
        if m.get("final"):
            assert os.path.isfile(os.path.join(out_dir, m["final"]))
            print(f"  сегменты готовы, склейка: {m['final']}")
        else:
            print("  сегменты готовы (авто-склейка пропущена — нет ffmpeg?)")

        print("[6] правка промпта сегмента + перерендер…")
        _, _ = req("PATCH", f"{A}/gpuraid/longvideo/{label}/segments/0",
                   {"prompt": "новый промпт сегмента"})
        _, m = req("GET", f"{A}/gpuraid/longvideo/{label}")
        assert m["segments"][0]["prompt"] == "новый промпт сегмента"
        assert m["segments"][0].get("dirty") is True, "готовый сегмент должен стать dirty"
        _, r = req("POST", f"{A}/gpuraid/longvideo/{label}/rerender",
                   {"index": 0, "seed": 99, "prompt": "новый промпт сегмента"})
        job_id = r["job_id"]
        deadline = time.time() + 180
        while time.time() < deadline:
            _, snap = req("GET", f"{A}/gpuraid/jobs/{job_id}")
            if snap["state"] in ("COMPLETE", "FAILED", "PARTIAL", "CANCELLED"):
                break
            time.sleep(2)
        assert snap["state"] == "COMPLETE", snap
        _, m = req("GET", f"{A}/gpuraid/longvideo/{label}")
        seg0 = m["segments"][0]
        assert seg0["status"] == "done" and seg0["seed"] == 99, seg0
        assert "dirty" not in seg0, "dirty должен сняться после перерендера"
        print("  ok")

        print("[7] рестарт-персистентность: state на диске…")
        _, m = req("GET", f"{A}/gpuraid/longvideo/{label}")
        assert m["schema"] == 2 and m["mode"] == "story"
        assert "template_graph" not in m, "GET не должен отдавать тяжёлые поля"
        print("  ok")

        print("\nE2E STORY OK — план, кадры, сегменты, правки и перерендер работают.")
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
