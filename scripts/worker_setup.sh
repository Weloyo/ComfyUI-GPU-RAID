#!/usr/bin/env bash
# GPU RAID: bootstrap воркера на арендованной Linux-GPU машине (RunPod, Vast.ai, свой сервер).
#
# Использование:
#   export GPURAID_TOKEN="секрет"          # обязательный
#   bash worker_setup.sh [PORT] [COMFY_DIR]
#
# RunPod: пробросьте HTTP-порт (по умолчанию 8188) в шаблоне пода — скрипт сам
# напечатает connection string вида gpuraid://TOKEN@<pod>-8188.proxy.runpod.net.
# Иначе поднимет cloudflared quick tunnel.
#
# Многокарточная аренда «90 ГБ под одну модель»: ставьте на воркера ComfyUI-MultiGPU
# (DisTorch) — внутри одной машины шардинг по NVLink/PCIe работает; для мастера
# это будет один воркер с большим VRAM.

set -euo pipefail

PORT="${1:-8188}"
COMFY_DIR="${2:-}"
REPO_URL="${GPURAID_REPO:-https://github.com/weloyo3000/MultiDiffusion}"

if [[ -z "${GPURAID_TOKEN:-}" ]]; then
    echo "ОШИБКА: задайте токен:  export GPURAID_TOKEN=\"...\"" >&2
    exit 1
fi

# --- где ComfyUI ---
if [[ -z "$COMFY_DIR" ]]; then
    for cand in /workspace/ComfyUI /ComfyUI "$HOME/ComfyUI"; do
        [[ -d "$cand/comfy" ]] && COMFY_DIR="$cand" && break
    done
    COMFY_DIR="${COMFY_DIR:-/workspace/ComfyUI}"
fi
echo "[*] ComfyUI: $COMFY_DIR (порт $PORT)"

if [[ ! -d "$COMFY_DIR/comfy" ]]; then
    git clone --depth 1 https://github.com/comfyanonymous/ComfyUI "$COMFY_DIR"
fi
python3 -m pip install -q -r "$COMFY_DIR/requirements.txt"

# --- расширение + базовые ноды ---
SRC="$(cd "$(dirname "$0")/.." && pwd)"
CN="$COMFY_DIR/custom_nodes"
mkdir -p "$CN"
[[ -e "$CN/comfyui-gpu-raid" ]] || ln -s "$SRC" "$CN/comfyui-gpu-raid" 2>/dev/null \
    || cp -r "$SRC" "$CN/comfyui-gpu-raid"
for repo in \
    https://github.com/city96/ComfyUI-GGUF \
    https://github.com/kijai/ComfyUI-KJNodes \
    https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite; do
    name="$(basename "$repo")"
    [[ -d "$CN/$name" ]] || git clone --depth 1 "$repo" "$CN/$name" || true
    [[ -f "$CN/$name/requirements.txt" ]] && python3 -m pip install -q -r "$CN/$name/requirements.txt" || true
done

# --- запуск ---
LOG="/tmp/comfy_${PORT}.log"
echo "[*] запускаю ComfyUI (лог: $LOG)"
GPURAID_TOKEN="$GPURAID_TOKEN" nohup python3 "$COMFY_DIR/main.py" \
    --listen 0.0.0.0 --port "$PORT" >>"$LOG" 2>&1 &

for i in $(seq 1 120); do
    sleep 3
    curl -sf "http://127.0.0.1:$PORT/system_stats" >/dev/null && break
    [[ $i -eq 120 ]] && { echo "ComfyUI не поднялся, смотрите $LOG"; exit 1; }
done
echo "[*] ComfyUI готов"

# --- self-test авторизации ---
code="$(curl -s -o /dev/null -w '%{http_code}' -H 'X-Forwarded-For: 203.0.113.7' \
    "http://127.0.0.1:$PORT/system_stats")"
if [[ "$code" != "401" ]]; then
    echo "[!] middleware не активен (код $code) — поднимаю authproxy на $((PORT + 10000))"
    GPURAID_TOKEN="$GPURAID_TOKEN" nohup python3 "$SRC/proxy/authproxy.py" \
        --port "$((PORT + 10000))" --target "http://127.0.0.1:$PORT" \
        >>/tmp/authproxy.log 2>&1 &
    TUNNEL_PORT=$((PORT + 10000))
else
    echo "[*] auth ok (401 без токена)"
    TUNNEL_PORT="$PORT"
fi

# --- адрес ---
echo
echo "======================================================================"
if [[ -n "${RUNPOD_POD_ID:-}" ]]; then
    echo "Строка подключения (вставьте в панель GPU RAID):"
    echo "    gpuraid://$GPURAID_TOKEN@${RUNPOD_POD_ID}-${TUNNEL_PORT}.proxy.runpod.net?name=runpod"
else
    if ! command -v cloudflared >/dev/null; then
        curl -sL -o /usr/local/bin/cloudflared \
            https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 \
            && chmod +x /usr/local/bin/cloudflared
    fi
    nohup cloudflared tunnel --url "http://127.0.0.1:$TUNNEL_PORT" \
        --protocol http2 --no-autoupdate >>/tmp/cloudflared.log 2>&1 &
    sleep 8
    URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/cloudflared.log | tail -1 || true)"
    if [[ -n "$URL" ]]; then
        echo "Строка подключения (вставьте в панель GPU RAID):"
        echo "    gpuraid://$GPURAID_TOKEN@${URL#https://}?name=rented"
    else
        echo "cloudflared не выдал URL — смотрите /tmp/cloudflared.log"
    fi
fi
echo "======================================================================"
