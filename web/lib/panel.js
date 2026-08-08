// Sidebar-панель GPU RAID: воркеры, задания, offload, Long Video, история.
import { app } from "../../../scripts/app.js";
import { gr, toast, viewURL, clientId } from "./api.js";
import { el, esc, fmtDur, fmtGb, platformBadge, stateDot } from "./format.js";

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
        this.openProject = null;     // manifest
        this.edit = { order: [], excluded: new Set(), trims: {} };
        this._timer = null;
        this._editTimer = null;
        this._editorDirty = false;
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
            // авто-цепочка «Всё ▶»: кадры готовы -> рендер сегментов
            if (data.manifest && data.manifest.state === "kf_done"
                && this._autoSeg === data.label) {
                this._autoSeg = null;
                gr.post(`/story/${data.label}/render`, { client_id: clientId() })
                    .then(() => toast("info",
                        "Сценарист: кадры готовы — рендер сегментов запущен"))
                    .catch((e) => toast("error", "Сегменты не запущены", e.message));
            }
            this.refreshProjects();
            if (this.openProject && data.label === this.openProject.label) {
                if (data.deleted) {
                    this.openProject = null;
                    this.renderEditor();
                } else if (data.manifest) {
                    this.openProject = { ...this.openProject, ...data.manifest };
                    this.renderEditor();
                }
            }
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
            this.renderOffload();
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
        const mk = (id, title, open = true) => {
            const box = el("details", { class: "gr-section", ...(open ? { open: "" } : {}) });
            box.appendChild(el("summary", {}, title));
            const body = el("div", { class: "gr-body", id: `gr-${id}` });
            box.appendChild(body);
            this.root.appendChild(box);
            return body;
        };
        this.elModes = mk("modes", "Режимы");
        this.elWorkers = mk("workers", "Воркеры");
        this.elAdd = mk("add", "Добавить воркеров", false);
        this.elJobs = mk("jobs", "Задания");
        this.elStory = mk("story", "Сценарист: сюжет → видео", false);
        this.elPipeline = mk("pipeline", "Pipeline: большая модель по частям", false);
        this.elOffload = mk("offload", "Offload: весь workflow на воркера", false);
        this.elLV = mk("lv", "Long Video", false);
        this.elHistory = mk("history", "История", false);
        this.buildModes();
        this.buildAdd();
        this.buildStory();
        this.buildPipeline();
        this.buildLV();
    }

    // ------------------------------------------------------------- pipeline

    buildPipeline() {
        const box = this.elPipeline;
        box.innerHTML = "";
        const analyzeBtn = el("button", { class: "gr-btn gr-primary" },
            "Проанализировать текущий workflow");
        analyzeBtn.onclick = () => this.pipelineAnalyze();
        box.appendChild(analyzeBtn);
        box.appendChild(el("div", { class: "gr-muted" },
            "Одна модель, которая не влезает в один GPU: компоненты (энкодер / "
            + "диффузия / VAE) разъезжаются по разным воркерам, промежуточные "
            + "тензоры едут бандлами. Спец-ноды не нужны — обычный workflow."));
        this.elPipeReport = el("div", {});
        box.appendChild(this.elPipeReport);
    }

    async pipelineAnalyze() {
        try {
            const p = await app.graphToPrompt();
            this._pipeGraph = p.output;
            this._pipeWorkflow = p.workflow;
            this.pipeReport = await gr.post("/pipeline/analyze", { graph: p.output });
            this.pipePlacement = { ...(this.pipeReport.placement || {}) };
            this.renderPipeline();
        } catch (e) { toast("error", "Анализ не удался", e.message); }
    }

    renderPipeline() {
        const box = this.elPipeReport;
        box.innerHTML = "";
        const r = this.pipeReport;
        if (!r) return;
        for (const w of r.warnings || []) {
            box.appendChild(el("div", { class: "gr-muted" }, `⚠ ${esc(w)}`));
        }
        for (const isl of r.islands || []) {
            const row = el("div", { class: "gr-job" });
            const models = Object.entries(isl.models || {})
                .flatMap(([f, names]) => names.map((n) => `${f}/${n}`));
            row.appendChild(el("div", {},
                `<b>Стадия ${isl.id}</b> · ~${esc(String(isl.vram_est_gb))} ГБ VRAM` +
                `<div class="gr-muted">${esc(isl.classes.join(", "))}</div>` +
                (models.length ? `<div class="gr-muted">${esc(models.join(" · "))}</div>` : "")));
            const sel = el("select", { class: "gr-select" });
            for (const w of r.workers || []) {
                const opt = el("option", { value: w.id },
                    esc(`${w.name} (${w.vram_gb} ГБ)`));
                if (this.pipePlacement[isl.id] === w.id) opt.selected = true;
                sel.appendChild(opt);
            }
            sel.onchange = () => { this.pipePlacement[isl.id] = sel.value; };
            row.appendChild(sel);
            box.appendChild(row);
        }
        for (const c of r.cuts || []) {
            box.appendChild(el("div", { class: "gr-muted" },
                `⇢ ${esc(c.type)}: ${esc(c.from)} → ${esc(c.to)} · ~${c.est_mb} МБ` +
                (c.warn ? ' <span class="gr-err">&gt; лимита туннеля</span>' : "")));
        }
        const label = el("input", { class: "gr-input", placeholder: "имя задания",
            value: "pipeline" });
        const run = el("button", { class: "gr-btn gr-primary" }, "Запустить конвейер");
        run.onclick = async () => {
            try {
                const resp = await gr.post("/pipeline/start", {
                    graph: this._pipeGraph, workflow_ui: this._pipeWorkflow,
                    placement: this.pipePlacement, label: label.value,
                    client_id: clientId(),
                });
                toast("success", `Конвейер запущен: ${resp.stages} стадий`,
                    "прогресс — в разделе «Задания»");
            } catch (e) { toast("error", "Конвейер не запущен", e.message); }
        };
        const bar = el("div", { class: "gr-btns" });
        bar.append(label, run);
        box.appendChild(bar);
    }

    // ------------------------------------------------------------- сценарист

    buildStory() {
        const box = this.elStory;
        box.innerHTML = "";
        this.stStory = el("textarea", { class: "gr-textarea", rows: "4",
            placeholder: "Опишите сюжет видео — LLM (или эвристика) разобьёт его на сегменты "
                + "с ключевыми кадрами и промптами; всё редактируется до рендера" });
        this.stLabel = el("input", { class: "gr-input", placeholder: "имя проекта",
            value: "story" });
        this.stCount = el("input", { class: "gr-input gr-tiny", type: "number", min: "0",
            max: "64", value: "0", title: "сегментов (0 = авто)" });
        this.stDur = el("input", { class: "gr-input gr-tiny", type: "number", min: "0.5",
            step: "0.5", value: "5", title: "секунд на сегмент" });
        this.stAspect = el("select", { class: "gr-select" });
        for (const a of ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"]) {
            this.stAspect.appendChild(el("option", { value: a }, a));
        }
        this.stLLM = el("input", { type: "checkbox", checked: "", title: "разбивать через LLM" });
        const planBtn = el("button", { class: "gr-btn gr-primary" }, "Составить план");
        planBtn.onclick = () => this.storyPlan();

        box.appendChild(this.stStory);
        box.appendChild(this.row("Проект", this.stLabel));
        box.appendChild(this.row("Сегменты", this.stCount, this.stDur, this.stAspect,
            this.stLLM, planBtn));
        box.appendChild(el("div", { class: "gr-muted" },
            "Шаблон сегмента = текущий workflow: два LoadImage с заголовками "
            + "GPURAID:START_IMAGE / GPURAID:END_IMAGE (FLF2V) и Save-нода видео. "
            + "Если на канве есть нода «GPU RAID Сценарист» — параметры берутся из неё "
            + "(или просто жмите Queue). План появится в «Long Video → Проекты»."));
    }

    async storyPlan() {
        try {
            const p = await app.graphToPrompt();
            const hasDirector = Object.values(p.output || {})
                .some((n) => n.class_type === "GPURAID_StoryDirector");
            const params = hasDirector ? {} : {
                label: this.stLabel.value,
                segments_count: parseInt(this.stCount.value || "0", 10),
                segment_duration_s: parseFloat(this.stDur.value || "5"),
                aspect: this.stAspect.value,
                use_llm: this.stLLM.checked,
            };
            if (!hasDirector) {
                if (!this.stStory.value.trim()) {
                    toast("warn", "Сценарист", "опишите сюжет в поле выше");
                    return;
                }
                params.story = this.stStory.value;
            }
            const r = await gr.post("/story/plan",
                { graph: p.output, params, client_id: clientId() });
            toast("success", `План «${r.label}» готов`,
                "правьте промпты и жмите «Кадры ▶»");
            await this.refreshProjects();
            this.openEditor(r.label);
        } catch (e) { toast("error", "План не составлен", e.message); }
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
             "Сценарист на MiniMax H3: сюжет → кадры → сегменты → видео любой длины"],
            ["pipeline_minimax_h3", "🐘 Большая модель",
             "H3 по частям на нескольких GPU: загрузите пример и жмите Pipeline → Анализ"],
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

        const d = el("details", { class: "gr-subdetails" });
        d.appendChild(el("summary", {}, "Настройки LLM (Сценарист)"));
        this.llmUrl = el("input", { class: "gr-input",
            placeholder: "base_url: http://127.0.0.1:11434/v1 (Ollama/LM Studio/OpenRouter)" });
        this.llmModel = el("input", { class: "gr-input", placeholder: "модель, напр. qwen2.5:32b" });
        this.llmKey = el("input", { class: "gr-input", type: "password",
            placeholder: "API-ключ (не обязателен для локальных LLM)" });
        this.llmSaved = el("span", { class: "gr-muted" });
        const save = el("button", { class: "gr-btn gr-primary" }, "Сохранить");
        save.onclick = async () => {
            try {
                await gr.patch("/settings", { llm: {
                    base_url: this.llmUrl.value.trim(), model: this.llmModel.value.trim(),
                } });
                if (this.llmKey.value.trim()) {
                    await gr.post("/secrets", { llm_api_key: this.llmKey.value.trim() });
                }
                this.llmKey.value = "";
                toast("success", "Настройки LLM сохранены");
                this.refreshSettings();
            } catch (e) { toast("error", "Не сохранено", e.message); }
        };
        d.appendChild(this.row("URL", this.llmUrl));
        d.appendChild(this.row("Модель", this.llmModel));
        d.appendChild(this.row("Ключ", this.llmKey, this.llmSaved));
        d.appendChild(save);
        box.appendChild(d);

        const rd = el("details", { class: "gr-subdetails" });
        rd.appendChild(el("summary", {}, "Автоподключение воркеров (gist) и секреты"));
        this.gistId = el("input", { class: "gr-input",
            placeholder: "gist_id приватного гиста — тот же вписывается в ноутбуки" });
        this.ghToken = el("input", { class: "gr-input", type: "password",
            placeholder: "GitHub-токен (fine-grained, только право на Gists)" });
        this.kaggleJson = el("textarea", { class: "gr-textarea", rows: "2",
            placeholder: 'kaggle.json для автозапуска Kaggle: {"username":..., "key":...}' });
        this.rdSaved = el("span", { class: "gr-muted" });
        const rdSave = el("button", { class: "gr-btn gr-primary" }, "Сохранить");
        rdSave.onclick = async () => {
            try {
                await gr.patch("/settings", { rendezvous: { gist_id: this.gistId.value.trim() } });
                const sp = {};
                if (this.ghToken.value.trim()) sp.gh_token = this.ghToken.value.trim();
                if (this.kaggleJson.value.trim()) sp.kaggle_json = this.kaggleJson.value.trim();
                if (Object.keys(sp).length) await gr.post("/secrets", sp);
                this.ghToken.value = "";
                this.kaggleJson.value = "";
                toast("success", "Автоподключение настроено");
                this.refreshSettings();
            } catch (e) { toast("error", "Не сохранено", e.message); }
        };
        rd.appendChild(this.row("Gist", this.gistId));
        rd.appendChild(this.row("GH-токен", this.ghToken, this.rdSaved));
        rd.appendChild(this.kaggleJson);
        rd.appendChild(rdSave);
        box.appendChild(rd);
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

        const llm = this.settings.llm || {};
        if (document.activeElement !== this.llmUrl) this.llmUrl.value = llm.base_url || "";
        if (document.activeElement !== this.llmModel) this.llmModel.value = llm.model || "";
        this.llmSaved.textContent = this.secretsView.has_llm_key ? "ключ сохранён ✓" : "ключа нет";

        const rdCfg = this.settings.rendezvous || {};
        if (document.activeElement !== this.gistId) this.gistId.value = rdCfg.gist_id || "";
        const flags = [];
        if (this.secretsView.has_gh_token) flags.push("GH ✓");
        if (this.secretsView.has_kaggle_json) flags.push("kaggle.json ✓");
        this.rdSaved.textContent = flags.join(" · ") || "секретов нет";
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
            const check = el("button", { class: "gr-btn" }, "Проверить");
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
            } catch (e) { toast("error", "Kaggle не запущен", e.message); }
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
                "автоподключение не настроено: gist_id + GH-токен в секции «Режимы»";
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
            const btns = el("div", { class: "gr-btns" });
            const open = el("button", { class: "gr-btn gr-small" }, "Редактор");
            open.onclick = () => this.openEditor(p.label);
            btns.appendChild(open);
            const del = el("button", { class: "gr-btn gr-small gr-danger",
                title: "удалить проект со всеми сегментами" }, "✕");
            del.onclick = async () => {
                if (!confirm(`Удалить проект «${p.label}» со всеми сегментами?`)) return;
                try {
                    await gr.del(`/longvideo/${p.label}`);
                    if (this.openProject?.label === p.label) {
                        this.openProject = null;
                        this.renderEditor();
                    }
                    this.refreshProjects();
                } catch (e) { toast("error", "Не удалось удалить", e.message); }
            };
            btns.appendChild(del);
            row.appendChild(btns);
            box.appendChild(row);
        }
    }

    async openEditor(label) {
        try {
            this.openProject = await gr.get(`/longvideo/${label}`);
            const all = (this.openProject.segments || []).map((s) => s.index);
            const stored = this.openProject.edit || {};
            // правки персистятся в манифесте: подхватываем их, а не сбрасываем
            const order = (stored.order || []).filter((i) => all.includes(i));
            for (const i of all) if (!order.includes(i)) order.push(i);
            this.edit = {
                order,
                excluded: new Set(stored.excluded || []),
                trims: { ...(stored.trims || {}) },
                crossfade_s: stored.crossfade_s ?? this.openProject.crossfade_s ?? 0,
            };
            this.renderEditor();
        } catch (e) { toast("error", "Не открыть проект", e.message); }
    }

    saveEdit() {
        clearTimeout(this._editTimer);
        this._editTimer = setTimeout(() => {
            if (!this.openProject) return;
            gr.patch(`/longvideo/${this.openProject.label}/edit`, {
                order: this.edit.order,
                excluded: [...this.edit.excluded],
                trims: this.edit.trims,
                crossfade_s: this.edit.crossfade_s || 0,
            }).catch(() => {});
        }, 600);
    }

    renderEditor() {
        const box = this.elEditor;
        const m = this.openProject;
        // не перерисовывать, пока пользователь печатает в редакторе (WS-события
        // приходят на каждый сегмент) — перерисуемся после потери фокуса
        if (m && box.contains(document.activeElement) &&
            /^(TEXTAREA|INPUT)$/.test(document.activeElement.tagName)) {
            this._editorDirty = true;
            if (!this._editorFocusHook) {
                this._editorFocusHook = true;
                box.addEventListener("focusout", () => setTimeout(() => {
                    if (this._editorDirty && !(box.contains(document.activeElement) &&
                        /^(TEXTAREA|INPUT)$/.test(document.activeElement.tagName))) {
                        this._editorDirty = false;
                        this.renderEditor();
                    }
                }, 150));
            }
            return;
        }
        box.innerHTML = "";
        if (!m) return;
        box.appendChild(el("div", { class: "gr-subtitle" },
            `Редактор: ${esc(m.label)}` +
            (m.mode === "story" ? ` <span class="gr-muted">· ${esc(m.state || "")}</span>` : "")));
        if (m.keyframes && m.keyframes.length) this.renderStoryBlock(box, m);
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
                `<span class="gr-muted">${esc(s.worker || "")}` +
                (s.duration_s ? ` · ${esc(String(s.duration_s))}с` : "") + `</span>` +
                (s.dirty ? ` <span class="gr-chip gr-chip-yellow">промпт изменён — нужен перерендер</span>` : "") +
                (s.stale ? ` <span class="gr-chip gr-chip-yellow">кадр изменён — перерендерите</span>` : "") +
                (s.error ? ` <span class="gr-err">${esc(s.error)}</span>` : "")));

            const pr = el("textarea", { class: "gr-textarea gr-seg-prompt", rows: "2",
                placeholder: "промпт сегмента (пусто = из шаблона)" });
            pr.value = s.prompt || "";
            meta.appendChild(pr);

            const ctl = el("div", { class: "gr-btns" });
            const seedIn = el("input", { class: "gr-input gr-tiny", type: "number",
                placeholder: String(s.seed ?? ""), title: "seed (пусто = случайный при перерендере)" });
            const saveBtn = el("button", { class: "gr-btn gr-small",
                title: "сохранить промпт/seed в проект (без рендера)" }, "Сохранить");
            saveBtn.onclick = async () => {
                try {
                    const patch = { prompt: pr.value };
                    if (seedIn.value.trim()) patch.seed = parseInt(seedIn.value, 10);
                    await gr.patch(`/longvideo/${m.label}/segments/${idx}`, patch);
                    toast("success", `Сегмент #${idx} сохранён`);
                } catch (e) { toast("error", "Не сохранено", e.message); }
            };
            const rer = el("button", { class: "gr-btn gr-small",
                title: "перегенерировать с текущим промптом" }, "заново");
            rer.onclick = async () => {
                try {
                    await gr.post(`/longvideo/${m.label}/rerender`, {
                        index: idx,
                        prompt: pr.value,
                        seed: seedIn.value.trim() ? parseInt(seedIn.value, 10) : null,
                    });
                    toast("info", `Сегмент #${idx}: перегенерация запущена`);
                } catch (e) { toast("error", "Не запущено", e.message); }
            };
            const up = el("button", { class: "gr-btn gr-small" }, "↑");
            up.onclick = () => this.moveSeg(idx, -1);
            const down = el("button", { class: "gr-btn gr-small" }, "↓");
            down.onclick = () => this.moveSeg(idx, 1);
            const onoff = el("button", { class: "gr-btn gr-small" },
                this.edit.excluded.has(idx) ? "вкл" : "искл");
            onoff.onclick = () => {
                this.edit.excluded.has(idx) ? this.edit.excluded.delete(idx) : this.edit.excluded.add(idx);
                this.saveEdit();
                this.renderEditor();
            };
            ctl.append(saveBtn, rer, up, down, onoff);
            const trim = this.edit.trims[idx] || this.edit.trims[String(idx)] || {};
            const tin = el("input", { class: "gr-input gr-tiny", placeholder: "in,c",
                title: "трим от, сек", value: trim.in_s ? String(trim.in_s) : "" });
            const tout = el("input", { class: "gr-input gr-tiny", placeholder: "out,c",
                title: "трим до, сек", value: trim.out_s ? String(trim.out_s) : "" });
            const saveTrim = () => {
                this.edit.trims[idx] = { in_s: parseFloat(tin.value || "0") || 0,
                    out_s: parseFloat(tout.value || "0") || 0 };
                this.saveEdit();
            };
            tin.onchange = saveTrim;
            tout.onchange = saveTrim;
            ctl.append(tin, tout, seedIn);
            meta.appendChild(ctl);
            row.appendChild(meta);
            box.appendChild(row);
        }
        const fade = el("input", { class: "gr-input gr-tiny", type: "number", min: "0", step: "0.1",
            value: String(this.edit.crossfade_s || 0), title: "кроссфейд, сек" });
        fade.onchange = () => {
            this.edit.crossfade_s = parseFloat(fade.value || "0") || 0;
            this.saveEdit();
        };
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
        this.saveEdit();
        this.renderEditor();
    }

    renderStoryBlock(box, m) {
        const sub = `gpuraid_story/${m.label}`;
        const bar = el("div", { class: "gr-btns" });
        const kfBtn = el("button", { class: "gr-btn gr-primary",
            title: "рендер ключевых кадров (только неготовых) параллельно на всех GPU" },
            "Кадры ▶");
        kfBtn.onclick = async () => {
            const idx = (m.keyframes || []).filter((k) => k.status !== "done")
                .map((k) => k.index);
            try {
                await gr.post(`/story/${m.label}/keyframes/render`,
                    { indices: idx.length ? idx : null, client_id: clientId() });
                toast("info", `Кадры: рендер ${idx.length || m.keyframes.length} шт.`);
            } catch (e) { toast("error", "Кадры не запущены", e.message); }
        };
        const segBtn = el("button", { class: "gr-btn gr-primary",
            title: "рендер сегментов FLF2V (неготовых и устаревших) параллельно" },
            "Рендер сегментов ▶");
        segBtn.onclick = async () => {
            const idx = (m.segments || []).filter((s) => s.status !== "done" || s.stale)
                .map((s) => s.index);
            try {
                await gr.post(`/story/${m.label}/render`,
                    { indices: idx.length ? idx : null, client_id: clientId() });
                toast("info", `Сегменты: рендер ${idx.length || m.segments.length} шт.`);
            } catch (e) { toast("error", "Сегменты не запущены", e.message); }
        };
        const allBtn = el("button", { class: "gr-btn",
            title: "кадры, затем автоматически сегменты" }, "Всё ▶");
        allBtn.onclick = () => { this._autoSeg = m.label; kfBtn.onclick(); };
        const tmplBtn = el("button", { class: "gr-btn gr-small",
            title: "сделать текущий workflow на канве T2I-шаблоном ключевых кадров "
                + "(нужны GPURAID:PROMPT и SaveImage)" }, "Шаблон кадра из канвы");
        tmplBtn.onclick = async () => {
            try {
                const p = await app.graphToPrompt();
                await gr.post(`/story/${m.label}/keyframe_template`, { graph: p.output });
                toast("success", "Шаблон ключевых кадров сохранён");
            } catch (e) { toast("error", "Шаблон не принят", e.message); }
        };
        bar.append(kfBtn, segBtn, allBtn, tmplBtn);
        box.appendChild(bar);
        if (m.llm && m.llm.error) {
            box.appendChild(el("div", { class: "gr-muted" },
                `⚠ LLM: ${esc(m.llm.error)} — план собран эвристикой`));
        }
        const strip = el("div", { class: "gr-kfstrip" });
        for (const k of (m.keyframes || [])) {
            const card = el("div", { class: "gr-kf" });
            if (k.status === "done" && k.file) {
                card.appendChild(el("img", { class: "gr-kf-img",
                    src: viewURL(k.file, sub, "input", true) }));
            } else {
                card.appendChild(el("div", { class: "gr-kf-img gr-video-stub" },
                    esc(k.status || "draft")));
            }
            card.appendChild(el("div", { class: "gr-muted" }, `кадр ${k.index}` +
                (k.error ? ` <span class="gr-err">${esc(k.error)}</span>` : "")));
            const pr = el("textarea", { class: "gr-textarea gr-seg-prompt", rows: "2",
                placeholder: "промпт кадра" });
            pr.value = k.prompt || "";
            card.appendChild(pr);
            const ctl = el("div", { class: "gr-btns" });
            const seedIn = el("input", { class: "gr-input gr-tiny", type: "number",
                placeholder: String(k.seed ?? ""), title: "seed (пусто = случайный)" });
            const saveB = el("button", { class: "gr-btn gr-small",
                title: "сохранить промпт/seed кадра (без рендера)" }, "Сохранить");
            saveB.onclick = async () => {
                try {
                    const patch = { prompt: pr.value };
                    if (seedIn.value.trim()) patch.seed = parseInt(seedIn.value, 10);
                    await gr.patch(`/story/${m.label}/keyframes/${k.index}`, patch);
                    toast("success", `Кадр ${k.index} сохранён`);
                } catch (e) { toast("error", "Не сохранено", e.message); }
            };
            const rerB = el("button", { class: "gr-btn gr-small",
                title: "перегенерировать кадр (смежные сегменты станут «устаревшими»)" },
                "заново");
            rerB.onclick = async () => {
                try {
                    await gr.post(`/story/${m.label}/keyframes/${k.index}/rerender`, {
                        prompt: pr.value,
                        seed: seedIn.value.trim() ? parseInt(seedIn.value, 10) : null,
                    });
                    toast("info", `Кадр ${k.index}: перегенерация`);
                } catch (e) { toast("error", "Не запущено", e.message); }
            };
            ctl.append(saveB, rerB, seedIn);
            card.appendChild(ctl);
            strip.appendChild(card);
        }
        box.appendChild(strip);
    }
}
