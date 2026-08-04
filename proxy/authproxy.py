#!/usr/bin/env python3
"""Standalone токен-прокси перед ComfyUI (fallback, если middleware не встал).

Запуск:  python authproxy.py --port 18188 --target http://127.0.0.1:8188
Токен:   env GPURAID_TOKEN или --token. Заголовок X-GPURAID-Token,
         query ?gpuraid_token= или cookie gpuraid_token.

Зависимости: aiohttp (есть в окружении ComfyUI).
"""

import argparse
import asyncio
import hmac
import os

import aiohttp
from aiohttp import web

HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}
TOKEN_HEADER = "X-GPURAID-Token"


def _ok(supplied, token):
    return bool(supplied) and hmac.compare_digest(supplied.encode(), token.encode())


def make_app(target, token):
    session = None

    async def get_session():
        nonlocal session
        if session is None or session.closed:
            session = aiohttp.ClientSession(auto_decompress=False)
        return session

    def authorized(request):
        if _ok(request.headers.get(TOKEN_HEADER, ""), token):
            return True
        if _ok(request.cookies.get("gpuraid_token", ""), token):
            return True
        if _ok(request.query.get("gpuraid_token", ""), token):
            return True
        return False

    async def ws_proxy(request):
        ws_server = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024)
        await ws_server.prepare(request)
        url = target.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        url += request.rel_url.path_qs
        s = await get_session()
        try:
            async with s.ws_connect(url, max_msg_size=64 * 1024 * 1024) as ws_client:
                async def pump(src, dst):
                    async for msg in src:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await dst.send_str(msg.data)
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            await dst.send_bytes(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED,
                                          aiohttp.WSMsgType.ERROR):
                            break

                await asyncio.gather(pump(ws_server, ws_client), pump(ws_client, ws_server),
                                     return_exceptions=True)
        finally:
            await ws_server.close()
        return ws_server

    async def handler(request):
        if not authorized(request):
            return web.json_response({"error": "unauthorized", "hint": TOKEN_HEADER}, status=401)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await ws_proxy(request)

        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
        body = await request.read()
        s = await get_session()
        try:
            async with s.request(
                request.method, target + str(request.rel_url),
                headers=headers, data=body if body else None,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=None, sock_read=600),
            ) as upstream:
                out_headers = {k: v for k, v in upstream.headers.items()
                               if k.lower() not in HOP_HEADERS}
                response = web.StreamResponse(status=upstream.status, headers=out_headers)
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(1 << 20):
                    await response.write(chunk)
                await response.write_eof()
                return response
        except aiohttp.ClientError as e:
            return web.json_response({"error": f"upstream: {e}"}, status=502)

    app = web.Application(client_max_size=1024 * 1024 * 1024)
    app.router.add_route("*", "/{tail:.*}", handler)
    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18188)
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--target", default="http://127.0.0.1:8188")
    parser.add_argument("--token", default=os.environ.get("GPURAID_TOKEN", ""))
    args = parser.parse_args()
    if not args.token:
        raise SystemExit("Задайте токен: env GPURAID_TOKEN или --token")
    print(f"[authproxy] {args.listen}:{args.port} -> {args.target} (токен активен)")
    web.run_app(make_app(args.target.rstrip("/"), args.token),
                host=args.listen, port=args.port, print=None)


if __name__ == "__main__":
    main()
