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
