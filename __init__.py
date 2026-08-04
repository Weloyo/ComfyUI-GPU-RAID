"""ComfyUI-GPU-RAID — распределённая генерация: локальная GPU + облачные воркеры.

Точка входа custom-node пакета. Устанавливается junction'ом/клоном в custom_nodes.
"""

import logging
import traceback

log = logging.getLogger("gpu_raid")

WEB_DIRECTORY = "./web"
NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

try:
    from .gpu_raid.nodes import (
        NODE_CLASS_MAPPINGS as _NODES,
        NODE_DISPLAY_NAME_MAPPINGS as _NAMES,
    )

    NODE_CLASS_MAPPINGS.update(_NODES)
    NODE_DISPLAY_NAME_MAPPINGS.update(_NAMES)

    from .gpu_raid import auth as _auth

    _auth.install()

    from .gpu_raid import routes as _routes  # noqa: F401  (регистрация маршрутов)

    from .gpu_raid.consts import VERSION

    log.info("GPU RAID v%s загружен (%d нод)", VERSION, len(NODE_CLASS_MAPPINGS))
except Exception:
    log.error("GPU RAID: ошибка инициализации:\n%s", traceback.format_exc())

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
