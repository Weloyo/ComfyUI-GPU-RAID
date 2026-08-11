// Нода «Воркеры»: парк GPU, добавление, автостоп и задания — на канве.
//
// Раньше это жило в боковой панели; теперь панель — только секреты, а всё
// живое (кто в сети, что считается, кого погасить) — здесь, рядом с графом.
// Привязка конкретной модели к воркеру — не тут, а в нодах-лоадерах.
import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { gr, toast } from "./api.js";
import { el, esc, fmtGb, platformBadge, stateDot, stateText, workerError }
    from "./format.js";
import { RUNTIME_TYPE } from "./loaderui.js";

const MODES = [
    ["keep", "⚡ Держать", "воркеры не останавливаются после заданий"],
    ["eco", "🌙 Эко", "автостоп облачных воркеров после N минут простоя"],
    ["instant", "⏻ Сразу гасить", "останавливать облачных воркеров сразу после задания"],
    ["local_only", "🏠 Только локально", "облачные воркеры не используются"],
];

export class WorkersNodeUI {
    constructor(node, body) {
        this.node = node;
        this.box = body;
        this.workers = [];
        this.settings = {};
        this.jobs = new Map();
        this.parity = new Map();

        this.box.appendChild(el("div", { class: "gr-muted gr-node-hint" },
            "Выход «воркеры» справа — шина: подключите его ко всем нодам-лоадерам "
            + "(один и тот же провод), и в каждом лоадере откроется список "
            + "доступных воркеров."));
        this.modesBar = el("div", { class: "gr-modes" });
        this.box.appendChild(this.modesBar);

        this.elWorkers = el("div", { class: "gr-node-report" });
        this.box.appendChild(this.elWorkers);

        const add = el("div", { class: "gr-btns" });
        this.colabBtn = el("a", { class: "gr-btn", target: "_blank", href: "#",
            title: "откроется ноутбук — нажмите Run All; воркер подключится сам (gist)" },
            "▶ Colab-ноутбук");
        const kaggleBtn = el("button", { class: "gr-btn",
            title: "пуш batch-кернела через Kaggle API (нужны ключи в панели секретов)" },
            "▶ Kaggle-воркер");
        kaggleBtn.onclick = async () => {
            try {
                const r = await gr.post("/runtime/start", { assign: "platform:kaggle" });
                toast("success", "Kaggle-кернел запущен", r.kernel || r.detail || "");
            } catch (e) {
                toast(e.status === 409 ? "warn" : "error", "Kaggle не запущен", e.message, 9000);
            }
        };
        add.append(this.colabBtn, kaggleBtn);
        this.box.appendChild(add);
        this.rdStatus = el("div", { class: "gr-muted" });
        this.box.appendChild(this.rdStatus);

        this.ta = el("textarea", { class: "gr-textarea", rows: "2",
            placeholder: "gpuraid://TOKEN@xxx.trycloudflare.com\n(по строке на воркера — ручной запасной путь)" });
        const addBtn = el("button", { class: "gr-btn gr-primary" }, "Добавить воркеров");
        addBtn.onclick = () => this.addLines(addBtn);
        this.box.appendChild(this.ta);
        this.box.appendChild(addBtn);

        this.box.appendChild(el("div", { class: "gr-subtitle" }, "Задания"));
        this.elJobs = el("div", { class: "gr-node-report" });
        this.box.appendChild(this.elJobs);

        this._events = [];
        for (const name of ["worker", "unit", "job_started", "job_done"]) {
            const fn = (ev) => this.onEvent(name, ev.detail);
            api.addEventListener("gpuraid." + name, fn);
            this._events.push([name, fn]);
        }
        this._timer = setInterval(() => this.refresh(), 8000);
        this.ensureOutputs();
        this.refresh();
    }

    /** Один выход-шина «воркеры»: один и тот же провод тянется к каждому
     *  лоадеру и открывает там список доступных воркеров. Лишние выходы
     *  (старой схемы «по рантайму на выход») убираем, если они без проводов;
     *  занятые остаются и работают той же шиной. */
    ensureOutputs() {
        const node = this.node;
        let first = (node.outputs || []).findIndex((o) => o.type === RUNTIME_TYPE);
        if (first < 0) {
            node.addOutput("воркеры", RUNTIME_TYPE);
            first = node.outputs.length - 1;
        }
        node.outputs[first].name = "воркеры";
        node.outputs[first].label = "воркеры";
        for (let i = node.outputs.length - 1; i > first; i--) {
            const o = node.outputs[i];
            if (o.type === RUNTIME_TYPE && !(o.links || []).length) {
                node.removeOutput(i);
            }
        }
        if (node.properties) delete node.properties.gpuraid_outputs;
        node.setDirtyCanvas(true, true);
    }

