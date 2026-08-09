"""Жизненный цикл воркеров: тикер автостопа по политике из настроек.

Каждые 30с собирает телеметрию (занятость, простой, возраст сессии) и через
чистые правила lifecycle_rules решает, кого гасить. Остановка = POST
/gpuraid/worker/shutdown на воркере (watchdog в ноутбуке видит sentinel и
завершает рантайм платформенно: Colab runtime.unassign(), Kaggle — выход
из batch-скрипта).
"""

import asyncio
import logging
import time

from . import events, lifecycle_rules as rules
from .dispatcher import MANAGER
from .workers import REGISTRY

log = logging.getLogger("gpu_raid")

TICK_S = 30


class Lifecycle:
    def __init__(self):
        self._task = None
        self.keep_alive_until = {}   # worker_id -> epoch («не гасить после этого задания»)

    def start(self, loop):
        if self._task is None or self._task.done():
            self._task = loop.create_task(self._loop())

    def keep_alive(self, worker_ids, minutes=10):
        until = time.time() + minutes * 60
        for wid in worker_ids:
            self.keep_alive_until[wid] = max(self.keep_alive_until.get(wid, 0), until)

    async def _loop(self):
        while True:
            await asyncio.sleep(TICK_S)
            try:
                await self.tick()
            except Exception:
                log.exception("lifecycle tick failed")

    def _view(self, record, now, busy):
        from . import distribute

        wid = record["id"]
        # закачка модели — тоже занятость этого воркера, иначе eco/instant
        # погасят его посреди 40 ГБ
        busy = busy or distribute.is_downloading(wid)
        st = REGISTRY.status.get(wid, {})
        last = MANAGER.worker_last_active.get(wid)
        online_since = st.get("online_since")
        ref = max(t for t in (last, online_since) if t) if (last or online_since) else None
        started = st.get("worker_started_ts") or online_since
        return {
            "kind": record.get("kind"),
            "pinned": bool(record.get("pinned")),
            "state": st.get("state"),
            "busy": busy,
            "has_worked": last is not None,
            "idle_s": (now - ref) if ref else None,
            "session_age_min": ((now - started) / 60.0) if started else 0,
            "keep_alive_until": self.keep_alive_until.get(wid, 0),
        }

    def _global_busy(self):
        # busy правила трактуют глобально: пока есть хоть один НЕзавершённый
        # job, не гасим никого. Пересчитываем на каждом шаге — см. tick().
        return any(not j.done_event.is_set() for j in MANAGER.jobs.values())

    def _managed_records(self):
        # только включённые облачные/LAN: выключенный воркер выведен из пула,
        # heartbeat его не пробит (телеметрия заморожена), и гасить его незачем —
        # иначе тик за тиком мастер долбится в мёртвый туннель и спамит тостами
        return [r for r in REGISTRY.records(include_local=False) if r.get("enabled")]

    async def tick(self):
        cfg = REGISTRY.settings().get("lifecycle") or {}
        # сами опрашиваем прогресс закачек моделей: иначе статус двигает только
        # открытая вкладка панели, и брошенная вкладкой закачка держит воркера
        # busy вечно — а busy в правилах глушит и eco/instant, и предохранитель
        # budget_min (квота выгорает до платформенного лимита сессии)
        try:
            from . import distribute
            await distribute.progress()
        except Exception:
            log.exception("lifecycle: опрос закачек не удался")
        for record in self._managed_records():
            # now и busy пересчитываем на КАЖДОГО воркера: остановки идут через
            # await (POST в туннель до 15с), и за это время мог прийти новый job —
            # со старым снимком busy мы бы погасили воркера, взявшего юнит
            now = time.time()
            busy = self._global_busy()
            view = self._view(record, now, busy)
            decision, reason = rules.decide(cfg, view, now)
            if decision == rules.STOP:
                await self.stop_worker(record, reason)

    def preview(self):
        """Для GET /gpuraid/lifecycle: что тикер думает о каждом воркере."""
        cfg = REGISTRY.settings().get("lifecycle") or {}
        now = time.time()
        busy = self._global_busy()
        out = []
        for record in self._managed_records():
            view = self._view(record, now, busy)
            decision, reason = rules.decide(cfg, view, now)
            out.append({
                "id": record["id"], "name": record["name"],
                "kind": view["kind"], "pinned": view["pinned"], "state": view["state"],
                "busy": view["busy"],
                "idle_s": round(view["idle_s"]) if view["idle_s"] is not None else None,
                "session_age_min": round(view["session_age_min"], 1),
                "decision": decision, "reason": reason,
            })
        return out

    async def stop_worker(self, record, reason=""):
        """Останавливает воркера через его /gpuraid/worker/shutdown."""
        wc = REGISTRY.client(record)
        try:
            data = await wc.shutdown()
        except Exception as e:
            log.warning("shutdown %s: %s", record["id"], e)
            events.toast("warn", f"Воркер «{record['name']}»: команда остановки не дошла ({e})")
            return False
        if not (data or {}).get("ok"):
            events.toast("warn",
                         f"Воркер «{record['name']}» не поддерживает остановку "
                         f"({(data or {}).get('reason', 'старый bootstrap?')})")
            return False
        REGISTRY.set_status(record["id"], state="stopped", error="")
        events.toast("info", f"⏻ Воркер «{record['name']}» остановлен ({reason})"
                     if reason else f"⏻ Воркер «{record['name']}» остановлен")
        return True


LIFECYCLE = Lifecycle()
