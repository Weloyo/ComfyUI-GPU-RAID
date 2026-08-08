// UI внутри нод GPU RAID: раскадровка Сценариста, Длинное видео, Offload, Конвейер.
// Вся работа с промптами, кадрами и запуском живёт в рабочей области; в панели
// расширения остаются только воркеры, настройки и мониторинг.
import { app } from "../../../scripts/app.js";
import { gr, toast, clientId } from "./api.js";
import { el, esc, fmtGb } from "./format.js";
import { ProjectEditor } from "./editor.js";

export const NODE_STORY = "GPURAID_StoryDirector";
export const NODE_LV = "GPURAID_LongVideo";
export const NODE_OFFLOAD = "GPURAID_Offload";
export const NODE_PIPELINE = "GPURAID_Pipeline";

// ---------------------------------------------------------------- утилиты

function widget(node, name) {
    return (node.widgets || []).find((w) => w.name === name);
}

function wval(node, name, fallback) {
    const w = widget(node, name);
    return w === undefined ? fallback : w.value;
}

function lines(text) {
    return String(text || "").split("\n").map((s) => s.trim()).filter(Boolean);
}

/** Ноды типа type, которые реально уедут в prompt (без mute/bypass). */
export function findNodes(type) {
    const nodes = app.graph?._nodes || [];
    return nodes.filter((n) => n.type === type && n.mode !== 2 && n.mode !== 4);
}

/** Тело ноды: контейнер под наш UI + разумный стартовый размер. */
function nodeBody(node, { width = 480, height = 420, minHeight = 160 } = {}) {
    const box = el("div", { class: "gr-node" });
    // колесо мыши внутри тела ноды должно скроллить содержимое, а не зумить канву
    box.addEventListener("wheel", (e) => e.stopPropagation(), { passive: false });
    node.addDOMWidget("gpuraid_ui", "gpuraid", box, {
        serialize: false,
        hideOnZoom: false,
        getMinHeight: () => minHeight,
    });
    node.size = [Math.max(node.size?.[0] || 0, width),
                 Math.max(node.size?.[1] || 0, height)];
    return box;
}

/** Точка на канве примерно в центре текущего вида (чтобы новая нода не легла в угол). */
function viewCenter() {
    try {
        const ds = app.canvas.ds;
        const cv = app.canvas.canvas;
        const p = ds.convertOffsetToCanvas([cv.width / 2, cv.height / 2]);
        if (p && isFinite(p[0]) && isFinite(p[1])) return [p[0] - 260, p[1] - 200];
    } catch (e) { /* ignore */ }
    return [80, 80];
}

function prop(node, key, fallback = "") {
    if (!node.properties) node.properties = {};
    if (node.properties[key] === undefined) node.properties[key] = fallback;
    return node.properties[key];
}

// ---------------------------------------------------------------- проекты

/** Общая обвязка нод-проектов: кнопка запуска, привязка к проекту, редактор. */
class ProjectNodeUI {
    constructor(node, cfg) {
        this.node = node;
        this.cfg = cfg;
        const box = nodeBody(node, cfg.size);

        const bar = el("div", { class: "gr-btns gr-node-bar" });
        this.runBtn = el("button", { class: "gr-btn gr-primary", title: cfg.runTitle },
            cfg.runLabel);
        this.runBtn.onclick = () => this.run();
        this.sel = el("select", { class: "gr-select gr-proj-sel",
            title: "проект, привязанный к этой ноде" });
        this.sel.onmousedown = () => this.refreshProjects();
        this.sel.onchange = () => this.bind(this.sel.value);
        bar.append(this.runBtn, el("span", { class: "gr-muted" }, "проект:"), this.sel);
        box.appendChild(bar);
        if (cfg.hint) box.appendChild(el("div", { class: "gr-muted gr-node-hint" }, cfg.hint));

        const edRoot = el("div", { class: "gr-node-editor" });
        box.appendChild(edRoot);
        this.editor = new ProjectEditor(edRoot);

        prop(node, "gpuraid_project", "");
        this.refreshProjects();
        // onConfigure (загрузка workflow) прилетает позже создания ноды
        setTimeout(() => this.sync(), 0);
    }

    async refreshProjects() {
        let projects = [];
        try { projects = (await gr.get("/longvideo")).projects || []; } catch (e) { /* ignore */ }
        this.projects = projects;
        const cur = this.node.properties.gpuraid_project || "";
        this.sel.innerHTML = "";
        this.sel.appendChild(el("option", { value: "" }, "— новый —"));
        for (const p of projects) {
            this.sel.appendChild(el("option", { value: p.label },
                esc(`${p.label} (${p.state} ${p.done}/${p.segments})`)));
        }
        if (cur && !projects.some((p) => p.label === cur)) {
            this.sel.appendChild(el("option", { value: cur }, esc(`${cur} (нет на диске)`)));
        }
        this.sel.value = cur;
    }

