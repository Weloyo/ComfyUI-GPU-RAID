// Runtime-блок в нодах-лоадерах: привязка «модель → воркер» прямо на канве.
//
// Каждый лоадер (UNETLoader, CLIPLoader, VAELoader, …) получает в теле ноды
// блок GPU RAID: где считать эту модель (локально / Colab / Kaggle / конкретный
// воркер), статус этого рантайма, кнопки запуска/остановки, сценарий автостопа,
// наличие файла на воркере и закачку с публичной ссылки. Нода подкрашивается
// цветом платформы — раскладка большой модели видна с одного взгляда.
//
// Привязка хранится в properties.gpuraid_runtime и уезжает вместе с workflow;
// бэкенд собирает её оттуда же (gpu_raid/placement.py) при запуске конвейера.
import { app } from "../../../scripts/app.js";
import { gr, toast } from "./api.js";
import { el, esc, fmtGb, stateDot, workerError } from "./format.js";

export const RUNTIME_PROP = "gpuraid_runtime";
// шина «Воркеры → лоадер»: один и тот же провод от единственного выхода ноды
// Воркеров; после подключения в лоадере открывается список доступных воркеров
// (сама привязка выбирается дропдауном и пишется в RUNTIME_PROP)
export const RUNTIME_TYPE = "GPURAID_RUNTIME";
export const RUNTIME_INPUT = "gpuraid_runtime";

try {   // цвет проводов шины — тот же синий, что у точки «остановлен»
    window.LGraphCanvas.link_type_colors[RUNTIME_TYPE] = "#4a86b8";
} catch (e) { /* litegraph ещё не готов — не критично, цвет дефолтный */ }

// заголовок/тело ноды по платформе привязки — «интуитивно понятно, где считает»
const NODE_COLORS = {
    local: { color: "#2c5f2d", bgcolor: "#243324" },
    colab: { color: "#8a5a12", bgcolor: "#39301c" },
    kaggle: { color: "#1c6ea0", bgcolor: "#1e2c38" },
    other: { color: "#5f4a8a", bgcolor: "#2c2738" },
};

const SCENARIOS = [
    ["", "сценарий: как у всех"],
    ["keep", "⚡ держать"],
    ["eco", "🌙 эко (автостоп)"],
    ["instant", "⏻ гасить сразу"],
];

function widget(node, name) {
    return (node.widgets || []).find((w) => w.name === name);
}

// ---------------------------------------------------------------- реестр

let _optionsPromise = null;   // GET /runtime/options — классы лоадеров и опции
let _lastOptions = null;

function fetchOptions(force = false) {
    if (!_optionsPromise || force) {
        _optionsPromise = gr.get("/runtime/options")
            .then((r) => { _lastOptions = r; return r; })
            .catch((e) => { _optionsPromise = null; throw e; });
    }
    return _optionsPromise;
}

const BLOCKS = new Set();
let _timer = null;
let _ticking = false;

function ensurePolling() {
    if (!_timer) _timer = setInterval(() => pollAll(), 8000);
}

async function pollAll(immediate = false) {
    if (_ticking || !BLOCKS.size) return;
    _ticking = true;
    try {
        const options = await fetchOptions(true);
        const items = [];
        const seen = new Set();
        for (const b of BLOCKS) {
            for (const it of b.collect()) {
                const key = `${it.assign}|${it.folder}|${it.filename}`;
                if (!seen.has(key)) { seen.add(key); items.push(it); }
            }
        }
        let statusMap = new Map();
        if (items.length) {
            try {
                const st = await gr.post("/runtime/status", { items });
                for (const it of st.items || []) {
                    statusMap.set(`${it.assign}|${it.folder}|${it.filename}`, it);
                }
            } catch (e) { /* сервер занят — покажем в следующий тик */ }
        }
        for (const b of BLOCKS) b.render(options, statusMap);
    } catch (e) {
        /* сервер недоступен — блоки остаются с прошлым статусом */
    } finally {
        _ticking = false;
    }
}