    dispose() {
        clearInterval(this._timer);
        for (const [name, fn] of this._events) {
            api.removeEventListener("gpuraid." + name, fn);
        }
    }

    onConfigure() { this.refresh(); }

    // ------------------------------------------------------------- события

    onEvent(name, data) {
        if (name === "worker") {
            const w = this.workers.find((x) => x.id === data.id);
            if (w) { w.status = { ...w.status, ...data }; this.renderWorkers(); }
        } else if (name === "job_started") {
            this.jobs.set(data.job_id, data);
            this.renderJobs();
        } else if (name === "unit") {
            const job = this.jobs.get(data.job_id);
            if (job && job.units) {
                const u = job.units.find((x) => x.index === data.index);
                if (u) Object.assign(u, data);
                job.done = job.units.filter((x) => x.state === "DONE").length;
                this.renderJobs();
            }
        } else if (name === "job_done") {
            const job = this.jobs.get(data.job_id);
            if (job) Object.assign(job, data, { finished: true });
            setTimeout(() => { this.jobs.delete(data.job_id); this.renderJobs(); }, 15000);
            this.renderJobs();
        }
    }

    // ------------------------------------------------------------- данные

    async refresh() {
        try {
            const [w, j, lc] = await Promise.all([
                gr.get("/workers"), gr.get("/jobs"), gr.get("/lifecycle"),
            ]);
            this.workers = w.workers || [];
            this.settings = w.settings || {};
            this.lifecycle = lc;
            const activeIds = new Set((j.active || []).map((s) => s.job_id));
            for (const snap of j.active || []) this.jobs.set(snap.job_id, snap);
            for (const [jobId, job] of this.jobs) {
                if (activeIds.has(jobId) || job.finished) continue;
                job.finished = true;   // job_done потерялся при реконнекте WS
                setTimeout(() => { this.jobs.delete(jobId); this.renderJobs(); }, 15000);
            }
            this.ensureOutputs();
            this.renderModes();
            this.renderWorkers();
            this.renderJobs();
            this.renderRendezvous();
        } catch (e) { /* сервер занят/рестартует */ }
    }

    // ------------------------------------------------------------- режимы

    renderModes() {
        const bar = this.modesBar;
        bar.innerHTML = "";
        const lc = this.settings.lifecycle || {};
        for (const [key, title, hint] of MODES) {
            const b = el("button", {
                class: "gr-btn gr-small gr-mode" + (lc.policy === key ? " gr-mode-on" : ""),
                title: hint + " (глобально; сценарий конкретной привязки — в ноде-лоадере)",
            }, esc(title));
            b.onclick = async () => {
                try { await gr.patch("/settings", { lifecycle: { policy: key } }); }
                catch (e) { toast("error", "Ошибка", e.message); }
                this.refresh();
            };
            bar.appendChild(b);
        }
        if (lc.policy === "eco") {
            const min = el("input", { class: "gr-input gr-tiny", type: "number",
                min: "1", max: "180", value: String(lc.idle_stop_min ?? 10),
                title: "минут простоя до автостопа" });
            min.onchange = async () => {
                try {
                    await gr.patch("/settings", { lifecycle: {
                        idle_stop_min: parseInt(min.value || "10", 10),
                    } });
                } catch (e) { /* ignore */ }
            };
            bar.appendChild(min);
        }
    }

    // ------------------------------------------------------------- воркеры

    renderWorkers() {
        const box = this.elWorkers;
        box.innerHTML = "";
        if (!this.workers.length) {
            box.appendChild(el("div", { class: "gr-muted" }, "нет данных"));
            return;
        }
        for (const w of this.workers) box.appendChild(this.workerRow(w));
    }

