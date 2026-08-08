// GPU RAID — входной модуль расширения: настройки, перехват Queue, sidebar, события.
//
// Разделение труда в UI: рабочая область (канва) — ноды-пульты с раскадровкой,
// промптами, кадрами и запуском (web/lib/nodeui.js); левая панель — воркеры,
// настройки и мониторинг (web/lib/panel.js).
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { gr, toast, clientId } from "./lib/api.js";
import { GPURaidPanel } from "./lib/panel.js";
import { broadcast } from "./lib/editor.js";
import { NODE_LV, NODE_OFFLOAD, NODE_PIPELINE, NODE_STORY } from "./lib/nodeui.js";

let panel = null;

function setting(id, fallback) {
    try {
        const v = app.extensionManager.setting.get(id);
        return v === undefined ? fallback : v;
    } catch (e) {
        return fallback;
    }
}

function injectCss() {
    const href = new URL("./style.css", import.meta.url).href;
    if (!document.querySelector(`link[href="${href}"]`)) {
        const link = document.createElement("link");
        link.rel = "stylesheet";
        link.href = href;
        document.head.appendChild(link);
    }
}

function graphHasStripe(output) {
    if (!output) return false;
    let d = false, c = false;
    for (const node of Object.values(output)) {
        if (node.class_type === "GPURAID_Distributor") d = true;
        else if (node.class_type === "GPURAID_Collector") c = true;
        if (d && c) return true;
    }
    return false;
}

/** Нода-пульт класса cls, у которой не снят on_queue (и которая уедет в prompt). */
function armedNode(output, cls) {
    for (const [id, node] of Object.entries(output || {})) {
        if (node.class_type !== cls) continue;
        if (node.inputs && node.inputs.on_queue === false) continue;
        const live = app.graph?.getNodeById?.(Number(id));
        if (live) return live;
    }
    return null;
}

// приоритет, если на канве armed сразу несколько пультов
const PULTS = [
    [NODE_STORY, "Сценарист"],
    [NODE_PIPELINE, "Конвейер"],
    [NODE_OFFLOAD, "Выполнить на воркере"],
    [NODE_LV, "Длинное видео"],
];

const NOOP_QUEUE = { prompt_id: "", number: -1, node_errors: {} };

function hookQueue() {
    const original = api.queuePrompt.bind(api);
    api.queuePrompt = async function (number, data, ...rest) {
        try {
            if (!setting("GPURaid.Enabled", true)) return original(number, data, ...rest);
            const output = data?.output;

            // 1) ноды-пульты: Queue = «сделай то, что написано на кнопке ноды»
            const armed = PULTS
                .map(([cls, title]) => [armedNode(output, cls), title])
                .filter(([node]) => node);
            if (armed.length) {
                const [node, title] = armed[0];
                if (armed.length > 1) {
                    toast("warn", `GPU RAID: активных пультов несколько — выполняю «${title}»`,
                        armed.map(([, t]) => t).join(", ")
                        + ". Снимите on_queue у лишних нод.", 8000);
                }
                if (!node.__gr?.run) return original(number, data, ...rest);
                await node.__gr.run();
                return NOOP_QUEUE;
            }

            // 2) страйпинг: Distributor+Collector в графе
            if (!graphHasStripe(output)) return original(number, data, ...rest);
            try {
                const r = await gr.post("/stripe", {
                    graph: output,
                    workflow_ui: data.workflow,
                    client_id: clientId(),
                });
                toast("info", "GPU RAID: страйпинг запущен",
                    `${r.units} вариантов на ${r.workers.length} GPU`);
                return { prompt_id: r.job_id, number: -1, node_errors: {} };
            } catch (e) {
                if (setting("GPURaid.FallbackLocal", true)) {
                    if (setting("GPURaid.Debug", false) || e.status !== 409) {
                        toast("warn", "GPU RAID: выполняю локально", e.message, 4000);
                    }
                    return original(number, data, ...rest);
                }
                toast("error", "GPU RAID", e.message);
                throw e;
            }
        } catch (e) {
            if (e?.__gpuraid_rethrow) throw e;
            // любой сбой перехвата не должен терять нажатие Queue
            return original(number, data, ...rest);
        }
    };
}

const EVENTS = ["worker", "unit", "job_started", "job_done", "longvideo"];

app.registerExtension({
    name: "GPURaid",
    settings: [
        {
            id: "GPURaid.Enabled",
            name: "Включить распределение (перехват Queue)",
            type: "boolean",
            defaultValue: true,
        },
        {
            id: "GPURaid.FallbackLocal",
            name: "При недоступности воркеров выполнять локально",
            type: "boolean",
            defaultValue: true,
        },
        {
            id: "GPURaid.Debug",
            name: "Debug-уведомления",
            type: "boolean",
            defaultValue: false,
        },
    ],
    aboutPageBadges: [
        { label: "GPU RAID", url: "https://github.com/Weloyo/ComfyUI-GPU-RAID", icon: "pi pi-server" },
    ],
    setup() {
        injectCss();
        hookQueue();
        for (const name of EVENTS) {
            api.addEventListener("gpuraid." + name, (ev) => {
                try { panel?.onEvent(name, ev.detail); } catch (e) { /* ignore */ }
                try { broadcast(name, ev.detail); } catch (e) { /* ignore */ }
            });
        }
        api.addEventListener("gpuraid.toast", (ev) => {
            const d = ev.detail || {};
            toast(d.severity || "info", d.text || "GPU RAID", "", d.life || 6000);
        });
        try {
            app.extensionManager.registerSidebarTab({
                id: "gpu-raid",
                icon: "pi pi-server",
                title: "GPU RAID",
                tooltip: "GPU RAID — воркеры, режимы и задания",
                type: "custom",
                render: (el) => { panel = new GPURaidPanel(el); },
                destroy: () => { panel?.dispose(); panel = null; },
            });
        } catch (e) {
            console.error("GPU RAID: sidebar недоступен", e);
        }
        console.log("GPU RAID: расширение загружено");
    },
});
