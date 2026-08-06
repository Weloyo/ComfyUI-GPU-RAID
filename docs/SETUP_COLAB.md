# Google Colab: воркер на L4/A100 (только платный тариф)

## ⚠️ Про правила

FAQ Google Colab на **бесплатном** тарифе прямо запрещает
«running distributed computing workers» и «remote control such as SSH shells» —
ровно то, чем является воркер. На **платных** тарифах (Pro, Pro+, Pay-As-You-Go
— при положительном балансе compute units) эти ограничения сняты.

Поэтому ноутбук `notebooks/colab_worker.ipynb` требует явно подтвердить
`I_USE_PAID_COLAB = True` и не работает без этого. Не запускайте его на
бесплатном тарифе: рискуете аккаунтом Google.

## Что даёт Colab Pro (~$10/мес, 100 CU)

- **L4 24 ГБ** (~1.9 CU/час) — лучший вариант: тянет Flux, Wan 2.2 fp8-видео,
  bf16 работает (никаких `--force-fp16`).
- A100 40 ГБ — для самых тяжёлых задач, но дорог по CU.
- Сессии до 24 ч; при нулевом балансе CU действуют ограничения бесплатного
  тарифа — следите за балансом.

## Запуск

1. Откройте `notebooks/colab_worker.ipynb` в Colab (File → Upload notebook,
   или откройте из своего GitHub).
2. Runtime → Change runtime type → **L4**.
3. В конфиге: `I_USE_PAID_COLAB = True`, при желании `MODEL_PRESET = "sdxl"`,
   `HF_TOKEN` в Secrets (ключик слева).
4. `USE_DRIVE_CACHE = True` — кэш моделей в `MyDrive/gpuraid_models/<папка>/`:
   скачанные модели переживают перезапуски (Drive медленнее локального диска,
   но быстрее повторного скачивания). Большие видео-модели лучше качать с HF
   каждый раз — на Colab это 100+ МБ/с.
5. Запустите все ячейки → в конце строка `gpuraid://…` → в панель GPU RAID.

## MiniMax H3 (видео, ~40 ГБ весов)

`MODEL_PRESET = "minimax_h3"` работает так же, как в Kaggle-ноутбуке — качает
4 файла напрямую с HF в HF-кэш. Ноутбук сам добавляет `--cache-none`. Для GPU
с большим VRAM (A100/96 ГБ и т.п.) системная RAM всё равно может быть тесной —
если тариф даёт High-RAM (Runtime → Change runtime type), включите его.

**Важно: включите `USE_DRIVE_CACHE = True`.** Без него каждый новый рантайм
(эфемерный диск) заново качает все ~40 ГБ с HF. С `USE_DRIVE_CACHE = True`
кэш `huggingface_hub` переносится в `MyDrive/gpuraid_models/hf_home` — первая
сессия качает как обычно, все следующие находят файлы на месте и не качают
заново. Нужно ~40+ ГБ свободного места в Google Drive.

## Типичный сценарий с вашим парком

- Kaggle 2×T4 — страйпинг SDXL/Z-Image картинок;
- Colab L4 — offload видео Wan 2.2 fp8 или Flux-генерация;
- локальная 4070 Ti — всё вместе с ними через work-stealing.