    workerRow(w) {
        const st = w.status || {};
        const row = el("div", { class: "gr-worker" + (w.enabled ? "" : " gr-worker-off") });
        const head = el("div", { class: "gr-worker-head" });
        head.appendChild(el("span", {
            class: `gr-dot ${w.enabled ? stateDot(st.state) : "gr-dot-gray"}`,
            title: w.enabled
                ? (st.state === "stopped" ? "остановлен (lifecycle)" : (st.state || ""))
                : "выключен: задания не получает",
        }));
        const twins = this.workers.filter((x) => x.name === w.name).length > 1;
        head.appendChild(el("span", { class: "gr-name", title: w.url },
            esc(w.name) + (twins && w.session ? ` · ${esc(String(w.session).slice(0, 6))}` : "")));
        head.appendChild(el("span", { class: "gr-badge" }, esc(stateText(st.state, w.enabled))));
        const badge = platformBadge(w.platform || st.platform || (w.kind === "cloud" ? "generic" : ""));
        if (badge) head.appendChild(el("span", { class: "gr-badge" }, esc(badge)));
        const gpu = st.gpu ? `${st.gpu} · ${fmtGb(st.vram_total_gb)}` : "";
        head.appendChild(el("span", { class: "gr-muted gr-grow" },
            esc(gpu) + (st.latency_ms != null && st.state === "online" ? ` · ${st.latency_ms}мс` : "")));
        const chip = this.parity.get(w.id);
        if (chip) head.appendChild(el("span", { class: `gr-chip gr-chip-${chip.level}`,
            title: (chip.notes || []).join("\n") }, chip.level === "green" ? "готов" : chip.level));
        row.appendChild(head);

        const problem = workerError(st.error, st.state);
        if (problem) row.appendChild(el("div", { class: "gr-muted" }, `↳ ${esc(problem)}`));
        if (st.state === "stopped") {
            row.appendChild(el("div", { class: "gr-muted" },
                "↳ остановлен командой мастера. Поднять — кнопкой ▶ в ноде-лоадере "
                + "(Kaggle) или заново запустив ноутбук (Colab)."));
        }

        const btns = el("div", { class: "gr-btns" });
        const toggle = el("button", { class: "gr-btn gr-small" }, w.enabled ? "Выкл" : "Вкл");
        toggle.onclick = () => this.patchWorker(w.id, { enabled: !w.enabled });
        btns.appendChild(toggle);
        const check = el("button", { class: "gr-btn gr-small",
            title: "сверить ноды и модели воркера с текущим workflow" }, "Проверить");
        check.onclick = () => this.checkWorker(w.id);
        btns.appendChild(check);
        if (w.id !== "local") {
            const pin = el("button", {
                class: "gr-btn gr-small" + (w.pinned ? " gr-mode-on" : ""),
                title: "закреплён: автостоп не трогает этого воркера",
            }, "📌");
            pin.onclick = () => this.patchWorker(w.id, { pinned: !w.pinned });
            btns.appendChild(pin);
            if (w.kind === "cloud" && st.state === "online") {
                const stop = el("button", { class: "gr-btn gr-small gr-danger",
                    title: "остановить рантайм (квота перестанет тратиться)" }, "⏻");
                stop.onclick = async () => {
                    if (!confirm(`Остановить рантайм воркера «${w.name}»?`)) return;
                    try {
                        const r = await gr.post(`/workers/${w.id}/stop`);
                        if (!r.stopped) toast("warn", "Воркер не остановлен — смотрите тосты");
                        this.refresh();
                    } catch (e) { toast("error", "Не остановлен", e.message); }
                };
                btns.appendChild(stop);
            }
            const edit = el("button", { class: "gr-btn gr-small" }, "URL");
            edit.onclick = async () => {
                const url = prompt("Новый URL воркера (токен и remap сохранятся):", w.url);
                if (url) this.patchWorker(w.id, { url: url.trim() });
            };
            btns.appendChild(edit);
            const del = el("button", { class: "gr-btn gr-small gr-danger" }, "✕");
            del.onclick = async () => {
                if (confirm(`Удалить воркера «${w.name}»?`)) {
                    await gr.del(`/workers/${w.id}`);
                    this.refresh();
                }
            };
            btns.appendChild(del);
        }
        row.appendChild(btns);

        const report = this.parity.get(w.id);
        if (report && report.level !== "green") row.appendChild(this.parityDetails(w, report));
        return row;
    }

    parityDetails(w, report) {
        const box = el("div", { class: "gr-parity" });
        for (const cls of report.missing_classes || []) {
            box.appendChild(el("div", { class: "gr-muted" }, `✕ нода: ${esc(cls)}`));
        }
        for (const [folder, names] of Object.entries(report.missing_models || {})) {
            for (const name of names) {
                const line = el("div", { class: "gr-parity-line" });
                line.appendChild(el("span", {}, `✕ ${esc(folder)}/${esc(name)}`));
                for (const cand of (report.suggestions || {})[name] || []) {
                    const b = el("button", { class: "gr-btn gr-small", title: "записать remap" },
                        `→ ${esc(cand)}`);
                    b.onclick = () => this.patchWorker(w.id,
                        { add_remap: { folder, master: name, worker: cand } })
                        .then(() => this.checkWorker(w.id));
                    line.appendChild(b);
                }
                const manual = el("button", { class: "gr-btn gr-small",
                    title: "указать имя файла на воркере вручную" }, "→ вручную…");
                manual.onclick = () => {
                    const target = prompt(
                        `Имя модели НА ВОРКЕРЕ вместо «${name}» (папка ${folder}):`, name);
                    if (target && target.trim()) this.patchWorker(w.id,
                        { add_remap: { folder, master: name, worker: target.trim() } })
                        .then(() => this.checkWorker(w.id));
                };
                line.appendChild(manual);
                box.appendChild(line);
            }
        }
        if (Object.keys(report.missing_models || {}).length) {
            box.appendChild(el("div", { class: "gr-muted" },
                "скачать недостающее — кнопкой «Скачать» в ноде-лоадере этой модели "
                + "или нодой «Модели на воркерах»"));
        }
        return box;
    }

