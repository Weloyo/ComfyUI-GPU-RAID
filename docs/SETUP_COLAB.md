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

## Типичный сценарий с вашим парком

- Kaggle 2×T4 — страйпинг SDXL/Z-Image картинок;
- Colab L4 — offload видео Wan 2.2 fp8 или Flux-генерация;
- локальная 4070 Ti — всё вместе с ними через work-stealing.