// ---------------------------------------------------------------- блок ноды

class LoaderRuntimeUI {
    constructor(node, modelInputs) {
        this.node = node;
        this.modelInputs = modelInputs;    // {имя_виджета: папка_моделей}
        if (!node.properties) node.properties = {};
        if (node.properties[RUNTIME_PROP] === undefined) node.properties[RUNTIME_PROP] = "";

        const box = el("div", { class: "gr-node gr-loader" });
        box.addEventListener("wheel", (e) => e.stopPropagation(), { passive: false });

        const bar = el("div", { class: "gr-row" });
        this.dot = el("span", { class: "gr-dot gr-dot-gray" });
        this.sel = el("select", { class: "gr-select gr-grow",
            title: "где считать эту модель: воркер/платформа рантайма" });
        this.sel.onchange = () => this.setAssign(this.sel.value);
        this.startBtn = el("button", { class: "gr-btn gr-small",
            title: "запустить рантайм этой привязки" }, "▶");
        this.startBtn.onclick = () => this.start();
        this.stopBtn = el("button", { class: "gr-btn gr-small gr-danger",
            title: "остановить рантайм (квота перестанет тратиться)" }, "⏻");
        this.stopBtn.onclick = () => this.stop();
        bar.append(this.dot, this.sel, this.startBtn, this.stopBtn);
        box.appendChild(bar);

        this.statusLine = el("div", { class: "gr-muted gr-loader-status" });
        box.appendChild(this.statusLine);

        this.modelsBox = el("div", { class: "gr-loader-models" });
        box.appendChild(this.modelsBox);

        const scRow = el("div", { class: "gr-row" });
        this.scenarioSel = el("select", { class: "gr-select gr-grow",
            title: "автостоп рантайма этой привязки (платформы или воркера); "
                + "минуты простоя для «эко» — глобальные" });
        for (const [value, label] of SCENARIOS) {
            this.scenarioSel.appendChild(el("option", { value }, esc(label)));
        }
        this.scenarioSel.onchange = () => this.setScenario(this.scenarioSel.value);
        scRow.append(this.scenarioSel);
        this.scenarioRow = scRow;
        box.appendChild(scRow);

        node.addDOMWidget("gpuraid_runtime_ui", "gpuraid", box, {
            serialize: false,
            hideOnZoom: false,
            getMinHeight: () => 118,
        });
        const minW = 300, addH = 128;
        const baseH = node.computeSize ? node.computeSize()[1] : 0;
        node.size = [Math.max(node.size?.[0] || 0, minW),
                     Math.max(node.size?.[1] || 0, baseH + addH)];

        this._origColors = { color: node.color, bgcolor: node.bgcolor };
        this._urlOpen = {};
        this.wired = false;
        this.ensureInput();
        BLOCKS.add(this);
        ensurePolling();
        if (_lastOptions) this.render(_lastOptions, new Map());
        pollAll();
    }

    dispose() { BLOCKS.delete(this); }

    get assign() { return String(this.node.properties?.[RUNTIME_PROP] || ""); }

    // ------------------------------------------------------------ провод

    /** Вход-шина для провода от ноды «Воркеры» (переживает configure). */
    ensureInput() {
        const node = this.node;
        if (!(node.inputs || []).some((i) => i.name === RUNTIME_INPUT)) {
            node.addInput(RUNTIME_INPUT, RUNTIME_TYPE);
        }
        const inp = node.inputs.find((i) => i.name === RUNTIME_INPUT);
        if (inp) inp.label = "воркеры";
    }

    /** Подключён ли провод от ноды «Воркеры»: он открывает список воркеров
     *  в дропдауне (сам выбор — за дропдауном; отключение провода выбранную
     *  привязку не сбрасывает, но менять её снова можно только с проводом). */
    syncFromWire() {
        const node = this.node;
        const inp = (node.inputs || []).find((i) => i.name === RUNTIME_INPUT);
        this.wired = inp != null && inp.link != null;
    }

