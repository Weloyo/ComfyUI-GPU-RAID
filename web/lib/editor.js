// Редактор проекта: лента ключевых кадров, сегменты, рендер, монтаж и экспорт.
// Живёт ВНУТРИ ноды на канве (см. nodeui.js). Панель расширения проекты только
// перечисляет — вся работа с промптами и кадрами идёт в рабочей области.
import { app } from "../../../scripts/app.js";
import { gr, toast, viewURL, clientId } from "./api.js";
import { el, esc, fmtDur } from "./format.js";

// все живые редакторы: WS-события расходятся по ним из gpuraid.js
const EDITORS = new Set();

export function broadcast(name, data) {
    for (const ed of [...EDITORS]) {
        try { ed.onEvent(name, data); } catch (e) { /* один битый редактор не роняет остальные */ }
    }
}

const KIND_LABEL = { story: "сюжет", chain: "цепочка", keyframes: "кадры" };
const STATE_LABEL = {
    draft: "черновик", kf_rendering: "кадры рендерятся", kf_done: "кадры готовы",
    running: "рендер", done: "готово", partial: "частично", failed: "ошибка",
};

export class ProjectEditor {
    constructor(root) {
        this.root = root;
        this.root.classList.add("gr-editor");
        this.label = "";
        this.manifest = null;
        this.error = "";
        this.edit = { order: [], excluded: new Set(), trims: {}, crossfade_s: 0 };
        this.jobs = new Map();      // job_id -> снимок задания ЭТОГО проекта
        this._rev = {};             // ключ элемента -> версия (сброс кэша превью)
        this._sig = {};             // ключ элемента -> подпись состояния
        this._editTimer = null;
        this._dirty = false;
        this._focusHook = false;
        this._autoSeg = false;      // «Всё ▶»: досылать сегменты после кадров
        EDITORS.add(this);
        this.render();
    }

    dispose() {
        EDITORS.delete(this);
        clearTimeout(this._editTimer);
        this.root.innerHTML = "";
    }

    // ------------------------------------------------------------- данные

    /** Привязать редактор к проекту (label из свойств ноды). */
    async setProject(label, force = false) {
        label = String(label || "").trim();
        if (!force && label === this.label) return;
        this.label = label;
        this.manifest = null;
        this.error = "";
        this.jobs.clear();
        if (!label) { this.render(); return; }
        await this.refresh();
    }

    async refresh() {
        if (!this.label) return;
        try {
            this.applyManifest(await gr.get(`/longvideo/${encodeURIComponent(this.label)}`));
        } catch (e) {
            this.manifest = null;
            this.error = e.status === 404 ? "" : e.message;
            this.render();
        }
    }

    applyManifest(m) {
        this.manifest = m;
        this.error = "";
        this._bumpRevisions(m);
        const all = (m.segments || []).map((s) => s.index);
        const stored = m.edit || {};
        // порядок/исключения/тримы персистятся в манифесте — подхватываем, а не сбрасываем
        const order = (stored.order || []).filter((i) => all.includes(i));
        for (const i of all) if (!order.includes(i)) order.push(i);
        this.edit = {
            order,
            excluded: new Set(stored.excluded || []),
            trims: { ...(stored.trims || {}) },
            crossfade_s: stored.crossfade_s ?? m.crossfade_s ?? 0,
        };
        this.render();
    }

    /** Превью кэшируются браузером по имени файла — версию крутим только когда
     *  элемент реально пересчитали (иначе картинки мигали бы на каждый рендер). */
    _bumpRevisions(m) {
        const check = (key, item) => {
            const sig = `${item.status}|${item.seed}|${item.worker}|${item.file}`;
            if (this._sig[key] !== sig) {
                this._sig[key] = sig;
                this._rev[key] = (this._rev[key] || 0) + 1;
            }
        };
        for (const k of m.keyframes || []) check(`k${k.index}`, k);
        for (const s of m.segments || []) check(`s${s.index}`, s);
    }

