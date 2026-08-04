// Sidebar-панель GPU RAID: воркеры, задания, offload, Long Video, история.
import { app } from "../../../scripts/app.js";
import { gr, toast, viewURL, clientId } from "./api.js";
import { el, esc, fmtDur, fmtGb, stateDot } from "./format.js";

export class GPURaidPanel {
    constructor(root) {
        this.root = root;
        this.workers = [];
        this.settings = {};
        this.jobs = new Map();       // job_id -> snapshot
        this.history = [];
        this.projects = [];
        this.parity = new Map();     // worker_id -> report
        this.openProject = null;     // manifest
        this.edit = { order: [], excluded: new Set(), trims: {} };
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
            this.refreshProjects();
            if (this.openProject && data.label === this.openProject.label && data.manifest) {
                this.openProject = { ...this.openProject, ...data.manifest };
                this.renderEditor();
            }
        }
    }

    // ------------------------------------------------------------- данные

    async refreshAll() {
        try {
            const [w, j] = await Promise.all([gr.get("/workers"), gr.get("/jobs")]);
            this.workers = w.workers || [];
            this.settings = w.settings || {};
            for (const snap of j.active || []) this.jobs.set(snap.job_id, snap);
            this.history = j.history || this.history;
            this.renderWorkers();
            this.renderJobs();
            this.renderHistory();
            this.renderOffload();
        } catch (e) { /* сервер занят/рестартует */ }
        this.refreshProjects();
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
        const mk = (id, title, open = true) => {
            const box = el("details", { class: "gr-section", ...(open ? { open: "" } : {}) });
            box.appendChild(el("summary", {}, title));
            const body = el("div", { class: "gr-body", id: `gr-${id}` });
            box.appendChild(body);
            this.root.appendChild(box);
            return body;
        };
        this.elWorkers = mk("workers", "Воркеры");
        this.elAdd = mk("add", "Добавить воркеров", false);
        this.elJobs = mk("jobs", "Задания");
        this.elOffload = mk("offload", "Offload: весь workflow на воркера", false);
        this.elLV = mk("lv", "Long Video", false);
        this.elHistory = mk("history", "История", false);
        this.buildAdd();
        this.buildLV();
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
            head.appendChild(el("span", { class: `gr-dot ${stateDot(st.state)}` }));
            head.appendChild(el("span", { class: "gr-name", title: w.url }, esc(w.name)));
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
            const check = el("button", { class: "gr-btn" }, "Проверить");
            check.onclick = () => this.checkWorker(w.id);
            btns.appendChild(check);
            if (w.id !== "local") {
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
        const ta = el("textarea", { class: "gr-textarea", rows: "3",
            placeholder: "gpuraid://TOKEN@xxx.trycloudflare.com\n(по строке на воркера — строки печатает ноутбук)" });
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

    // ------------------------------------------------------------- offload

    renderOffload() {
        const box = this.elOffload;
        box.innerHTML = "";
        const sel = el("select", { class: "gr-select" });
        for (const w of this.workers) {
            if (!w.enabled || w.id === "local") continue;
            const opt = el("option", { value: w.id },
                esc(`${w.name} (${w.status?.gpu || w.status?.state || "?"})`));
            sel.appendChild(opt);
        }
        if (!sel.children.length) {
            box.appendChild(el("div", { class: "gr-muted" }, "нет включённых удалённых воркеров"));
            return;
        }
        const label = el("input", { class: "gr-input", placeholder: "имя задания", value: "video" });
        const btn = el("button", { class: "gr-btn gr-primary" }, "Запустить текущий workflow");
        btn.onclick = async () => {
            try {
                const p = await app.graphToPrompt();
                const r = await gr.post("/offload", {
                    graph: p.output, workflow_ui: p.workflow,
                    worker_id: sel.value, label: label.value, client_id: clientId(),
                });
                toast("info", "Offload запущен", (r.warnings || []).join("; "));
            } catch (e) { toast("error", "Offload не запущен", e.message); }
        };
        box.appendChild(sel);
        box.appendChild(label);
        box.appendChild(btn);
        box.appendChild(el("div", { class: "gr-muted" },
            "Задание уйдёт целиком на выбранный воркер; результаты вернутся в output/gpuraid/…"));
    }

    // ------------------------------------------------------------- long video

    buildLV() {
        const box = this.elLV;
        box.innerHTML = "";
        const form = el("div", { class: "gr-lvform" });
        this.lvMode = el("select", { class: "gr-select" });
        this.lvMode.appendChild(el("option", { value: "chain" }, "chain — продолжение (любая длина)"));
        this.lvMode.appendChild(el("option", { value: "keyframes" }, "keyframes — параллельно (FLF2V)"));
        this.lvLabel = el("input", { class: "gr-input", placeholder: "имя проекта", value: "myvideo" });
        this.lvCount = el("input", { class: "gr-input", type: "number", min: "1", max: "999", value: "4",
            title: "сегментов (chain)" });
        this.lvSeed = el("input", { class: "gr-input", type: "number", min: "0", value: "0", title: "seed" });
        this.lvPolicy = el("select", { class: "gr-select" });
        for (const p of ["increment", "fixed", "random"]) this.lvPolicy.appendChild(el("option", { value: p }, p));
        this.lvPrompts = el("textarea", { class: "gr-textarea", rows: "3",
            placeholder: "промпты сегментов — по строке (пусто = из workflow)" });
        this.lvKeys = el("textarea", { class: "gr-textarea", rows: "2",
            placeholder: "keyframes: имена файлов из input, по строке (мин. 2)" });
        this.lvFade = el("input", { class: "gr-input", type: "number", min: "0", step: "0.1", value: "0",
            title: "кроссфейд, сек" });
        const run = el("button", { class: "gr-btn gr-primary" }, "Собрать длинное видео");
        run.onclick = () => this.startLV();

        form.appendChild(this.row("Режим", this.lvMode));
        form.appendChild(this.row("Проект", this.lvLabel));
        form.appendChild(this.row("Сегментов", this.lvCount));
        form.appendChild(this.row("Seed / политика", this.lvSeed, this.lvPolicy));
        form.appendChild(this.lvPrompts);
        form.appendChild(this.lvKeys);
        form.appendChild(this.row("Кроссфейд, с", this.lvFade, run));
        form.appendChild(el("div", { class: "gr-muted" },
            "Шаблон = текущий workflow. Пометьте ноды заголовками GPURAID:START_IMAGE, " +
            "GPURAID:END_IMAGE (для keyframes), GPURAID:PROMPT, GPURAID:VIDEO_OUT."));
        box.appendChild(form);
        this.elProjects = el("div", {});
        box.appendChild(this.elProjects);
        this.elEditor = el("div", {});
        box.appendChild(this.elEditor);
    }

    row(label, ...controls) {
        const r = el("div", { class: "gr-row" });
        r.appendChild(el("span", { class: "gr-label" }, esc(label)));
        for (const c of controls) r.appendChild(c);
        return r;
    }

    async startLV() {
        try {
            const p = await app.graphToPrompt();
            const params = {
                mode: this.lvMode.value,
                label: this.lvLabel.value,
                count: parseInt(this.lvCount.value || "0", 10),
                seed: parseInt(this.lvSeed.value || "0", 10),
                seed_policy: this.lvPolicy.value,
                crossfade_s: parseFloat(this.lvFade.value || "0"),
                prompts: this.lvPrompts.value.split("\n").filter((x) => x.trim()),
                keyframes: this.lvKeys.value.split("\n").filter((x) => x.trim()),
            };
            const r = await gr.post("/longvideo/start", { graph: p.output, params, client_id: clientId() });
            toast("success", `Long Video «${r.label}» запущен`);
        } catch (e) { toast("error", "Long Video не запущен", e.message); }
    }

    renderProjects() {
        if (!this.elProjects) return;
        const box = this.elProjects;
        box.innerHTML = "<div class='gr-subtitle'>Проекты</div>";
        if (!this.projects.length) { box.appendChild(el("div", { class: "gr-muted" }, "пока нет")); return; }
        for (const p of this.projects) {
            const row = el("div", { class: "gr-proj" });
            row.appendChild(el("span", {},
                `<b>${esc(p.label)}</b> <span class="gr-muted">${esc(p.mode)} · ${esc(p.state)} · ${p.done}/${p.segments}</span>`));
            const open = el("button", { class: "gr-btn gr-small" }, "Редактор");
            open.onclick = () => this.openEditor(p.label);
            row.appendChild(open);
            box.appendChild(row);
        }
    }

    async openEditor(label) {
        try {
            this.openProject = await gr.get(`/longvideo/${label}`);
            this.edit = { order: (this.openProject.segments || []).map((s) => s.index),
                excluded: new Set(), trims: {} };
            this.renderEditor();
        } catch (e) { toast("error", "Не открыть проект", e.message); }
    }

    renderEditor() {
        const box = this.elEditor;
        const m = this.openProject;
        box.innerHTML = "";
        if (!m) return;
        box.appendChild(el("div", { class: "gr-subtitle" }, `Редактор: ${esc(m.label)}`));
        const segMap = new Map((m.segments || []).map((s) => [s.index, s]));
        const sub = `gpuraid/${m.label}`;
        for (const idx of this.edit.order) {
            const s = segMap.get(idx);
            if (!s) continue;
            const row = el("div", { class: "gr-seg" + (this.edit.excluded.has(idx) ? " gr-seg-off" : "") });
            if (s.status === "done") {
                const v = el("video", { class: "gr-video", controls: "", preload: "metadata",
                    src: viewURL(s.file, sub, "output", true) });
                row.appendChild(v);
            } else {
                row.appendChild(el("div", { class: "gr-video gr-video-stub" }, esc(s.status)));
            }
            const meta = el("div", { class: "gr-seg-meta" });
            meta.appendChild(el("div", {}, `<b>#${s.index}</b> seed ${esc(String(s.seed ?? ""))} ` +
                `<span class="gr-muted">${esc(s.worker || "")}</span>` +
                (s.error ? ` <span class="gr-err">${esc(s.error)}</span>` : "")));
            if (s.prompt) meta.appendChild(el("div", { class: "gr-muted gr-clip" }, esc(s.prompt)));

            const ctl = el("div", { class: "gr-btns" });
            const up = el("button", { class: "gr-btn gr-small" }, "↑");
            up.onclick = () => this.moveSeg(idx, -1);
            const down = el("button", { class: "gr-btn gr-small" }, "↓");
            down.onclick = () => this.moveSeg(idx, 1);
            const onoff = el("button", { class: "gr-btn gr-small" },
                this.edit.excluded.has(idx) ? "вкл" : "искл");
            onoff.onclick = () => {
                this.edit.excluded.has(idx) ? this.edit.excluded.delete(idx) : this.edit.excluded.add(idx);
                this.renderEditor();
            };
            const rer = el("button", { class: "gr-btn gr-small" }, "заново");
            rer.onclick = async () => {
                const seed = prompt("Seed (пусто = случайный):", "");
                try {
                    await gr.post(`/longvideo/${m.label}/rerender`,
                        { index: idx, seed: seed ? parseInt(seed, 10) : null });
                    toast("info", `Сегмент #${idx}: перегенерация запущена`);
                } catch (e) { toast("error", "Не запущено", e.message); }
            };
            ctl.append(up, down, onoff, rer);
            const tin = el("input", { class: "gr-input gr-tiny", placeholder: "in,c", title: "трим от, сек" });
            const tout = el("input", { class: "gr-input gr-tiny", placeholder: "out,c", title: "трим до, сек" });
            const saveTrim = () => {
                this.edit.trims[idx] = { in_s: parseFloat(tin.value || "0") || 0,
                    out_s: parseFloat(tout.value || "0") || 0 };
            };
            tin.onchange = saveTrim;
            tout.onchange = saveTrim;
            ctl.append(tin, tout);
            meta.appendChild(ctl);
            row.appendChild(meta);
            box.appendChild(row);
        }
        const fade = el("input", { class: "gr-input gr-tiny", type: "number", min: "0", step: "0.1",
            value: String(m.crossfade_s || 0), title: "кроссфейд, сек" });
        const exp = el("button", { class: "gr-btn gr-primary" }, "Экспорт");
        exp.onclick = async () => {
            try {
                const order = this.edit.order.filter((i) => !this.edit.excluded.has(i));
                await gr.post(`/longvideo/${m.label}/export`, {
                    order, trims: this.edit.trims, crossfade_s: parseFloat(fade.value || "0"),
                });
            } catch (e) { toast("error", "Экспорт не удался", e.message); }
        };
        const closeBtn = el("button", { class: "gr-btn" }, "Закрыть");
        closeBtn.onclick = () => { this.openProject = null; this.renderEditor(); };
        const bar = el("div", { class: "gr-btns" });
        bar.append(fade, exp, closeBtn);
        if (m.final) {
            const link = el("a", { class: "gr-btn", target: "_blank",
                href: viewURL(m.final, sub, "output", true) }, "▶ итоговое видео");
            bar.appendChild(link);
        }
        box.appendChild(bar);
    }

    moveSeg(idx, dir) {
        const order = this.edit.order;
        const pos = order.indexOf(idx);
        const np = pos + dir;
        if (pos < 0 || np < 0 || np >= order.length) return;
        [order[pos], order[np]] = [order[np], order[pos]];
        this.renderEditor();
    }
}
