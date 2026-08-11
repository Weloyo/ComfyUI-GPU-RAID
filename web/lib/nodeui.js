// UI внутри нод GPU RAID: История/Раскадровка/Видеоряд, Длинное видео, Offload, Конвейер.
// Вся работа с промптами, кадрами и запуском живёт в рабочей области; в панели
// расширения остаются только воркеры, настройки и мониторинг.
import { app } from "../../../scripts/app.js";
import { gr, toast, clientId } from "./api.js";
import { el, esc, fmtGb } from "./format.js";
import { ProjectEditor } from "./editor.js";
import { ModelsNodeUI } from "./models.js";
import { WorkersNodeUI } from "./workersui.js";

export const NODE_STORY = "GPURAID_Story";
export const NODE_STORYBOARD = "GPURAID_Storyboard";
export const NODE_VIDEOSEQ = "GPURAID_VideoSequence";
export const NODE_LV = "GPURAID_LongVideo";
export const NODE_OFFLOAD = "GPURAID_Offload";
export const NODE_PIPELINE = "GPURAID_Pipeline";
export const NODE_MODELS = "GPURAID_Models";
export const NODE_WORKERS = "GPURAID_Workers";

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

/** Нативный виджет настройки (continuity_mode/prompt_format/preview_*) сам по
 *  себе ни во что не пишется — рендер читает манифест с сервера. Без этого
 *  изменение виджета на канве молча ничего не значит. */
