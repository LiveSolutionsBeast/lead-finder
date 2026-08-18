/**
 * background.js — Lead Finder service worker (manifest v3)
 * ==========================================================
 * Responsibilities:
 *   1. Hold the API key + base URL (so the content script never sees them).
 *   2. Proxy extract/match/inject calls to the lead-finder server.
 *   3. Default the API base to the Tailscale hostname so the extension
 *      works from any device on the user's Tailnet without manual config.
 *   4. Default the API key to the canonical dev key.
 *
 * Per t2.3 + t2.8: there is NO silent path. The popup ALWAYS shows. The user
 * must click Send. The background worker never pushes data on its own.
 */

const DEFAULT_API_BASE = "http://lsb-wsl.tail4f816e.ts.net:8798";
const DEFAULT_API_KEY = "lf_key_P9xZq3RvLm7YwT2hJm8FvNu4BcDEs6";

// ── Config storage ───────────────────────────────────────────────────────────
const getConfig = () =>
  new Promise((resolve) => {
    chrome.storage.local.get(
      { apiBase: DEFAULT_API_BASE, apiKey: DEFAULT_API_KEY },
      (cfg) => resolve(cfg),
    );
  });

// ── API call helper ──────────────────────────────────────────────────────────
const apiCall = async (path, method, body) => {
  const cfg = await getConfig();
  const url = `${cfg.apiBase.replace(/\/+$/, "")}${path}`;
  const headers = {
    "X-LF-Key": cfg.apiKey,
    "Content-Type": "application/json",
  };
  const opts = { method, headers };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const resp = await fetch(url, opts);
  let json = null;
  try { json = await resp.json(); } catch { /* not JSON */ }
  return { status: resp.status, ok: resp.ok, json };
};

// ── Message handler ──────────────────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || !msg.type) return false;

  if (msg.type === "LF_CONFIG_GET") {
    getConfig().then((cfg) => sendResponse({ ok: true, config: cfg }));
    return true; // async
  }

  if (msg.type === "LF_CONFIG_SET") {
    const { apiBase, apiKey } = msg;
    chrome.storage.local.set({ apiBase, apiKey }, () => {
      sendResponse({ ok: true });
    });
    return true; // async
  }

  if (msg.type === "LF_PUSH") {
    // The popup already showed the user a confirmation. Forward to the server.
    apiCall("/api/inject/linkedin-profile", "POST", msg.payload).then((r) => {
      sendResponse(r);
    });
    return true; // async
  }

  if (msg.type === "LF_MATCH") {
    apiCall("/api/inject/linkedin-profile/match", "POST", msg.payload).then((r) => {
      sendResponse(r);
    });
    return true; // async
  }

  if (msg.type === "LF_AUDIT") {
    const limit = msg.limit || 50;
    apiCall(`/api/audit/plugin-pushes?limit=${encodeURIComponent(limit)}`, "GET")
      .then((r) => sendResponse(r));
    return true; // async
  }

  if (msg.type === "LF_PING_SERVER") {
    apiCall("/api/health", "GET").then((r) => sendResponse(r));
    return true; // async
  }

  if (msg.type === "LF_DEBUG_STRUCTURE") {
    // Debug snapshot. We keep the service worker alive until the fetch
    // completes by returning true, then respond with the server result.
    apiCall("/api/debug/profile-structure", "POST", msg.payload)
      .then((r) => {
        console.log("[lead-finder/bg] debug snapshot:", r.status, r.json);
        sendResponse({ ok: r.ok, status: r.status, json: r.json });
      })
      .catch((err) => {
        console.warn("[lead-finder/bg] debug snapshot failed:", err);
        sendResponse({ ok: false, error: String(err) });
      });
    return true; // async
  }

  return false;
});

// ── Open the popup from a content-script highlight click (t3.1) ───────────
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (msg && msg.type === "LF_HIGHLIGHT_EXTRACT" && sender.tab && sender.tab.id) {
    // Stash the highlight payload in storage; the popup reads it on open.
    chrome.storage.local.set({
      pendingHighlight: {
        text: msg.text,
        context_label: msg.context_label,
        page_url: msg.page_url,
        captured_at: new Date().toISOString(),
      },
    });
  }
});