    onWireChange() {
        this.syncFromWire();
        if (_lastOptions) this.render(_lastOptions, new Map());
        pollAll(true);
    }

    // ------------------------------------------------------------ данные

    /** Значение модели каждого model-входа (виджет может быть переведён в линк). */
    models() {
        const out = [];
        for (const [name, folder] of Object.entries(this.modelInputs)) {
            const w = widget(this.node, name);
            const value = w && typeof w.value === "string" ? w.value : "";
            out.push({ input: name, folder, filename: value });
        }
        return out;
    }

    collect() {
        const assign = this.assign;
        if (!assign) return [];
        return this.models().filter((m) => m.filename)
            .map((m) => ({ assign, folder: m.folder, filename: m.filename }));
    }

    // ------------------------------------------------------------ действия

    setAssign(value) {
        this.node.properties[RUNTIME_PROP] = value || "";
        this.paint(value || "");
        this.node.setDirtyCanvas(true, true);
        pollAll(true);
    }

    async start() {
        this.startBtn.disabled = true;
        try {
            const r = await gr.post("/runtime/start", { assign: this.assign });
            if (r.open_url) window.open(r.open_url, "_blank");
            toast(r.already ? "info" : "success", "GPU RAID", r.detail || "запущено", 8000);
        } catch (e) {
            toast(e.status === 409 ? "warn" : "error", "Рантайм не запущен", e.message, 9000);
        } finally {
            this.startBtn.disabled = false;
            pollAll(true);
        }
    }

    async stop() {
        const opt = this.currentOption();
        const name = opt?.worker?.name || opt?.label || this.assign;
        if (!confirm(`Остановить рантайм «${name}»?`)) return;
        this.stopBtn.disabled = true;
        try {
            const r = await gr.post("/runtime/stop", { assign: this.assign });
            if (!r.stopped) toast("warn", "Воркер не остановлен — смотрите тосты");
        } catch (e) {
            toast(e.status === 409 ? "warn" : "error", "Не остановлен", e.message, 8000);
        } finally {
            this.stopBtn.disabled = false;
            pollAll(true);
        }
    }

    async setScenario(policy) {
        try {
            await gr.post("/runtime/scenario", {
                assign: this.assign, scenario: { policy },
            });
            toast("success", "Сценарий сохранён",
                policy ? "" : "привязка наследует глобальную политику");
        } catch (e) {
            toast(e.status === 409 ? "warn" : "error", "Сценарий не сохранён", e.message, 8000);
        }
        pollAll(true);
    }

    async download(m, workerId) {
        try {
            const r = await gr.post("/models/distribute", {
                items: [{ folder: m.folder, filename: m.filename, workers: [workerId] }],
            });
            for (const err of r.errors || []) toast("warn", "Закачка не запущена", err, 9000);
            if ((r.started || []).length) toast("info", "Закачка запущена", m.filename);
        } catch (e) {
            toast("error", "Закачка не запущена", e.message, 8000);
        }
        pollAll(true);
    }

    async saveUrl(m, url) {
        try {
            const r = await gr.post("/models/library", {
                url, filename: m.filename, folder: m.folder });
            if (r.warning) toast("warn", "Ссылка принята, но подозрительная", r.warning, 9000);
            else toast("success", "Источник сохранён", m.filename);
            this._urlOpen[m.input] = false;
        } catch (e) {
            toast("error", "Ссылка не сохранена", e.message, 8000);
        }
        pollAll(true);
    }

    // ------------------------------------------------------------ отрисовка

    currentOption() {
        const options = _lastOptions?.options || [];
        return options.find((o) => o.value === this.assign) || null;
    }

