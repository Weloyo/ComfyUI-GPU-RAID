"""Сверка требований графа с инвентарём воркера (модели + классы нод).

Чистая логика: сетевые вызовы делает вызывающая сторона (routes/dispatcher),
сюда передаются уже полученные /object_info и /models/*.
"""

import difflib

from .consts import FOLDER_ALIASES, GPURAID_CLASSES
from .graph_rewrite import extract_requirements

GREEN, YELLOW, RED = "green", "yellow", "red"


def check(requirements, worker_classes, worker_models, model_remap=None):
    """Отчёт готовности воркера к графу.

    requirements   — результат extract_requirements(graph)
    worker_classes — set имён классов из /object_info воркера
    worker_models  — {folder: list[names]} (уже с учётом FOLDER_ALIASES — см. gather_folders)
    model_remap    — {folder: {master: worker}} текущий ремап воркера
    """
    model_remap = model_remap or {}
    report = {
        "level": GREEN,
        "missing_classes": [],
        "missing_models": {},
        "suggestions": {},
        "remap_applied": {},
        "notes": [],
    }

    for ct in sorted(requirements["classes"]):
        if ct in GPURAID_CLASSES:
            # юнит-графы и offload их не содержат — наличие на воркере не требуется
            continue
        if worker_classes and ct not in worker_classes:
            report["missing_classes"].append(ct)

    for folder, names in requirements["models"].items():
        have = set(worker_models.get(folder, []))
        # инвентарь может отдавать пути с подпапками — сравниваем и по basename
        have_flat = have | {n.replace("\\", "/").split("/")[-1] for n in have}
        for name in sorted(names):
            mapped = model_remap.get(folder, {}).get(name)
            candidate = mapped or name
            flat = candidate.replace("\\", "/").split("/")[-1]
            if candidate in have or flat in have_flat:
                if mapped:
                    report["remap_applied"][name] = mapped
                continue
            report["missing_models"].setdefault(folder, []).append(name)
            pool = sorted(have_flat)
            close = difflib.get_close_matches(flat, pool, n=3, cutoff=0.4)
            if close:
                report["suggestions"][name] = close

    if report["missing_classes"]:
        report["level"] = RED
        report["notes"].append(
            "На воркере нет custom-нод: " + ", ".join(report["missing_classes"][:8])
        )
    elif report["missing_models"]:
        report["level"] = YELLOW
        total = sum(len(v) for v in report["missing_models"].values())
        report["notes"].append(f"Не хватает моделей: {total}")
    return report


def folders_to_query(requirements):
    """Какие папки (с учётом синонимов) спрашивать у воркера через /models/*."""
    folders = set()
    for folder in requirements["models"]:
        folders.update(FOLDER_ALIASES.get(folder, (folder,)))
    return sorted(folders)


def merge_folder_listings(requirements, listings):
    """Сшивает ответы /models/* в {folder_из_требований: [имена]} c учётом синонимов."""
    merged = {}
    for folder in requirements["models"]:
        names = []
        for alias in FOLDER_ALIASES.get(folder, (folder,)):
            names.extend(listings.get(alias, []))
        merged[folder] = names
    return merged


__all__ = ["check", "extract_requirements", "folders_to_query", "merge_folder_listings",
           "GREEN", "YELLOW", "RED"]
