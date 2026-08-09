// Мелкие форматтеры для панели.

export function fmtGb(v) {
    return (v === undefined || v === null) ? "?" : `${v} ГБ`;
}

export function fmtDur(seconds) {
    if (!seconds && seconds !== 0) return "";
    const s = Math.round(seconds);
    if (s < 60) return `${s}с`;
    const m = Math.floor(s / 60);
    return `${m}м ${s % 60}с`;
}

export function stateDot(state) {
    const map = {
        online: "gr-dot-green", offline: "gr-dot-red",
        stopped: "gr-dot-blue", unknown: "gr-dot-gray",
    };
    return map[state] || "gr-dot-gray";
}

const STATE_TEXT = {
    online: "в работе",
    offline: "недоступен",
    stopped: "остановлен",
    unknown: "не опрошен",
};

export function stateText(state, enabled = true) {
    if (!enabled) return "выключен";
    return STATE_TEXT[state] || (state ? String(state) : "не опрошен");
}

/** Ошибка воркера человеческим языком: голое имя исключения ничего не говорит. */
export function workerError(error, state) {
    const text = String(error || "");
    if (!text) return "";
    if (/DNSError|Cannot connect to host|ClientConnectorError/i.test(text)) {
        return "туннель не отвечает — сессия на платформе завершена";
    }
    if (/530/.test(text)) {
        return "туннель жив, но за ним никого нет — воркер остановлен";
    }
    if (/401/.test(text)) return "неверный токен воркера";
    if (/TimeoutError/i.test(text)) return "нет ответа вовремя";
    return text.replace(/^[A-Za-z]+Error:\s*/, "");
}

export function platformBadge(platform) {
    const map = { colab: "колаб", kaggle: "каггл", generic: "облако" };
    return map[platform] || "";
}

export function esc(text) {
    const div = document.createElement("div");
    div.textContent = text ?? "";
    return div.innerHTML;
}

export function el(tag, attrs = {}, html = "") {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
        if (k === "class") node.className = v;
        else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
        else if (v !== undefined && v !== null) node.setAttribute(k, v);
    }
    if (html) node.innerHTML = html;
    return node;
}
