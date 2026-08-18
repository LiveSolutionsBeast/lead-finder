# Lead Finder v2 — Final System Report

**Date:** 2026-07-24
**Sessions:** Session 1 (SMTP hardening) + Session 2 (LinkedIn plugin) + Session 3 (Integration)
**Status:** **PASSED** — system is **READY FOR PRODUCTION** with documented risks.
**Post-mortems:** `PHASE_0_REPORT.md`, `PHASE_1_REPORT.md`, `PHASE_2_3_REPORT.md`, `SESSION_3_E2E_RESULTS.md`, `SESSION_3_T42_RESULTS.md`, `SESSION_3_T43_RESULTS.md`.

---

## 1. What this system is

Lead Finder v2 is a **single database with two front doors** for ingesting and validating business leads:

- **Pathway A — Lead Finder search:** automated discovery, bulk AI enrichment, email pattern inference, SMTP validation. Long-running pipeline, batches of dozens of leads at a time.
- **Pathway B — LinkedIn plugin:** human-in-the-loop profile injection via a Chrome extension. The user (with their premium LinkedIn membership) browses profiles and pushes enriched, real data into the system one at a time, with explicit confirmation.

Both pathways write to the same `contacts`, `companies`, `contact_experience`, and `pending_pushes` tables. Both share the same SMTP validation pipeline. Both produce contacts that the export pipeline turns into Salesforce-ready CSVs.

The critical value at the end is **real, validated emails you can send to without burning your sender reputation**.

---

## 2. System architecture (where the pieces live)

```
┌──────────────────────────────────────────────────────┐
│ Pathway A: Lead Finder search                        │
│   (lf_search.py → lf_ai_enrich.py → lf_email_patterns│
│    → lf_email_validator.py → lf_pipeline.py)        │
│                                                      │
│   Runs as background jobs in the lead-finder server. │
│   Produces many contacts at once with derived emails. │
└──────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────┐
│ Pathway B: LinkedIn plugin (human-in-the-loop)       │
│   (Chrome extension content.js → background.js →     │
│    /api/inject/linkedin-profile → contacts)          │
│                                                      │
│   User browses LinkedIn, popup shows extracted data, │
│   user picks one of 4 match actions, send POSTs.     │
│   One profile at a time. Always shows confirmation.  │
└──────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────┐
│ Shared database (SQLite at lf.db)                    │
│   contacts (with validation_* fields)                │
│   companies                                          │
│   contact_experience                                 │
│   pending_pushes (audit log for Pathway B)           │
│   search_sessions (audit log for Pathway A)          │
└──────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────┐
│ Shared validation pipeline (lf_email_validator.py)   │
│   2-probe design: random local-part, then actual.    │
│   Writes 11 fields via write_contact_validation().   │
│   Manual override endpoint for the user.             │
└──────────────────────────────────────────────────────┘
                          │
                          ▼
┌──────────────────────────────────────────────────────┐
│ Export pipeline (filtered by validation status)      │
│   /api/email/ready-for-export  — global export       │
│   /api/export/contacts/{key}   — session export     │
│   /api/export/email-patterns/{key} — patterns        │
│                                                      │
│   Only includes "Okay to Send" + email_ready=1.      │
└──────────────────────────────────────────────────────┘
```

---

## 3. The validation state machine

A contact's email moves through these states (CONTRACTS.md §1). At every transition, 11 fields are persisted via `write_contact_validation()`.

| `smtp_validation_status` | Set by | Meaning | Exportable? |
|---|---|---|---|
| `NULL` | insert, plugin | Not yet tested | No |
| `"Okay to Send"` | validator, manual_override | Confirmed deliverable | **Yes** (also requires `email_ready_for_export=1`) |
| `"Do Not Send"` | validator, manual_invalid | Mailbox does not exist | No |
| `"Maybe"` | validator | Soft response (greylist, etc.) | No |

### 2-probe validation flow

For each email to validate, the SMTP pipeline does:

1. **Random local-part probe.** Generate `randomprobe@<domain>`. Send EHLO+STARTTLS+MAIL FROM+RCPT TO.
   - **250** → server is **catch-all**. Mark `Do Not Send` with confidence 0.4. (The server accepts any local-part, so we cannot prove a specific address exists.)
   - **550/553** → server is **not catch-all**. Continue to step 2.
   - **4xx or timeout** → soft response. Mark `Maybe` with confidence 0.3.
2. **Actual local-part probe** (only if step 1 returned 550). Send RCPT TO with the real local-part.
   - **250** → mailbox exists. Mark `Okay to Send` with confidence 0.9.
   - **550/553** → mailbox does not exist. Mark `Do Not Send` with confidence 0.95.
   - **4xx/timeout** → soft response. Mark `Maybe` with confidence 0.3.

Connection failure retries once with 2s backoff. The single helper `_smtp_probe_rcpt` is used by both probes; all SMTP code paths go through it.

### Manual override

If the SMTP probe is wrong (e.g., the server is on a blocklist but the address is real), the user can mark a contact as known-good or known-bad:

```http
POST /api/contact/{contact_id}/manual-validate
Content-Type: application/json
X-LF-Key: lf_k_…

{
  "status": "valid",         // or "invalid"
  "reason": "Confirmed via direct send"   // required
}
```

This sets `validation_method = "manual_override"`, `validation_confidence = 1.0`, and updates the export flag. The reason is stored in `validation_response` and `email_rejected_reason` for audit.

---

## 4. The 4-case match engine (LinkedIn plugin)

When the user clicks "Send" in the plugin popup, the server runs the 4-priority disambiguation per CONTRACTS.md §5:

1. **linkedin_slug match** — strongest, immutable. If a contact with this slug exists, the push updates it.
2. **Normalized name + same company** — if the contact name normalizes to an existing contact at the same company, update that contact and bind its linkedin_slug.
3. **Normalized name + fuzzy company match (Levenshtein ≤ 3)** — if the company name is within 3 edits of an existing company, create a new contact there.
4. **Email match** — fallback. Only used if no name/slug match.

The popup lets the user **see and confirm** which path the server picked, and **override** if they want a different action. The user's choice is always respected; the popup's auto-match is a default, not a hard rule.

### is_manually_edited protection (t3.4)

If a contact has `is_manually_edited=1` (set by user edits in the admin view), the plugin will **refuse to overwrite it** unless the popup collects an explicit `confirm_overwrite_manual=true` checkbox. This protects carefully-edited records from being clobbered by re-extraction or stale plugin data.

---

## 5. End-to-end usage

### Pathway A — Lead Finder search (bulk)

1. The user creates a search session via the dashboard (`/finder/`).
2. `lf_search.py` runs the discovery queries (SearXNG, Brave, SerpAPI) and produces a list of companies.
3. `lf_ai_enrich.py` enriches each company with description, industry, employee count, etc.
4. `lf_pipeline.py` extracts contact names from search results and AI inference.
5. `lf_email_patterns.py` infers email patterns (first.last, flast, etc.) and produces derived emails.
6. `lf_email_validator.py` runs each derived email through the 2-probe SMTP flow.
7. The user reviews the session, manually overrides any false negatives via the contact detail view, and exports to CSV.

### Pathway B — LinkedIn plugin (human-in-the-loop)

1. **One-time install:** load the unpacked extension at `static/chrome-extension/` in Chrome's developer mode (see `PLAN.md` "Chrome Extension Deployment").
2. **Browse to a profile** at `https://www.linkedin.com/in/<slug>`.
3. **Click the Lead Finder icon.** The popup shows extracted data from the Experience section.
4. **Edit any field if needed.** The 4-case match radio shows what action the server will take.
5. **Click "Send to Lead Finder."** The popup POSTs to `/api/inject/linkedin-profile` with the chosen action.
6. The server writes a `pending_pushes` audit row, performs the action, and returns the resulting `contact_id` / `company_id`.
7. If the contact has an email, the validation pipeline picks it up on the next batch run.
8. The user reviews the new contact in the admin view (`/finder/contact/{id}`) and either accepts, edits, or marks invalid.

