// FC Inventory — web UI controller (modernised to async/await + fetch).
//
// Same DOM structure and localStorage behaviour as the v1.0.0 vanilla-JS
// version; replaces `setInterval`+`XMLHttpRequest` with a self-rescheduling
// async polling loop, and surfaces 4xx/5xx response bodies to the user.

(() => {
    "use strict";

    const $ = (id) => document.getElementById(id);

    // ── localStorage ─────────────────────────────────────────
    const saveCredentials = () => {
        const h = $("host").value.trim();
        if (h) localStorage.setItem("fc_host", h);
        const p = $("port").value;
        if (p) localStorage.setItem("fc_port", p);
        const u = $("username").value.trim();
        if (u) localStorage.setItem("fc_username", u);
    };

    const loadCredentials = () => {
        const h = localStorage.getItem("fc_host");
        if (h) $("host").value = h;
        const p = localStorage.getItem("fc_port");
        if (p) $("port").value = p;
        const u = localStorage.getItem("fc_username");
        if (u) $("username").value = u;
    };

    // ── postJSON: throws an Error with a server-provided message on !ok ──
    async function postJSON(url, body) {
        const resp = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
        });
        const text = await resp.text();
        let data = null;
        try { data = text ? JSON.parse(text) : null; } catch (_) { data = text; }
        if (!resp.ok) {
            const msg =
                (data && typeof data === "object" && (data.detail?.error || data.error)) ||
                (typeof data === "string" ? data : `HTTP ${resp.status}`);
            const err = new Error(msg);
            err.status = resp.status;
            err.body = data;
            throw err;
        }
        return data;
    }

    // ── Start a new collection ──────────────────────────────
    async function startCollection() {
        const host = $("host").value.trim();
        const port = parseInt($("port").value, 10) || 7443;
        const username = $("username").value.trim();
        const password = $("password").value;
        if (!host || !username || !password) {
            showFormError("Please fill in all fields.");
            return;
        }
        hideFormError();
        saveCredentials();
        $("btn-collect").disabled = true;

        try {
            await postJSON("/api/collect", { host, port, username, password });
            $("form-section").style.display    = "none";
            $("progress-section").style.display = "block";
            $("result-section").style.display   = "none";
            await pollLoop();
        } catch (err) {
            $("btn-collect").disabled = false;
            showFormError(err.message || "Failed to start collection.");
        }
    }

    // ── Self-rescheduling progress poll ─────────────────────
    async function pollLoop() {
        while (true) {
            try {
                const resp = await fetch("/api/progress");
                if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
                const data = await resp.json();
                const pct = data.percent || 0;
                $("progress-bar").style.width = pct + "%";
                $("percent-text").textContent = pct + "%";
                $("step-text").textContent = data.current_step || "Working...";
                if (data.status === "done")        { showSuccess();       return; }
                if (data.status === "cancelled")  { resetForm();         return; }
                if (data.status === "error")       { showError(data.error || "An unknown error occurred."); return; }
            } catch (e) {
                $("step-text").textContent = "Connection lost, retrying...";
            }
            await new Promise((res) => setTimeout(res, 2000));
        }
    }

    // ── Cancel ──────────────────────────────────────────────
    async function cancelCollection() {
        $("step-text").textContent = "Cancelling...";
        try { await postJSON("/api/cancel", {}); } catch (_) { /* best-effort */ }
    }

    // ── UI state transitions ───────────────────────────────
    function showSuccess() {
        $("progress-section").style.display = "none";
        $("result-section").style.display   = "block";
        $("success-box").style.display      = "block";
        $("error-box").style.display        = "none";
    }
    function showError(message) {
        $("progress-section").style.display = "none";
        $("result-section").style.display   = "block";
        $("success-box").style.display      = "none";
        $("error-box").style.display        = "block";
        $("error-detail").textContent       = message;
    }
    function resetForm() {
        $("form-section").style.display    = "block";
        $("progress-section").style.display = "none";
        $("result-section").style.display   = "none";
        $("password").value = "";
        $("btn-collect").disabled = false;
        $("progress-bar").style.width = "0%";
        $("percent-text").textContent = "0%";
        hideFormError();
        $("password").focus();
    }
    function showFormError(msg) {
        const el = $("form-error");
        el.textContent = msg;
        el.style.display = "block";
    }
    function hideFormError() {
        $("form-error").style.display = "none";
    }

    // ── Init ────────────────────────────────────────────────
    window.addEventListener("DOMContentLoaded", async () => {
        loadCredentials();
        try {
            const r = await fetch("/api/version");
            if (r.ok) {
                const v = await r.json();
                const tag = $("version-tag");
                if (tag && v.version) tag.textContent = "v" + v.version;
            }
        } catch (_) { /* offline is fine */ }

        document.querySelectorAll("#form-section input").forEach((input) => {
            input.addEventListener("keydown", (e) => {
                if (e.key === "Enter") { e.preventDefault(); startCollection(); }
            });
        });
        if ($("host").value && $("username").value) $("password").focus();
        else $("host").focus();
    });

    // Expose handlers for the inline `onclick=` attributes in the HTML.
    window.startCollection  = startCollection;
    window.cancelCollection = cancelCollection;
    window.resetForm        = resetForm;
    window.showFormError    = showFormError;
    window.hideFormError    = hideFormError;
})();
