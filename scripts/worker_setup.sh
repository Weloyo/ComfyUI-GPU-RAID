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
REPO_URL="${GPURAID_REPO:-https://github.com/Weloyo/ComfyUI-GPU-RAID}"

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

# --- запуск: ComfyUI ТОЛЬКО на loopback, наружу его отдаёт токен-authproxy ---
# Голый ComfyUI на 0.0.0.0 был бы виден по {pod}-PORT.proxy.runpod.net БЕЗ
# авторизации, если middleware расширения не встал. Поэтому фронтим всегда
# (как python-бутстрап): публичный слушатель — только authproxy на $PORT.
INTERNAL_PORT=$((PORT + 10000))
LOG="/tmp/comfy_${INTERNAL_PORT}.log"
echo "[*] запускаю ComfyUI на 127.0.0.1:$INTERNAL_PORT (лог: $LOG)"
GPURAID_TOKEN="$GPURAID_TOKEN" nohup python3 "$COMFY_DIR/main.py" \
    --listen 127.0.0.1 --port "$INTERNAL_PORT" >>"$LOG" 2>&1 &

for i in $(seq 1 120); do
    sleep 3
    curl -sf "http://127.0.0.1:$INTERNAL_PORT/system_stats" >/dev/null && break
    [[ $i -eq 120 ]] && { echo "ComfyUI не поднялся, смотрите $LOG"; exit 1; }
done
echo "[*] ComfyUI готов"

# self-test: встало ли middleware расширения (диагностика — доступ наружу
# в любом случае закрывает authproxy ниже)
code="$(curl -s -o /dev/null -w '%{http_code}' -H 'X-Forwarded-For: 203.0.113.7' \
    "http://127.0.0.1:$INTERNAL_PORT/system_stats")"
if [[ "$code" == "401" ]]; then
    echo "[*] auth middleware активен на :$INTERNAL_PORT"
else
    echo "[!] auth middleware НЕ активен (код $code) — наружу закрывает authproxy"
fi

# authproxy — единственный публичный слушатель, на пробрасываемом $PORT
echo "[*] authproxy 0.0.0.0:$PORT -> 127.0.0.1:$INTERNAL_PORT"
GPURAID_TOKEN="$GPURAID_TOKEN" nohup python3 "$SRC/proxy/authproxy.py" \
    --listen 0.0.0.0 --port "$PORT" --target "http://127.0.0.1:$INTERNAL_PORT" \
    >>/tmp/authproxy.log 2>&1 &
sleep 2
TUNNEL_PORT="$PORT"

# --- адрес ---
echo
echo "======================================================================"
if [[ -n "${RUNPOD_POD_ID:-}" ]]; then
    echo "Строка подключения (вставьте в панель GPU RAID):"
    echo "    gpuraid://$GPURAID_TOKEN@${RUNPOD_POD_ID}-${TUNNEL_PORT}.proxy.runpod.net?name=runpod"
else
    CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    if ! command -v cloudflared >/dev/null; then
        tmp="$(mktemp)"
        # -f: HTTP-ошибка GitHub НЕ должна попасть в бинарник (иначе Popen ENOEXEC,
        #    и command -v потом считает его установленным). Ставим на место только
        #    при успехе, во временный файл — атомарно.
        if curl -fSL --retry 3 -o "$tmp" "$CF_URL" && [[ -s "$tmp" ]]; then
            chmod +x "$tmp"
            mv "$tmp" /usr/local/bin/cloudflared
        else
            rm -f "$tmp"
        fi
    fi
    if ! command -v cloudflared >/dev/null; then
        echo "cloudflared не установить (сеть?) — поднимите туннель к 127.0.0.1:$TUNNEL_PORT вручную"
    else
        nohup cloudflared tunnel --url "http://127.0.0.1:$TUNNEL_PORT" \
            --protocol http2 --no-autoupdate >>/tmp/cloudflared.log 2>&1 &
        URL=""
        for _ in $(seq 1 30); do          # до 60с: на нагруженной сети URL печатается позже 8с
            sleep 2
            URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/cloudflared.log | tail -1 || true)"
            [[ -n "$URL" ]] && break
        done
        if [[ -n "$URL" ]]; then
            echo "Строка подключения (вставьте в панель GPU RAID):"
            echo "    gpuraid://$GPURAID_TOKEN@${URL#https://}?name=rented"
        else
            echo "cloudflared не выдал URL — смотрите /tmp/cloudflared.log"
        fi
    fi
fi
echo "======================================================================"