### Unified export

Once the user has a mix of contacts from both pathways:

- **Global export:** `GET /api/email/ready-for-export?limit=500` returns all contacts with `smtp_validation_status = "Okay to Send" AND email_ready_for_export = 1`. This is the safest, most cross-cutting export.
- **Per-session export:** `GET /api/export/contacts/{session_key}` returns the contacts in a specific search session, filtered to the same set.
- **Email patterns:** `GET /api/export/email-patterns/{session_key}` returns the discovered email patterns for a session, useful for building domain-patterns documentation.

All three endpoints default to filtered output. To bypass the filter (e.g., to review unvalidated contacts), pass `?include_unvalidated=1`.

---

## 6. What changed in Session 3 (the integration)

Session 3 verified that Session 1 and Session 2 work together. Specifically:

### t4.1 — End-to-end test with 20 real profiles (`docs/SESSION_3_E2E_RESULTS.md`)

Sent 20 real profiles through the same `/api/inject/linkedin-profile` endpoint the Chrome extension calls. Covered every action path:

| Action | Count | Result |
|---|---|---|
| `update_existing` (existing companies) | 10 | All pass — contact updated in place, `linkedin_slug` set, `source_linkedin_verified=1` |
| `new_contact_existing_company` | 3 | All pass — new contact at Austal USA, SpaceX, Honeywell Aerospace |
| `new_company_new_contact` | 3 | All pass — new companies TransAstra Corp, Rocket Lab Ltd, Kairos Aerospace Inc created |
| Auto-match by `linkedin_slug` | 2 | All pass — server auto-routed by slug, contacts updated |
| `discard` | 2 | All pass — audit row written, no contact/company created |
| **Total** | **20** | **20/20 passed** (0.01-0.02s per push, all returned 200 OK) |

### t4.2 — SMTP validation status lifecycle (`docs/SESSION_3_T42_RESULTS.md`)

Verified the cross-session handoff:

1. Plugin push with no email → `smtp_validation_status = NULL` (correct, nothing to validate).
2. Plugin push with email `rick@greenfieldpaper.com` → `smtp_validation_status = NULL` immediately, then `POST /api/contact/{id}/validate-email` ran the 2-probe flow and wrote:
   - `smtp_validation_status = "Do Not Send"` (the address is invalid)
   - `validation_method = "smtp_live"`
   - `validation_confidence = 0.95`
   - `validation_mx_host = "aspmx.l.google.com"`
   - `validation_response = "550 5.1.1 The email account that you tried to reach does not exist..."`
   - `validation_latency_ms = 1714`
3. Existing contact #1999 updated with a new email → `smtp_validation_status = NULL`, then validator wrote `Maybe` / `smtp_live`.

All 3 cases passed.

### t4.3 — Export filter fix (`docs/SESSION_3_T43_RESULTS.md`)

**Two bugs found and fixed:**

1. **Export pipeline did not filter by validation status.** `/api/export/contacts/{session_key}` exported all session contacts, including unvalidated ones — violating `CONTRACTS.md §1`. Fix: added a filter clause to the endpoint (`lf_server.py` lines 2412-2471). Default now filters to `smtp_validation_status = "Okay to Send" AND email_ready_for_export = 1`. Opt-out via `?include_unvalidated=1` for debugging.

2. **Plugin `update_existing` was setting `is_manually_edited=1`** on every push. `patch_contact` (`lf_db.py:662`) defaults to `is_manually_edited=1` for back-compat, and the inject handler wasn't overriding it. This meant every plugin update self-tripped the t3.4 guard on the next push. Fix: explicit `"is_manually_edited": 0` in the inject handler's patch dict (`lf_server.py:3327`).

