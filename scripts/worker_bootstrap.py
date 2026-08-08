"""Bootstrap воркера GPU RAID для Colab/Kaggle/аренды (вызывается из ноутбуков).

Только stdlib: clone/install/запуск ComfyUI, модели (Kaggle Datasets + HF),
auth self-test с fallback-прокси, cloudflared/pinggy-туннели, печать
connection strings. Все функции идемпотентны — ячейку можно перезапускать.
"""

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request

CUSTOM_NODE_REPOS = [
    "https://github.com/city96/ComfyUI-GGUF",
    "https://github.com/kijai/ComfyUI-KJNodes",
    "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite",
]

CLOUDFLARED_URL = (
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
)

HF_PRESETS = {
    # preset -> [(repo_id, filename_in_repo, model_folder)]
    "sdxl": [("stabilityai/stable-diffusion-xl-base-1.0", "sd_xl_base_1.0.safetensors", "checkpoints")],
    # MiniMax H3 (release 2026-08-03, Comfy-Org repackaged), суммарно ~40 ГБ.
    # Репо под MiniMax H3 Community License — может требовать accept на странице HF
    # и HF_TOKEN. Качается в HF-кэш (эфемерный диск) + симлинк: лимит 20 ГБ
    # /kaggle/working не задевается. На Kaggle запускать в ОДНОинстансном режиме.
    "minimax_h3": [
        ("Comfy-Org/MiniMax-H3", "diffusion_models/minimax_h3_fl2va_pruned_int8_convrot.safetensors", "diffusion_models"),
        ("Comfy-Org/MiniMax-H3", "text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors", "text_encoders"),
        ("Comfy-Org/MiniMax-H3", "vae/minimax_h3_video_vae_fp16.safetensors", "vae"),
        ("Comfy-Org/MiniMax-H3", "vae/minimax_h3_audio_vae_fp32.safetensors", "vae"),
    ],
    "none": [],
}


def sh(cmd, check=True, **kw):
    print("+", cmd if isinstance(cmd, str) else " ".join(cmd), flush=True)
    return subprocess.run(cmd, shell=isinstance(cmd, str), check=check, **kw)


def gen_token(token=""):
    return token.strip() or secrets.token_urlsafe(18)


def platform_detect():
    if os.path.isdir("/kaggle"):
        return "kaggle"
    if "COLAB_RELEASE_TAG" in os.environ or os.path.isdir("/content"):
        return "colab"
    return "generic"


def platform_shutdown(platform):
    """Погасить рантайм платформенно. Для Kaggle-batch достаточно выйти из скрипта."""
    if platform == "colab":
        try:
            from google.colab import runtime
            print("[shutdown] runtime.unassign() — рантайм Colab освобождается")
            runtime.unassign()
        except Exception as e:
            print(f"[shutdown] runtime.unassign не сработал: {e}")


# ---------------------------------------------------------------------------
# gist-rendezvous: воркер сам сообщает мастеру свой адрес
# ---------------------------------------------------------------------------

