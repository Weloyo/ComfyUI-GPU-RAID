"""Отправка событий gpuraid.* в UI мастера (через WS ComfyUI) с троттлингом."""

import logging
import time

log = logging.getLogger("gpu_raid")

_last_sent = {}


def send(event, data, throttle_key=None, min_interval=0.25):
    if throttle_key is not None:
        now = time.monotonic()
        if now - _last_sent.get(throttle_key, 0.0) < min_interval:
            return
        _last_sent[throttle_key] = now
    try:
        from server import PromptServer

        PromptServer.instance.send_sync("gpuraid." + event, data)
    except Exception:
        log.debug("send_sync failed for %s", event, exc_info=True)


def toast(severity, text, life=6000):
    send("toast", {"severity": severity, "text": text, "life": life})


def clear_prefix(prefix):
    """Забыть троттлинг-метки с данным префиксом (например, всех юнитов job'а).

    Без этого _last_sent растёт неограниченно: ключ на каждый юнит каждого
    job'а копится вечно, пока жив процесс мастера.
    """
    for key in [k for k in _last_sent if k.startswith(prefix)]:
        _last_sent.pop(key, None)
