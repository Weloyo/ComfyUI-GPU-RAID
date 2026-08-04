"""HTTP/WS-клиент одного воркера (стоковый API ComfyUI + /gpuraid/* при наличии).

Устойчивость к туннелям: reconnect WS с backoff, потоковое скачивание,
токен в заголовке X-GPURAID-Token на каждом запросе (и на WS-handshake).
"""

import asyncio
import json
import logging
import os
import time

import aiohttp

from .consts import TOKEN_HEADER

log = logging.getLogger("gpu_raid")


class SubmitError(Exception):
    def __init__(self, message, node_errors=None, status=None):
        super().__init__(message)
        self.node_errors = node_errors or {}
        self.status = status


class WorkerClient:
    def __init__(self, worker_id, url, token=""):
        self.worker_id = worker_id
        self.url = url.rstrip("/")
        self.token = token or ""
        self._session = None

    # ---------------- базовое ----------------

    def _headers(self):
        return {TOKEN_HEADER: self.token} if self.token else {}

    async def session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=None, connect=15, sock_read=120),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def get_json(self, path, timeout=15):
        s = await self.session()
        async with s.get(self.url + path, timeout=aiohttp.ClientTimeout(total=timeout)) as r:
            if r.status == 401:
                raise SubmitError("401: неверный токен воркера", status=401)
            if r.status == 404:
                return None
            r.raise_for_status()
            return await r.json()

    async def post_json(self, path, payload, timeout=30):
        s = await self.session()
        async with s.post(
            self.url + path, json=payload, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as r:
            if r.status == 401:
                raise SubmitError("401: неверный токен воркера", status=401)
            body = None
            try:
                body = await r.json()
            except Exception:
                pass
            return r.status, body

    # ---------------- стоковый API ----------------

    async def probe(self, timeout=5):
        t0 = time.monotonic()
        try:
            data = await self.get_json("/prompt", timeout=timeout)
            latency = int((time.monotonic() - t0) * 1000)
            queue = (data or {}).get("exec_info", {}).get("queue_remaining")
            return {"ok": True, "latency_ms": latency, "queue_remaining": queue}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def system_stats(self):
        return await self.get_json("/system_stats", timeout=15) or {}

    async def object_info(self):
        return await self.get_json("/object_info", timeout=120) or {}

    async def models(self, folder):
        data = await self.get_json(f"/models/{folder}", timeout=20)
        return data if isinstance(data, list) else []

    async def info(self):
        """GET /gpuraid/info — None, если расширение на воркере не установлено."""
        try:
            return await self.get_json("/gpuraid/info", timeout=10)
        except SubmitError:
            raise
        except Exception:
            return None

    async def upload_file(self, local_path, remote_name=None, subfolder="gpuraid"):
        """POST /upload/image (принимает любой файл). Возвращает значение для входа ноды."""
        remote_name = remote_name or os.path.basename(local_path)
        s = await self.session()
        with open(local_path, "rb") as f:
            form = aiohttp.FormData()
            form.add_field("image", f, filename=remote_name,
                           content_type="application/octet-stream")
            form.add_field("subfolder", subfolder)
            form.add_field("type", "input")
            form.add_field("overwrite", "true")
            async with s.post(
                self.url + "/upload/image", data=form,
                timeout=aiohttp.ClientTimeout(total=600),
            ) as r:
                if r.status == 401:
                    raise SubmitError("401: неверный токен воркера", status=401)
                if r.status != 200:
                    text = (await r.text())[:300]
                    raise SubmitError(f"upload {remote_name}: HTTP {r.status} {text}")
                data = await r.json()
        sub = data.get("subfolder") or subfolder
        name = data.get("name") or remote_name
        return f"{sub}/{name}" if sub else name

    async def submit(self, graph, prompt_id, client_id):
        payload = {"prompt": graph, "client_id": client_id, "prompt_id": prompt_id}
        status, body = await self.post_json("/prompt", payload, timeout=60)
        if status == 200 and body:
            return body.get("prompt_id", prompt_id)
        message = "ошибка валидации на воркере"
        node_errors = {}
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                message = err.get("message", message)
                details = err.get("details")
                if details:
                    message += f": {details}"
            node_errors = body.get("node_errors") or {}
            if node_errors and message == "ошибка валидации на воркере":
                first = next(iter(node_errors.values()))
                errs = first.get("errors") if isinstance(first, dict) else None
                if errs:
                    message = errs[0].get("message", message)
        raise SubmitError(f"HTTP {status}: {message}", node_errors=node_errors, status=status)

    async def history(self, prompt_id):
        data = await self.get_json(f"/history/{prompt_id}", timeout=20)
        if isinstance(data, dict) and prompt_id in data:
            return data[prompt_id]
        return None

    async def queue_ids(self):
        data = await self.get_json("/queue", timeout=15) or {}

        def ids(items):
            out = set()
            for item in items or []:
                if isinstance(item, (list, tuple)) and len(item) > 1:
                    out.add(item[1])
            return out

        return {"running": ids(data.get("queue_running")), "pending": ids(data.get("queue_pending"))}

    async def download_view(self, fileref, dest_path):
        """fileref: {filename, subfolder, type} -> скачивает в dest_path (стримом)."""
        from urllib.parse import urlencode

        query = urlencode({
            "filename": fileref.get("filename", ""),
            "subfolder": fileref.get("subfolder", "") or "",
            "type": fileref.get("type", "output") or "output",
        })
        s = await self.session()
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        tmp = dest_path + ".part"
        async with s.get(
            self.url + "/view?" + query,
            timeout=aiohttp.ClientTimeout(total=None, sock_read=180),
        ) as r:
            if r.status != 200:
                raise SubmitError(f"/view {fileref.get('filename')}: HTTP {r.status}")
            with open(tmp, "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 20):
                    f.write(chunk)
        os.replace(tmp, dest_path)
        return dest_path

    async def cancel_prompt(self, prompt_id):
        """Отмена: новый jobs-API, затем страховочные пути."""
        try:
            status, _ = await self.post_json(f"/api/jobs/{prompt_id}/cancel", {}, timeout=15)
            if status in (200, 204):
                return True
        except Exception:
            pass
        try:
            ids = await self.queue_ids()
            if prompt_id in ids["pending"]:
                await self.post_json("/queue", {"delete": [prompt_id]}, timeout=15)
                return True
            if prompt_id in ids["running"]:
                await self.post_json("/interrupt", {"prompt_id": prompt_id}, timeout=15)
                return True
        except Exception as e:
            log.debug("cancel fallback failed on %s: %s", self.worker_id, e)
        return False

    async def free(self):
        try:
            await self.post_json("/free", {"unload_models": True, "free_memory": True}, timeout=30)
        except Exception:
            pass

    # ---------------- /gpuraid/* воркера ----------------

    async def download_model(self, payload):
        status, body = await self.post_json("/gpuraid/download_model", payload, timeout=30)
        if status != 200:
            raise SubmitError(f"download_model: HTTP {status} {body}")
        return body

    async def download_status(self, task_id):
        return await self.get_json(f"/gpuraid/download_status/{task_id}", timeout=15)

    # ---------------- WebSocket ----------------

    def ws_url(self, client_id):
        base = self.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        return f"{base}/ws?clientId={client_id}"

    async def ws_loop(self, client_id, on_event, should_run):
        """Держит WS с reconnect/backoff; on_event(dict) для каждого JSON-события.

        Служебные события: {"type": "_ws_connected"|"_ws_lost"}.
        """
        backoffs = (1, 2, 5, 10, 30)
        attempt = 0
        while should_run():
            try:
                s = await self.session()
                async with s.ws_connect(
                    self.ws_url(client_id), heartbeat=20, max_msg_size=64 * 1024 * 1024
                ) as ws:
                    attempt = 0
                    await ws.send_json({"type": "feature_flags", "data": {}})
                    on_event({"type": "_ws_connected"})
                    async for msg in ws:
                        if not should_run():
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                on_event(json.loads(msg.data))
                            except Exception:
                                pass
                        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("ws %s: %s", self.worker_id, e)
            if not should_run():
                break
            on_event({"type": "_ws_lost"})
            await asyncio.sleep(backoffs[min(attempt, len(backoffs) - 1)])
            attempt += 1
