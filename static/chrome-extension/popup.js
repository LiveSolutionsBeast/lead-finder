/**
 * popup.js — Lead Finder extension popup
 * ========================================
 * The ALWAYS-SHOW confirmation popup. The user must pick a match action and
 * click Send. There is no auto-send, no silent update.
 *
 * Flow:
 *   1. Open popup
 *   2. Ask content script for extracted data (chrome.tabs.sendMessage)
 *   3. Pre-fill the form
 *   4. Call /api/inject/linkedin-profile/match on the server to get candidate
 *      matches. Show 4 radio options based on what came back.
 *   5. User edits any field, picks an action, clicks Send.
 *   6. We POST /api/inject/linkedin-profile and show the result.
 */

const $ = (id) => document.getElementById(id);

const elements = {
  statusPill: $("statusPill"),
  extracting: $("extracting"),
  extractHint: $("extractHint"),
  formSection: $("formSection"),
  matchSection: $("matchSection"),
  matchOptions: $("matchOptions"),
  contactTieDropdown: $("contactTieDropdown"),
  contactTieSelect: $("contactTieSelect"),
  manualConfirmRow: $("manualConfirmRow"),
  confirmOverwriteManual: $("confirmOverwriteManual"),
  sendBtn: $("sendBtn"),
  rescanBtn: $("rescanBtn"),
  debugBtn: $("debugBtn"),
  result: $("result"),
  // form fields
  linkedin_url: $("linkedin_url"),
  full_name: $("full_name"),
  first_name: $("first_name"),
  last_name: $("last_name"),
  current_title: $("current_title"),
  current_company: $("current_company"),
  location: $("location"),
  email: $("email"),
  phone: $("phone"),
  expCount: $("expCount"),
  expList: $("expList"),
  newCompanyName: $("newCompanyName"),
  // config
  apiBase: $("apiBase"),
  apiKey: $("apiKey"),
  saveConfigBtn: $("saveConfigBtn"),
  configStatus: $("configStatus"),
  serverHint: $("serverHint"),
  // highlight inbox
  highlightInbox: $("highlightInbox"),
  hlContext: $("hlContext"),
  hlText: $("hlText"),
  hlCopyBtn: $("hlCopyBtn"),
  hlDismissBtn: $("hlDismissBtn"),
};

let extracted = null;        // last data from content script
let matchResult = null;      // last server matcher result
let is_manually_edited = false;  // t3.4: drives the manual-edit warning

// ── Status helpers ──────────────────────────────────────────────────────────
const setStatus = (text, kind) => {
  elements.statusPill.textContent = text;
  elements.statusPill.className = "pill " + (kind || "");
};
const showResult = (text, kind) => {
  elements.result.textContent = text;
  elements.result.className = kind || "";
};

// ── Step 1: ask content script for the data ─────────────────────────────────
const requestExtraction = () => {
  return new Promise((resolve) => {
    chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
      const tab = tabs[0];
      if (!tab || !tab.url || !/linkedin\.com\/in\//.test(tab.url)) {
        setStatus("not on /in/*", "error");
        elements.extracting.innerHTML =
          '<p>This popup is meant for LinkedIn profile pages.</p>'
          + '<p class="hint">Open a profile (e.g. https://www.linkedin.com/in/someone) and click the extension icon.</p>';
        elements.extractHint.textContent = "";
        return resolve(null);
      }
      chrome.tabs.sendMessage(tab.id, { type: "LF_EXTRACT" }, (resp) => {
        if (chrome.runtime.lastError || !resp || !resp.ok || !resp.data) {
          setStatus("extraction failed", "error");
          elements.extractHint.textContent =
            "Could not read the Experience section. Try Re-scan or scroll the page so it loads.";
          return resolve(null);
        }
        resolve(resp.data);
      });
    });
  });
};

const requestReExtraction = async () => {
  setStatus("re-scanning…", "warn");
  const tabs = await new Promise((r) => chrome.tabs.query({ active: true, currentWindow: true }, r));
  const tab = tabs[0];
  if (!tab) return null;
  return new Promise((resolve) => {
    chrome.tabs.sendMessage(tab.id, { type: "LF_RE_EXTRACT" }, (resp) => {
      if (chrome.runtime.lastError || !resp || !resp.ok) {
        setStatus("re-scan failed", "error");
        return resolve(null);
      }
      resolve(resp.data);
    });
  });
};

