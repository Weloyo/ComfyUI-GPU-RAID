"""E2E-смок на одной машине: два CPU-инстанса ComfyUI, реальный stripe на 4 юнита.

Поднимает мастера (:8189, без токена) и воркера (:8190, токен + строгий auth),
регистрирует воркера, гонит страйп-граф на EmptyImage (без моделей), проверяет
COMPLETE 4/4, исполнение хвоста и 401 без токена. Ваш основной ComfyUI (:8188)
не затрагивается.

Запуск:
  D:\\ComfyUI_windows_portable\\python_embeded\\python.exe tests\\e2e_local.py [COMFY_DIR]
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

COMFY = sys.argv[1] if len(sys.argv) > 1 else r"D:\ComfyUI_windows_portable"
PY = os.path.join(COMFY, "python_embeded", "python.exe")
MAIN = os.path.join(COMFY, "ComfyUI", "main.py")
TOKEN = "e2etoken"
A = "http://127.0.0.1:8189"
B = "http://127.0.0.1:8190"


def req(method, url, body=None, headers=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw else {})


def wait_ready(base, headers=None, timeout=420):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            status, _ = req("GET", base + "/system_stats", headers=headers, timeout=5)
            if status == 200:
                print(f"  {base} готов за {int(time.time() - t0)}с")
                return
        except Exception:
            time.sleep(3)
    raise SystemExit(f"{base} не поднялся за {timeout}с")


def spawn(port, env_extra):
    env = dict(os.environ)
    env.pop("GPURAID_TOKEN", None)
    env.pop("GPURAID_AUTH_STRICT", None)
    env.update(env_extra)
    log = open(os.path.join(os.path.dirname(__file__), f"e2e_{port}.log"), "wb")
    return subprocess.Popen(
        [PY, "-s", MAIN, "--windows-standalone-build", "--port", str(port),
         "--listen", "127.0.0.1", "--cpu"],
        cwd=COMFY, env=env, stdout=log, stderr=subprocess.STDOUT,
    )


STRIPE_GRAPH = {
    "10": {"class_type": "GPURAID_Distributor",
           "inputs": {"seed": 100, "total_variants": 4, "min_vram_gb": 0}},
    "1": {"class_type": "EmptyImage",
          "inputs": {"width": 64, "height": 64, "batch_size": 1, "color": ["10", 0]}},
    "11": {"class_type": "GPURAID_Collector", "inputs": {"images": ["1", 0], "job_id": ""}},
    "9": {"class_type": "SaveImage",
          "inputs": {"images": ["11", 0], "filename_prefix": "gpuraid_e2e"}},
}


def main():
    procs = []
    wid = None
    try:
        print("[1] запускаю мастера :8189 и воркера :8190 (CPU)…")
        procs.append(spawn(8189, {}))
        procs.append(spawn(8190, {"GPURAID_TOKEN": TOKEN, "GPURAID_AUTH_STRICT": "1"}))
        wait_ready(A)
        wait_ready(B, headers={"X-GPURAID-Token": TOKEN})

        print("[2] auth: запрос к воркеру без токена должен получить 401…")
        try:
            req("GET", B + "/system_stats")
            raise SystemExit("ОШИБКА: воркер пустил без токена")
        except urllib.error.HTTPError as e:
            assert e.code == 401, f"ожидали 401, получили {e.code}"
        print("  401 ok")

        print("[3] расширение на мастере…")
        _, info = req("GET", A + "/gpuraid/info")
        assert info.get("version"), info
        print(f"  GPU RAID v{info['version']}")

        print("[4] регистрирую воркера…")
        _, r = req("POST", A + "/gpuraid/workers",
                   {"connection_strings": f"gpuraid://{TOKEN}@127.0.0.1:8190?tls=0&name=e2e-w"})
        assert r.get("added"), r
        wid = r["added"][0]["id"]
        for _ in range(30):
            _, w = req("GET", A + "/gpuraid/workers")
            rec = next(x for x in w["workers"] if x["id"] == wid)
            if rec["status"].get("state") == "online":
                break
            time.sleep(2)
        else:
            raise SystemExit("воркер не стал online")
        print("  online")

        print("[5] stripe 4 юнита…")
        _, r = req("POST", A + "/gpuraid/stripe",
                   {"graph": STRIPE_GRAPH, "workflow_ui": {"nodes": []}, "client_id": "e2e"})
        job_id = r["job_id"]
        print(f"  job {job_id}, воркеров: {len(r['workers'])}")
        assert len(r["workers"]) == 2, r

        snap = None
        for _ in range(120):
            _, snap = req("GET", f"{A}/gpuraid/jobs/{job_id}")
            if snap["state"] in ("COMPLETE", "PARTIAL", "FAILED", "CANCELLED", "TAIL_QUEUED"):
                break
            time.sleep(2)
        print(f"  итог: {snap['state']}, done {snap['done']}/{snap['total']}, "
              f"per_worker {snap['per_worker']}, errors {snap['errors']}")
        assert snap["done"] == 4, snap
        assert snap["state"] in ("COMPLETE", "TAIL_QUEUED"), snap

        print("[6] хвост (Collector+SaveImage на мастере)…")
        deadline = time.time() + 60
        tail_files = []
        while time.time() < deadline and not tail_files:
            time.sleep(3)
            _, hist = req("GET", A + "/history")
            for entry in hist.values():
                outputs = entry.get("outputs", {})
                for node_out in outputs.values():
                    for item in node_out.get("images", []) or []:
                        if str(item.get("filename", "")).startswith("gpuraid_e2e"):
                            tail_files.append(item["filename"])
        assert tail_files, "хвост не дал файлов gpuraid_e2e*"
        print(f"  файлы хвоста: {sorted(set(tail_files))[:5]} (всего {len(tail_files)})")
        assert len(set(tail_files)) >= 4, "в батче хвоста меньше 4 изображений"

        print("\nE2E OK — страйпинг, auth, сборка и хвост работают.")
        return 0
    finally:
        if wid:
            try:
                req("DELETE", f"{A}/gpuraid/workers/{wid}")
            except Exception:
                pass
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