    async patchWorker(id, patch) {
        try { await gr.patch(`/workers/${id}`, patch); }
        catch (e) { toast("error", "Ошибка", e.message); }
        return this.refresh();
    }

    async checkWorker(id) {
        try {
            const p = await app.graphToPrompt();
            const report = await gr.post(`/workers/${id}/check`, { graph: p.output });
            this.parity.set(id, report);
            this.renderWorkers();
            if (report.level === "green") toast("success", "Воркер готов к текущему workflow");
        } catch (e) { toast("error", "Проверка не удалась", e.message); }
    }

    async addLines(btn) {
        btn.disabled = true;
        try {
            const r = await gr.post("/workers", { connection_strings: this.ta.value });
            if (r.added?.length) toast("success", `Добавлено воркеров: ${r.added.length}`);
            for (const err of r.errors || []) toast("warn", "Строка не разобрана", err);
            this.ta.value = "";
            this.refresh();
        } catch (e) {
            toast("error", "Не удалось добавить", e.message);
        } finally { btn.disabled = false; }
    }

    renderRendezvous() {
        const lc = this.lifecycle || {};
        const rd = lc.rendezvous || {};
        if (lc.colab_notebook_url) this.colabBtn.href = lc.colab_notebook_url;
        if (!rd.configured) {
            this.rdStatus.textContent =
                "автоподключение не настроено: панель → «Подключения и ключи» → GitHub";
            return;
        }
        const ago = rd.last_poll_ts ? Math.round(Date.now() / 1000 - rd.last_poll_ts) : null;
        this.rdStatus.textContent = "rendezvous активен"
            + (ago != null ? ` · gist опрошен ${ago}с назад` : " · жду первого опроса")
            + (rd.last_error ? ` · ⚠ ${rd.last_error}` : "");
    }

    // ------------------------------------------------------------- задания

    renderJobs() {
        const box = this.elJobs;
        box.innerHTML = "";
        const active = [...this.jobs.values()];
        if (!active.length) {
            box.appendChild(el("div", { class: "gr-muted" }, "нет активных заданий"));
            return;
        }
        for (const job of active) {
            const row = el("div", { class: "gr-job" });
            const total = job.total || (job.units ? job.units.length : 0) || 1;
            const done = job.done || 0;
            row.appendChild(el("div", { class: "gr-job-head" },
                `<b>${esc(job.label || job.job_id)}</b> <span class="gr-muted">${esc(job.kind)} · ${esc(job.state)}</span>`));
            const bar = el("div", { class: "gr-bar" });
            let frac = done / total;
            if (job.units) {
                let partial = 0;
                for (const u of job.units) {
                    if (u.state === "RUNNING" && u.progress && u.progress[1] > 0)
                        partial += u.progress[0] / u.progress[1];
                }
                frac = Math.min(1, (done + partial) / total);
            }
            bar.appendChild(el("div", { class: "gr-bar-fill", style: `width:${Math.round(frac * 100)}%` }));
            row.appendChild(bar);
            for (const u of job.units || []) {
                if (u.state === "QUEUED" || u.state === "DONE") continue;
                const pct = u.progress && u.progress[1] ? Math.round(u.progress[0] / u.progress[1] * 100) : 0;
                row.appendChild(el("div", { class: "gr-muted gr-unit" },
                    `#${u.index} · ${esc(u.worker_id || "?")} · ${esc(u.state)}${pct ? ` · ${pct}%` : ""}`
                    + (u.error ? ` · <span class="gr-err">${esc(u.error)}</span>` : "")));
            }
            if (!job.finished) {
                const cancel = el("button", { class: "gr-btn gr-small gr-danger" }, "Отменить");
                cancel.onclick = () => gr.post(`/jobs/${job.job_id}/cancel`).catch(() => {});
                row.appendChild(cancel);
            }
            box.appendChild(row);
        }
    }
}
