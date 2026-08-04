"""gpu_raid — пакет расширения GPU RAID.

Здесь намеренно нет импортов: подмодули с зависимостями от ComfyUI
(server, folder_paths, torch) импортируются только из корневого
__init__.py пакета custom-node, а чистые модули (consts, graph_rewrite,
parity) можно импортировать где угодно, включая тесты.
"""

from .consts import VERSION  # noqa: F401
