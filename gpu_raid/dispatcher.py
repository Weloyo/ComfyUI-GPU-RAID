"""Диспетчер GPU RAID: job'ы, юниты, consumer-циклы воркеров, мониторинг, ретраи.

Вся оркестрация живёт на event loop PromptServer'а. Из потоков нод —
через asyncio.run_coroutine_threadsafe (см. nodes.py).
"""

import asyncio
import copy
import logging
import os
import queue as thread_queue
import time
import uuid

import folder_paths

from . import events, results
from .consts import SAVE_NODE_ID
from .graph_rewrite import (
    RewriteError,
    apply_remap,
    build_tail,
    build_unit_template,
    classify_job_type,
    collect_upload_refs,
    render_unit,
    rewrite_upload_refs,
    splice_gpuraid,
    strip_annotation,
    validate_stripe,
)
from .worker_client import SubmitError
from .workers import LOCAL_ID, REGISTRY

log = logging.getLogger("gpu_raid")

PID_NS = uuid.UUID("f7a3b0c4-9d2e-4c7a-8b1f-2e5d6a9c0f31")

QUEUED, ASSIGNED, RUNNING, FETCHING, DONE, DEAD = (
    "QUEUED", "ASSIGNED", "RUNNING", "FETCHING", "DONE", "DEAD",
)


def make_pid(job_id, index, attempt):
    return str(uuid.uuid5(PID_NS, f"{job_id}:{index}:{attempt}"))


class UnitFailure(Exception):
    def __init__(self, message, retriable=True, worker_fault=True, incompatible=False):
        super().__init__(message)
        self.retriable = retriable
        self.worker_fault = worker_fault      # засчитывать ли страйк воркеру
        self.incompatible = incompatible      # воркер не годен для job (без инкремента attempts)


class UnitCancelled(Exception):
    pass


class Unit:
    __slots__ = ("index", "state", "attempts", "worker_id", "prompt_id", "files",
                 "error", "progress", "meta", "t_submitted", "t_done")

    def __init__(self, index, meta=None):
        self.index = index
        self.state = QUEUED
        self.attempts = 0
        self.worker_id = None
        self.prompt_id = None
        self.files = []
        self.error = ""
        self.progress = (0, 0)
        self.meta = meta or {}
        self.t_submitted = 0.0
        self.t_done = 0.0

    def snapshot(self):
        return {
            "index": self.index, "state": self.state, "attempts": self.attempts,
            "worker_id": self.worker_id, "progress": list(self.progress),
            "error": self.error, "label": self.meta.get("label", ""),
        }


class TrackedPrompt:
    __slots__ = ("progress", "kick", "outcome", "error_text", "last_event")

    def __init__(self):
        self.progress = (0, 0)
        self.kick = asyncio.Event()
        self.outcome = None      # None | "success" | "error" | "interrupted"
        self.error_text = ""
        self.last_event = 0.0


class WsHub:
    """Один WS на воркера: мультиплекс событий по prompt_id + авто-reconnect."""

    def __init__(self, worker_id):
        self.worker_id = worker_id
        self.client_id = f"gpuraid-{worker_id}"
        self.tracked = {}
        self.task = None
        self.connected = False
        self.last_used = time.monotonic()

    def track(self, prompt_id):
        tp = TrackedPrompt()
        self.tracked[prompt_id] = tp
        self.last_used = time.monotonic()
        return tp

    def untrack(self, prompt_id):
        self.tracked.pop(prompt_id, None)
        self.last_used = time.monotonic()

    def _should_run(self):
        return bool(self.tracked) or (time.monotonic() - self.last_used) < 120

    def ensure(self, loop, wc):
        if self.task is None or self.task.done():
            self.task = loop.create_task(wc.ws_loop(self.client_id, self.on_event, self._should_run))

    def on_event(self, event):
        etype = event.get("type")
        if etype == "_ws_connected":
            self.connected = True
            for tp in self.tracked.values():
                tp.kick.set()
            return
        if etype == "_ws_lost":
            self.connected = False
            return
        data = event.get("data") or {}
        pid = data.get("prompt_id")
        tp = self.tracked.get(pid)
        if tp is None and pid is None and len(self.tracked) == 1:
            tp = next(iter(self.tracked.values()))
        if tp is None:
            return
        tp.last_event = time.monotonic()
        if etype == "progress":
            try:
                tp.progress = (int(data.get("value", 0)), int(data.get("max", 0)))
            except (TypeError, ValueError):
                pass
            tp.kick.set()
        elif etype == "execution_success":
            tp.outcome = "success"
            tp.kick.set()
        elif etype == "execution_error":
            tp.outcome = "error"
            tp.error_text = "{}: {}".format(
                data.get("node_type", "?"), data.get("exception_message", "ошибка исполнения")
            )
            tp.kick.set()
        elif etype == "execution_interrupted":
            tp.outcome = "interrupted"
            tp.kick.set()
        elif etype == "executing" and data.get("node") is None:
            tp.kick.set()