// ── Step 2: pre-fill the form ────────────────────────────────────────────────
const fillForm = (data) => {
  elements.linkedin_url.value = data.linkedin_url || "";
  elements.full_name.value = data.full_name || "";
  elements.first_name.value = data.first_name || "";
  elements.last_name.value = data.last_name || "";
  elements.current_title.value = data.current_title || "";
  elements.current_company.value = data.current_company || "";
  elements.location.value = data.location || "";
  elements.email.value = data.email || "";
  elements.phone.value = data.phone || "";
  // experience
  const exp = data.experience || [];
  elements.expCount.textContent = exp.length;
  elements.expList.innerHTML = exp.map((e) => {
    const dates = [e.started_at, e.ended_at || "Present"].filter(Boolean).join(" – ");
    const current = e.is_current ? " <span class='exp-dates'>(current)</span>" : "";
    return `<div class="exp-entry">
      <div class="exp-title">${escapeHtml(e.title || "")}</div>
      <div class="exp-company">${escapeHtml(e.company || "")}${current}</div>
      <div class="exp-dates">${escapeHtml(dates)}</div>
    </div>`;
  }).join("");
  elements.newCompanyName.textContent = data.current_company || "this profile";
};

// ── Step 3: run the matcher on the server ───────────────────────────────────
const runMatcher = async (overrides = {}) => {
  if (!extracted) return null;
  setStatus("matching…", "warn");
  const payload = {
    linkedin_url: extracted.linkedin_url,
    linkedin_slug: extracted.linkedin_slug,
    full_name: elements.full_name.value.trim() || extracted.full_name,
    current_company: elements.current_company.value.trim() || extracted.current_company,
    email: extracted.email,
  };
  if (overrides.full_name !== undefined) payload.full_name = overrides.full_name;
  if (overrides.current_company !== undefined) payload.current_company = overrides.current_company;
  return new Promise((resolve) => {
    let settled = false;
    // 15-second timeout: if the service worker doesn't respond, surface
    // an error instead of leaving the user stuck on "matching…".
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      setStatus("matcher timed out (15s) — click Re-scan", "error");
      resolve(null);
    }, 15000);
    chrome.runtime.sendMessage(
      { type: "LF_MATCH", payload },
      (resp) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (chrome.runtime.lastError || !resp || !resp.ok) {
          setStatus("matcher offline", "error");
          return resolve(null);
        }
        resolve(resp.json);
      },
    );
  });
};

// ── Step 4: render the 4-case match options ─────────────────────────────────
const renderMatchOptions = (matchResult) => {
  if (!matchResult) {
    // No match data. Show all 4 options, but no detail.
    for (const opt of elements.matchOptions.querySelectorAll(".match-opt")) {
      opt.hidden = false;
    }
    // Pre-select new_company_new_contact
    selectAction("new_company_new_contact");
    return;
  }

  // Update existing — show if there's a best_contact
  const updateOpt = elements.matchOptions.querySelector('[data-action="update_existing"]');
  const updateDetail = $("matchUpdateDetail");
  if (matchResult.best_contact) {
    updateOpt.hidden = false;
    const c = matchResult.best_contact;
    updateDetail.textContent = `${c.full_name} at ${c.company_name || "(no company)"} (${c.matched_field}, contact #${c.contact_id})`;
    is_manually_edited = !!c.is_manually_edited;
    if (is_manually_edited) {
      elements.manualConfirmRow.hidden = false;
    } else {
      elements.manualConfirmRow.hidden = true;
    }
  } else {
    updateOpt.hidden = true;
  }

  // New contact at existing company — show if a company candidate exists
  const newAtExistingOpt = elements.matchOptions.querySelector('[data-action="new_contact_existing_company"]');
  const newAtExistingDetail = $("matchNewAtExistingDetail");
  if (matchResult.best_company) {
    newAtExistingOpt.hidden = false;
    const co = matchResult.best_company;
    newAtExistingDetail.textContent = `Match: ${co.name} (${co.matched_field}, distance ${co.match_distance}, company #${co.company_id})`;
  } else {
    newAtExistingOpt.hidden = true;
  }

  // New company + new contact — always available
  // (already visible)

  // Discard — always available
  // (already visible)

  // If there are ties at the strongest strength, show the dropdown
  const ties = matchResult.contact_ties || [];
  if (ties.length > 0 && matchResult.best_contact) {
    elements.contactTieDropdown.hidden = false;
    elements.contactTieSelect.innerHTML = "";
    const addOpt = (label, value) => {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      elements.contactTieSelect.appendChild(opt);
    };
    addOpt(
      `${matchResult.best_contact.full_name} at ${matchResult.best_contact.company_name} (#${matchResult.best_contact.contact_id})`,
      matchResult.best_contact.contact_id,
    );
    for (const t of ties) {
      addOpt(
        `${t.full_name} at ${t.company_name || "(no company)"} (#${t.contact_id})`,
        t.contact_id,
      );
    }
  } else {
    elements.contactTieDropdown.hidden = true;
  }

  // Pre-select the strongest available action
  if (matchResult.best_contact) {
    selectAction("update_existing");
  } else if (matchResult.best_company) {
    selectAction("new_contact_existing_company");
  } else {
    selectAction("new_company_new_contact");
  }
};

