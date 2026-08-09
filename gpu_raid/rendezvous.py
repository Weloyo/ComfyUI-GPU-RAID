"""Авторегистрация воркеров через приватный GitHub Gist.

Схема: воркер при старте (и периодически из watchdog'а) PATCH'ит в gist файл
w_<session8>.json со своей connection string; мастер опрашивает gist и сам
добавляет/обновляет воркеров — без копипасты строк. Оба конца ходят в GitHub
исходящими запросами: мастер по-прежнему не выставляется в интернет.

Формат файла (один файл на сессию воркера — конкурирующие PATCH разных
воркеров не затирают друг друга):
  {"v": 1, "name": "colab-0", "platform": "colab", "session": "a1b2c3d4",
   "string": "gpuraid://TOKEN@xxx.trycloudflare.com?name=colab-0",
   "ts": 1791234567, "state": "up" | "down"}

Чистые функции (parse_gist_files, entry_valid) не трогают сеть/ComfyUI и
покрыты тестами; сетевой цикл — класс Rendezvous ниже.
"""

import asyncio
import json
import logging
import time

log = logging.getLogger("gpu_raid")

ENTRY_TTL_S = 600  # воркер перепубликует ts каждые ~4 мин; старше 10 мин = труп


def parse_gist_files(files):
    """files: dict имя->{content,...} из ответа GitHub API -> [entry].

    Мусорные/чужие файлы молча пропускаются.
    """
    entries = []
    for name, meta in (files or {}).items():
        if not str(name).startswith("w_"):
            continue
        try:
            data = json.loads((meta or {}).get("content") or "")
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("v") != 1:
            continue
        if not data.get("session") or not data.get("string"):
            continue
        entries.append(data)
    return entries


def entry_valid(entry, now, ttl_s=ENTRY_TTL_S):
    """Живая ли запись: state=up и не протухла по ts."""
    if entry.get("state") == "down":
        return False
    try:
        ts = float(entry.get("ts") or 0)
    except (TypeError, ValueError):
        return False
    return 0 < ts and (now - ts) <= ttl_s


class Rendezvous:
    def __init__(self):
        self._task = None
        self._etag = None
        self.last_poll_ts = 0.0
        self.last_error = ""

    def start(self, loop):
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._loop())

    def snapshot(self, settings, secrets_view):
        return {
            "configured": bool((settings.get("rendezvous") or {}).get("gist_id")
                               and secrets_view.get("has_gh_token")),
            "last_poll_ts": self.last_poll_ts,
            "last_error": self.last_error,
        }

    async def _loop(self):
        # импорты здесь, чтобы модуль оставался импортируемым в тестах без ComfyUI
        from .workers import REGISTRY

        while True:
            poll_s = 30.0
            try:
                cfg = REGISTRY.settings().get("rendezvous") or {}
                poll_s = max(10.0, float(cfg.get("poll_s") or 30))
                gist_id = str(cfg.get("gist_id") or "").strip()
                if gist_id:
                    await self._poll(gist_id)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.debug("rendezvous: %s", e)
            await asyncio.sleep(poll_s)

    async def _poll(self, gist_id):
        import aiohttp

        from . import secrets as secret_store

        token = secret_store.get("gh_token")
        if not token:
            self.last_error = "нет GitHub-токена (панель → секреты)"
            return
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "comfyui-gpu-raid",
        }
        if self._etag:
            headers["If-None-Match"] = self._etag
        async with aiohttp.ClientSession() as s:
            async with s.get(f"https://api.github.com/gists/{gist_id}",
                             headers=headers,
                             timeout=aiohttp.ClientTimeout(total=20)) as r:
                self.last_poll_ts = time.time()
                if r.status == 304:
                    self.last_error = ""
                    return
                if r.status != 200:
                    self.last_error = f"gist HTTP {r.status}"
                    return
                self._etag = r.headers.get("ETag")
                data = await r.json()
        self.last_error = ""
        now = time.time()
        for entry in parse_gist_files(data.get("files")):
            try:
                await self._apply(entry, now)
            except Exception as e:
                log.warning("rendezvous apply %s: %s", entry.get("session"), e)

    async def _apply(self, entry, now):
        from . import events
        from .workers import REGISTRY, parse_connection_string

        session = str(entry["session"])
        if REGISTRY.is_dismissed(session):
            return          # воркера удалили руками — не воскрешаем
        try:
            parsed = parse_connection_string(entry["string"])
        except ValueError:
            return
        records = REGISTRY.records(include_local=False)
        rec = next((w for w in records if w.get("session") == session), None)
        if rec is None:
            # ручная запись с тем же адресом — привязываем сессию к ней
            rec = next((w for w in records if w.get("url") == parsed["url"]), None)

        if not entry_valid(entry, now):
            if rec is not None and entry.get("state") == "down" \
                    and REGISTRY.status.get(rec["id"], {}).get("state") != "stopped":
                REGISTRY.set_status(rec["id"], state="stopped", error="")
            return

        if rec is None:
            added, _errors = await REGISTRY.add_from_lines(entry["string"])
            if not added:
                return
            rec = added[0]
            await REGISTRY.update(rec["id"], {
                "session": session, "platform": entry.get("platform", ""),
            })
            events.toast("success",
                         f"Воркер «{rec['name']}» подключился автоматически")
            await REGISTRY.probe_worker(rec, full=True)
            return

        patch = {}
        if rec.get("url") != parsed["url"]:
            patch["url"] = parsed["url"]  # туннель переродился
        if parsed["token"] and rec.get("token") != parsed["token"]:
            patch["token"] = parsed["token"]
        if rec.get("session") != session:
            patch["session"] = session
        if entry.get("platform") and rec.get("platform") != entry.get("platform"):
            patch["platform"] = entry["platform"]
        if patch:
            await REGISTRY.update(rec["id"], patch)
            if "url" in patch:
                events.toast("info",
                             f"Воркер «{rec['name']}»: адрес туннеля обновлён автоматически")
                await REGISTRY.probe_worker(rec, full=True)


RENDEZVOUS = Rendezvous()