class Job:
    def __init__(self, kind, client_id="", label=""):
        self.job_id = uuid.uuid4().hex[:12]
        self.kind = kind                  # stripe | offload | upscale | longvideo
        self.state = "CREATED"
        self.client_id = client_id or ""
        self.label = label or self.job_id
        self.created = time.time()
        self.t0 = time.monotonic()
        self.job_type = "image"
        self.min_vram_gb = 0.0
        self.units = []
        self.queue = asyncio.PriorityQueue()
        self.cancelled = False
        self.eligible = []                # worker records
        self.excluded = []                # [{id, reason}]
        self.upload_specs = []            # [(nid, key, local_path, remote_name)]
        self.uploaded = {}                # worker_id -> {(nid,key): remote_value}
        self.strikes = {}
        self.inflight = {}                # index -> (worker_id, prompt_id)
        self.errors = []
        self.warnings = []
        self.stats = {"per_worker": {}}
        self.tail_prompt_id = None
        self.done_event = asyncio.Event()
        self.thread_results = None        # queue.Queue для upscale-нод
        self.build_graph = None           # callable(unit) -> graph (без спец-обработки воркера)
        self.timeouts = {}
        self.outdir = None
        self.finished = None              # итоговое состояние

    def snapshot(self, with_units=True):
        done = sum(1 for u in self.units if u.state == DONE)
        dead = sum(1 for u in self.units if u.state == DEAD)
        snap = {
            "job_id": self.job_id, "kind": self.kind, "state": self.state,
            "label": self.label, "created": self.created, "job_type": self.job_type,
            "total": len(self.units), "done": done, "dead": dead,
            "per_worker": self.stats.get("per_worker", {}),
            "errors": self.errors[-5:], "warnings": self.warnings,
            "excluded": self.excluded, "outdir": self.outdir,
            "wall_s": round(time.monotonic() - self.t0, 1),
        }
        if with_units:
            snap["units"] = [u.snapshot() for u in self.units]
        return snap


