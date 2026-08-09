"""Общие помощники e2e-тестов: два CPU-инстанса ComfyUI + HTTP-обвязка.

COMFY-каталог: env GPURAID_E2E_COMFY или дефолтный портабл.
"""

import json
import os
import subprocess
import time
import urllib.request

COMFY = os.environ.get("GPURAID_E2E_COMFY", r"D:\ComfyUI_windows_portable")
PY = os.path.join(COMFY, "python_embeded", "python.exe")
MAIN = os.path.join(COMFY, "ComfyUI", "main.py")


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


def spawn(port, env_extra, user_dir, log_name=None):
    """user_dir изолирует workers.json от боевого инстанса (в обе стороны)."""
    env = dict(os.environ)
    env.pop("GPURAID_TOKEN", None)
    env.pop("GPURAID_AUTH_STRICT", None)
    env.update(env_extra)
    os.makedirs(user_dir, exist_ok=True)
    log = open(os.path.join(os.path.dirname(__file__),
                            log_name or f"e2e_{port}.log"), "wb")
    return subprocess.Popen(
        # --disable-auto-launch: иначе каждый тестовый инстанс открывает
        # пользователю окно браузера, а инстанс под токеном отвечает туда
        # «unauthorized» — три прогона тестов = шесть таких окон
        [PY, "-s", MAIN, "--windows-standalone-build", "--port", str(port),
         "--listen", "127.0.0.1", "--cpu", "--disable-auto-launch",
         "--user-directory", user_dir],
        cwd=COMFY, env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def register_worker(master, token, port, name):
    _, r = req("POST", master + "/gpuraid/workers",
               {"connection_strings": f"gpuraid://{token}@127.0.0.1:{port}?tls=0&name={name}"})
    assert r.get("added"), r
    wid = r["added"][0]["id"]
    for _ in range(30):
        _, w = req("GET", master + "/gpuraid/workers")
        rec = next(x for x in w["workers"] if x["id"] == wid)
        if rec["status"].get("state") == "online":
            print("  воркер online")
            return wid
        time.sleep(2)
    raise SystemExit("воркер не стал online")


def wait_manifest_state(master, label, states, timeout=240):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        _, m = req("GET", f"{master}/gpuraid/longvideo/{label}")
        last = m.get("state")
        if last in states:
            return m
        time.sleep(2)
    raise SystemExit(f"проект {label}: state={last}, ждали {states} за {timeout}с")