def publish_rendezvous(gh_token, gist_id, session, name, platform, string, state="up"):
    """PATCH одного файла w_<session>.json в приватном gist (stdlib urllib).

    Файл-на-сессию: конкурирующие воркеры (Colab+Kaggle) не затирают друг
    друга. Возвращает True/False, никогда не бросает.
    """
    if not gh_token or not gist_id:
        return False
    payload = {
        "v": 1, "name": name, "platform": platform, "session": session,
        "string": string, "ts": int(time.time()), "state": state,
    }
    body = json.dumps({"files": {f"w_{session}.json": {
        "content": json.dumps(payload, ensure_ascii=False)}}}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}", data=body, method="PATCH",
        headers={
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "comfyui-gpu-raid-worker",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            ok = 200 <= r.status < 300
    except Exception as e:
        print(f"[rendezvous] публикация не удалась: {e}")
        return False
    if ok:
        print(f"[rendezvous] {name}: {state} опубликован в gist")
    return ok


# ---------------------------------------------------------------------------
# установка
# ---------------------------------------------------------------------------

def install_comfy(comfy_dir):
    if not os.path.isdir(os.path.join(comfy_dir, "comfy")):
        sh(["git", "clone", "--depth", "1",
            "https://github.com/comfyanonymous/ComfyUI", comfy_dir])
    sh([sys.executable, "-m", "pip", "install", "-q", "-r",
        os.path.join(comfy_dir, "requirements.txt")])
    return comfy_dir


def install_custom_nodes(comfy_dir, gpuraid_src, extra_repos=None):
    cn = os.path.join(comfy_dir, "custom_nodes")
    os.makedirs(cn, exist_ok=True)
    dst = os.path.join(cn, "comfyui-gpu-raid")
    if not os.path.isdir(dst):
        try:
            os.symlink(os.path.abspath(gpuraid_src), dst)
        except OSError:
            shutil.copytree(gpuraid_src, dst)
    for repo in (extra_repos if extra_repos is not None else CUSTOM_NODE_REPOS):
        name = repo.rstrip("/").split("/")[-1]
        target = os.path.join(cn, name)
        if not os.path.isdir(target):
            sh(["git", "clone", "--depth", "1", repo, target], check=False)
        req = os.path.join(target, "requirements.txt")
        if os.path.isfile(req):
            sh([sys.executable, "-m", "pip", "install", "-q", "-r", req], check=False)


# ---------------------------------------------------------------------------
# модели
# ---------------------------------------------------------------------------

def link_kaggle_datasets(comfy_dir, input_root="/kaggle/input"):
    """Симлинкует модели из датасетов в models/.

    Поддерживаются два вида раскладки датасета:
      1) manifest.json: {"files": [{"name": ..., "model_folder": ...}]}
      2) подпапки с именами папок моделей: checkpoints/, vae/, loras/, ...
    """
    known = ("checkpoints", "vae", "loras", "unet", "diffusion_models", "text_encoders",
             "clip", "clip_vision", "controlnet", "upscale_models", "embeddings")
    linked = 0
    if not os.path.isdir(input_root):
        return 0
    for ds in sorted(os.listdir(input_root)):
        root = os.path.join(input_root, ds)
        manifest = os.path.join(root, "manifest.json")
        if os.path.isfile(manifest):
            try:
                data = json.load(open(manifest, encoding="utf-8"))
            except Exception as e:
                print(f"! manifest {ds}: {e}")
                continue
            for item in data.get("files", []):
                src = os.path.join(root, item.get("path", item["name"]))
                if not os.path.isfile(src):
                    src = _find_file(root, item["name"])
                if src:
                    linked += _link(src, comfy_dir, item["model_folder"], item["name"])
        for sub in known:
            subdir = os.path.join(root, sub)
            if os.path.isdir(subdir):
                for dirpath, _, files in os.walk(subdir):
                    for f in files:
                        linked += _link(os.path.join(dirpath, f), comfy_dir, sub, f)
    print(f"[models] прилинковано из датасетов: {linked}")
    return linked


def _find_file(root, name):
    for dirpath, _, files in os.walk(root):
        if name in files:
            return os.path.join(dirpath, name)
    return None


def _link(src, comfy_dir, folder, name):
    dst_dir = os.path.join(comfy_dir, "models", folder)
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, name)
    if os.path.exists(dst):
        return 0
    try:
        os.symlink(src, dst)
        return 1
    except OSError as e:
        print(f"! link {name}: {e}")
        return 0