**Verification:** With both fixes applied, t4.1 (20/20 pass), t4.2 (3/3 pass), and t4.3 verification (sessions with no validated contacts return 0 rows; sessions with validated contacts return exactly the validated ones) all pass.

### t4.4 — Refactor and document

- Verified that `write_contact_validation()` (lf_db.py:1031) is the single source of truth for all 11 validation field writes. All 5 callers funnel through it.
- The one carve-out is `lf_server.py:3364`, where the inject handler resets all 11 fields to NULL when a new email is attached. This was an intentional "please re-validate" signal (CONTRACTS.md §2 rule 7), not a write of a validation result. The carve-out is now explicitly documented in a comment.
- Found and fixed a second integration bug (see t4.3 Bug 2 above): the inject handler's `update_existing` path now explicitly sets `is_manually_edited=0` to override `patch_contact`'s back-compat default of 1.
- Added docstring inventory: all 10 Session 2 public helpers in `lf_db.py` have docstrings; all public functions in `lf_matcher.py` and `lf_name_match.py` have docstrings.
- This FINAL_REPORT.md + the updated PLAN.md "System Status" section.

---

## 7. Known risks (carried forward from Phase 1)

1. **Probe IP is on Spamhaus blocklist.** SMTP probes to Outlook-hosted domains (`@boeing.com`, `@honeywell.com`, etc.) return 550 even for valid addresses. ~50% of the 120-address stress test hit Outlook MX servers. Mitigation: enable `rotate_sender_identity: true` in `lf_config.json` and add more domains to the `sender_domains` pool, or use a different probe IP. The manual-validate endpoint is the per-contact escape hatch.
2. **p95 latency > 5s target.** 10.8% of addresses take > 5s (p95=8.5s). This is the new baseline with 2 probes + retry + jitter. If the 5s target is hard, reduce per-probe `smtp_timeout` from 10s to 5s (trades robustness for latency).
3. **Catch-all domains cannot be validated.** `spacex.com`, `kairosaerospace.com`, and others return 250 on any local-part, so the validator correctly marks them `Do Not Send` (with confidence 0.4). For these domains, the user must rely on manual-validate for any address they trust.
4. **Only 1 of 23 historical "Okay to Send" survived revalidation.** The old single-probe design was almost entirely false-positive. Any contact that was previously exported from the old data should be re-validated.

---

## 8. Open questions

