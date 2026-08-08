"""Реестр воркеров: workers.json + статус-кэш + heartbeat + разбор connection string.

Connection string: gpuraid://<token>@<host>[:port][?tls=0][&name=<label>]
Также принимаются голые URL: http(s)://host:port (без токена).
"""

import asyncio
import ipaddress
import logging
import re
import time
import uuid
from urllib.parse import parse_qs, urlsplit

from comfy.cli_args import args as comfy_args

from . import config, events
from .worker_client import WorkerClient

log = logging.getLogger("gpu_raid")

LOCAL_ID = "local"


def _is_private_host(host):
    if host in ("localhost",):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private
    except ValueError:
        return False


def parse_connection_string(line):
    """-> dict {url, token, name} | RewriteError-подобный ValueError с причиной."""
    s = line.strip()
    if not s:
        raise ValueError("пустая строка")

    token = ""
    name = None
    tls = None

    if s.startswith("gpuraid://"):
        rest = s[len("gpuraid://"):]
        if "@" in rest:
            token, rest = rest.split("@", 1)
        # допускаем вложенную схему: gpuraid://tok@https://host
        m = re.match(r"^(https?)://(.*)$", rest)
        if m:
            tls = m.group(1) == "https"
            rest = m.group(2)
        if "?" in rest:
            rest, query = rest.split("?", 1)
            q = parse_qs(query.replace("&amp;", "&"))
            if "tls" in q:
                tls = q["tls"][0] not in ("0", "false", "no")
            if "name" in q:
                name = q["name"][0]
        hostport = rest.strip().strip("/")
    elif s.startswith(("http://", "https://")):
        parts = urlsplit(s)
        tls = parts.scheme == "https"
        hostport = parts.netloc
        q = parse_qs(parts.query)
        if "name" in q:
            name = q["name"][0]
    else:
        hostport = s.strip("/")

    if not hostport:
        raise ValueError(f"не разобран адрес в '{line}'")
    host = hostport.rsplit(":", 1)[0] if ":" in hostport and not hostport.endswith("]") else hostport

    if tls is None:
        tls = not _is_private_host(host)
    scheme = "https" if tls else "http"
    url = f"{scheme}://{hostport}"
    return {"url": url, "token": token, "name": name or host.split(".")[0]}


