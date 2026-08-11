// Модели на воркерах: сверка графа с инвентарём и рассылка (нода на канве).
//
// Сами модели выбираются как обычно — в лоадерах на канве; там же (кнопка 🔗
// в runtime-блоке лоадера) задаются публичные ссылки-источники. Здесь только
// ответ на вопрос «у кого из воркеров этого файла нет» и кнопка, после которой
// каждый воркер тянет файл сам с публичной ссылки, мимо канала мастера.
import { app } from "../../../scripts/app.js";
import { gr, toast } from "./api.js";
import { el, esc } from "./format.js";

function gb(bytes) {
    return (bytes / 1073741824).toFixed(1);
}

// --------------------------------------------------------------- нода

export class ModelsNodeUI {
    constructor(node, body) {
        this.node = node;
        this.box = body;
        this.plan = null;
        this.tasks = [];
        this._timer = null;

        const bar = el("div", { class: "gr-btns gr-node-bar" });
        this.scanBtn = el("button", { class: "gr-btn gr-primary",
            title: "какие модели нужны текущему графу и у кого их нет" }, "Сверить граф");
        this.scanBtn.onclick = () => this.scan();
        this.sendBtn = el("button", { class: "gr-btn",
            title: "каждый воркер скачает недостающее сам, с публичной ссылки" },
            "Разослать недостающие ▶");
        this.sendBtn.onclick = () => this.distribute();
        bar.append(this.scanBtn, this.sendBtn);
        this.box.appendChild(bar);
        this.box.appendChild(el("div", { class: "gr-muted gr-node-hint" },
            "Модели выбираются в обычных лоадерах на канве. Ссылка-источник "
            + "файла — кнопка 🔗 в runtime-блоке лоадера (или ниже, если "
            + "ссылки нет)."));
        this.report = el("div", { class: "gr-node-report" });
        this.box.appendChild(this.report);
        this.progress = el("div", { class: "gr-progress" });
        this.box.appendChild(this.progress);
        this.render();
    }

    dispose() { clearInterval(this._timer); }

    async scan() {
        this.scanBtn.disabled = true;
        try {
            const p = await app.graphToPrompt();
            this.plan = await gr.post("/models/plan", { graph: p.output });
            this.render();
        } catch (e) {
            toast(e.status === 409 ? "warn" : "error", "Сверка не удалась", e.message, 8000);
        } finally { this.scanBtn.disabled = false; }
    }

    /** Строки, которых кому-то не хватает и для которых известен источник. */
    sendable() {
        return (this.plan?.models || [])
            .filter((m) => m.missing_on.length && m.url)
            .map((m) => ({ folder: m.folder, filename: m.filename, url: m.url,
                workers: m.missing_on }));
    }

    async distribute() {
        if (!this.plan) { await this.scan(); }
        const items = this.sendable();
        if (!items.length) {
            const noSrc = (this.plan?.models || []).filter((m) => m.missing_on.length && !m.url);
            toast(noSrc.length ? "warn" : "info", "Рассылать нечего",
                noSrc.length
                    ? `нет ссылок для: ${noSrc.map((m) => m.filename).join(", ")}`
                    : "у всех воркеров всё на месте", 9000);
            return;
        }
        const total = items.reduce((s, i) => s + i.workers.length, 0);
        if (!confirm(`Запустить ${total} закачек на воркерах?`)) return;
        this.sendBtn.disabled = true;
        try {
            const r = await gr.post("/models/distribute", { items });
            for (const err of r.errors || []) toast("warn", "Не запущено", err, 9000);
            toast("info", `Закачек запущено: ${(r.started || []).length}`,
                "прогресс — ниже в ноде");
            this.poll(true);
        } catch (e) {
            toast("error", "Рассылка не удалась", e.message, 8000);
        } finally { this.sendBtn.disabled = false; }
    }