    paint(assign) {
        const plat = !assign ? ""
            : assign === "local" ? "local"
            : assign.startsWith("platform:") ? assign.split(":", 2)[1]
            : "other";
        const c = NODE_COLORS[plat] || (plat ? NODE_COLORS.other : null);
        if (c) {
            this.node.color = c.color;
            this.node.bgcolor = c.bgcolor;
        } else {
            this.node.color = this._origColors.color;
            this.node.bgcolor = this._origColors.bgcolor;
        }
    }

    render(options, statusMap) {
        const assign = this.assign;
        const opts = options?.options || [];
        // --- список воркеров: открывается проводом от ноды «Воркеры» ---
        this.sel.innerHTML = "";
        if (this.wired) {
            this.sel.appendChild(el("option", { value: "" }, "— авто (раскладка сама) —"));
            let found = !assign;
            for (const o of opts) {
                const isCurrent = o.value === assign;
                // в списке ТОЛЬКО активные воркеры; неактивный показываем лишь
                // если он уже выбран (иначе селект врал бы о текущей привязке)
                if (o.state !== "online" && !isCurrent) continue;
                const w = o.worker;
                const label = o.state === "online"
                    ? `${o.label}${w?.gpu ? ` — ${w.gpu}` : ""}`
                    : `${o.label} — ${o.state === "none" ? "не запускался"
                        : o.state === "stopped" ? "остановлен" : "не в сети"}`;
                this.sel.appendChild(el("option", { value: o.value }, esc(label)));
                if (isCurrent) found = true;
            }
            if (!found) this.sel.appendChild(el("option", { value: assign },
                esc(`${assign} (нет в реестре)`)));
            this.sel.disabled = false;
            this.sel.title = "где считать эту модель — из воркеров, которые "
                + "сейчас в сети; поднять новых можно кнопками в ноде «Воркеры»";
        } else {
            // без провода список закрыт: видна только уже выбранная привязка
            if (assign) {
                const cur = opts.find((o) => o.value === assign);
                this.sel.appendChild(el("option", { value: assign },
                    esc(cur ? cur.label : assign)));
            } else {
                this.sel.appendChild(el("option", { value: "" },
                    "— подключите ноду «Воркеры» —"));
            }
            this.sel.disabled = true;
            this.sel.title = "список воркеров открывается проводом от ноды "
                + "«GPU RAID Воркеры» (вход «воркеры» слева)";
        }
        this.sel.value = assign;

        // --- статус привязки ---
        const opt = opts.find((o) => o.value === assign);
        this.paint(assign);
        if (!assign) {
            this.dot.className = "gr-dot gr-dot-gray";
            this.statusLine.textContent = this.wired
                ? "выберите воркера из списка (пусто = авто-раскладка)"
                : "подключите провод от ноды «Воркеры» — появится список воркеров";
            this.startBtn.style.display = "none";
            this.stopBtn.style.display = "none";
            this.scenarioRow.style.display = "none";
        } else if (opt) {
            this.dot.className = `gr-dot ${stateDot(opt.state === "none" ? "unknown" : opt.state)}`;
            // привязка живёт и без провода — но менять её можно только с ним
            const wirePrefix = this.wired ? "" : "⊶ провод отключён · ";
            const w = opt.worker;
            if (w) {
                this.statusLine.textContent = wirePrefix
                    + `${w.name}${w.gpu ? ` · ${w.gpu}` : ""}`
                    + (w.vram_total_gb ? ` · ${fmtGb(w.vram_total_gb)}` : "")
                    + (w.latency_ms != null ? ` · ${w.latency_ms}мс` : "");
            } else {
                const err = workerError(opt.workers?.[0]?.error, opt.state);
                this.statusLine.textContent = wirePrefix
                    + (opt.state === "none"
                        ? (opt.can_start ? "воркера ещё нет — нажмите ▶, чтобы запустить"
                                         : "воркера ещё нет — поднимите его на платформе")
                        : `не в сети${err ? ` — ${err}` : ""}`
                          + (opt.can_start ? " · ▶ запустит заново" : ""));
            }
            this.startBtn.style.display = opt.can_start && !w ? "" : "none";
            this.stopBtn.style.display = opt.can_stop ? "" : "none";
            const cloud = assign !== "local";
            this.scenarioRow.style.display = cloud ? "" : "none";
            if (cloud) {
                const pol = opt.scenario?.policy || "";
                if (this.scenarioSel.value !== pol) this.scenarioSel.value = pol;
            }
        } else {
            this.dot.className = "gr-dot gr-dot-red";
            this.statusLine.textContent = "привязка не найдена в реестре (воркер удалён?)";
            this.startBtn.style.display = "none";
            this.stopBtn.style.display = "none";
            this.scenarioRow.style.display = "none";
        }

        // --- модели: наличие на воркере привязки, ссылка, закачка ---
        this.renderModels(opt, statusMap);
    }