const selectAction = (action) => {
  const radio = elements.matchOptions.querySelector(`input[value="${action}"]`);
  if (radio) radio.checked = true;
  validateSend();
};

const validateSend = () => {
  const checked = elements.matchOptions.querySelector('input[name="match_action"]:checked');
  const hasAction = !!checked;
  // t3.4: if is_manually_edited and action is update_existing, require confirmation
  const needsConfirm = is_manually_edited
    && checked && checked.value === "update_existing"
    && !elements.confirmOverwriteManual.checked;
  elements.sendBtn.disabled = !hasAction || needsConfirm;
  if (needsConfirm) {
    showResult("Tick the confirmation box to allow overwriting this manually-edited contact.", "error");
  } else if (hasAction) {
    showResult("", "");
  }
};

// Wire up event listeners on the match options
elements.matchOptions.addEventListener("change", validateSend);
elements.confirmOverwriteManual.addEventListener("change", validateSend);

// ── Step 5: send to the server ───────────────────────────────────────────────
const buildPayload = () => {
  // Re-derive first/last from the form so an edit flows through.
  const fullName = elements.full_name.value.trim();
  let first = elements.first_name.value.trim();
  let last = elements.last_name.value.trim();
  if ((!first || !last) && fullName) {
    const parts = fullName.split(/\s+/);
    first = first || parts[0];
    last = last || parts.slice(1).join(" ");
  }
  // Experience (read from last extracted, since we don't expose editors per row)
  // v1: trust what the content script captured.
  const exp = (extracted && extracted.experience) || [];

  const checked = elements.matchOptions.querySelector('input[name="match_action"]:checked');
  const action = checked ? checked.value : "new_company_new_contact";

  const tieSelect = elements.contactTieSelect;
  const chosenContactId = (tieSelect && !elements.contactTieDropdown.hidden && tieSelect.value)
    ? Number(tieSelect.value)
    : (matchResult && matchResult.best_contact ? matchResult.best_contact.contact_id : null);
  const chosenCompanyId = matchResult && matchResult.best_company ? matchResult.best_company.company_id : null;

  return {
    linkedin_url: elements.linkedin_url.value.trim(),
    linkedin_slug: extracted ? extracted.linkedin_slug : "",
    full_name: fullName,
    first_name: first,
    last_name: last,
    current_title: elements.current_title.value.trim(),
    current_company: elements.current_company.value.trim(),
    location: elements.location.value.trim(),
    email: elements.email.value.trim() || null,
    phone: elements.phone.value.trim() || null,
    experience: exp,
    match_action: action,
    matched_contact_id: chosenContactId,
    matched_company_id: chosenCompanyId,
    confirm_overwrite_manual: elements.confirmOverwriteManual.checked,
  };
};

const send = () => {
  const payload = buildPayload();
  showResult("Sending…", "");
  elements.sendBtn.disabled = true;
  chrome.runtime.sendMessage({ type: "LF_PUSH", payload }, (resp) => {
    if (chrome.runtime.lastError || !resp || !resp.ok) {
      const detail = (resp && resp.json && resp.json.detail) || chrome.runtime.lastError?.message || "unknown error";
      showResult(`Send failed: ${detail}`, "error");
      setStatus("send failed", "error");
      elements.sendBtn.disabled = false;
      return;
    }
    const r = resp.json;
    showResult(`OK — action=${r.matched_action}, contact_id=${r.contact_id}, company_id=${r.company_id}`, "ok");
    setStatus("pushed", "ok");
    elements.sendBtn.disabled = true; // can't re-send
  });
};