    poll(immediate = false) {
        clearInterval(this._timer);
        const tick = async () => {
            try {
                this.tasks = (await gr.get("/models/progress")).tasks || [];
            } catch (e) { return; }
            this.renderProgress();
            const active = this.tasks.some((t) => t.state !== "done" && t.state !== "error");
            if (!active) {
                clearInterval(this._timer);
                this._timer = null;
                if (this.tasks.length) this.scan();   // инвентарь изменился
            }
        };
        this._timer = setInterval(tick, 4000);
        if (immediate) tick();
    }

    render() {
        const box = this.report;
        box.innerHTML = "";
        const plan = this.plan;
        if (!plan) {
            box.appendChild(el("div", { class: "gr-muted" },
                "нажмите «Сверить граф» — покажу, каких моделей не хватает воркерам"));
            return;
        }
        for (const w of plan.workers) {
            if (w.error) {
                box.appendChild(el("div", { class: "gr-muted" },
                    `⚠ ${esc(w.name)}: ${esc(w.error)}`));
            }
            if ((w.missing_classes || []).length) {
                box.appendChild(el("div", { class: "gr-muted" },
                    `⚠ ${esc(w.name)}: нет нод — ${esc(w.missing_classes.join(", "))}`
                    + " (модели тут не помогут: нужен тот же набор custom-нод)"));
            }
        }
        if (!plan.models.length) {
            box.appendChild(el("div", { class: "gr-muted" },
                "в графе нет моделей из известных лоадеров"));
            return;
        }
        for (const m of plan.models) {
            box.appendChild(this.modelRow(m, plan.workers));
        }
    }

    modelRow(m, workers) {
        const row = el("div", { class: "gr-modelrow gr-modelrow-plan" });
        const head = el("div", { class: "gr-row" });
        head.appendChild(el("span", { class: "gr-grow" },
            `<b>${esc(m.filename)}</b> <span class="gr-muted">${esc(m.folder)}`
            + (m.size_gb ? ` · ${esc(String(m.size_gb))} ГБ` : "") + "</span>"));
        row.appendChild(head);

        const cells = el("div", { class: "gr-btns" });
        for (const w of workers) {
            const state = m.workers[w.id] || "unknown";
            const mark = { have: "✓", missing: "✕", unknown: "?" }[state];
            cells.appendChild(el("span", {
                class: `gr-chip gr-chip-${state === "have" ? "green" : (state === "missing" ? "red" : "yellow")}`,
                title: state === "have" ? "есть" : (state === "missing" ? "нет" : "не удалось узнать"),
            }, `${mark} ${esc(w.name)}`));
        }
        row.appendChild(cells);

        if (m.missing_on.length && !m.url) {
            const warn = el("div", { class: "gr-muted" },
                "нет ссылки — вставьте её, она сохранится в библиотеку:");
            const input = el("input", { class: "gr-input",
                placeholder: "https://…/" + m.filename });
            const save = el("button", { class: "gr-btn gr-small gr-primary" }, "Сохранить ссылку");
            save.onclick = async () => {
                try {
                    await gr.post("/models/library", {
                        url: input.value.trim(), filename: m.filename, folder: m.folder });
                    toast("success", "Источник сохранён", m.filename);
                    this.scan();
                } catch (e) { toast("error", "Не сохранено", e.message, 8000); }
            };
            const bar = el("div", { class: "gr-btns" });
            bar.append(input, save);
            row.append(warn, bar);
        }
        return row;
    }

    renderProgress() {
        const box = this.progress;
        box.innerHTML = "";
        for (const t of this.tasks) {
            const frac = t.bytes_total ? t.bytes_done / t.bytes_total : 0;
            const line = el("div", { class: "gr-job" });
            line.appendChild(el("div", { class: "gr-muted" },
                `${esc(t.worker)} · ${esc(t.filename)} · ${esc(t.state)}`
                + (t.bytes_total ? ` · ${gb(t.bytes_done)}/${gb(t.bytes_total)} ГБ` : "")
                + (t.error ? ` · <span class="gr-err">${esc(t.error)}</span>` : "")));
            const bar = el("div", { class: "gr-bar" });
            bar.appendChild(el("div", { class: "gr-bar-fill",
                style: `width:${Math.round(frac * 100)}%` }));
            line.appendChild(bar);
            box.appendChild(line);
        }
    }
}