    onEvent(name, data) {
        if (name === "longvideo") {
            if (!this.label || data.label !== this.label) return;
            if (data.deleted) { this.manifest = null; this.render(); return; }
            if (!data.manifest) return;
            if (this._autoSeg && data.manifest.state === "kf_done") {
                this._autoSeg = false;
                gr.post(`/story/${encodeURIComponent(this.label)}/render`,
                    { client_id: clientId() })
                    .then(() => toast("info", "Кадры готовы — пошёл рендер сегментов"))
                    .catch((e) => toast("error", "Сегменты не запущены", e.message));
            }
            this.applyManifest({ ...(this.manifest || {}), ...data.manifest });
            return;
        }
        if (!this.label) return;
        if (name === "job_started") {
            if (String(data.label || "").split("/")[0] !== this.label) return;
            this.jobs.set(data.job_id, data);
            this.renderProgress();
        } else if (name === "unit") {
            const job = this.jobs.get(data.job_id);
            if (!job || !job.units) return;
            const u = job.units.find((x) => x.index === data.index);
            if (u) Object.assign(u, data);
            job.done = job.units.filter((x) => x.state === "DONE").length;
            this.renderProgress();
        } else if (name === "job_done") {
            if (!this.jobs.has(data.job_id)) return;
            this.jobs.delete(data.job_id);
            this.renderProgress();
        }
    }

    // ------------------------------------------------------------- запросы

    async renderKeyframes(all = false) {
        const m = this.manifest;
        const idx = all ? null : (m.keyframes || [])
            .filter((k) => k.status !== "done").map((k) => k.index);
        try {
            await gr.post(`/story/${encodeURIComponent(this.label)}/keyframes/render`,
                { indices: idx && idx.length ? idx : null, client_id: clientId() });
            toast("info", `Кадры: рендер ${(idx && idx.length) || (m.keyframes || []).length} шт.`);
        } catch (e) {
            this._autoSeg = false;
            toast("error", "Кадры не запущены", e.message);
        }
    }

    async renderSegments(all = false) {
        const m = this.manifest;
        const story = m.mode === "story";
        const idx = all ? null : (m.segments || [])
            .filter((s) => s.status !== "done" || s.stale || s.dirty).map((s) => s.index);
        try {
            if (story) {
                await gr.post(`/story/${encodeURIComponent(this.label)}/render`,
                    { indices: idx && idx.length ? idx : null, client_id: clientId() });
            } else {
                // chain/keyframes: общего «дорендери» нет — перерендериваем поштучно
                const todo = idx && idx.length ? idx
                    : (all ? (m.segments || []).map((s) => s.index) : []);
                if (!todo.length) {
                    toast("info", "Сегменты", "нечего перерендеривать — все готовы");
                    return;
                }
                for (const i of todo) {
                    await gr.post(`/longvideo/${encodeURIComponent(this.label)}/rerender`,
                        { index: i });
                }
            }
            toast("info", "Сегменты: рендер запущен");
        } catch (e) { toast("error", "Сегменты не запущены", e.message); }
    }

    saveEdit() {
        clearTimeout(this._editTimer);
        this._editTimer = setTimeout(() => {
            if (!this.label) return;
            gr.patch(`/longvideo/${encodeURIComponent(this.label)}/edit`, {
                order: this.edit.order,
                excluded: [...this.edit.excluded],
                trims: this.edit.trims,
                crossfade_s: this.edit.crossfade_s || 0,
            }).catch(() => {});
        }, 600);
    }

    moveSeg(idx, dir) {
        const order = this.edit.order;
        const pos = order.indexOf(idx);
        const np = pos + dir;
        if (pos < 0 || np < 0 || np >= order.length) return;
        [order[pos], order[np]] = [order[np], order[pos]];
        this.saveEdit();
        this.render();
    }

    // ------------------------------------------------------------- отрисовка

