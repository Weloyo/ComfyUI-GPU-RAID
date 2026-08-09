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


class _MultipartStream:
    """head + содержимое файла + tail как один поток для urllib.

    Нужен, чтобы не собирать тело запроса в памяти: файлы бывают под сотню
    мегабайт. urllib читает `read(n)`, поэтому больше ничего и не требуется.
    """

    HEAD, FILE, TAIL, DONE = 0, 1, 2, 3

    def __init__(self, head, fileobj, tail):
        self._head, self._file, self._tail = head, fileobj, tail
        self._stage = self.HEAD
        self._buf = b""

    def read(self, size=-1):
        want = size if size and size > 0 else 1 << 20
        out = bytearray()
        while len(out) < want:
            if self._buf:
                take = want - len(out)
                out += self._buf[:take]
                self._buf = self._buf[take:]
                continue
            if self._stage == self.HEAD:
                self._buf, self._stage = self._head, self.FILE
            elif self._stage == self.FILE:
                chunk = self._file.read(want)
                if chunk:
                    self._buf = chunk
                else:
                    self._stage = self.TAIL
            elif self._stage == self.TAIL:
                self._buf, self._stage = self._tail, self.DONE
            else:
                break
        return bytes(out)


class WorkerClient:
    def __init__(self, worker_id, url, token=""):
        self.worker_id = worker_id
        self.url = url.rstrip("/")
        self.token = token or ""
        self._session = None

    # ---------------- базовое ----------------

    def _headers(self):
        return {TOKEN_HEADER: self.token} if self.token else {}

    def _is_local_host(self):
        """Воркер в локальной сети? Тогда keep-alive безопасен и полезен."""
        import ipaddress
        from urllib.parse import urlsplit

        host = (urlsplit(self.url).hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1"):
            return True
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            return False        # доменное имя = почти всегда туннель
        return addr.is_loopback or addr.is_private

    async def session(self):
        if self._session is None or self._session.closed:
            # Через бесплатный cloudflared-туннель переиспользование keep-alive
            # ломается: первый запрос по соединению проходит, второй висит до
            # таймаута (проверено: /prompt 0.5с, следом /system_stats — 15с в
            # TimeoutError; с force_close оба по 0.5с). Молчаливые зависания
            # выглядели бы как «воркер тупит», поэтому для туннелей соединение
            # не переиспользуем — лишний TLS-хендшейк дешевле потерянных минут.
            connector = aiohttp.TCPConnector(
                force_close=not self._is_local_host(), enable_cleanup_closed=True)
            self._session = aiohttp.ClientSession(
                headers=self._headers(), connector=connector,
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

    def _upload_blocking(self, local_path, remote_name, subfolder, timeout):
        """multipart вручную и stdlib-клиентом — по той же причине, что в
        download_view: aiohttp на мегабайтном теле через туннель рвёт
        соединение («WinError 64»), stdlib кладёт тот же файл за 7 с.

        Тело не собирается в памяти целиком: бандлы шардинга бывают под сотню
        мегабайт, а держать их копию в RAM мастера незачем.
        """
        import urllib.error
        import urllib.request
        import uuid

        boundary = "----gpuraid" + uuid.uuid4().hex

        def field(name, value):
            return (f"--{boundary}\r\nContent-Disposition: form-data; "
                    f'name="{name}"\r\n\r\n{value}\r\n').encode()

        head = (field("subfolder", subfolder) + field("type", "input")
                + field("overwrite", "true")
                + (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; "
                   f'filename="{remote_name}"\r\n'
                   "Content-Type: application/octet-stream\r\n\r\n").encode())
        tail = f"\r\n--{boundary}--\r\n".encode()
        size = os.path.getsize(local_path)

        with open(local_path, "rb") as f:
            body = _MultipartStream(head, f, tail)
            req = urllib.request.Request(
                self.url + "/upload/image", data=body, method="POST",
                headers={**self._headers(),
                         "Content-Type": f"multipart/form-data; boundary={boundary}",
                         "Content-Length": str(len(head) + size + len(tail))})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read().decode("utf-8", "replace") or "{}")
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise SubmitError("401: неверный токен воркера", status=401) from e
                text = e.read()[:300].decode("utf-8", "replace")
                raise SubmitError(f"upload {remote_name}: HTTP {e.code} {text}") from e

    async def upload_file(self, local_path, remote_name=None, subfolder="gpuraid"):
        """POST /upload/image (принимает любой файл). Возвращает значение для входа ноды."""
        remote_name = remote_name or os.path.basename(local_path)
        data = await asyncio.to_thread(
            self._upload_blocking, local_path, remote_name, subfolder, 600)
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
        """fileref: {filename, subfolder, type} -> скачивает в dest_path.

        Тело тянет БЛОКИРУЮЩИЙ stdlib-клиент в отдельном потоке, а не aiohttp.
        Причина найдена экспериментом (2026-08-09, живой Kaggle-воркер за
        бесплатным cloudflared): aiohttp получает заголовки и верный
        Content-Length, принимает ~1.4 КБ тела и встаёт намертво до таймаута.
        Не зависит от keep-alive, User-Agent, Accept-Encoding, семейства
        адресов и типа event loop; тот же файл stdlib качает за 2 с, а тот же
        aiohttp из того же процесса тянет 4 МБ с huggingface за 6 с — ломается
        именно связка «aiohttp + туннель» (родня уже известной поломки
        keep-alive там же). Результат обязан вернуться, поэтому здесь
        надёжность важнее единообразия клиента.
        """
        from urllib.parse import urlencode

        query = urlencode({
            "filename": fileref.get("filename", ""),
            "subfolder": fileref.get("subfolder", "") or "",
            "type": fileref.get("type", "output") or "output",
        })
        return await asyncio.to_thread(
            self._download_blocking, self.url + "/view?" + query, dest_path, 180)

    def _download_blocking(self, url, dest_path, timeout):
        """Тело файла тянет stdlib-клиент — почему не aiohttp, см. download_view."""
        import urllib.error
        import urllib.request

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        tmp = dest_path + ".part"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
        except urllib.error.HTTPError as e:
            raise SubmitError(f"/view: HTTP {e.code}") from e
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

    async def shutdown(self):
        """POST /gpuraid/worker/shutdown — просьба воркеру погасить себя.

        Возвращает {"ok": bool, ...}; сетевые ошибки пробрасываются наверх.
        """
        status, body = await self.post_json("/gpuraid/worker/shutdown", {}, timeout=15)
        if status == 200:
            return body or {"ok": True}
        return {"ok": False, "reason": (body or {}).get("reason", f"HTTP {status}")}

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