function wireSettingWidget(node, widgetName, endpoint) {
    const w = widget(node, widgetName);
    if (!w) return;
    const orig = w.callback;
    w.callback = function (value, ...rest) {
        const r = orig ? orig.apply(this, [value, ...rest]) : undefined;
        const label = node.properties?.gpuraid_project;
        if (label) {
            gr.patch(`/story/${encodeURIComponent(label)}/${endpoint}`, { [widgetName]: value })
                .catch((e) => toast("error", "Настройка не сохранена", e.message));
        }
        return r;
    };
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

/** Добавить (или найти) ноду типа type на канве и показать её. */
export function revealNodeOnCanvas(type) {
    let node = findNodes(type)[0];
    if (!node) {
        const LG = window.LiteGraph;
        if (!LG) { toast("error", "GPU RAID", "LiteGraph недоступен"); return null; }
        node = LG.createNode(type);
        if (!node) { toast("error", "GPU RAID", `нода ${type} не зарегистрирована`); return null; }
        app.graph.add(node);
        node.pos = viewCenter();
    }
    try { app.canvas.centerOnNode(node); app.canvas.setDirty(true, true); } catch (e) { /* ignore */ }
    return node;
}

function prop(node, key, fallback = "") {
    if (!node.properties) node.properties = {};
    if (node.properties[key] === undefined) node.properties[key] = fallback;
    return node.properties[key];
}

/**
 * Апстрим-нода, подключённая ко входу "project" (коннектор GPURAID_PROJECT),
 * и её привязанный проект — если есть. `getInputNode` — штатный LiteGraph-
 * метод; на случай расхождений в версии фронтенда — тот же сырой обход
 * через graph.links/getNodeById, что уже использует openProjectOnCanvas.
 */
function upstreamProjectLabel(node) {
    try {
        const slot = (node.inputs || []).findIndex((i) => i.name === "project");
        if (slot < 0) return null;
        let up = typeof node.getInputNode === "function" ? node.getInputNode(slot) : null;
        if (!up) {
            const linkId = node.inputs[slot]?.link;
            const linkInfo = linkId != null ? app.graph.links[linkId] : null;
            if (linkInfo) up = app.graph.getNodeById(linkInfo.origin_id);
        }
        return up?.properties?.gpuraid_project || null;
    } catch (e) {
        return null;
    }
}

/** Ноды, подключённые к выходу "project" этой ноды (симметрично
 *  upstreamProjectLabel) — чтобы протолкнуть свежую привязку вниз по цепочке. */
function downstreamProjectNodes(node) {
    const slot = (node.outputs || []).findIndex((o) => o.name === "project");
    if (slot < 0) return [];
    const out = [];
    for (const id of node.outputs[slot]?.links || []) {
        const li = app.graph.links[id];
        const n = li ? app.graph.getNodeById(li.target_id) : null;
        if (n) out.push(n);
    }
    return out;
}

/** Подхватить привязку из коннектора, если она есть и отличается от текущей
 *  (тот же confirm(), что при ручном переключении дропдауном). Отключение
 *  связи привязку НЕ сбрасывает — дропдаун остаётся рабочим фоллбэком. */
function autoBindFromConnector(ui) {
    const label = upstreamProjectLabel(ui.node);
    if (!label) return;
    const cur = ui.node.properties.gpuraid_project || "";
    if (label === cur) return;
    if (cur && !confirm(
        `Нода привязана к проекту «${cur}». Переключить на «${label}» (по коннектору)?`)) {
        return;
    }
    ui.bind(label);
}

// ---------------------------------------------------------------- проекты

/** Общая обвязка нод-проектов: кнопка запуска, привязка к проекту, редактор. */
class ProjectNodeUI {
    constructor(node, cfg) {
        this.node = node;
        this.cfg = cfg;
        const box = nodeBody(node, cfg.size);

        const bar = el("div", { class: "gr-btns gr-node-bar" });
        // Раскадровка/Видеоряд не имеют отдельного "создать" действия — только
        // кнопки рендера внутри редактора; runBtn им не нужен
        if (cfg.onRun) {
            this.runBtn = el("button", { class: "gr-btn gr-primary", title: cfg.runTitle },
                cfg.runLabel);
            this.runBtn.onclick = () => this.run();
            bar.appendChild(this.runBtn);
        }
        this.sel = el("select", { class: "gr-select gr-proj-sel",
            title: "проект, привязанный к этой ноде" });
        this.sel.onmousedown = () => this.refreshProjects();
        this.sel.onchange = () => this.bind(this.sel.value);
        bar.append(el("span", { class: "gr-muted" }, "проект:"), this.sel);
        box.appendChild(bar);
        if (cfg.hint) box.appendChild(el("div", { class: "gr-muted gr-node-hint" }, cfg.hint));

        const edRoot = el("div", { class: "gr-node-editor" });
        box.appendChild(edRoot);
        this.editor = new ProjectEditor(edRoot, cfg.stage);

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
        // привязка появилась/сменилась здесь — толкнуть вниз по GPURAID_PROJECT
        // коннектору: сама эта нода могла быть подключена ДО того, как у неё
        // появился label (типичный случай — «План ▶» на уже свёрстанном примере)
        for (const n of downstreamProjectNodes(this.node)) n.__gr?.sync?.();
    }

    /** Перечитать привязку из свойств ноды (после загрузки workflow) — сперва
     *  пробуя коннектор, иначе то, что уже сохранено в properties. */
    sync() {
        autoBindFromConnector(this);
        this.editor.setProject(this.node.properties.gpuraid_project || "", true);
        this.sel.value = this.node.properties.gpuraid_project || "";
    }

    onConfigure() {
        this.refreshProjects();
        setTimeout(() => this.sync(), 0);
    }

    /** LiteGraph-колбэк ноды (патчится ниже, в beforeRegisterNodeDef): вход
     *  "project" только что подключили — подхватить привязку сразу, не ждать
     *  следующего onConfigure. */
    onConnectionsChange(type, index) {
        const INPUT = window.LiteGraph?.INPUT;
        if (INPUT !== undefined && type !== INPUT) return;
        if (this.node.inputs?.[index]?.name !== "project") return;
        autoBindFromConnector(this);
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

/** «План»: LLM (или эвристика) разбирает сюжет из виджетов ноды на сегменты
 *  с таймингом. Графа канвы не требует — шаблоны захватываются отдельно, уже
 *  над готовым проектом, нодами Раскадровка/Видеоряд. */
export async function runStoryPlan(node) {
    const params = {
        story: wval(node, "story", ""),
        label: wval(node, "label", "story"),
        segments_count: parseInt(wval(node, "segments_count", 0), 10) || 0,
        segment_duration_s: parseFloat(wval(node, "segment_duration_s", 5.0)) || 5.0,
        max_segment_duration_s: parseFloat(wval(node, "max_segment_duration_s", 0)) || null,
        max_total_duration_s: parseFloat(wval(node, "max_total_duration_s", 0)) || null,
        fps: parseInt(wval(node, "fps", 24), 10) || 24,
        aspect: wval(node, "aspect", "16:9"),
        short_edge: parseInt(wval(node, "short_edge", 768), 10) || 768,
        snap: wval(node, "snap", "minimax_h3"),
        use_llm: !!wval(node, "use_llm", true),
        model: wval(node, "model", ""),
        system_prompt: wval(node, "system_prompt", ""),
        temperature: parseFloat(wval(node, "temperature", 0.7)),
        max_tokens: parseInt(wval(node, "max_tokens", 0), 10) || 0,
        seed: parseInt(wval(node, "seed", 0), 10) || 0,
    };
    const r = await gr.post("/story/plan", { params, client_id: clientId() });
    toast("success", `План «${r.label}» готов`,
        "подключите Раскадровку и Видеоряд (коннектором или дропдауном «проект:»), "
        + "захватите шаблоны, затем «Кадры ▶»");
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
    // История/Раскадровка/Видеоряд — три ноды одного проекта; открываем на
    // Видеоряде (он показывает сегменты — обычно то, ради чего открывают
    // проект повторно). Остальные две можно добавить и привязать вручную.
    const type = mode === "story" ? NODE_VIDEOSEQ : NODE_LV;
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
            this.status.textContent = "добавьте воркера в ноде «GPU RAID Воркеры»";
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
            this.status.textContent =
                `задание ${r.job_id} — прогресс в ноде «Воркеры» и тостах`;
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
        const box = nodeBody(node, { width: 520, height: 380, minHeight: 200 });
        const bar = el("div", { class: "gr-btns gr-node-bar" });
        this.anaBtn = el("button", { class: "gr-btn",
            title: "разрезать текущий workflow на стадии по привязкам лоадеров "
                + "и показать раскладку" },
            "Раскладка");
        this.anaBtn.onclick = () => this.analyze();
        this.runBtn = el("button", { class: "gr-btn gr-primary",
            title: "запустить стадии по привязкам лоадеров (Queue делает то же)" },
            "Запустить конвейер ▶");
        this.runBtn.onclick = () => this.run();
        bar.append(this.anaBtn, this.runBtn);
        box.appendChild(bar);
        box.appendChild(el("div", { class: "gr-muted gr-node-hint" },
            "Где какой модели считаться — задаётся в самих нодах-лоадерах "
            + "(блок GPU RAID: локально / Colab / Kaggle / воркер). Здесь — "
            + "раскладка стадий, предупреждения, запуск и прогресс. "
            + "Промежуточные тензоры едут бандлами, спец-ноды в графе не нужны."));
        this.report = el("div", { class: "gr-node-report" });
        box.appendChild(this.report);
        this.progress = el("div", { class: "gr-progress" });
        box.appendChild(this.progress);
        this.renderReport();
    }

    async analyze() {
        this.anaBtn.disabled = true;
        try {
            const p = await app.graphToPrompt();
            this._report = await gr.post("/pipeline/analyze",
                { graph: p.output, workflow_ui: p.workflow });
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
            box.appendChild(el("div", { class: "gr-muted" },
                "нажмите «Раскладка» — покажу, какая стадия на какой GPU поедет "
                + "(по привязкам в нодах-лоадерах)"));
            return;
        }
        for (const w of r.warnings || []) {
            box.appendChild(el("div", { class: "gr-muted" }, `⚠ ${esc(w)}`));
        }
        const workerName = (id) => {
            const w = (r.workers || []).find((x) => x.id === id);
            return w ? `${w.name} (${w.vram_gb} ГБ)` : (id || "—");
        };
        for (const isl of r.islands || []) {
            const row = el("div", { class: "gr-job" });
            const models = Object.entries(isl.models || {})
                .flatMap(([f, names]) => names.map((n) => `${f}/${n}`));
            const via = isl.assign
                ? ` <span class="gr-muted">· привязка «${esc(isl.assign_label)}»`
                  + (isl.decided_by ? ` из ${esc(isl.decided_by)}` : "") + "</span>"
                : ' <span class="gr-muted">· авто</span>';
            row.appendChild(el("div", {},
                `<b>Стадия ${isl.id}</b> → ${esc(workerName(isl.worker_id))}${via}`
                + `<div class="gr-muted">~${esc(String(isl.vram_est_gb))} ГБ VRAM · ${esc(isl.classes.join(", "))}</div>`
                + (models.length ? `<div class="gr-muted">${esc(models.join(" · "))}</div>` : "")));
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
            const p = await app.graphToPrompt();
            const resp = await gr.post("/pipeline/start", {
                graph: p.output, workflow_ui: p.workflow,
                label: wval(this.node, "label", "pipeline"), client_id: clientId(),
            });
            toast("success", `Конвейер запущен: ${resp.stages} стадий`);
            this.watch(resp.job_id);
        } catch (e) {
            toast(e.status === 409 ? "warn" : "error", "Конвейер не запущен", e.message, 8000);
        } finally {
            this.runBtn.disabled = false;
        }
    }

    /** Прогресс стадий прямо в ноде: поллинг снапшота job'а до финала. */
    watch(jobId) {
        clearInterval(this._timer);
        const tick = async () => {
            let snap;
            try { snap = await gr.get(`/jobs/${jobId}`); }
            catch (e) { clearInterval(this._timer); this._timer = null; return; }
            this.renderJob(snap);
            if (snap.finished || ["COMPLETE", "FAILED", "PARTIAL", "CANCELLED"].includes(snap.state)) {
                clearInterval(this._timer);
                this._timer = null;
            }
        };
        this._timer = setInterval(tick, 3000);
        tick();
    }

    renderJob(snap) {
        const box = this.progress;
        box.innerHTML = "";
        if (!snap) return;
        const row = el("div", { class: "gr-job" });
        row.appendChild(el("div", { class: "gr-job-head" },
            `<b>${esc(snap.label || snap.job_id)}</b> <span class="gr-muted">${esc(snap.state)}</span>`));
        for (const u of snap.units || []) {
            const pct = u.progress && u.progress[1]
                ? ` · ${Math.round(u.progress[0] / u.progress[1] * 100)}%` : "";
            row.appendChild(el("div", { class: "gr-muted gr-unit" },
                `${esc(u.label || `стадия ${u.index}`)} · ${esc(u.state)}${pct}`
                + (u.error ? ` · <span class="gr-err">${esc(u.error)}</span>` : "")));
        }
        const finished = ["COMPLETE", "FAILED", "PARTIAL", "CANCELLED"].includes(snap.state);
        if (!finished) {
            const cancel = el("button", { class: "gr-btn gr-small gr-danger" }, "Отменить");
            cancel.onclick = () => gr.post(`/jobs/${snap.job_id}/cancel`).catch(() => {});
            row.appendChild(cancel);
        }
        box.appendChild(row);
    }

    onConfigure() { this._report = null; this.renderReport(); }
    dispose() { clearInterval(this._timer); }
}

// ---------------------------------------------------------------- регистрация

const BUILDERS = {
    [NODE_STORY]: (node) => new ProjectNodeUI(node, {
        runLabel: "План ▶",
        runTitle: "разобрать сюжет на сегменты с таймингом (LLM или эвристика); "
            + "ничего не рендерится",
        errTitle: "План не составлен",
        stage: "story",
        hint: "Раскадровка и Видеоряд подключаются коннектором (или дропдауном «проект:»).",
        size: { width: 540, height: 620, minHeight: 320 },
        onRun: runStoryPlan,
    }),
    [NODE_STORYBOARD]: (node) => {
        wireSettingWidget(node, "continuity_mode", "storyboard_settings");
        return new ProjectNodeUI(node, {
            stage: "storyboard",
            hint: "Шаблон кадра — «Шаблон кадра из канвы» ниже (свой T2I-workflow на "
                + "канве: GPURAID:PROMPT + Save-нода) или встроенный дефолт (Z-Image). "
                + "Непрерывность стиля — style_bible в ноде Истории.",
            size: { width: 540, height: 520, minHeight: 280 },
        });
    },
    [NODE_VIDEOSEQ]: (node) => {
        for (const name of ["prompt_format", "preview_short_edge", "preview_steps"]) {
            wireSettingWidget(node, name, "video_settings");
        }
        return new ProjectNodeUI(node, {
            stage: "videoseq",
            hint: "Шаблон сегмента — «Шаблон сегмента из канвы» ниже (FLF2V-workflow на "
                + "канве: GPURAID:START_IMAGE/END_IMAGE + Save-видео-нода, опционально "
                + "GPURAID:STEPS для дешёвого черновика).",
            size: { width: 540, height: 560, minHeight: 300 },
        });
    },
    [NODE_LV]: (node) => new ProjectNodeUI(node, {
        runLabel: "Собрать ▶",
        runTitle: "запустить сборку длинного видео по параметрам ноды",
        errTitle: "Длинное видео не запущено",
        stage: "chain",
        hint: "Шаблон сегмента = текущий канвас (маркеры GPURAID:START_IMAGE, "
            + "GPURAID:END_IMAGE, GPURAID:PROMPT, GPURAID:VIDEO_OUT).",
        size: { width: 540, height: 560, minHeight: 300 },
        onRun: runLongVideo,
    }),
    [NODE_OFFLOAD]: (node) => new OffloadNodeUI(node),
    [NODE_PIPELINE]: (node) => new PipelineNodeUI(node),
    [NODE_MODELS]: (node) => new ModelsNodeUI(
        node, nodeBody(node, { width: 560, height: 420, minHeight: 220 })),
    [NODE_WORKERS]: (node) => new WorkersNodeUI(
        node, nodeBody(node, { width: 560, height: 540, minHeight: 260 })),
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

        const origConnChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function () {
            const r = origConnChange?.apply(this, arguments);
            try { this.__gr?.onConnectionsChange?.(...arguments); } catch (e) { /* ignore */ }
            return r;
        };
    },
});
