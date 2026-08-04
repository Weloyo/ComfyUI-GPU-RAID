// GPU RAID — входной модуль расширения: настройки, перехват Queue, sidebar, события.
import { app } from "../../scripts/app.js";
import { api } from "../../scripts/api.js";
import { gr, toast, clientId } from "./lib/api.js";
import { GPURaidPanel } from "./lib/panel.js";

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

function hookQueue() {
    const original = api.queuePrompt.bind(api);
    api.queuePrompt = async function (number, data, ...rest) {
        try {
            if (!setting("GPURaid.Enabled", true)) return original(number, data, ...rest);
            const output = data?.output;
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

async function offloadDialog() {
    let workers = [];
    try {
        const r = await gr.get("/workers");
        workers = (r.workers || []).filter((w) => w.enabled && w.id !== "local");
    } catch (e) {
        toast("error", "GPU RAID", "сервер недоступен");
        return;
    }
    if (!workers.length) {
        toast("warn", "GPU RAID", "нет включённых удалённых воркеров");
        return;
    }
    const overlay = document.createElement("div");
    overlay.className = "gr-overlay";
    const dlg = document.createElement("div");
    dlg.className = "gr-dialog";
    dlg.innerHTML = "<div class='gr-subtitle'>Выполнить workflow на воркере</div>";
    for (const w of workers) {
        const b = document.createElement("button");
        b.className = "gr-btn gr-wide";
        const gpu = w.status?.gpu ? ` — ${w.status.gpu}` : "";
        b.textContent = `${w.name}${gpu} (${w.status?.state || "?"})`;
        b.onclick = async () => {
            overlay.remove();
            try {
                const p = await app.graphToPrompt();
                const r = await gr.post("/offload", {
                    graph: p.output, workflow_ui: p.workflow,
                    worker_id: w.id, label: "offload", client_id: clientId(),
                });
                toast("info", "GPU RAID: offload запущен", (r.warnings || []).join("; "));
            } catch (e) {
                toast("error", "Offload не запущен", e.message);
            }
        };
        dlg.appendChild(b);
    }
    const cancel = document.createElement("button");
    cancel.className = "gr-btn";
    cancel.textContent = "Отмена";
    cancel.onclick = () => overlay.remove();
    dlg.appendChild(cancel);
    overlay.appendChild(dlg);
    overlay.onclick = (ev) => { if (ev.target === overlay) overlay.remove(); };
    document.body.appendChild(overlay);
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
    commands: [
        {
            id: "GPURaid.RunOnWorker",
            label: "GPU RAID: Run on worker…",
            function: offloadDialog,
        },
    ],
    menuCommands: [
        { path: ["Extensions", "GPU RAID"], commands: ["GPURaid.RunOnWorker"] },
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
                tooltip: "GPU RAID — распределённая генерация",
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
