// Sidebar-панель GPU RAID: воркеры, режимы, глобальные настройки и мониторинг.
//
// Всё, что касается генерации (промпты, ключевые кадры, сегменты, запуск
// offload/конвейера), живёт на канве в нодах — см. lib/nodeui.js и lib/editor.js.
// Здесь остаётся только то, что применяется ко всему workflow целиком.
import { app } from "../../../scripts/app.js";
import { gr, toast } from "./api.js";
import { el, esc, fmtDur, fmtGb, platformBadge, stateDot } from "./format.js";
import { openProjectOnCanvas } from "./nodeui.js";
import { ConnectionsUI } from "./connections.js";

export class GPURaidPanel {
    constructor(root) {
        this.root = root;
        this.workers = [];
        this.settings = {};
        this.secretsView = {};
        this.jobs = new Map();       // job_id -> snapshot
        this.history = [];
        this.projects = [];
        this.parity = new Map();     // worker_id -> report
        this._timer = null;
        this.build();
        this.refreshAll();
        this._timer = setInterval(() => this.refreshAll(), 8000);
    }

    dispose() {
        if (this._timer) clearInterval(this._timer);
        this.root.innerHTML = "";
    }

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
            this.history.unshift(data);
            this.history = this.history.slice(0, 20);
            setTimeout(() => { this.jobs.delete(data.job_id); this.renderJobs(); }, 15000);
            this.renderJobs();
            this.renderHistory();
        } else if (name === "longvideo") {
            // сам проект перерисует нода на канве, панели хватит списка
            this.refreshProjects();
        }
    }

    // ------------------------------------------------------------- данные

    async refreshAll() {
        try {
            const [w, j, s] = await Promise.all([
                gr.get("/workers"), gr.get("/jobs"), gr.get("/settings"),
            ]);
            this.workers = w.workers || [];
            this.settings = s.settings || w.settings || {};
            this.secretsView = s.secrets || {};
            for (const snap of j.active || []) this.jobs.set(snap.job_id, snap);
            this.history = j.history || this.history;
            this.renderModes();
            this.renderWorkers();
            this.renderJobs();
            this.renderHistory();
        } catch (e) { /* сервер занят/рестартует */ }
        try {
            this.lifecycle = await gr.get("/lifecycle");
            this.renderLifecycle();
        } catch (e) { /* ignore */ }
        this.refreshProjects();
    }

    async refreshSettings() {
        try {
            const s = await gr.get("/settings");
            this.settings = s.settings || this.settings;
            this.secretsView = s.secrets || {};
            this.renderModes();
        } catch (e) { /* ignore */ }
    }

    async refreshProjects() {
        try {
            const p = await gr.get("/longvideo");
            this.projects = p.projects || [];
            this.renderProjects();
        } catch (e) { /* ignore */ }
    }

    // ------------------------------------------------------------- каркас

    build() {
        this.root.classList.add("gr-panel");
        this.root.innerHTML = "";
        this._summaries = {};
        const mk = (id, title, open = true) => {
            const box = el("details", { class: "gr-section", ...(open ? { open: "" } : {}) });
            const sum = el("summary", {}, title);
            box.appendChild(sum);
            this._summaries[id] = sum;
            const body = el("div", { class: "gr-body", id: `gr-${id}` });
            box.appendChild(body);
            this.root.appendChild(box);
            return body;
        };
        this.elModes = mk("modes", "Режимы");
        this.elWorkers = mk("workers", "Воркеры");
        this.elAdd = mk("add", "Добавить воркеров", false);
        this.elConn = mk("connections", "Подключения и ключи", false);
        this.elJobs = mk("jobs", "Задания");
        this.elProjects = mk("projects", "Проекты видео", false);
        this.elHistory = mk("history", "История", false);
        this.buildModes();
        this.buildAdd();
        this.connections = new ConnectionsUI(this.elConn);
        this.connections.onSummary = (ok, total) => {
            this._summaries.connections.textContent =
                `Подключения и ключи — ${ok}/${total}`;
        };
    }

    row(label, ...controls) {
        const r = el("div", { class: "gr-row" });
        r.appendChild(el("span", { class: "gr-label" }, esc(label)));
        for (const c of controls) r.appendChild(c);
        return r;
    }

    // ------------------------------------------------------------- режимы

    buildModes() {
        const box = this.elModes;
        box.innerHTML = "";
        this.modesBar = el("div", { class: "gr-modes" });
        box.appendChild(this.modesBar);
        this.modesHint = el("div", { class: "gr-muted" });
        box.appendChild(this.modesHint);

        box.appendChild(el("div", { class: "gr-subtitle" }, "Сценарии"));
        const presets = el("div", { class: "gr-modes" });
        const CARDS = [
            ["story_minimax_h3", "♾️ Бесконечное видео",
             "Сценарист на MiniMax H3: сюжет → кадры → сегменты → видео любой длины. "
             + "Вся работа — в ноде «Сценарист» на канве"],
            ["pipeline_minimax_h3", "🐘 Большая модель",
             "H3 по частям на нескольких GPU: загрузите пример и жмите «Проанализировать» "
             + "в ноде «Конвейер»"],
        ];
        for (const [name, title, hint] of CARDS) {
            const b = el("button", { class: "gr-btn gr-mode", title: hint }, esc(title));
            b.onclick = async () => {
                if (!confirm(`Загрузить пример «${title}»? Текущий workflow на канве будет заменён.`)) return;
                try {
                    const r = await gr.get(`/example/${name}`);
                    await app.loadGraphData(r.workflow);
                    toast("success", "Пример загружен", hint);
                } catch (e) { toast("error", "Пример не загружен", e.message); }
            };
            presets.appendChild(b);
        }
        box.appendChild(presets);
        box.appendChild(el("div", { class: "gr-muted" },
            "Ключи LLM, GitHub, Kaggle, HF и Civitai — в разделе "
            + "«Подключения и ключи»: там же ссылки на страницы, где они берутся, "
            + "и проверка одной кнопкой."));
    }

    renderModes() {
        if (!this.modesBar) return;
        const bar = this.modesBar;
        bar.innerHTML = "";
        const lc = this.settings.lifecycle || {};
        const MODES = [
            ["keep", "⚡ Держать", "воркеры не останавливаются после заданий"],
            ["eco", "🌙 Эко", "автостоп облачных воркеров после N минут простоя"],
            ["instant", "⏻ Сразу гасить", "останавливать облачных воркеров сразу после задания"],
            ["local_only", "🏠 Только локально", "облачные воркеры не используются"],
        ];
        for (const [key, title, hint] of MODES) {
            const b = el("button", {
                class: "gr-btn gr-mode" + (lc.policy === key ? " gr-mode-on" : ""),
                title: hint,
            }, esc(title));
            b.onclick = async () => {
                try { await gr.patch("/settings", { lifecycle: { policy: key } }); }
                catch (e) { toast("error", "Ошибка", e.message); }
                this.refreshSettings();
            };
            bar.appendChild(b);
        }
        if (lc.policy === "eco") {
            const min = el("input", { class: "gr-input gr-tiny", type: "number", min: "1",
                max: "180", value: String(lc.idle_stop_min ?? 10),
                title: "минут простоя до автостопа" });
            min.onchange = async () => {
                try {
                    await gr.patch("/settings", { lifecycle: {
                        idle_stop_min: parseInt(min.value || "10", 10),
                    } });
                } catch (e) { /* ignore */ }
            };
            bar.appendChild(min);
            bar.appendChild(el("span", { class: "gr-muted" }, "мин"));
        }
        const cur = MODES.find((m) => m[0] === lc.policy);
        this.modesHint.textContent = cur ? cur[2] : "";
    }

    // ------------------------------------------------------------- воркеры

    renderWorkers() {
        const box = this.elWorkers;
        box.innerHTML = "";
        if (!this.workers.length) box.appendChild(el("div", { class: "gr-muted" }, "нет данных"));
        for (const w of this.workers) {
            const st = w.status || {};
            const row = el("div", { class: "gr-worker" });
            const head = el("div", { class: "gr-worker-head" });
            head.appendChild(el("span", { class: `gr-dot ${stateDot(st.state)}`,
                title: st.state === "stopped" ? "остановлен (lifecycle)" : (st.state || "") }));
            head.appendChild(el("span", { class: "gr-name", title: w.url }, esc(w.name)));
            const badge = platformBadge(w.platform || st.platform ||
                (w.kind === "cloud" ? "generic" : ""));
            if (badge) head.appendChild(el("span", { class: "gr-badge" }, esc(badge)));
            const gpu = st.gpu ? `${st.gpu} · ${fmtGb(st.vram_total_gb)}` : (st.error ? esc(st.error) : "");
            head.appendChild(el("span", { class: "gr-muted gr-grow" },
                esc(gpu) + (st.latency_ms != null ? ` · ${st.latency_ms}мс` : "")));
            const chip = this.parity.get(w.id);
            if (chip) head.appendChild(el("span", { class: `gr-chip gr-chip-${chip.level}`,
                title: (chip.notes || []).join("\n") }, chip.level === "green" ? "готов" : chip.level));
            row.appendChild(head);

            const btns = el("div", { class: "gr-btns" });
            const toggle = el("button", { class: "gr-btn" }, w.enabled ? "Выкл" : "Вкл");
            toggle.onclick = () => this.patchWorker(w.id, { enabled: !w.enabled });
            btns.appendChild(toggle);
            const check = el("button", { class: "gr-btn",
                title: "сверить ноды и модели воркера с текущим workflow на канве" }, "Проверить");
            check.onclick = () => this.checkWorker(w.id);
            btns.appendChild(check);
            if (w.id !== "local") {
                const pin = el("button", {
                    class: "gr-btn" + (w.pinned ? " gr-mode-on" : ""),
                    title: "закреплён: автостоп жизненного цикла не трогает этого воркера",
                }, "📌");
                pin.onclick = () => this.patchWorker(w.id, { pinned: !w.pinned });
                btns.appendChild(pin);
                if (w.kind === "cloud" && st.state === "online") {
                    const stop = el("button", { class: "gr-btn gr-danger",
                        title: "остановить рантайм воркера (квота перестанет тратиться)" }, "⏻");
                    stop.onclick = async () => {
                        if (!confirm(`Остановить рантайм воркера «${w.name}»?`)) return;
                        try {
                            const r = await gr.post(`/workers/${w.id}/stop`);
                            if (!r.stopped) toast("warn", "Воркер не остановлен — смотрите тосты");
                            this.refreshAll();
                        } catch (e) { toast("error", "Не остановлен", e.message); }
                    };
                    btns.appendChild(stop);
                }
                const edit = el("button", { class: "gr-btn" }, "URL");
                edit.onclick = async () => {
                    const url = prompt("Новый URL воркера (токен и remap сохранятся):", w.url);
                    if (url) this.patchWorker(w.id, { url: url.trim() });
                };
                btns.appendChild(edit);
                const del = el("button", { class: "gr-btn gr-danger" }, "✕");
                del.onclick = async () => {
                    if (confirm(`Удалить воркера «${w.name}»?`)) {
                        await gr.del(`/workers/${w.id}`); this.refreshAll();
                    }
                };
                btns.appendChild(del);
            }
            row.appendChild(btns);

            const report = this.parity.get(w.id);
            if (report && (report.level !== "green")) row.appendChild(this.parityDetails(w, report));
            box.appendChild(row);
        }
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
                    const b = el("button", { class: "gr-btn gr-small", title: "записать remap" }, `→ ${esc(cand)}`);
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
                const dl = el("button", { class: "gr-btn gr-small" }, "Скачать на воркера…");
                dl.onclick = () => this.downloadToWorker(w.id, folder, name);
                line.appendChild(dl);
                box.appendChild(line);
            }
        }
        return box;
    }

    async downloadToWorker(wid, folder, name) {
        let url = "";
        try {
            const cat = await gr.get("/catalog");
            const hit = (cat.catalog || []).find((c) => c.filename === name);
            url = hit ? hit.url : "";
        } catch (e) { /* ignore */ }
        url = prompt(`URL для скачивания ${name} на воркера (HF/Civitai):`, url);
        if (!url) return;
        try {
            const r = await gr.post(`/workers/${wid}/download_model`,
                { folder, url: url.trim(), filename: name });
            toast("info", "Загрузка запущена", name);
            this.pollDownload(wid, r.task_id, name);
        } catch (e) { toast("error", "Не удалось запустить загрузку", e.message); }
    }

    async pollDownload(wid, taskId, name) {
        const tick = async () => {
            try {
                const st = await gr.get(`/workers/${wid}/download_status/${taskId}`);
                if (st.state === "done") {
                    toast("success", `Модель скачана: ${name}`);
                    this.checkWorker(wid);
                    return;
                }
                if (st.state === "error") { toast("error", `Загрузка ${name}: ${st.error}`); return; }
                const pct = st.bytes_total ? Math.round(st.bytes_done / st.bytes_total * 100) : 0;
                toast("info", `Загрузка ${name}: ${pct}%`, "", 2500);
                setTimeout(tick, 5000);
            } catch (e) { /* stop */ }
        };
        setTimeout(tick, 4000);
    }

    async patchWorker(id, patch) {
        try { await gr.patch(`/workers/${id}`, patch); } catch (e) { toast("error", "Ошибка", e.message); }
        return this.refreshAll();
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

    buildAdd() {
        const box = this.elAdd;
        const auto = el("div", { class: "gr-btns" });
        this.colabBtn = el("a", { class: "gr-btn", target: "_blank", href: "#",
            title: "откроется ноутбук — нажмите Run All; дальше воркер подключится сам (gist)" },
            "▶ Открыть Colab-ноутбук");
        const kaggleBtn = el("button", { class: "gr-btn",
            title: "пуш batch-кернела через Kaggle API; нужен kaggle.json и настроенный gist" },
            "▶ Запустить Kaggle-воркера");
        kaggleBtn.onclick = async () => {
            const preset = prompt("Пресет моделей для Kaggle (none | sdxl | minimax_h3):", "none");
            if (preset === null) return;
            try {
                const r = await gr.post("/kaggle/start", { model_preset: (preset || "none").trim() });
                toast("success", "Kaggle-кернел запущен", r.kernel);
            } catch (e) {
                toast("error", "Kaggle не запущен",
                    `${e.message} — проверьте раздел «Подключения и ключи»`, 9000);
            }
        };
        auto.append(this.colabBtn, kaggleBtn);
        box.appendChild(auto);
        this.rdStatus = el("div", { class: "gr-muted" });
        box.appendChild(this.rdStatus);

        const ta = el("textarea", { class: "gr-textarea", rows: "3",
            placeholder: "gpuraid://TOKEN@xxx.trycloudflare.com\n(по строке на воркера — ручной запасной путь)" });
        const btn = el("button", { class: "gr-btn gr-primary" }, "Добавить");
        btn.onclick = async () => {
            try {
                const r = await gr.post("/workers", { connection_strings: ta.value });
                if (r.added?.length) toast("success", `Добавлено воркеров: ${r.added.length}`);
                for (const err of r.errors || []) toast("warn", "Строка не разобрана", err);
                ta.value = "";
                this.refreshAll();
            } catch (e) { toast("error", "Не удалось добавить", e.message); }
        };
        box.appendChild(ta);
        box.appendChild(btn);
    }

    renderLifecycle() {
        if (!this.rdStatus) return;
        const lc = this.lifecycle || {};
        const rd = lc.rendezvous || {};
        if (this.colabBtn && lc.colab_notebook_url) this.colabBtn.href = lc.colab_notebook_url;
        if (!rd.configured) {
            this.rdStatus.textContent =
                "автоподключение не настроено: раздел «Подключения и ключи» → GitHub";
            return;
        }
        const ago = rd.last_poll_ts ? Math.round(Date.now() / 1000 - rd.last_poll_ts) : null;
        this.rdStatus.textContent = "rendezvous активен" +
            (ago != null ? ` · gist опрошен ${ago}с назад` : " · жду первого опроса") +
            (rd.last_error ? ` · ⚠ ${rd.last_error}` : "");
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
            row.appendChild(el("div", { class: "gr-muted" }, `${done}/${total} юнитов`));
            if (job.units) {
                for (const u of job.units) {
                    if (u.state === "QUEUED" || u.state === "DONE") continue;
                    const pct = u.progress && u.progress[1] ? Math.round(u.progress[0] / u.progress[1] * 100) : 0;
                    row.appendChild(el("div", { class: "gr-muted gr-unit" },
                        `#${u.index} · ${esc(u.worker_id || "?")} · ${esc(u.state)}${pct ? ` · ${pct}%` : ""}` +
                        (u.error ? ` · <span class="gr-err">${esc(u.error)}</span>` : "")));
                }
            }
            if (!job.finished) {
                const cancel = el("button", { class: "gr-btn gr-danger" }, "Отменить");
                cancel.onclick = () => gr.post(`/jobs/${job.job_id}/cancel`).catch(() => {});
                row.appendChild(cancel);
            }
            box.appendChild(row);
        }
    }

    renderHistory() {
        const box = this.elHistory;
        box.innerHTML = "";
        if (!this.history.length) { box.appendChild(el("div", { class: "gr-muted" }, "пусто")); return; }
        for (const h of this.history.slice(0, 10)) {
            const per = Object.entries(h.per_worker || {}).map(([k, v]) => `${k}:${v}`).join(" ");
            box.appendChild(el("div", { class: "gr-hist" },
                `<b>${esc(h.label || h.job_id)}</b> · ${esc(h.kind)} · ${esc(h.state)} · ` +
                `${h.done}/${h.total} за ${fmtDur(h.wall_s)} <span class="gr-muted">${esc(per)}</span>`));
        }
    }

    // ------------------------------------------------------------- проекты

    renderProjects() {
        const box = this.elProjects;
        if (!box) return;
        box.innerHTML = "";
        box.appendChild(el("div", { class: "gr-muted" },
            "Раскадровка, промпты и рендер — в нодах «Сценарист» и «Длинное видео» на канве. "
            + "Здесь только список того, что уже лежит в output/gpuraid/."));
        if (!this.projects.length) {
            box.appendChild(el("div", { class: "gr-muted" }, "проектов пока нет"));
            return;
        }
        for (const p of this.projects) {
            const row = el("div", { class: "gr-proj" });
            row.appendChild(el("span", { class: "gr-grow" },
                `<b>${esc(p.label)}</b> <span class="gr-muted">${esc(p.mode)} · ${esc(p.state)} · ${p.done}/${p.segments}</span>`));
            const open = el("button", { class: "gr-btn gr-small gr-primary",
                title: "привязать проект к ноде на канве и перейти к ней" }, "Открыть на канве");
            open.onclick = () => openProjectOnCanvas(p.label, p.mode);
            row.appendChild(open);
            box.appendChild(row);
        }
    }
}