class JobManager:
    def __init__(self):
        self.jobs = {}
        self.history = []
        self.hubs = {}
        self.reserved = set()

    # ------------------------------------------------------------------ utils

    @property
    def loop(self):
        from server import PromptServer

        return PromptServer.instance.loop

    def hub(self, worker_id):
        h = self.hubs.get(worker_id)
        if h is None:
            h = WsHub(worker_id)
            self.hubs[worker_id] = h
        return h

    def _register(self, job):
        self.jobs[job.job_id] = job
        events.send("job_started", job.snapshot())

    def _timeouts_for(self, job_type):
        t = REGISTRY.settings()["timeouts"]
        if job_type == "video":
            return {"startup": t["video_startup_s"], "stall": t["video_stall_s"], "hard": t["hard_cap_s"]}
        return {"startup": t["image_startup_s"], "stall": t["image_stall_s"], "hard": t["hard_cap_s"]}

    async def _eligible_workers(self, job, only_worker_id=None, need_probe=True):
        records = []
        for record in REGISTRY.enabled_records():
            wid = record["id"]
            if only_worker_id and wid != only_worker_id:
                continue
            if wid in self.reserved and wid != only_worker_id:
                job.excluded.append({"id": wid, "reason": "занят offload-задачей"})
                continue
            status = REGISTRY.status.get(wid, {})
            if need_probe and status.get("state") != "online":
                ok = False
                try:
                    ok = await REGISTRY.probe_worker(record, full=True)
                except Exception:
                    ok = False
                if not ok:
                    job.excluded.append({"id": wid, "reason": "офлайн"})
                    continue
                status = REGISTRY.status.get(wid, {})
            vram = status.get("vram_total_gb")
            if job.min_vram_gb and vram and vram + 0.01 < job.min_vram_gb:
                job.excluded.append({"id": wid, "reason": f"VRAM {vram} ГБ < {job.min_vram_gb} ГБ"})
                continue
            records.append(record)
        return records

    def _resolve_upload_specs(self, refs, job_id):
        """[(nid,key,value)] -> [(nid,key,local_path,remote_name)]; проверяет существование."""
        specs = []
        for nid, key, value in refs:
            base, ann = strip_annotation(value)
            path = folder_paths.get_annotated_filepath(value)
            if not path or not os.path.isfile(path):
                raise RewriteError(f"Входной файл не найден: {value}")
            remote = f"{job_id[:8]}_{os.path.basename(base)}"
            specs.append((str(nid), key, path, remote))
        return specs

    # ------------------------------------------------------------------ stripe

    async def start_stripe(self, graph, workflow_ui, client_id):
        spec = validate_stripe(graph)
        job = Job("stripe", client_id=client_id)
        job.job_type = spec["job_type"]
        job.min_vram_gb = spec["min_vram_gb"]
        job.timeouts = self._timeouts_for(job.job_type)

        template, refs = build_unit_template(graph, spec)
        job.upload_specs = self._resolve_upload_specs(refs, job.job_id)
        job.spec = spec
        job.graph = graph
        job.workflow_ui = workflow_ui

        base_seed = spec["base_seed"]
        total = spec["total_variants"]

        def builder(unit):
            seed = (base_seed + unit.index) % (2 ** 64)
            return render_unit(template, seed, unit.index,
                               results.unit_prefix(job.job_id, unit.index))

        job.build_graph = builder

        job.eligible = await self._eligible_workers(job)
        if not job.eligible:
            raise RewriteError("Нет доступных воркеров: " +
                               ("; ".join(e["reason"] for e in job.excluded) or "список пуст"))
        if total < 2:
            raise RewriteError("total_variants < 2 — распределять нечего")
        if len(job.eligible) < 2:
            raise RewriteError("Меньше двух доступных GPU — выполняю локально")

        results.recv_dir(job.job_id)
        job.units = [Unit(i) for i in range(total)]
        for u in job.units:
            job.queue.put_nowait((1, u.index))
        job.state = "DISPATCHING"
        self._register(job)
        self.loop.create_task(self._run_parallel(job, finalize=self._finalize_stripe))
        return job

    async def _finalize_stripe(self, job):
        done = [u for u in job.units if u.state == DONE]
        if not done:
            job.finished = "FAILED"
            events.toast("error", f"GPU RAID: job {job.label} не выполнен — "
                                  + (job.errors[-1] if job.errors else "все юниты погибли"))
            return
        if any(u.state == DEAD for u in job.units):
            job.finished = "PARTIAL"
            dead_idx = [u.index for u in job.units if u.state == DEAD]
            events.toast("warn",
                         f"GPU RAID: собрано {len(done)}/{len(job.units)} вариантов "
                         f"(потеряны: {dead_idx})")
        else:
            job.finished = "COMPLETE"
        await self._submit_tail(job)

    async def _submit_tail(self, job):
        tail = build_tail(job.graph, job.spec, job.job_id)
        wc = REGISTRY.client(LOCAL_ID)
        pid = make_pid(job.job_id, -1, 0)
        try:
            extra = {"prompt": tail, "client_id": job.client_id or "gpuraid",
                     "prompt_id": pid,
                     "extra_data": {"extra_pnginfo": {"workflow": job.workflow_ui}}}
            status, body = await wc.post_json("/prompt", extra, timeout=60)
            if status != 200:
                raise SubmitError(f"HTTP {status}: {body}")
            job.tail_prompt_id = pid
            job.state = "TAIL_QUEUED"
        except Exception as e:
            job.errors.append(f"хвост: {e}")
            job.finished = "PARTIAL"
            events.toast("error", f"GPU RAID: не удалось поставить хвост графа: {e}")

    # ------------------------------------------------------------------ offload

    async def start_offload(self, graph, workflow_ui, worker_id, label, client_id):
        record = REGISTRY.get(worker_id)
        if record is None or not record.get("enabled"):
            raise RewriteError("Воркер не найден или выключен")
        spliced, warnings = splice_gpuraid(graph)
        job = Job("offload", client_id=client_id, label=label or f"offload-{worker_id}")
        job.warnings = warnings
        job.job_type = classify_job_type(spliced)
        job.timeouts = self._timeouts_for(job.job_type)
        refs = collect_upload_refs(spliced)
        job.upload_specs = self._resolve_upload_specs(refs, job.job_id)
        job.units = [Unit(0, meta={"label": job.label})]
        job.build_graph = lambda unit: spliced
        job.eligible = [record]
        job.state = "DISPATCHING"
        self.reserved.add(worker_id)
        self._register(job)
        self.loop.create_task(self._run_offload(job, record))
        return job

    async def _run_offload(self, job, record):
        unit = job.units[0]
        wc = REGISTRY.client(record)
        try:
            await self._execute_unit(job, record, wc, unit, fetch="offload")
            job.finished = "COMPLETE"
            job.state = "COMPLETE"
            events.toast("success",
                         f"GPU RAID: offload «{job.label}» готов — {len(unit.files)} файл(ов) в {job.outdir}")
        except UnitCancelled:
            job.finished = job.state = "CANCELLED"
        except Exception as e:
            job.finished = job.state = "FAILED"
            job.errors.append(str(e))
            events.toast("error", f"GPU RAID: offload «{job.label}» не выполнен: {e}")
        finally:
            self.reserved.discard(record["id"])
            job.done_event.set()
            self._archive(job)

    # ------------------------------------------------------------------ upscale (вызывается из потока ноды)

    def start_upscale_blocking(self, units_payload, min_vram_gb, label="tiled-upscale"):
        """Создаёт job на loop'е из потока ноды. Возвращает (job_id, queue.Queue)."""

        async def _create():
            job = Job("upscale", label=label)
            job.job_type = "image"
            job.min_vram_gb = float(min_vram_gb or 0)
            job.timeouts = self._timeouts_for("image")
            job.thread_results = thread_queue.Queue()
            graphs = {}
            for item in units_payload:
                idx = item["index"]
                unit = Unit(idx, meta={"out_file": item["out_file"], "label": f"tile {idx}"})
                job.units.append(unit)
                graphs[idx] = (item["graph"], item["uploads"])

            def builder(unit):
                return graphs[unit.index][0]

            job.build_graph = builder
            job.unit_uploads = {idx: up for idx, (_, up) in graphs.items()}
            job.eligible = await self._eligible_workers(job)
            job.eligible = [r for r in job.eligible if r["id"] != LOCAL_ID]
            if not job.eligible:
                return None, None
            for unit in job.units:
                job.queue.put_nowait((1, unit.index))
            job.state = "DISPATCHING"
            self._register(job)
            self.loop.create_task(self._run_parallel(job, finalize=None))
            return job.job_id, job.thread_results

        fut = asyncio.run_coroutine_threadsafe(_create(), self.loop)
        return fut.result(timeout=60)

    def cancel_unit_blocking(self, job_id, index):
        async def _cancel():
            job = self.jobs.get(job_id)
            if not job:
                return
            entry = job.inflight.get(index)
            if entry:
                wid, pid = entry
                try:
                    await REGISTRY.client(wid).cancel_prompt(pid)
                except Exception:
                    pass

        asyncio.run_coroutine_threadsafe(_cancel(), self.loop)

    def cancel_job_blocking(self, job_id):
        asyncio.run_coroutine_threadsafe(self.cancel(job_id), self.loop)

    # ------------------------------------------------------------------ общий параллельный прогон

    async def _run_parallel(self, job, finalize):
        consumers = [
            self.loop.create_task(self._consumer(job, record)) for record in job.eligible
        ]
        await asyncio.gather(*consumers, return_exceptions=True)
        # финальная зачистка: юниты, оставшиеся без воркеров
        for unit in job.units:
            if unit.state not in (DONE, DEAD):
                unit.state = DEAD
                unit.error = unit.error or "не осталось живых воркеров"
                self._emit_unit(job, unit)
        job.state = "ASSEMBLING"
        if finalize is not None:
            try:
                await finalize(job)
            except Exception as e:
                log.exception("finalize failed")
                job.errors.append(str(e))
                job.finished = job.finished or "FAILED"
        if job.kind == "upscale" and job.thread_results is not None:
            job.thread_results.put(("__job_done__", None))
        job.state = job.finished or ("CANCELLED" if job.cancelled else "COMPLETE")
        job.done_event.set()
        self._archive(job)

    def _archive(self, job):
        summary = job.snapshot(with_units=False)
        summary["wall_s"] = round(time.monotonic() - job.t0, 1)
        self.history.insert(0, summary)
        del self.history[20:]
        events.send("job_done", summary)
        if job.kind == "stripe":
            results.gc_jobs(REGISTRY.settings().get("keep_last_jobs", 5))

    def _emit_unit(self, job, unit, throttle=False):
        payload = {"job_id": job.job_id, **unit.snapshot()}
        if throttle:
            events.send("unit", payload, throttle_key=f"u:{job.job_id}:{unit.index}", min_interval=0.3)
        else:
            events.send("unit", payload)

    async def _consumer(self, job, record):
        wid = record["id"]
        wc = REGISTRY.client(record)
        job.stats["per_worker"].setdefault(wid, 0)
        last_probe_ok = 0.0
        while not job.cancelled:
            if job.strikes.get(wid, 0) >= 2:
                break
            try:
                _, index = await asyncio.wait_for(job.queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                if all(u.state in (DONE, DEAD) for u in job.units):
                    break
                if job.done_event.is_set():
                    break
                continue
            unit = job.units[index]
            if unit.state in (DONE, DEAD):
                continue

            now = time.monotonic()
            if now - last_probe_ok > 10:
                probe = await wc.probe(timeout=REGISTRY.settings()["timeouts"]["probe_s"])
                if not probe.get("ok"):
                    REGISTRY.set_status(wid, state="offline", error=probe.get("error", ""))
                    job.strikes[wid] = job.strikes.get(wid, 0) + 2
                    job.queue.put_nowait((0, index))
                    break
                last_probe_ok = now

            try:
                await self._execute_unit(job, record, wc, unit, fetch=job.kind)
                job.stats["per_worker"][wid] += 1
            except UnitCancelled:
                job.queue.put_nowait((0, index))
                break
            except UnitFailure as e:
                unit.error = str(e)
                if e.worker_fault:
                    job.strikes[wid] = job.strikes.get(wid, 0) + 1
                if not e.incompatible:
                    unit.attempts += 1
                max_retries = int(REGISTRY.settings().get("max_retries", 2))
                if e.retriable and unit.attempts <= max_retries:
                    unit.state = QUEUED
                    unit.progress = (0, 0)
                    job.queue.put_nowait((0, index))
                else:
                    unit.state = DEAD
                    unit.t_done = time.monotonic()
                self._emit_unit(job, unit)
                if e.incompatible:
                    job.excluded.append({"id": wid, "reason": str(e)})
                    break
            except Exception as e:
                log.exception("unit %s on %s crashed", index, wid)
                unit.error = f"{type(e).__name__}: {e}"
                unit.attempts += 1
                if unit.attempts <= int(REGISTRY.settings().get("max_retries", 2)):
                    unit.state = QUEUED
                    job.queue.put_nowait((0, index))
                else:
                    unit.state = DEAD
                self._emit_unit(job, unit)
                job.strikes[wid] = job.strikes.get(wid, 0) + 1

    # ------------------------------------------------------------------ исполнение юнита

    async def _uploads_for(self, job, unit):
        per_unit = getattr(job, "unit_uploads", None)
        if per_unit is not None:
            return per_unit.get(unit.index, [])
        return job.upload_specs

    async def _ensure_uploads(self, job, record, wc, unit):
        """Заливает входные файлы юнита на воркера, возвращает mapping для rewrite."""
        specs = await self._uploads_for(job, unit)
        if not specs:
            return {}
        if record["id"] == LOCAL_ID:
            return {}
        done = job.uploaded.setdefault(record["id"], {})
        mapping = {}
        for nid, key, local_path, remote_name in specs:
            cache_key = (nid, key, local_path)
            if cache_key not in done:
                try:
                    size = os.path.getsize(local_path)
                    if size > 95 * 1024 * 1024 and record.get("kind") == "cloud":
                        job.warnings.append(
                            f"{os.path.basename(local_path)} ~{size // (1 << 20)}МБ: cloudflared "
                            "ограничен 100 МБ на запрос — используйте zrok/меньший файл"
                        )
                    done[cache_key] = await wc.upload_file(local_path, remote_name)
                except SubmitError as e:
                    raise UnitFailure(f"upload входного файла: {e}", worker_fault=True)
            mapping[(nid, key)] = done[cache_key]
        return mapping

    async def _execute_unit(self, job, record, wc, unit, fetch="stripe"):
        wid = record["id"]
        unit.state = ASSIGNED
        unit.worker_id = wid
        self._emit_unit(job, unit)

        graph = copy.deepcopy(job.build_graph(unit))
        graph = apply_remap(graph, record.get("model_remap"))
        mapping = await self._ensure_uploads(job, record, wc, unit)
        if mapping:
            rewrite_upload_refs(graph, mapping)

        pid = make_pid(job.job_id, unit.index, unit.attempts)
        unit.prompt_id = pid
        hub = self.hub(wid)
        hub.ensure(self.loop, wc)

        # дедуп: не отправлять повторно, если сервер уже знает этот prompt_id
        already = None
        try:
            already = await wc.history(pid)
        except Exception:
            already = None
        if already is None:
            try:
                ids = await wc.queue_ids()
                queued = pid in ids["running"] or pid in ids["pending"]
            except Exception:
                queued = False
            if not queued:
                try:
                    await wc.submit(graph, pid, hub.client_id)
                except SubmitError as e:
                    if e.status == 401:
                        raise UnitFailure(str(e), worker_fault=True)
                    if e.node_errors or (e.status and 400 <= e.status < 500):
                        raise UnitFailure(f"воркер несовместим: {e}", incompatible=True)
                    raise UnitFailure(f"submit: {e}", worker_fault=True)
                except Exception as e:
                    raise UnitFailure(f"submit: {type(e).__name__}: {e}", worker_fault=True)

        unit.state = RUNNING
        unit.t_submitted = time.monotonic()
        job.inflight[unit.index] = (wid, pid)
        self._emit_unit(job, unit)
        try:
            hist = already if (already and (already.get("status") or {}).get("completed")) \
                else await self._monitor(job, wc, unit, hub, pid)
        finally:
            job.inflight.pop(unit.index, None)
            hub.untrack(pid)

        unit.state = FETCHING
        self._emit_unit(job, unit)
        try:
            if fetch == "offload":
                await self._fetch_offload(job, wc, unit, hist)
            elif fetch == "upscale":
                await self._fetch_upscale(job, wc, unit, hist)
            elif fetch == "longvideo":
                await self._fetch_longvideo(job, wc, unit, hist)
            else:
                await self._fetch_stripe(job, wc, unit, hist)
        except UnitFailure:
            raise
        except Exception as e:
            raise UnitFailure(f"скачивание результата: {type(e).__name__}: {e}", worker_fault=True)

        unit.state = DONE
        unit.t_done = time.monotonic()
        self._emit_unit(job, unit)

    async def _monitor(self, job, wc, unit, hub, pid):
        tp = hub.track(pid)
        t = job.timeouts
        t_start = time.monotonic()
        first_activity = None
        last_change = t_start
        last_poll = 0.0
        last_queue_check = 0.0
        poll_iv = 3.0 if job.job_type == "image" else 5.0
        hist_fail = 0

        while True:
            if job.cancelled:
                await wc.cancel_prompt(pid)
                raise UnitCancelled()
            try:
                await asyncio.wait_for(tp.kick.wait(), timeout=0.5)
                tp.kick.clear()
            except asyncio.TimeoutError:
                pass
            now = time.monotonic()

            if tp.progress != unit.progress:
                unit.progress = tp.progress
                if first_activity is None:
                    first_activity = now
                last_change = now
                self._emit_unit(job, unit, throttle=True)

            if tp.outcome or now - last_poll >= poll_iv:
                last_poll = now
                try:
                    hist = await wc.history(pid)
                    hist_fail = 0
                except Exception:
                    hist_fail += 1
                    hist = None
                    if hist_fail >= 8:
                        raise UnitFailure("воркер недоступен (history)", worker_fault=True)
                if hist is not None:
                    status = hist.get("status") or {}
                    if status.get("completed"):
                        return hist
                    if status.get("status_str") == "error" or tp.outcome == "error":
                        raise UnitFailure(
                            "ошибка на воркере: " + (tp.error_text or _extract_history_error(status)),
                            worker_fault=True,
                        )
                    if tp.outcome == "interrupted" or status.get("status_str") == "interrupted":
                        raise UnitFailure("прервано на воркере", worker_fault=False)
                elif tp.outcome == "error":
                    raise UnitFailure("ошибка на воркере: " + tp.error_text, worker_fault=True)
                elif tp.outcome == "interrupted":
                    raise UnitFailure("прервано на воркере", worker_fault=False)

            # активность при мёртвом WS: присутствие в очереди воркера
            if not hub.connected and now - last_queue_check >= max(poll_iv * 2, 8.0):
                last_queue_check = now
                try:
                    ids = await wc.queue_ids()
                    if pid in ids["running"]:
                        if first_activity is None:
                            first_activity = now
                        last_change = now
                    elif pid not in ids["pending"] and now - unit.t_submitted > 20:
                        hist = await wc.history(pid)
                        if hist and (hist.get("status") or {}).get("completed"):
                            return hist
                        raise UnitFailure("prompt пропал из очереди воркера", worker_fault=True)
                except UnitFailure:
                    raise
                except Exception:
                    pass

            if first_activity is None and now - t_start > t["startup"]:
                await wc.cancel_prompt(pid)
                raise UnitFailure(f"нет старта за {t['startup']}с", worker_fault=True)
            if first_activity is not None and now - last_change > t["stall"] and hub.connected:
                await wc.cancel_prompt(pid)
                raise UnitFailure(f"нет прогресса {t['stall']}с", worker_fault=True)
            if now - t_start > t["hard"]:
                await wc.cancel_prompt(pid)
                raise UnitFailure("превышен жёсткий лимит времени", worker_fault=True)

    async def _fetch_stripe(self, job, wc, unit, hist):
        outputs = (hist or {}).get("outputs", {})
        images = (outputs.get(SAVE_NODE_ID) or {}).get("images") or []
        images = [f for f in images if f.get("type") == "output"]
        if not images:
            raise UnitFailure("воркер не вернул изображений (outputs пуст)", worker_fault=True)
        dest_dir = results.recv_dir(job.job_id)
        unit.files = []
        for k, ref in enumerate(images):
            ext = os.path.splitext(ref.get("filename", ""))[1] or ".png"
            dest = os.path.join(dest_dir, f"u{unit.index:04d}_{k:02d}{ext}")
            await wc.download_view(ref, dest)
            unit.files.append(dest)

    async def _fetch_offload(self, job, wc, unit, hist):
        outputs = (hist or {}).get("outputs", {})
        if job.outdir is None:
            job.outdir, _ = results.deliver_dir(job.label)
        seen = set()
        unit.files = []
        for node_id, node_out in outputs.items():
            if not isinstance(node_out, dict):
                continue
            for key, items in node_out.items():
                if not isinstance(items, list):
                    continue
                for ref in items:
                    if not (isinstance(ref, dict) and ref.get("filename")):
                        continue
                    if ref.get("type") not in (None, "output"):
                        continue
                    name = ref["filename"]
                    if name in seen:
                        name = f"{node_id}_{name}"
                    seen.add(name)
                    dest = os.path.join(job.outdir, name)
                    await wc.download_view(ref, dest)
                    unit.files.append(dest)
        if not unit.files:
            raise UnitFailure("offload завершился, но выходных файлов нет "
                              "(проверьте, что в workflow есть Save-нода)", worker_fault=False)

    async def _fetch_longvideo(self, job, wc, unit, hist):
        """Скачивает видеофайл сегмента (выход ноды unit.meta['out_node'])."""
        video_ext = (".mp4", ".webm", ".mov", ".mkv", ".gif", ".webp", ".avi")
        outputs = (hist or {}).get("outputs", {})
        candidates = []
        preferred = outputs.get(unit.meta.get("out_node")) or {}
        pools = [preferred] + [v for k, v in outputs.items() if v is not preferred]
        for pool in pools:
            if not isinstance(pool, dict):
                continue
            for items in pool.values():
                if not isinstance(items, list):
                    continue
                for ref in items:
                    if isinstance(ref, dict) and ref.get("filename"):
                        if ref.get("type") in (None, "output"):
                            candidates.append(ref)
            if any(str(c.get("filename", "")).lower().endswith(video_ext) for c in candidates):
                break
        ref = next(
            (c for c in candidates if str(c.get("filename", "")).lower().endswith(video_ext)),
            None,
        )
        if ref is None:
            raise UnitFailure("сегмент без видеофайла на выходе (нужна VHS_VideoCombine/SaveVideo)",
                              worker_fault=False)
        dest = unit.meta["out_file"]
        await wc.download_view(ref, dest)
        unit.files = [dest]

    async def _fetch_upscale(self, job, wc, unit, hist):
        outputs = (hist or {}).get("outputs", {})
        images = (outputs.get(SAVE_NODE_ID) or {}).get("images") or []
        images = [f for f in images if f.get("type") == "output"]
        if not images:
            raise UnitFailure("тайл без результата", worker_fault=True)
        dest = unit.meta["out_file"]
        await wc.download_view(images[0], dest)
        unit.files = [dest]
        if job.thread_results is not None:
            job.thread_results.put((unit.index, dest))

    # ------------------------------------------------------------------ cancel

    async def cancel(self, job_id):
        job = self.jobs.get(job_id)
        if job is None or job.done_event.is_set():
            return False
        job.cancelled = True
        try:
            while True:
                job.queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        for index, (wid, pid) in list(job.inflight.items()):
            try:
                await REGISTRY.client(wid).cancel_prompt(pid)
            except Exception:
                pass
        events.toast("info", f"GPU RAID: job «{job.label}» отменяется…")
        return True


def _extract_history_error(status):
    for msg in status.get("messages", []) or []:
        try:
            if msg[0] == "execution_error":
                d = msg[1]
                return f"{d.get('node_type', '?')}: {d.get('exception_message', '')}"
        except Exception:
            continue
    return status.get("status_str", "ошибка")


MANAGER = JobManager()