    renderModels(opt, statusMap) {
        const box = this.modelsBox;
        box.innerHTML = "";
        const assign = this.assign;
        for (const m of this.models()) {
            if (!m.filename) {
                box.appendChild(el("div", { class: "gr-muted" },
                    `${esc(m.input)}: имя модели задаётся линком — наличие не проверить`));
                continue;
            }
            const st = statusMap.get(`${assign}|${m.folder}|${m.filename}`);
            const row = el("div", { class: "gr-loader-model" });
            const line = el("div", { class: "gr-row" });
            let mark = "?", cls = "yellow", hint = "наличие неизвестно";
            if (!assign) { mark = "·"; cls = "yellow"; hint = "привязка не задана"; }
            else if (st?.present === "have") { mark = "✓"; cls = "green"; hint = "файл на воркере есть"; }
            else if (st?.present === "missing") { mark = "✕"; cls = "red"; hint = "файла на воркере нет"; }
            line.appendChild(el("span", { class: `gr-chip gr-chip-${cls}`, title: hint }, mark));
            line.appendChild(el("span", { class: "gr-grow gr-loader-file", title: `${m.folder}/${m.filename}` },
                esc(m.filename) + (st?.size_gb ? ` <span class="gr-muted">· ${esc(String(st.size_gb))} ГБ</span>` : "")));
            const linkBtn = el("button", { class: "gr-btn gr-small",
                title: st?.url ? `источник: ${st.url}` : "ссылка-источник не задана" },
                st?.url ? "🔗" : "🔗?");
            linkBtn.onclick = () => {
                this._urlOpen[m.input] = !this._urlOpen[m.input];
                this.render(_lastOptions, statusMap);
            };
            line.appendChild(linkBtn);
            if (st?.present === "missing" && opt?.worker) {
                if (st.url) {
                    const dl = el("button", { class: "gr-btn gr-small gr-primary",
                        title: "воркер скачает файл сам, с публичной ссылки" }, "Скачать");
                    dl.onclick = () => this.download(m, opt.worker.id);
                    line.appendChild(dl);
                } else {
                    this._urlOpen[m.input] = true;
                }
            }
            row.appendChild(line);

            if (this._urlOpen[m.input]) {
                const urlRow = el("div", { class: "gr-row" });
                const input = el("input", { class: "gr-input",
                    placeholder: `https://…/${m.filename}`, value: st?.url || "" });
                const save = el("button", { class: "gr-btn gr-small gr-primary" }, "Сохранить");
                save.onclick = () => this.saveUrl(m, input.value.trim());
                urlRow.append(input, save);
                row.appendChild(urlRow);
                if (!st?.url) row.appendChild(el("div", { class: "gr-muted" },
                    "прямая публичная ссылка (HF «Download» …/resolve/… или Civitai "
                    + "api/download) — сохранится в библиотеку источников"));
            }

            const dlTask = st?.download;
            if (dlTask && dlTask.state !== "done") {
                const frac = dlTask.bytes_total ? dlTask.bytes_done / dlTask.bytes_total : 0;
                const barBox = el("div", { class: "gr-bar" });
                barBox.appendChild(el("div", { class: "gr-bar-fill",
                    style: `width:${Math.round(frac * 100)}%` }));
                row.appendChild(barBox);
                row.appendChild(el("div", { class: "gr-muted" },
                    `закачка: ${esc(dlTask.state)}`
                    + (dlTask.bytes_total
                        ? ` · ${(dlTask.bytes_done / 2 ** 30).toFixed(1)}/${(dlTask.bytes_total / 2 ** 30).toFixed(1)} ГБ`
                        : "")
                    + (dlTask.error ? ` · <span class="gr-err">${esc(dlTask.error)}</span>` : "")));
            }
            box.appendChild(row);
        }
    }

