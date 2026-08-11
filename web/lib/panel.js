// Sidebar-панель GPU RAID: ТОЛЬКО секреты и подключения.
//
// Всё остальное живёт на канве: привязка «модель → воркер» — в самих
// нодах-лоадерах (блок GPU RAID), парк машин и задания — в ноде
// «GPU RAID Воркеры», раскадровка и рендер — в нодах проектов.
// В панели остались ключи и токены — им на канве не место: workflow
// уезжает на воркеров и в git, а секреты остаются на мастере.
import { el } from "./format.js";
import { ConnectionsUI } from "./connections.js";
import { NODE_WORKERS, revealNodeOnCanvas } from "./nodeui.js";

export class GPURaidPanel {
    constructor(root) {
        this.root = root;
        this.root.classList.add("gr-panel");
        this.root.innerHTML = "";

        const box = el("details", { class: "gr-section", open: "" });
        this.summary = el("summary", {}, "Подключения и ключи");
        box.appendChild(this.summary);
        const body = el("div", { class: "gr-body" });
        box.appendChild(body);
        this.root.appendChild(box);
        this.connections = new ConnectionsUI(body);
        this.connections.onSummary = (ok, total) => {
            this.summary.textContent = `Подключения и ключи — ${ok}/${total}`;
        };

        const hint = el("div", { class: "gr-body gr-muted" },
            "Всё управление — на канве: привязка модели к воркеру и запуск "
            + "рантаймов — в нодах-лоадерах, парк машин и задания — в ноде "
            + "«GPU RAID Воркеры», запуск шардинга — Queue или нода «Конвейер».");
        this.root.appendChild(hint);
        const btn = el("button", { class: "gr-btn gr-wide" }, "Открыть ноду «Воркеры» на канве");
        btn.onclick = () => revealNodeOnCanvas(NODE_WORKERS);
        this.root.appendChild(btn);
    }

    dispose() {
        this.root.innerHTML = "";
    }

    /** События WS панели больше не нужны — их разбирают ноды на канве. */
    onEvent() {}
}