    bind(label) {
        this.node.properties.gpuraid_project = label || "";
        this.sel.value = label || "";
        this.editor.setProject(label || "", true);
    }

    /** Перечитать привязку из свойств ноды (после загрузки workflow). */
    sync() {
        this.editor.setProject(this.node.properties.gpuraid_project || "", true);
        this.sel.value = this.node.properties.gpuraid_project || "";
    }

    onConfigure() {
        this.refreshProjects();
        setTimeout(() => this.sync(), 0);
    }

    async run() {
        this.runBtn.disabled = true;
        try {
            const label = await this.cfg.onRun(this.node);
            if (label) this.bind(label);
        } catch (e) {
            toast(e.status === 409 ? "warn" : "error", this.cfg.errTitle, e.message, 8000);
        } finally {
            this.runBtn.disabled = false;
        }
    }

    dispose() { this.editor.dispose(); }
}

/** «План»: LLM (или эвристика) разбирает сюжет из виджетов ноды на сегменты. */
export async function runStoryPlan(node) {
    const p = await app.graphToPrompt();
    const r = await gr.post("/story/plan", { graph: p.output, client_id: clientId() });
    toast("success", `План «${r.label}» готов`,
        "правьте промпты кадров и сегментов, затем «Кадры ▶»");
    return r.label;
}

/** «Собрать»: сборка длинного видео по параметрам ноды. */
export async function runLongVideo(node) {
    const p = await app.graphToPrompt();
    const params = {
        mode: wval(node, "mode", "chain"),
        label: wval(node, "label", "myvideo"),
        count: parseInt(wval(node, "count", 4), 10) || 1,
        seed: parseInt(wval(node, "seed", 0), 10) || 0,
        seed_policy: wval(node, "seed_policy", "increment"),
        crossfade_s: parseFloat(wval(node, "crossfade_s", 0)) || 0,
        prompts: lines(wval(node, "prompts", "")),
        keyframes: lines(wval(node, "keyframes", "")),
    };
    const r = await gr.post("/longvideo/start", { graph: p.output, params,
        client_id: clientId() });
    toast("success", `Длинное видео «${r.label}» запущено`);
    return r.label;
}

/**
 * Открыть проект на канве: найти подходящую ноду или создать новую.
 * Вызывается из панели («Открыть на канве»).
 */