elements.sendBtn.addEventListener("click", send);
elements.rescanBtn.addEventListener("click", async () => {
  const fresh = await requestReExtraction();
  if (fresh) {
    extracted = fresh;
    fillForm(extracted);
    matchResult = await runMatcher();
    renderMatchOptions(matchResult);
    setStatus("ready", "ok");
  }
});
elements.debugBtn.addEventListener("click", async () => {
  const tabs = await new Promise((r) => chrome.tabs.query({ active: true, currentWindow: true }, r));
  const tab = tabs[0];
  if (!tab) return;
  setStatus("sending snapshot…", "warn");
  chrome.tabs.sendMessage(tab.id, { type: "LF_SEND_DEBUG" }, (resp) => {
    if (chrome.runtime.lastError || !resp || !resp.ok) {
      showResult("Debug snapshot failed", "error");
      setStatus("snapshot failed", "error");
      return;
    }
    showResult("Debug snapshot sent. Check server debug/ folder.", "ok");
    setStatus("snapshot sent", "ok");
  });
});

// ── Config (API base + key) ──────────────────────────────────────────────────
const loadConfig = () => new Promise((resolve) => {
  chrome.runtime.sendMessage({ type: "LF_CONFIG_GET" }, (resp) => {
    if (resp && resp.ok) {
      elements.apiBase.value = resp.config.apiBase || "";
      elements.apiKey.value = resp.config.apiKey || "";
      // Show which server the extension is talking to. Helps the user
      // verify the right host is in use (Tailscale vs localhost vs RDP IP).
      if (elements.serverHint) {
        elements.serverHint.textContent = `→ ${resp.config.apiBase}`;
      }
    }
    resolve();
  });
});

elements.saveConfigBtn.addEventListener("click", () => {
  chrome.runtime.sendMessage({
    type: "LF_CONFIG_SET",
    apiBase: elements.apiBase.value.trim(),
    apiKey: elements.apiKey.value.trim(),
  }, (resp) => {
    elements.configStatus.textContent = resp && resp.ok ? "Saved." : "Save failed.";
    setTimeout(() => { elements.configStatus.textContent = ""; }, 2000);
  });
});

// ── Highlight inbox (t3.1) ──────────────────────────────────────────────────
const showPendingHighlight = () => {
  chrome.storage.local.get("pendingHighlight", ({ pendingHighlight }) => {
    if (pendingHighlight && pendingHighlight.text) {
      elements.highlightInbox.hidden = false;
      elements.hlContext.textContent = pendingHighlight.context_label || "(no context)";
      elements.hlText.value = pendingHighlight.text;
    }
  });
};
elements.hlCopyBtn.addEventListener("click", () => {
  navigator.clipboard.writeText(elements.hlText.value).then(
    () => { elements.hlCopyBtn.textContent = "Copied!"; setTimeout(() => { elements.hlCopyBtn.textContent = "Copy"; }, 1500); },
  );
});
elements.hlDismissBtn.addEventListener("click", () => {
  chrome.storage.local.remove("pendingHighlight", () => {
    elements.highlightInbox.hidden = true;
  });
});

// ── Utility ─────────────────────────────────────────────────────────────────
const escapeHtml = (s) =>
  String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

// ── Main flow ────────────────────────────────────────────────────────────────
const main = async () => {
  await loadConfig();
  setStatus("reading…", "warn");
  extracted = await requestExtraction();
  if (!extracted) return;

  // The popup ALWAYS shows. Hide the placeholder.
  elements.extracting.hidden = true;
  elements.formSection.hidden = false;
  elements.matchSection.hidden = false;
  fillForm(extracted);

  matchResult = await runMatcher();
  if (matchResult) {
    renderMatchOptions(matchResult);
  } else {
    // Server offline or no key. Show all 4 options without candidate details.
    for (const opt of elements.matchOptions.querySelectorAll(".match-opt")) {
      opt.hidden = false;
    }
    selectAction("new_company_new_contact");
  }
  setStatus("ready", "ok");
  showPendingHighlight();
};

main();

// ── Re-run matcher when the user edits name or company ─────────────────────
// t3.5: manual edits must refresh candidate matches so the user can select an
// existing contact/company instead of creating duplicates.
const debounce = (fn, ms) => {
  let t;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
};

const refreshMatches = debounce(async () => {
  if (!extracted) return;
  matchResult = await runMatcher();
  renderMatchOptions(matchResult);
  setStatus("ready", "ok");
}, 400);

elements.full_name.addEventListener("input", refreshMatches);
elements.current_company.addEventListener("input", refreshMatches);
