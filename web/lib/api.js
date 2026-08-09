// Обёртки над api.fetchApi для /gpuraid/* + URL медиафайлов.
import { api } from "../../../scripts/api.js";
import { app } from "../../../scripts/app.js";

async function request(method, path, body) {
    const opts = { method, headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await api.fetchApi("/gpuraid" + path, opts);
    let data = {};
    try { data = await r.json(); } catch (e) { /* пустой ответ */ }
    if (!r.ok) {
        // некоторые роуты (например, добавление воркеров) кладут причины по
        // каждой невалидной строке в errors — без этого падения все строки
        // разом давали бесполезный тост «HTTP 400» вместо конкретной причины
        const fromList = Array.isArray(data.errors) && data.errors.length ? data.errors.join("; ") : null;
        const msg = data.reason || data.error || fromList || `HTTP ${r.status}`;
        const err = new Error(msg);
        err.status = r.status;
        throw err;
    }
    return data;
}

export const gr = {
    get: (path) => request("GET", path),
    post: (path, body) => request("POST", path, body ?? {}),
    patch: (path, body) => request("PATCH", path, body ?? {}),
    del: (path) => request("DELETE", path),
};

export function viewURL(filename, subfolder = "", type = "output", bust = false) {
    const q = `/view?filename=${encodeURIComponent(filename)}&subfolder=${encodeURIComponent(subfolder)}`
        + `&type=${type}` + (bust ? `&t=${Date.now()}` : "");
    try {
        if (typeof api.apiURL === "function") return api.apiURL(q);
    } catch (e) { /* fallthrough */ }
    return "/api" + q;
}

export function toast(severity, summary, detail = "", life = 6000) {
    try {
        app.extensionManager.toast.add({ severity, summary, detail, life });
    } catch (e) {
        console.log(`[GPU RAID ${severity}] ${summary} ${detail}`);
    }
}

export function clientId() {
    return api.clientId || api.initialClientId || "";
}
