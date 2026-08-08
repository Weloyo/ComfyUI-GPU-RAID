// Раздел «Подключения»: все внешние регистрации в одном месте.
//
// На каждого провайдера — карточка: зачем он нужен, ссылка ровно на ту
// страницу, где берётся ключ, поле для вставки, «Сохранить и проверить» (ключ
// сразу дёргается на живом API) и результат проверки. Проверка запоминается на
// сервере, так что после одного прохода расширение ничего не переспрашивает.
import { gr, toast } from "./api.js";
import { el, esc } from "./format.js";

function ago(ts) {
    if (!ts) return "";
    const s = Math.round(Date.now() / 1000 - ts);
    if (s < 60) return "только что";
    if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
    if (s < 86400) return `${Math.floor(s / 3600)} ч назад`;
    return `${Math.floor(s / 86400)} дн назад`;
}

export class ConnectionsUI {
    constructor(root) {
        this.root = root;
        this.data = null;
        this.extra = {};        // pid -> extra последней проверки (напр. список моделей)
        this.busy = new Set();
        this.onSummary = null;  // колбэк для заголовка секции
        this.refresh();
    }

    async refresh() {
        try {
            this.data = await gr.get("/connections");
        } catch (e) {
            this.root.innerHTML = "";
            this.root.appendChild(el("div", { class: "gr-muted" },
                "сервер недоступен"));
            return;
        }
        this.render();
    }

    summary() {
        const list = this.data?.providers || [];
        const ok = list.filter((p) => p.check?.ok).length;
        return { ok, total: list.length };
    }