    render() {
        const box = this.root;
        // не перерисовывать, пока пользователь печатает: WS-события прилетают на
        // каждый сегмент, иначе текст в промпте затирался бы на полуслове
        if (box.contains(document.activeElement)
            && /^(TEXTAREA|INPUT)$/.test(document.activeElement.tagName)) {
            this._dirty = true;
            if (!this._focusHook) {
                this._focusHook = true;
                box.addEventListener("focusout", () => setTimeout(() => {
                    const still = box.contains(document.activeElement)
                        && /^(TEXTAREA|INPUT)$/.test(document.activeElement.tagName);
                    if (this._dirty && !still) { this._dirty = false; this.render(); }
                }, 150));
            }
            return;
        }
        box.innerHTML = "";
        const m = this.manifest;
        if (!m) {
            box.appendChild(el("div", { class: "gr-muted gr-empty" },
                this.error ? `⚠ ${esc(this.error)}`
                    : (this.label
                        ? `проект «${esc(this.label)}» ещё не создан — нажмите кнопку запуска выше`
                        : "плана ещё нет — заполните параметры и нажмите кнопку запуска выше")));
            return;
        }
        box.appendChild(this.headerRow(m));
        this.elProgress = el("div", { class: "gr-progress" });
        box.appendChild(this.elProgress);
        this.renderProgress();
        box.appendChild(this.toolbar(m));
        if (m.llm && m.llm.error) {
            box.appendChild(el("div", { class: "gr-muted" },
                `⚠ LLM: ${esc(m.llm.error)} — план собран эвристикой`));
        }
        if ((m.keyframes || []).length) box.appendChild(this.keyframesBlock(m));
        box.appendChild(this.segmentsBlock(m));
        box.appendChild(this.footer(m));
    }

    headerRow(m) {
        const done = (m.segments || []).filter((s) => s.status === "done").length;
        const kfDone = (m.keyframes || []).filter((k) => k.status === "done").length;
        const head = el("div", { class: "gr-ed-head" });
        head.appendChild(el("b", {}, esc(m.label)));
        head.appendChild(el("span", { class: "gr-badge" },
            esc(KIND_LABEL[m.mode] || m.mode || "")));
        head.appendChild(el("span", { class: "gr-badge" },
            esc(STATE_LABEL[m.state] || m.state || "")));
        head.appendChild(el("span", { class: "gr-muted gr-grow" },
            ((m.keyframes || []).length ? `кадры ${kfDone}/${m.keyframes.length} · ` : "")
            + `сегменты ${done}/${(m.segments || []).length}`));
        return head;
    }

    renderProgress() {
        const box = this.elProgress;
        if (!box) return;
        box.innerHTML = "";
        for (const job of this.jobs.values()) {
            const total = job.total || (job.units ? job.units.length : 0) || 1;
            let frac = (job.done || 0) / total;
            if (job.units) {
                let partial = 0;
                for (const u of job.units) {
                    if (u.state === "RUNNING" && u.progress && u.progress[1] > 0) {
                        partial += u.progress[0] / u.progress[1];
                    }
                }
                frac = Math.min(1, ((job.done || 0) + partial) / total);
            }
            const row = el("div", { class: "gr-job" });
            row.appendChild(el("div", { class: "gr-muted" },
                `${esc(job.label || job.kind)} · ${job.done || 0}/${total}`));
            const bar = el("div", { class: "gr-bar" });
            bar.appendChild(el("div", { class: "gr-bar-fill",
                style: `width:${Math.round(frac * 100)}%` }));
            row.appendChild(bar);
            const cancel = el("button", { class: "gr-btn gr-small gr-danger" }, "Отменить");
            cancel.onclick = () => gr.post(`/jobs/${job.job_id}/cancel`).catch(() => {});
            row.appendChild(cancel);
            box.appendChild(row);
        }
    }