export function openProjectOnCanvas(label, mode) {
    const type = mode === "story" ? NODE_STORY : NODE_LV;
    const nodes = findNodes(type);
    let node = nodes.find((n) => n.properties?.gpuraid_project === label);
    if (!node && nodes.length === 1) {
        const cur = nodes[0].properties?.gpuraid_project;
        if (cur && cur !== label
            && !confirm(`Нода на канве привязана к проекту «${cur}». Переключить её на «${label}»?`)) {
            return;
        }
        node = nodes[0];
        node.__gr?.bind(label);
    }
    if (!node) {
        const LG = window.LiteGraph;
        if (!LG) { toast("error", "GPU RAID", "LiteGraph недоступен"); return; }
        node = LG.createNode(type);
        if (!node) { toast("error", "GPU RAID", `нода ${type} не зарегистрирована`); return; }
        app.graph.add(node);
        node.pos = viewCenter();
        node.__gr?.bind(label);
        toast("info", "Нода проекта добавлена на канву", label);
    }
    try { app.canvas.centerOnNode(node); app.canvas.setDirty(true, true); } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------- offload

class OffloadNodeUI {
    constructor(node) {
        this.node = node;
        const box = nodeBody(node, { width: 420, height: 180, minHeight: 96 });
        const bar = el("div", { class: "gr-btns gr-node-bar" });
        this.sel = el("select", { class: "gr-select gr-grow",
            title: "воркер, который посчитает весь workflow" });
        this.sel.onmousedown = () => this.refresh();
        this.sel.onchange = () => {
            const w = widget(this.node, "worker");
            if (w) { w.value = this.sel.value; this.node.setDirtyCanvas(true, true); }
        };
        this.runBtn = el("button", { class: "gr-btn gr-primary",
            title: "весь текущий workflow уедет на выбранного воркера, локальная GPU свободна" },
            "Выполнить на воркере ▶");
        this.runBtn.onclick = () => this.run();
        bar.append(this.sel, this.runBtn);
        box.appendChild(bar);
        box.appendChild(el("div", { class: "gr-muted gr-node-hint" },
            "Результаты вернутся в output/gpuraid/&lt;label&gt;_&lt;время&gt;/. "
            + "Ноды GPU RAID из графа вырезаются автоматически."));
        this.status = el("div", { class: "gr-muted" });
        box.appendChild(this.status);
        this.refresh();
    }

    async refresh() {
        let workers = [];
        try {
            workers = ((await gr.get("/workers")).workers || [])
                .filter((w) => w.enabled && w.id !== "local");
        } catch (e) {
            this.status.textContent = "сервер недоступен";
            return;
        }
        const cur = wval(this.node, "worker", "");
        this.sel.innerHTML = "";
        if (!workers.length) {
            this.sel.appendChild(el("option", { value: "" }, "нет включённых воркеров"));
            this.status.textContent = "добавьте воркера в панели GPU RAID";
            return;
        }
        for (const w of workers) {
            const st = w.status || {};
            this.sel.appendChild(el("option", { value: w.id },
                esc(`${w.name} — ${st.gpu || st.state || "?"}`
                    + (st.vram_total_gb ? ` (${fmtGb(st.vram_total_gb)})` : ""))));
        }
        const known = workers.some((w) => w.id === cur);
        this.sel.value = known ? cur : workers[0].id;
        if (!known) {
            const w = widget(this.node, "worker");
            if (w) w.value = this.sel.value;
        }
        this.status.textContent = "";
    }

    async run() {
        const workerId = this.sel.value;
        if (!workerId) { toast("warn", "GPU RAID", "нет включённых удалённых воркеров"); return; }
        this.runBtn.disabled = true;
        try {
            const p = await app.graphToPrompt();
            const r = await gr.post("/offload", {
                graph: p.output, workflow_ui: p.workflow, worker_id: workerId,
                label: wval(this.node, "label", "offload"), client_id: clientId(),
            });
            toast("info", "Offload запущен", (r.warnings || []).join("; "));
            this.status.textContent = `задание ${r.job_id} — прогресс в панели «Задания»`;
        } catch (e) {
            toast(e.status === 409 ? "warn" : "error", "Offload не запущен", e.message, 8000);
        } finally {
            this.runBtn.disabled = false;
        }
    }

    onConfigure() { this.refresh(); }
    dispose() {}
}

// ---------------------------------------------------------------- конвейер

class PipelineNodeUI {
    constructor(node) {
        this.node = node;
        prop(node, "gpuraid_placement", {});
        const box = nodeBody(node, { width: 520, height: 380, minHeight: 200 });
        const bar = el("div", { class: "gr-btns gr-node-bar" });
        this.anaBtn = el("button", { class: "gr-btn",
            title: "разрезать текущий workflow на стадии и предложить раскладку по воркерам" },
            "Проанализировать");
        this.anaBtn.onclick = () => this.analyze();
        this.runBtn = el("button", { class: "gr-btn gr-primary",
            title: "запустить стадии по сохранённой раскладке" }, "Запустить конвейер ▶");
        this.runBtn.onclick = () => this.run();
        bar.append(this.anaBtn, this.runBtn);
        box.appendChild(bar);
        box.appendChild(el("div", { class: "gr-muted gr-node-hint" },
            "Для моделей, которые не влезают целиком ни в один GPU: энкодер / диффузия / "
            + "VAE считают разные воркеры, промежуточные тензоры едут бандлами. "
            + "Спец-ноды в графе не нужны."));
        this.report = el("div", { class: "gr-node-report" });
        box.appendChild(this.report);
        this.renderReport();
    }

    get placement() { return this.node.properties.gpuraid_placement || {}; }

    async analyze() {
        this.anaBtn.disabled = true;
        try {
            const p = await app.graphToPrompt();
            this._graph = p.output;
            this._workflow = p.workflow;
            this._report = await gr.post("/pipeline/analyze", { graph: p.output });
            const saved = this.placement;
            const merged = { ...(this._report.placement || {}) };
            // ручная раскладка пользователя важнее автоматической
            for (const [k, v] of Object.entries(saved)) {
                if ((this._report.workers || []).some((w) => w.id === v)) merged[k] = v;
            }
            this.node.properties.gpuraid_placement = merged;
            this.renderReport();
        } catch (e) {
            toast(e.status === 409 ? "warn" : "error", "Анализ не удался", e.message, 8000);
        } finally {
            this.anaBtn.disabled = false;
        }
    }

    renderReport() {
        const box = this.report;
        box.innerHTML = "";
        const r = this._report;
        if (!r) {
            const n = Object.keys(this.placement).length;
            box.appendChild(el("div", { class: "gr-muted" }, n
                ? `раскладка сохранена (${n} стадий) — нажмите «Проанализировать», чтобы увидеть детали`
                : "нажмите «Проанализировать» — граф будет разрезан на стадии"));
            return;
        }
        for (const w of r.warnings || []) {
            box.appendChild(el("div", { class: "gr-muted" }, `⚠ ${esc(w)}`));
        }
        for (const isl of r.islands || []) {
            const row = el("div", { class: "gr-job" });
            const models = Object.entries(isl.models || {})
                .flatMap(([f, names]) => names.map((n) => `${f}/${n}`));
            row.appendChild(el("div", {},
                `<b>Стадия ${isl.id}</b> · ~${esc(String(isl.vram_est_gb))} ГБ VRAM`
                + `<div class="gr-muted">${esc(isl.classes.join(", "))}</div>`
                + (models.length ? `<div class="gr-muted">${esc(models.join(" · "))}</div>` : "")));
            const sel = el("select", { class: "gr-select" });
            for (const w of r.workers || []) {
                const opt = el("option", { value: w.id }, esc(`${w.name} (${w.vram_gb} ГБ)`));
                if (this.placement[isl.id] === w.id) opt.selected = true;
                sel.appendChild(opt);
            }
            sel.onchange = () => {
                this.node.properties.gpuraid_placement = {
                    ...this.placement, [isl.id]: sel.value,
                };
            };
            row.appendChild(sel);
            box.appendChild(row);
        }
        for (const c of r.cuts || []) {
            box.appendChild(el("div", { class: "gr-muted" },
                `⇢ ${esc(c.type)}: ${esc(c.from)} → ${esc(c.to)} · ~${c.est_mb} МБ`
                + (c.warn ? ' <span class="gr-err">&gt; лимита туннеля</span>' : "")));
        }
    }

    async run() {
        this.runBtn.disabled = true;
        try {
            let graph = this._graph;
            let workflow = this._workflow;
            if (!graph) {
                const p = await app.graphToPrompt();
                graph = p.output;
                workflow = p.workflow;
            }
            const resp = await gr.post("/pipeline/start", {
                graph, workflow_ui: workflow, placement: this.placement,
                label: wval(this.node, "label", "pipeline"), client_id: clientId(),
            });
            toast("success", `Конвейер запущен: ${resp.stages} стадий`,
                "прогресс — в панели «Задания»");
        } catch (e) {
            toast(e.status === 409 ? "warn" : "error", "Конвейер не запущен", e.message, 8000);
        } finally {
            this.runBtn.disabled = false;
        }
    }

    onConfigure() { this._report = null; this.renderReport(); }
    dispose() {}
}

// ---------------------------------------------------------------- регистрация

const BUILDERS = {
    [NODE_STORY]: (node) => new ProjectNodeUI(node, {
        runLabel: "План ▶",
        runTitle: "разобрать сюжет на сегменты (LLM или эвристика); ничего не рендерится",
        errTitle: "План не составлен",
        hint: "Шаблон сегмента = текущий канвас: два LoadImage с заголовками "
            + "GPURAID:START_IMAGE / GPURAID:END_IMAGE (FLF2V) и Save-нода видео.",
        size: { width: 540, height: 620, minHeight: 320 },
        onRun: runStoryPlan,
    }),
    [NODE_LV]: (node) => new ProjectNodeUI(node, {
        runLabel: "Собрать ▶",
        runTitle: "запустить сборку длинного видео по параметрам ноды",
        errTitle: "Длинное видео не запущено",
        hint: "Шаблон сегмента = текущий канвас (маркеры GPURAID:START_IMAGE, "
            + "GPURAID:END_IMAGE, GPURAID:PROMPT, GPURAID:VIDEO_OUT).",
        size: { width: 540, height: 560, minHeight: 300 },
        onRun: runLongVideo,
    }),
    [NODE_OFFLOAD]: (node) => new OffloadNodeUI(node),
    [NODE_PIPELINE]: (node) => new PipelineNodeUI(node),
};

app.registerExtension({
    name: "GPURaid.NodeUI",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        const build = BUILDERS[nodeData?.name];
        if (!build) return;

        const origCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = origCreated?.apply(this, arguments);
            try { this.__gr = build(this); }
            catch (e) { console.error("GPU RAID: UI ноды не построен", e); }
            return r;
        };

        const origConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const r = origConfigure?.apply(this, arguments);
            try { this.__gr?.onConfigure?.(); } catch (e) { /* ignore */ }
            return r;
        };

        const origRemoved = nodeType.prototype.onRemoved;
        nodeType.prototype.onRemoved = function () {
            try { this.__gr?.dispose?.(); } catch (e) { /* ignore */ }
            this.__gr = null;
            return origRemoved?.apply(this, arguments);
        };
    },
});
