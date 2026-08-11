# Аренда GPU (RunPod, Vast.ai, свой сервер): воркер без туннеля

Арендованная машина — самый надёжный воркер: постоянный публичный адрес,
без лимитов сессий и квот, любые GPU вплоть до H100 94 ГБ или мульти-GPU.

## RunPod

1. Создайте Pod (шаблон с PyTorch/CUDA), в настройках шаблона **Expose HTTP
   Port: 8188**.
2. В веб-терминале пода:
   ```bash
   git clone https://github.com/Weloyo/ComfyUI-GPU-RAID /workspace/gpu-raid
   export GPURAID_TOKEN="придумайте-длинный-секрет"
   bash /workspace/gpu-raid/scripts/worker_setup.sh 8188
   ```
3. Скрипт сам: ставит ComfyUI (если нет), расширение + GGUF/KJNodes/VHS,
   запускает с токеном, делает self-test авторизации и печатает строку
   подключения вида
   `gpuraid://ТОКЕН@abc123-8188.proxy.runpod.net?name=runpod` —
   вставьте её в ноду «GPU RAID Воркеры». Туннель не нужен: RunPod даёт HTTPS-прокси.
4. Модели: скачивайте прямо на под — кнопкой «Скачать» в ноде-лоадере
   (ссылка-источник HF/Civitai — кнопка 🔗 там же), либо `wget` в
   `/workspace/ComfyUI/models/<папка>/`.
   Network Volume сохранит их между запусками пода.

## Vast.ai / свой Linux-сервер

То же самое; если публичного HTTPS-адреса нет, скрипт автоматически поднимет
cloudflared quick tunnel и напечатает `gpuraid://ТОКЕН@xxx.trycloudflare.com`.

## Сценарий «90 ГБ VRAM под одну модель»

Если модель не помещается ни в одну карту (например, большая видео-модель в
bf16), арендуйте **многокарточную** машину (2×A100 80GB, 4×A40 и т.п.) и
поставьте на воркера [ComfyUI-MultiGPU / DisTorch](https://github.com/pollockjj/ComfyUI-MultiGPU):

- внутри одной машины слои модели шардируются по картам через NVLink/PCIe —
  там это работает (в отличие от шардинга через интернет);
- для GPU RAID такая машина — просто **один воркер с огромным VRAM**:
  подключаете её строкой `gpuraid://…`, ставите в workflow большой
  `min_vram_gb` (или используете Offload) — и тяжёлые задания уходят туда,
  пока локальная карта занята другим.

Установка на воркере: `git clone https://github.com/pollockjj/ComfyUI-MultiGPU`
в `custom_nodes` + использование DisTorch-лоадеров в workflow, который вы
запускаете через Offload.