    toolbar(m) {
        const bar = el("div", { class: "gr-btns" });
        const story = m.mode === "story";
        if ((m.keyframes || []).length) {
            const kf = el("button", { class: "gr-btn gr-primary",
                title: "рендер ключевых кадров (только неготовых) параллельно на всех GPU" },
                "Кадры ▶");
            kf.onclick = () => this.renderKeyframes(false);
            bar.appendChild(kf);
        }
        const seg = el("button", { class: "gr-btn gr-primary",
            title: "рендер сегментов (неготовых и устаревших) параллельно" }, "Сегменты ▶");
        seg.onclick = () => this.renderSegments(false);
        bar.appendChild(seg);
        if (story && (m.keyframes || []).length) {
            const all = el("button", { class: "gr-btn",
                title: "кадры, затем автоматически сегменты" }, "Всё ▶");
            all.onclick = () => { this._autoSeg = true; this.renderKeyframes(false); };
            bar.appendChild(all);
        }
        if (story) {
            const tmpl = el("button", { class: "gr-btn gr-small",
                title: "сделать текущий workflow на канве T2I-шаблоном ключевых кадров "
                    + "(нужны GPURAID:PROMPT и SaveImage)" }, "Шаблон кадра из канвы");
            tmpl.onclick = async () => {
                try {
                    const p = await app.graphToPrompt();
                    await gr.post(`/story/${encodeURIComponent(this.label)}/keyframe_template`,
                        { graph: p.output });
                    toast("success", "Шаблон ключевых кадров сохранён");
                } catch (e) { toast("error", "Шаблон не принят", e.message); }
            };
            bar.appendChild(tmpl);
        }
        const upd = el("button", { class: "gr-btn gr-small" }, "⟳");
        upd.title = "перечитать проект с диска";
        upd.onclick = () => this.refresh();
        bar.appendChild(upd);
        const del = el("button", { class: "gr-btn gr-small gr-danger",
            title: "удалить проект со всеми сегментами" }, "✕");
        del.onclick = async () => {
            if (!confirm(`Удалить проект «${m.label}» со всеми сегментами?`)) return;
            try {
                await gr.del(`/longvideo/${encodeURIComponent(this.label)}`);
                this.manifest = null;
                this.render();
            } catch (e) { toast("error", "Не удалось удалить", e.message); }
        };
        bar.appendChild(del);
        return bar;
    }

    // ---- ключевые кадры

    keyframesBlock(m) {
        const box = el("details", { class: "gr-subdetails", open: "" });
        box.appendChild(el("summary", {}, `Ключевые кадры (${m.keyframes.length})`));
        const strip = el("div", { class: "gr-kfstrip" });
        const sub = `gpuraid_story/${m.label}`;
        for (const k of m.keyframes) {
            strip.appendChild(this.keyframeCard(m, k, sub));
        }
        box.appendChild(strip);
        return box;
    }