class WorkerRegistry:
    def __init__(self):
        self._data = None
        self._lock = asyncio.Lock()
        self._clients = {}
        self.status = {}  # id -> {state, latency_ms, queue, gpu, vram_total_gb, vram_free_gb, last_seen, error}
        self._hb_task = None

    # ---------------- хранилище ----------------

    def _ensure_loaded(self):
        if self._data is None:
            self._data = config.load_json(
                config.workers_path(), {"version": 1, "workers": [], "settings": {}, "local_enabled": True}
            )
            self._data.setdefault("workers", [])
            self._data.setdefault("settings", {})
            self._data.setdefault("local_enabled", True)
        return self._data

    async def _save(self):
        async with self._lock:
            config.save_json_atomic(config.workers_path(), self._ensure_loaded())

    def settings(self):
        return config.merged_settings(self._ensure_loaded().get("settings"))

    async def update_settings(self, patch):
        data = self._ensure_loaded()
        stored = data.setdefault("settings", {})
        for key, value in (patch or {}).items():
            if isinstance(value, dict):
                merged = stored.setdefault(key, {})
                if isinstance(merged, dict):
                    merged.update(value)
                else:
                    stored[key] = dict(value)
            else:
                stored[key] = value
        await self._save()

    # ---------------- записи ----------------

    def _local_record(self):
        return {
            "id": LOCAL_ID,
            "name": "Локальная GPU",
            "url": f"http://127.0.0.1:{comfy_args.port}",
            "token": "",
            "enabled": bool(self._ensure_loaded().get("local_enabled", True)),
            "kind": "local",
            "model_remap": {},
        }

    def records(self, include_local=True):
        data = self._ensure_loaded()
        out = [self._local_record()] if include_local else []
        out.extend(data["workers"])
        return out

    def get(self, worker_id):
        if worker_id == LOCAL_ID:
            return self._local_record()
        for w in self._ensure_loaded()["workers"]:
            if w["id"] == worker_id:
                return w
        return None

    async def add_from_lines(self, text):
        added, errors = [], []
        for line in str(text).splitlines():
            if not line.strip():
                continue
            try:
                parsed = parse_connection_string(line)
            except ValueError as e:
                errors.append(str(e))
                continue
            record = {
                "id": uuid.uuid4().hex[:12],
                "name": parsed["name"],
                "url": parsed["url"],
                "token": parsed["token"],
                "enabled": True,
                "pinned": False,
                "kind": "lan" if _is_private_host(urlsplit(parsed["url"]).hostname or "") else "cloud",
                "model_remap": {},
                "added_at": int(time.time()),
                "notes": "",
            }
            self._ensure_loaded()["workers"].append(record)
            added.append(record)
        if added:
            await self._save()
        return added, errors

    async def update(self, worker_id, patch):
        if worker_id == LOCAL_ID:
            if "enabled" in patch:
                self._ensure_loaded()["local_enabled"] = bool(patch["enabled"])
                await self._save()
            return self._local_record()
        w = self.get(worker_id)
        if w is None:
            return None
        for key in ("name", "url", "token", "enabled", "notes", "kind",
                    "session", "platform"):
            if key in patch:
                w[key] = patch[key]
        if "pinned" in patch:
            w["pinned"] = bool(patch["pinned"])
        if "model_remap" in patch and isinstance(patch["model_remap"], dict):
            w["model_remap"] = patch["model_remap"]
        if "add_remap" in patch:
            add = patch["add_remap"]  # {folder, master, worker}
            w.setdefault("model_remap", {}).setdefault(add["folder"], {})[add["master"]] = add["worker"]
        self._clients.pop(worker_id, None)  # url/token могли смениться
        await self._save()
        return w

    async def delete(self, worker_id):
        if worker_id == LOCAL_ID:
            return False
        data = self._ensure_loaded()
        before = len(data["workers"])
        data["workers"] = [w for w in data["workers"] if w["id"] != worker_id]
        self._clients.pop(worker_id, None)
        self.status.pop(worker_id, None)
        if len(data["workers"]) != before:
            await self._save()
            return True
        return False

    # ---------------- клиенты и статусы ----------------

    def client(self, worker_id):
        record = self.get(worker_id) if isinstance(worker_id, str) else worker_id
        if record is None:
            raise KeyError(f"worker {worker_id} not found")
        wid = record["id"]
        cached = self._clients.get(wid)
        if cached is None or cached.url != record["url"].rstrip("/") or cached.token != (record.get("token") or ""):
            cached = WorkerClient(wid, record["url"], record.get("token") or "")
            self._clients[wid] = cached
        return cached

    def enabled_records(self):
        return [w for w in self.records() if w.get("enabled")]

    def set_status(self, worker_id, **fields):
        st = self.status.setdefault(worker_id, {})
        old_state = st.get("state")
        st.update(fields)
        st["last_update"] = time.time()
        if fields.get("state") and fields["state"] != old_state:
            events.send("worker", {"id": worker_id, **st})
        else:
            events.send("worker", {"id": worker_id, **st}, throttle_key=f"w:{worker_id}", min_interval=2.0)

    async def probe_worker(self, record, full=False):
        wc = self.client(record)
        prev = self.status.get(record["id"], {})
        probe = await wc.probe(timeout=self.settings()["timeouts"]["probe_s"])
        if not probe.get("ok"):
            # остановленный нами воркер недостижим — это норма, не «offline»
            state = "stopped" if prev.get("state") == "stopped" else "offline"
            self.set_status(record["id"], state=state, error=probe.get("error", ""))
            return False
        fields = {
            "state": "online",
            "latency_ms": probe.get("latency_ms"),
            "queue": probe.get("queue_remaining"),
            "error": "",
            "last_seen": time.time(),
        }
        if prev.get("state") != "online" or not prev.get("online_since"):
            fields["online_since"] = time.time()
        if full or "gpu" not in prev:
            try:
                stats = await wc.system_stats()
                dev = (stats.get("devices") or [{}])[0]
                fields["gpu"] = dev.get("name", "?")
                total = dev.get("vram_total") or 0
                free = dev.get("vram_free") or 0
                fields["vram_total_gb"] = round(total / (1024 ** 3), 1)
                fields["vram_free_gb"] = round(free / (1024 ** 3), 1)
                info = await wc.info()
                if info:
                    fields["ext_version"] = info.get("version")
                    if info.get("started_ts"):
                        fields["worker_started_ts"] = info["started_ts"]
                    if info.get("platform"):
                        fields["platform"] = info["platform"]
            except Exception as e:
                log.debug("system_stats failed for %s: %s", record["id"], e)
        self.set_status(record["id"], **fields)
        return True

    async def heartbeat_loop(self):
        tick = 0
        while True:
            try:
                interval = float(self.settings().get("heartbeat_s", 15))
                for record in self.enabled_records():
                    # остановленных пробим редко (мёртвый туннель = долгие таймауты),
                    # но не забываем: воркер могли поднять заново вручную
                    st = self.status.get(record["id"], {})
                    if st.get("state") == "stopped" and tick % 10 != 0:
                        continue
                    try:
                        await self.probe_worker(record, full=(tick % 4 == 0))
                    except Exception as e:
                        state = "stopped" if st.get("state") == "stopped" else "offline"
                        self.set_status(record["id"], state=state, error=str(e))
                tick += 1
            except Exception:
                log.exception("heartbeat loop error")
                interval = 15
            await asyncio.sleep(max(5.0, interval))

    def start_heartbeat(self, loop):
        if self._hb_task is None or self._hb_task.done():
            self._hb_task = loop.create_task(self.heartbeat_loop())


REGISTRY = WorkerRegistry()