1. **Spamhaus block on probe IP (47.34.180.80).** Same as Phase 0/1. Action: investigate whether the block is temporary (request removal from https://www.spamhaus.org/query/ip/47.34.180.80) or persistent. If persistent, consider sender-domain rotation.
2. **In-browser extension smoke test.** This is a headless environment, so the Session 3 E2E test exercised the server-side pipeline with real JSON payloads. The actual Chrome extension has not been loaded in a browser. The user (Anthony) should do a real-world smoke test: install the extension, browse 2-3 real Naval/Aerospace profiles, push them, confirm the data lands correctly in the admin view.
3. **Retention policy for `pending_pushes`.** The table accumulates forever. For the expected volume (a few pushes per day) this is fine for years, but a future `DELETE FROM pending_pushes WHERE created_at < ?` cron is recommended.
4. **Multiple contact ties render in a `<select>` dropdown.** If there are 5+ ties at the same strength, the popup gets cramped. Could be improved with a search-as-you-type list in a future iteration.

---

## 9. File index (what lives where)

### Server-side

| File | Purpose | Owner session |
|---|---|---|
| `lf_server.py` | FastAPI server, all HTTP endpoints, ~3500 lines | All |
| `lf_db.py` | SQLite helpers, all DB writes, ~2037 lines | All |
| `lf_email_validator.py` | 2-probe SMTP validation, `ValidationResult` dataclass | Session 1 |
| `lf_email_patterns.py` | Email pattern discovery (first.last, flast, etc.) | Pre-existing |
| `lf_matcher.py` | 4-case match engine for plugin ingest | Session 2 |
| `lf_name_match.py` | Name normalization, Levenshtein, fuzzy company match | Session 2 |
| `lf_export.py` | CSV writers (contacts, patterns, ready-for-export) | Pre-existing |
| `lf_search*.py` | Discovery, providers, SearXNG/Brave/SerpAPI | Pre-existing |
| `lf_ai_enrich.py` | Bulk AI enrichment via Ollama/DeepSeek | Pre-existing |
| `lf_openwebui_tools.py` | Open WebUI tool surface | Pre-existing |

### Client-side

| File | Purpose |
|---|---|
| `static/chrome-extension/manifest.json` | Manifest v3, host: `https://www.linkedin.com/in/*` |
| `static/chrome-extension/content.js` | Extracts Experience section data, 352 lines |
| `static/chrome-extension/popup.html` + `.css` + `.js` | Confirmation popup, 4-case match radio, Send button |
| `static/chrome-extension/background.js` | Service worker, proxies API calls, holds API key |
| `static/chrome-extension/content.css` | Highlight-to-extract mode styles |
| `static/chrome-extension/icons/` | icon{16,48,128}.png |

### UI

| File | Purpose |
|---|---|
| `static/finder/` | Lead Finder dashboard |
| `static/contacts.html` | Contact list with row-level manual-validate modal |
| `static/audit.html` | Audit log with 5 buckets, including LinkedIn plugin pushes |
| `static/extension.html` | Extension documentation and file inventory |

### Tests and scripts

| File | Purpose |
|---|---|
| `scripts/session3_e2e.py` | 20-profile E2E test, t4.1 |
| `scripts/session3_t42_smtp_lifecycle.py` | SMTP validation lifecycle test, t4.2 |
| `scripts/revalidate_existing_contacts.py` | Phase 1 t1.3 runner |
| `scripts/test_phase0_20.py` | Phase 1 t1.5 runner |
| `scripts/stress_test_100.py` | Phase 1 t1.6 runner |
| `/tmp/opencode/test_match.py` | Unit tests for `lf_matcher.py` and `lf_name_match.py` (11 assertions) |

### Documentation

| File | Purpose |
|---|---|
| `docs/PLAN.json` | Machine-readable plan, 5 phases |
| `docs/PLAN.md` | Human-readable plan, post-mortems, deployment notes |
| `docs/CONTRACTS.md` | Shared interface contract (validation states, plugin rules, API) |
| `docs/PHASE_0_REPORT.md` | SMTP reality check, 3 bugs + 1 design issue |
| `docs/PHASE_1_REPORT.md` | SMTP hardening post-mortem, 2-probe design |
| `docs/PHASE_2_3_REPORT.md` | Plugin v1 + polish post-mortem |
| `docs/SESSION_3_E2E_RESULTS.md` | t4.1 detailed results |
| `docs/SESSION_3_T42_RESULTS.md` | t4.2 detailed results |
| `docs/SESSION_3_T43_RESULTS.md` | t4.3 detailed results |
| `docs/FINAL_REPORT.md` | This file |
| `docs/session-prompts/SESSION_{1,2,3}_*.md` | Spawn prompts for each session |

---

## 10. What to read first

If you are new to the system:

1. **`docs/CONTRACTS.md`** — the source of truth for what every piece does.
2. **`docs/PLAN.md`** — the high-level plan, decisions made, why.
3. **`docs/PHASE_0_REPORT.md`** — the SMTP reality check that exposed the 3 bugs + 1 design issue.
4. **`docs/PHASE_1_REPORT.md`** — the 2-probe design and what's still risky.
5. **`docs/PHASE_2_3_REPORT.md`** — the plugin, how to install it, and what's still risky.
6. **This file** — the integrated system.

If you want to fix something, the contracts in `CONTRACTS.md` are the source of truth. If you need to change a contract, post to `PLAN.md` Open Questions and wait for the owner to resolve.