    keyframeCard(m, k, sub) {
        const card = el("div", { class: "gr-kf" });
        if (k.status === "done" && k.file) {
            const img = el("img", { class: "gr-kf-img", loading: "lazy",
                src: `${viewURL(k.file, sub, "input")}&r=${this._rev[`k${k.index}`] || 0}` });
            img.onclick = () => window.open(img.src, "_blank");
            img.title = "открыть в полном размере";
            card.appendChild(img);
        } else {
            card.appendChild(el("div", { class: "gr-kf-img gr-video-stub" },
                esc(k.status || "draft")));
        }
        card.appendChild(el("div", { class: "gr-muted" }, `кадр ${k.index}`
            + (k.worker ? ` · ${esc(k.worker)}` : "")
            + (k.error ? ` <span class="gr-err">${esc(k.error)}</span>` : "")));
        const pr = el("textarea", { class: "gr-textarea gr-seg-prompt", rows: "3",
            placeholder: "промпт кадра" });
        pr.value = k.prompt || "";
        card.appendChild(pr);
        const ctl = el("div", { class: "gr-btns" });
        const seedIn = el("input", { class: "gr-input gr-tiny", type: "number",
            placeholder: String(k.seed ?? ""), title: "seed (пусто = случайный)" });
        const save = el("button", { class: "gr-btn gr-small",
            title: "сохранить промпт/seed кадра (без рендера)" }, "Сохранить");
        save.onclick = async () => {
            try {
                const patch = { prompt: pr.value };
                if (seedIn.value.trim()) patch.seed = parseInt(seedIn.value, 10);
                await gr.patch(
                    `/story/${encodeURIComponent(this.label)}/keyframes/${k.index}`, patch);
                toast("success", `Кадр ${k.index} сохранён`);
            } catch (e) { toast("error", "Не сохранено", e.message); }
        };
        const again = el("button", { class: "gr-btn gr-small",
            title: "перегенерировать кадр (смежные сегменты станут «устаревшими»)" }, "заново");
        again.onclick = async () => {
            try {
                await gr.post(
                    `/story/${encodeURIComponent(this.label)}/keyframes/${k.index}/rerender`, {
                        prompt: pr.value,
                        seed: seedIn.value.trim() ? parseInt(seedIn.value, 10) : null,
                    });
                toast("info", `Кадр ${k.index}: перегенерация`);
            } catch (e) { toast("error", "Не запущено", e.message); }
        };
        ctl.append(save, again, seedIn);
        card.appendChild(ctl);
        return card;
    }

    // ---- сегменты

    segmentsBlock(m) {
        const box = el("details", { class: "gr-subdetails", open: "" });
        box.appendChild(el("summary", {}, `Сегменты (${(m.segments || []).length})`));
        const segMap = new Map((m.segments || []).map((s) => [s.index, s]));
        const sub = `gpuraid/${m.label}`;
        for (const idx of this.edit.order) {
            const s = segMap.get(idx);
            if (s) box.appendChild(this.segmentRow(m, s, sub));
        }
        return box;
    }