    render() {
        const box = this.root;
        // пока пользователь печатает в поле — не перетирать ввод
        if (box.contains(document.activeElement)
            && /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
        box.innerHTML = "";
        const list = this.data?.providers || [];
        const { ok, total } = this.summary();
        if (this.onSummary) this.onSummary(ok, total);
        box.appendChild(el("div", { class: "gr-muted" },
            `Подключено и проверено: ${ok} из ${total}. Всё необязательно — `
            + "без ключа просто не работает соответствующая часть."));
        for (const p of list) box.appendChild(this.card(p));
    }

    card(p) {
        const checked = p.check || {};
        const level = checked.ok ? "green" : (p.configured ? "red" : "gray");
        const card = el("details", { class: "gr-conn", ...(checked.ok ? {} : { open: "" }) });

        const head = el("summary", { class: "gr-conn-head" });
        head.appendChild(el("span", { class: `gr-dot gr-dot-${level}` }));
        head.appendChild(el("span", { class: "gr-name" }, esc(p.title)));
        head.appendChild(el("span", { class: "gr-muted gr-grow" },
            checked.ok ? esc(checked.detail || "готово")
                : (p.configured ? esc(checked.detail || "не проверено") : "не настроено")));
        card.appendChild(head);

        const body = el("div", { class: "gr-conn-body" });
        // «проверено N назад» имеет смысл только когда есть что проверять
        const when = (checked.ts && p.configured) ? ` · проверено ${ago(checked.ts)}` : "";
        body.appendChild(el("div", { class: "gr-muted" }, esc(p.why) + when));
        if (!checked.ok && p.fallback) {
            body.appendChild(el("div", { class: "gr-muted" }, `↳ ${esc(p.fallback)}`));
        }

        const links = el("div", { class: "gr-btns" });
        for (const l of p.links || []) {
            links.appendChild(el("a", { class: "gr-btn", href: l.url, target: "_blank",
                rel: "noopener noreferrer" }, `↗ ${esc(l.label)}`));
        }
        if (links.children.length) body.appendChild(links);

        if (p.steps) {
            const ol = el("ol", { class: "gr-steps" });
            for (const s of p.steps) ol.appendChild(el("li", {}, esc(s)));
            body.appendChild(ol);
        }

        const inputs = {};
        for (const f of p.fields || []) {
            const value = (p.values || {})[f.key] || "";
            const saved = f.secret && p.configured;
            let ctl;
            if (f.multiline) {
                ctl = el("textarea", { class: "gr-textarea", rows: "2",
                    placeholder: saved ? "сохранено ✓ — вставьте новое, чтобы заменить"
                        : f.placeholder || "" });
            } else {
                ctl = el("input", { class: "gr-input",
                    ...(f.secret ? { type: "password" } : {}),
                    placeholder: saved ? "сохранено ✓ — вставьте новое, чтобы заменить"
                        : f.placeholder || "" });
                if (!f.secret) ctl.value = value;
            }
            inputs[f.key] = ctl;
            body.appendChild(this.row(f.label, ctl));
        }

        // после проверки LLM отдаёт список моделей — выбираем из него, а не наугад
        const models = (this.extra[p.id] || {}).models;
        if (models && models.length && inputs.model) {
            const sel = el("select", { class: "gr-select" });
            sel.appendChild(el("option", { value: "" }, "— выбрать модель —"));
            for (const m of models) sel.appendChild(el("option", { value: m }, esc(m)));
            sel.value = (p.values || {}).model || "";
            sel.onchange = () => { inputs.model.value = sel.value; };
            body.appendChild(this.row("из списка", sel));
        }

        const btns = el("div", { class: "gr-btns" });
        if (!p.no_fields) {
            const save = el("button", { class: "gr-btn gr-primary",
                title: "сохранить и сразу проверить на живом API" }, "Сохранить и проверить");
            save.onclick = () => this.save(p, inputs, save);
            btns.appendChild(save);
        }
        const check = el("button", { class: "gr-btn" }, "Проверить");
        check.onclick = () => this.check(p, check);
        btns.appendChild(check);
        for (const a of p.actions || []) {
            const b = el("button", { class: "gr-btn", title: a.title || "" }, esc(a.label));
            b.onclick = () => this.action(p, a, b);
            btns.appendChild(b);
        }
        if (p.configured && !p.no_fields) {
            const forget = el("button", { class: "gr-btn gr-danger gr-small",
                title: "удалить сохранённые ключи этого сервиса" }, "Забыть");
            forget.onclick = () => this.forget(p, forget);
            btns.appendChild(forget);
        }
        body.appendChild(btns);
        card.appendChild(body);
        return card;
    }

    row(label, ctl) {
        const r = el("div", { class: "gr-row" });
        r.appendChild(el("span", { class: "gr-label" }, esc(label)));
        r.appendChild(ctl);
        return r;
    }

    async save(p, inputs, btn) {
        const payload = {};
        for (const [key, ctl] of Object.entries(inputs)) {
            const v = ctl.value.trim();
            if (v) payload[key] = v;
        }
        // непустые несекретные поля шлём всегда: их можно и очистить
        for (const f of p.fields || []) {
            if (!f.secret && !(f.key in payload)) payload[f.key] = inputs[f.key].value.trim();
        }
        btn.disabled = true;
        try {
            const r = await gr.post(`/connections/${p.id}`, payload);
            this.applyCheck(p, r.check);
            this.data = r.status || this.data;
            for (const ctl of Object.values(inputs)) {
                if (ctl.type === "password" || ctl.tagName === "TEXTAREA") ctl.value = "";
            }
            this.render();
        } catch (e) {
            toast("error", `${p.title}: не сохранено`, e.message, 8000);
        } finally { btn.disabled = false; }
    }

    async check(p, btn) {
        btn.disabled = true;
        try {
            const r = await gr.post(`/connections/${p.id}/check`);
            this.applyCheck(p, r.check);
            await this.refresh();
        } catch (e) {
            toast("error", `${p.title}: проверка не удалась`, e.message, 8000);
        } finally { btn.disabled = false; }
    }

    async forget(p, btn) {
        if (!confirm(`Удалить сохранённые ключи «${p.title}»?`)) return;
        btn.disabled = true;
        try {
            const r = await gr.post(`/connections/${p.id}/forget`);
            this.extra[p.id] = {};
            this.data = r.status || this.data;
            toast("info", p.title, "ключи удалены");
            this.render();
        } catch (e) {
            toast("error", p.title, e.message);
        } finally { btn.disabled = false; }
    }

    async action(p, a, btn) {
        if (a.id === "install_cli"
            && !confirm("Установить пакет kaggle в питон ComfyUI (pip install kaggle)?")) return;
        btn.disabled = true;
        const old = btn.textContent;
        btn.textContent = "…";
        try {
            const r = await gr.post(`/connections/${p.id}/action/${a.id}`);
            this.applyCheck(p, r.check);
            this.data = r.status || this.data;
            toast("success", `${p.title}: ${a.label.toLowerCase()}`,
                r.result?.gist_id ? `gist ${r.result.gist_id}` : "готово");
            this.render();
        } catch (e) {
            toast("error", `${p.title}: ${a.label.toLowerCase()}`, e.message, 9000);
        } finally { btn.disabled = false; btn.textContent = old; }
    }

    applyCheck(p, check) {
        if (!check) return;
        this.extra[p.id] = check.extra || {};
        toast(check.ok ? "success" : "warn", p.title,
            check.detail || (check.ok ? "готово" : "не проверено"),
            check.ok ? 4000 : 9000);
    }
}