    onConfigure() {
        // workflow загружен: вход-пип мог перетереться сериализацией, провод
        // и привязка приехали заново — восстановить и обновить
        this.ensureInput();
        this.syncFromWire();
        this.paint(this.assign);
        if (_lastOptions) this.render(_lastOptions, new Map());
        pollAll(true);
    }
}

// ---------------------------------------------------------------- регистрация

app.registerExtension({
    name: "GPURaid.LoaderUI",
    async beforeRegisterNodeDef(nodeType, nodeData) {
        let classes;
        try {
            classes = (await fetchOptions()).loader_classes || {};
        } catch (e) {
            return;   // сервер ещё не поднял /gpuraid — лоадеры останутся обычными
        }
        const modelInputs = classes[nodeData?.name];
        if (!modelInputs || !Object.keys(modelInputs).length) return;

        const origCreated = nodeType.prototype.onNodeCreated;
        nodeType.prototype.onNodeCreated = function () {
            const r = origCreated?.apply(this, arguments);
            try { this.__grLoader = new LoaderRuntimeUI(this, modelInputs); }
            catch (e) { console.error("GPU RAID: runtime-блок лоадера не построен", e); }
            return r;
        };

        const origConfigure = nodeType.prototype.onConfigure;
        nodeType.prototype.onConfigure = function () {
            const r = origConfigure?.apply(this, arguments);
            try { this.__grLoader?.onConfigure?.(); } catch (e) { /* ignore */ }
            return r;
        };

        const origRemoved = nodeType.prototype.onRemoved;
        nodeType.prototype.onRemoved = function () {
            try { this.__grLoader?.dispose?.(); } catch (e) { /* ignore */ }
            this.__grLoader = null;
            return origRemoved?.apply(this, arguments);
        };

        const origConnChange = nodeType.prototype.onConnectionsChange;
        nodeType.prototype.onConnectionsChange = function (type, index) {
            const r = origConnChange?.apply(this, arguments);
            try {
                const INPUT = window.LiteGraph?.INPUT;
                if ((INPUT === undefined || type === INPUT)
                    && this.inputs?.[index]?.name === RUNTIME_INPUT) {
                    this.__grLoader?.onWireChange?.();
                }
            } catch (e) { /* ignore */ }
            return r;
        };
    },
});

/** Есть ли в workflow лоадеры с удалёнными привязками (platform:/id:).
 *  Subgraph'ы обходятся только по реально инстанцированным определениям —
 *  зеркально gpu_raid/placement.iter_workflow_nodes. */
export function workflowHasRemoteAssignments(workflow) {
    const defs = new Map();
    for (const sg of workflow?.definitions?.subgraphs || []) {
        if (sg && sg.id != null) defs.set(String(sg.id), sg);
    }
    const isRemote = (n) => {
        const a = String(n?.properties?.[RUNTIME_PROP] || "");
        return a.startsWith("platform:") || a.startsWith("id:");
    };
    const walk = (container, seen) => {
        for (const node of container?.nodes || []) {
            if (isRemote(node)) return true;
            const sub = defs.get(String(node?.type));
            if (sub && !seen.has(String(sub.id))
                && walk(sub, new Set([...seen, String(sub.id)]))) {
                return true;
            }
        }
        return false;
    };
    return walk(workflow, new Set());
}