def hf_download(preset_or_list, comfy_dir, hf_token=None, drive_cache_dir=None):
    """Скачивает модели пресета в comfy_dir/models/<folder>/.

    HF-кэш (huggingface_hub) всегда остаётся на локальном диске — заворачивать
    его в Google Drive нельзя: Drive FUSE-mount ненадёжен для вложенных
    symlink-цепочек snapshot->blob, которые использует кэш huggingface_hub
    (проверено на живой Colab-сессии: 3 из 4 файлов по 5-20 ГБ тихо "терялись"
    — os.path.lexists() был True, os.path.exists() False). Поэтому:

    - итоговый файл в comfy_dir/models/... — всегда РЕАЛЬНАЯ копия с
      локального HF-кэша (не symlink на Drive);
    - drive_cache_dir (если передан) используется только как плоское
      персистентное хранилище готовых файлов между сессиями: если файл там
      уже есть — копируем его оттуда вместо скачивания; иначе после скачивания
      сохраняем туда копию для следующих сессий.
    """
    items = HF_PRESETS.get(preset_or_list, preset_or_list if isinstance(preset_or_list, list) else [])
    if not items:
        return
    sh([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub[hf_transfer]"], check=False)
    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
    from huggingface_hub import hf_hub_download

    for repo_id, filename, folder in items:
        name = os.path.basename(filename)
        dst_dir = os.path.join(comfy_dir, "models", folder)
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir, name)
        if os.path.exists(dst):
            print(f"[hf] уже есть: {filename}")
            continue

        drive_path = os.path.join(drive_cache_dir, folder, name) if drive_cache_dir else None
        if drive_path and os.path.exists(drive_path) and os.path.getsize(drive_path) > 0:
            print(f"[hf] копирую из Drive-кэша: {filename}")
            shutil.copy(drive_path, dst)
            continue

        print(f"[hf] скачиваю {repo_id}/{filename} -> {folder}")
        path = hf_hub_download(repo_id=repo_id, filename=filename, token=hf_token or None)
        try:
            os.symlink(path, dst)
        except OSError:
            shutil.copy(path, dst)

        if drive_path:
            os.makedirs(os.path.dirname(drive_path), exist_ok=True)
            print(f"[hf] сохраняю в Drive-кэш: {filename}")
            shutil.copy(os.path.realpath(dst), drive_path)


def model_inventory(comfy_dir):
    base = os.path.join(comfy_dir, "models")
    for folder in sorted(os.listdir(base) if os.path.isdir(base) else []):
        d = os.path.join(base, folder)
        if os.path.isdir(d):
            files = [f for f in os.listdir(d) if not f.startswith(".")]
            if files:
                print(f"  {folder}: {', '.join(sorted(files)[:8])}" +
                      (f" (+{len(files) - 8})" if len(files) > 8 else ""))


# ---------------------------------------------------------------------------
# запуск ComfyUI
# ---------------------------------------------------------------------------

def launch_comfy(comfy_dir, port, cuda_device, token, extra_args=(), log_path=None,
                 extra_env=None):
    env = dict(os.environ)
    env["GPURAID_TOKEN"] = token
    if cuda_device is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(cuda_device)
    env.update(extra_env or {})
    cmd = [sys.executable, os.path.join(comfy_dir, "main.py"),
           "--listen", "127.0.0.1", "--port", str(port), *extra_args]
    log = open(log_path or f"/tmp/comfy_{port}.log", "ab")
    proc = subprocess.Popen(cmd, cwd=comfy_dir, env=env, stdout=log, stderr=subprocess.STDOUT)
    print(f"[comfy] pid {proc.pid} порт {port} gpu {cuda_device}")
    return proc


def wait_ready(port, timeout=420):
    t0 = time.time()
    url = f"http://127.0.0.1:{port}/system_stats"
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                if r.status == 200:
                    print(f"[comfy] порт {port} готов за {int(time.time() - t0)}с")
                    return True
        except Exception:
            time.sleep(3)
    raise RuntimeError(f"ComfyUI на порту {port} не поднялся за {timeout}с — смотрите лог")


def auth_selftest(port, token):
    """Запрос с 'внешним' заголовком без токена должен получить 401."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/system_stats",
        headers={"X-Forwarded-For": "203.0.113.7"},
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return False  # пропустило без токена — плохо
    except urllib.error.HTTPError as e:
        if e.code != 401:
            return False
    except Exception:
        return False
    req2 = urllib.request.Request(
        f"http://127.0.0.1:{port}/system_stats",
        headers={"X-Forwarded-For": "203.0.113.7", "X-GPURAID-Token": token},
    )
    try:
        with urllib.request.urlopen(req2, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def start_authproxy(gpuraid_src, listen_port, target_port, token):
    proxy = os.path.join(gpuraid_src, "proxy", "authproxy.py")
    env = dict(os.environ, GPURAID_TOKEN=token)
    proc = subprocess.Popen(
        [sys.executable, proxy, "--port", str(listen_port),
         "--target", f"http://127.0.0.1:{target_port}"],
        env=env, stdout=open(f"/tmp/authproxy_{listen_port}.log", "ab"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(2)
    print(f"[authproxy] {listen_port} -> {target_port}")
    return proc


# ---------------------------------------------------------------------------
# туннели
# ---------------------------------------------------------------------------

def download_cloudflared(dest="/tmp/cloudflared"):
    if not os.path.isfile(dest):
        print("[tunnel] качаю cloudflared…")
        urllib.request.urlretrieve(CLOUDFLARED_URL, dest)
        os.chmod(dest, 0o755)
    return dest


def start_cloudflared(port, attempts=3):
    exe = download_cloudflared()
    for attempt in range(attempts):
        log_path = f"/tmp/cloudflared_{port}_{attempt}.log"
        log = open(log_path, "wb")
        proc = subprocess.Popen(
            [exe, "tunnel", "--url", f"http://127.0.0.1:{port}",
             "--protocol", "http2", "--no-autoupdate"],
            stdout=log, stderr=subprocess.STDOUT,
        )
        url = None
        for _ in range(60):
            time.sleep(2)
            try:
                text = open(log_path, "r", errors="replace").read()
            except OSError:
                continue
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", text)
            if m:
                url = m.group(0)
                break
            if proc.poll() is not None:
                break
        if url:
            print(f"[tunnel] {url} -> :{port}")
            return proc, url
        proc.kill()
        print(f"[tunnel] попытка {attempt + 1} не удалась, пробую ещё…")
    raise RuntimeError(
        "cloudflared не выдал URL. Fallback: запустите pinggy в отдельной ячейке:\n"
        f"  !ssh -o StrictHostKeyChecking=no -p 80 -R0:localhost:{port} a.pinggy.io\n"
        "и добавьте напечатанный https-URL воркеру вручную (Edit URL в панели)."
    )


# ---------------------------------------------------------------------------
# сборка всего
# ---------------------------------------------------------------------------

def connection_string(token, url, name):
    host = url.replace("https://", "").replace("http://", "").rstrip("/")
    return f"gpuraid://{token}@{host}?name={name}"


def bring_up(gpuraid_src, comfy_dir, token, gpus=(0,), base_port=8188,
             extra_args=(), use_datasets=True, hf_preset="none", hf_token=None,
             name_prefix="worker", drive_cache_dir=None,
             gist_id="", gh_token="", max_session_min=0):
    """Полный цикл: установка -> модели -> запуск N инстансов -> auth -> туннели
    -> публикация в gist (если задан).

    Возвращает info для watchdog(): {"strings", "procs", "urls", "instances",
    "platform", "shutdown_file", ...}. Старые ключи сохранены для keepalive().
    """
    if hf_preset == "minimax_h3":
        # ~35-40 ГБ весов при 32 ГБ RAM без свопа (Kaggle/Colab): держать их в RAM
        # нельзя — сессию убивает OOM (Kaggle "status code 42"). Стримим с NVMe.
        need = ("--fast-disk", "--disable-pinned-memory", "--cache-none")
        extra_args = tuple(extra_args) + tuple(f for f in need if f not in extra_args)
        print(f"[minimax_h3] RAM-guard: extra_args = {extra_args}")
    install_comfy(comfy_dir)
    install_custom_nodes(comfy_dir, gpuraid_src)
    if use_datasets:
        link_kaggle_datasets(comfy_dir)
    if hf_preset and hf_preset != "none":
        hf_download(hf_preset, comfy_dir, hf_token, drive_cache_dir=drive_cache_dir)
    print("[models] инвентарь:")
    model_inventory(comfy_dir)
    if shutil.which("ffmpeg") is None:
        print("! ffmpeg не найден — VHS_VideoCombine может не работать")

    platform = platform_detect()
    session_base = secrets.token_hex(4)
    shutdown_file = f"/tmp/gpuraid_shutdown_{session_base}"

    procs, urls, strings, instances = [], [], [], []
    for i, gpu in enumerate(gpus):
        port = base_port + i
        session = f"{session_base}{i}" if len(gpus) > 1 else session_base
        proc = launch_comfy(comfy_dir, port, gpu, token, extra_args, extra_env={
            "GPURAID_SHUTDOWN_FILE": shutdown_file,
            "GPURAID_PLATFORM": platform,
            "GPURAID_SESSION": session,
        })
        procs.append(proc)
        instances.append({
            "index": i, "gpu": gpu, "port": port, "session": session,
            "name": f"{name_prefix}-{i}", "comfy_proc": proc,
            "tunnel_proc": None, "tunnel_port": port, "url": "", "string": "",
            "restarted": False,
        })
    for inst in instances:
        port = inst["port"]
        wait_ready(port)
        tunnel_port = port
        if auth_selftest(port, token):
            print(f"[auth] middleware активен на :{port}")
        else:
            print(f"[auth] middleware НЕ активен на :{port} — поднимаю authproxy")
            tunnel_port = port + 10000
            procs.append(start_authproxy(gpuraid_src, tunnel_port, port, token))
        proc, url = start_cloudflared(tunnel_port)
        procs.append(proc)
        inst.update(tunnel_proc=proc, tunnel_port=tunnel_port, url=url,
                    string=connection_string(token, url, inst["name"]))
        urls.append(url)
        strings.append(inst["string"])
        publish_rendezvous(gh_token, gist_id, inst["session"], inst["name"],
                           platform, inst["string"])

    print("\n" + "=" * 72)
    if gist_id and gh_token:
        print("АДРЕСА ОПУБЛИКОВАНЫ В GIST — мастер подхватит воркеров сам.")
        print("Строки ниже — запасной вариант для ручного добавления:")
    else:
        print("СКОПИРУЙТЕ СТРОКИ В ПАНЕЛЬ GPU RAID (Добавить воркеров):")
    for s in strings:
        print("   " + s)
    print("=" * 72)
    return {
        "strings": strings, "procs": procs, "urls": urls,
        "instances": instances, "platform": platform, "token": token,
        "gist_id": gist_id, "gh_token": gh_token,
        "shutdown_file": shutdown_file, "max_session_min": float(max_session_min or 0),
        "comfy_dir": comfy_dir, "gpuraid_src": gpuraid_src,
        "extra_args": tuple(extra_args), "t0": time.time(),
    }


def keepalive(procs, log_paths=(), interval=60):
    """Старый цикл (совместимость): держит сессию и печатает хвосты логов."""
    try:
        while True:
            dead = [p.pid for p in procs if p.poll() is not None]
            if dead:
                print(f"[keepalive] умерли процессы: {dead} — смотрите логи в /tmp")
            for lp in log_paths:
                try:
                    with open(lp, "r", errors="replace") as f:
                        f.seek(max(0, os.path.getsize(lp) - 2000))
                        tail = f.read().strip().splitlines()[-3:]
                        for line in tail:
                            print(f"[{os.path.basename(lp)}] {line}")
                except OSError:
                    pass
            time.sleep(interval)
    except KeyboardInterrupt:
        print("stop")


def _kill(proc):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception:
        pass


def watchdog(info, interval=15, republish_s=240):
    """Умный keepalive: следит за процессами, туннелями, sentinel'ом и бюджетом.

    Выходит (и платформенно гасит рантайм), когда:
      * мастер прислал POST /gpuraid/worker/shutdown (расширение на воркере
        пишет sentinel-файл GPURAID_SHUTDOWN_FILE);
      * превышен max_session_min (самостраховка на случай смерти мастера).
    Умерший cloudflared перезапускается, новый URL перепубликуется в gist;
    умерший ComfyUI перезапускается один раз.
    """
    reason = ""
    last_publish = time.time()
    try:
        while True:
            if os.path.isfile(info["shutdown_file"]):
                reason = "команда мастера"
                break
            budget = info.get("max_session_min") or 0
            age_min = (time.time() - info["t0"]) / 60.0
            if budget and age_min >= budget:
                reason = f"самостраховка: {int(age_min)} мин ≥ {int(budget)} мин"
                break

            for inst in info["instances"]:
                # ComfyUI умер — одна попытка поднять заново
                if inst["comfy_proc"].poll() is not None:
                    if inst.get("restarted"):
                        print(f"[watchdog] comfy :{inst['port']} умер повторно — "
                              "смотрите /tmp/comfy_*.log")
                    else:
                        print(f"[watchdog] comfy :{inst['port']} умер — перезапускаю")
                        inst["restarted"] = True
                        inst["comfy_proc"] = launch_comfy(
                            info["comfy_dir"], inst["port"], inst["gpu"], info["token"],
                            info["extra_args"], extra_env={
                                "GPURAID_SHUTDOWN_FILE": info["shutdown_file"],
                                "GPURAID_PLATFORM": info["platform"],
                                "GPURAID_SESSION": inst["session"],
                            })
                        try:
                            wait_ready(inst["port"])
                        except RuntimeError as e:
                            print(f"[watchdog] {e}")
                # туннель умер — перезапуск + перепубликация нового URL
                if inst["tunnel_proc"] is not None and inst["tunnel_proc"].poll() is not None:
                    print(f"[watchdog] туннель :{inst['tunnel_port']} умер — перезапускаю")
                    try:
                        proc, url = start_cloudflared(inst["tunnel_port"])
                    except RuntimeError as e:
                        print(f"[watchdog] {e}")
                        continue
                    inst.update(tunnel_proc=proc, url=url,
                                string=connection_string(info["token"], url, inst["name"]))
                    publish_rendezvous(info["gh_token"], info["gist_id"], inst["session"],
                                       inst["name"], info["platform"], inst["string"])
                    last_publish = time.time()

            # периодическая перепубликация = heartbeat для rendezvous (TTL 10 мин)
            if time.time() - last_publish >= republish_s:
                for inst in info["instances"]:
                    publish_rendezvous(info["gh_token"], info["gist_id"], inst["session"],
                                       inst["name"], info["platform"], inst["string"])
                last_publish = time.time()
            time.sleep(interval)
    except KeyboardInterrupt:
        reason = "остановлено вручную"
    print(f"[watchdog] завершение: {reason}")
    for inst in info["instances"]:
        publish_rendezvous(info["gh_token"], info["gist_id"], inst["session"],
                           inst["name"], info["platform"], inst["string"], state="down")
    for proc in info["procs"]:
        _kill(proc)
    platform_shutdown(info["platform"])
    return reason