    segmentRow(m, s, sub) {
        const idx = s.index;
        const row = el("div", { class: "gr-seg" + (this.edit.excluded.has(idx) ? " gr-seg-off" : "") });
        if (s.status === "done" && s.file) {
            row.appendChild(el("video", { class: "gr-video", controls: "", preload: "metadata",
                src: `${viewURL(s.file, sub, "output")}&r=${this._rev[`s${idx}`] || 0}` }));
        } else {
            row.appendChild(el("div", { class: "gr-video gr-video-stub" }, esc(s.status)));
        }
        const meta = el("div", { class: "gr-seg-meta" });
        meta.appendChild(el("div", {},
            `<b>#${idx}</b> seed ${esc(String(s.seed ?? ""))} `
            + `<span class="gr-muted">${esc(s.worker || "")}`
            + (s.duration_s ? ` · ${esc(String(s.duration_s))}с` : "") + "</span>"
            + (s.dirty ? ' <span class="gr-chip gr-chip-yellow">промпт изменён — нужен перерендер</span>' : "")
            + (s.stale ? ' <span class="gr-chip gr-chip-yellow">кадр изменён — перерендерите</span>' : "")
            + (s.error ? ` <span class="gr-err">${esc(s.error)}</span>` : "")));

        const pr = el("textarea", { class: "gr-textarea gr-seg-prompt", rows: "2",
            placeholder: "промпт сегмента (пусто = из шаблона)" });
        pr.value = s.prompt || "";
        meta.appendChild(pr);

        const ctl = el("div", { class: "gr-btns" });
        const seedIn = el("input", { class: "gr-input gr-tiny", type: "number",
            placeholder: String(s.seed ?? ""), title: "seed (пусто = случайный при перерендере)" });
        const save = el("button", { class: "gr-btn gr-small",
            title: "сохранить промпт/seed в проект (без рендера)" }, "Сохранить");
        save.onclick = async () => {
            try {
                const patch = { prompt: pr.value };
                if (seedIn.value.trim()) patch.seed = parseInt(seedIn.value, 10);
                await gr.patch(
                    `/longvideo/${encodeURIComponent(this.label)}/segments/${idx}`, patch);
                toast("success", `Сегмент #${idx} сохранён`);
            } catch (e) { toast("error", "Не сохранено", e.message); }
        };
        const again = el("button", { class: "gr-btn gr-small",
            title: "перегенерировать с текущим промптом" }, "заново");
        again.onclick = async () => {
            try {
                await gr.post(`/longvideo/${encodeURIComponent(this.label)}/rerender`, {
                    index: idx,
                    prompt: pr.value,
                    seed: seedIn.value.trim() ? parseInt(seedIn.value, 10) : null,
                });
                toast("info", `Сегмент #${idx}: перегенерация запущена`);
            } catch (e) { toast("error", "Не запущено", e.message); }
        };
        const up = el("button", { class: "gr-btn gr-small", title: "выше в монтаже" }, "↑");
        up.onclick = () => this.moveSeg(idx, -1);
        const down = el("button", { class: "gr-btn gr-small", title: "ниже в монтаже" }, "↓");
        down.onclick = () => this.moveSeg(idx, 1);
        const onoff = el("button", { class: "gr-btn gr-small",
            title: "исключить сегмент из итогового монтажа" },
            this.edit.excluded.has(idx) ? "вкл" : "искл");
        onoff.onclick = () => {
            if (this.edit.excluded.has(idx)) this.edit.excluded.delete(idx);
            else this.edit.excluded.add(idx);
            this.saveEdit();
            this.render();
        };
        const trim = this.edit.trims[idx] || this.edit.trims[String(idx)] || {};
        const tin = el("input", { class: "gr-input gr-tiny", placeholder: "in,с",
            title: "обрезать спереди, сек", value: trim.in_s ? String(trim.in_s) : "" });
        const tout = el("input", { class: "gr-input gr-tiny", placeholder: "out,с",
            title: "обрезать сзади, сек", value: trim.out_s ? String(trim.out_s) : "" });
        const saveTrim = () => {
            this.edit.trims[idx] = {
                in_s: parseFloat(tin.value || "0") || 0,
                out_s: parseFloat(tout.value || "0") || 0,
            };
            this.saveEdit();
        };
        tin.onchange = saveTrim;
        tout.onchange = saveTrim;
        ctl.append(save, again, up, down, onoff, tin, tout, seedIn);
        meta.appendChild(ctl);
        row.appendChild(meta);
        return row;
    }

    footer(m) {
        const bar = el("div", { class: "gr-btns gr-ed-foot" });
        const fade = el("input", { class: "gr-input gr-tiny", type: "number", min: "0",
            step: "0.1", value: String(this.edit.crossfade_s || 0), title: "кроссфейд, сек" });
        fade.onchange = () => {
            this.edit.crossfade_s = parseFloat(fade.value || "0") || 0;
            this.saveEdit();
        };
        const exp = el("button", { class: "gr-btn gr-primary",
            title: "склеить включённые сегменты в итоговое видео" }, "Экспорт");
        exp.onclick = async () => {
            try {
                const order = this.edit.order.filter((i) => !this.edit.excluded.has(i));
                await gr.post(`/longvideo/${encodeURIComponent(this.label)}/export`, {
                    order, trims: this.edit.trims,
                    crossfade_s: parseFloat(fade.value || "0"),
                });
            } catch (e) { toast("error", "Экспорт не удался", e.message); }
        };
        bar.append(el("span", { class: "gr-muted" }, "кроссфейд, с"), fade, exp);
        if (m.final) {
            bar.appendChild(el("a", { class: "gr-btn", target: "_blank",
                href: viewURL(m.final, `gpuraid/${m.label}`, "output", true) },
                "▶ итоговое видео"));
        }
        if (m.wall_s) bar.appendChild(el("span", { class: "gr-muted" }, fmtDur(m.wall_s)));
        return bar;
    }
}
