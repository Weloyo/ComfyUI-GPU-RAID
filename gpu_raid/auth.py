"""Токен-авторизация инстанса ComfyUI (роль воркера) + защита master-эндпоинтов.

Middleware добавляется в PromptServer.instance.app при импорте расширения:
custom nodes импортируются до старта сервера (main.py: init_extra_nodes ->
add_routes -> start_all), список middlewares в этот момент ещё мутабелен.

Правила при заданном env GPURAID_TOKEN:
  - запрос с loopback БЕЗ forwarded-заголовков -> пропуск (локальные клиенты);
    исключение аннулируется, если есть CF-Connecting-IP/X-Forwarded-For/X-Real-IP
    (туннель cloudflared приходит с 127.0.0.1 — это удалённый трафик);
  - GPURAID_AUTH_STRICT=1 -> токен обязателен даже с loopback (локальные тесты);
  - токен принимается в заголовке X-GPURAID-Token, query ?gpuraid_token= или cookie
    (query устанавливает cookie — чтобы UI воркера открывался в браузере по ссылке).
"""

import hmac
import ipaddress
import logging
import os

from aiohttp import web

from .consts import (
    FORWARD_HEADERS,
    STRICT_ENV,
    TOKEN_COOKIE,
    TOKEN_ENV,
    TOKEN_HEADER,
    TOKEN_QUERY,
)

log = logging.getLogger("gpu_raid")


def configured_token():
    return os.environ.get(TOKEN_ENV, "").strip()


def _is_loopback(request):
    peer = request.remote
    if not peer:
        return False
    try:
        return ipaddress.ip_address(peer).is_loopback
    except ValueError:
        return peer == "localhost"


def has_forward_headers(request):
    return any(h in request.headers for h in FORWARD_HEADERS)


def is_local_request(request):
    """Локальный запрос (для loopback-only master-эндпоинтов)."""
    return _is_loopback(request) and not has_forward_headers(request)


def _token_ok(supplied, token):
    return bool(supplied) and hmac.compare_digest(supplied.encode(), token.encode())


@web.middleware
async def token_middleware(request, handler):
    token = configured_token()
    if not token:
        return await handler(request)

    strict = os.environ.get(STRICT_ENV, "") == "1"
    if not strict and is_local_request(request):
        return await handler(request)

    if _token_ok(request.headers.get(TOKEN_HEADER, ""), token):
        return await handler(request)
    if _token_ok(request.cookies.get(TOKEN_COOKIE, ""), token):
        return await handler(request)
    if _token_ok(request.query.get(TOKEN_QUERY, ""), token):
        response = await handler(request)
        try:
            response.set_cookie(TOKEN_COOKIE, token, httponly=True, samesite="Lax")
        except Exception:
            pass
        return response

    return web.json_response({"error": "unauthorized", "hint": "X-GPURAID-Token"}, status=401)


def install():
    """Вешает middleware. Возвращает True, если токен-защита активна."""
    try:
        from server import PromptServer

        app = PromptServer.instance.app
        if token_middleware not in app.middlewares:
            app.middlewares.append(token_middleware)
    except Exception:
        log.exception("GPU RAID: не удалось установить auth middleware")
        return False
    if configured_token():
        log.info("GPU RAID: токен-авторизация ВКЛЮЧЕНА (env %s)", TOKEN_ENV)
        return True
    log.info("GPU RAID: env %s не задан — auth middleware пассивен", TOKEN_ENV)
    return False
