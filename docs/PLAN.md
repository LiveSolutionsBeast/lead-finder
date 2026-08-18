# Lead Finder v2 Hardening + LinkedIn Plugin

**Created:** 2026-07-24
**Status:** Phase 5 complete — email pattern + SMTP verification hardened across both pathways
**Owner:** Anthony Turgman
**Machine-readable plan:** `PLAN.json`
**Shared interface spec:** `CONTRACTS.md` (v1.2)
**Phase 5 report:** `PHASE_5_HARDENING_PLAN.md`

---

## Phase 0–4 recap

Phases 0–4 built the foundation:
- Phase 0: SMTP reality check (3 bugs + 1 design issue found).
- Phase 1: 2-probe SMTP validation (`lf_email_validator.py`).
- Phase 2–3: LinkedIn Chrome extension and plugin ingest pipeline (`lf_server.py:3509-3827`).
- Phase 4: Integration and end-to-end tests (t4.1, t4.2, t4.3).

Read `PHASE_0_REPORT.md`, `PHASE_1_REPORT.md`, `PHASE_2_3_REPORT.md`, and `FINAL_REPORT.md` for details.

---

## Phase 5 — Email pattern + SMTP verification hardening

Phase 5 makes the two ingestion pathways converge on a single, unified email chain so **every contact that can have an email gets one, and every email is SMTP-validated or manually-overridden.**

### What was broken before Phase 5

1. **Pathway A (search) never ran the email chain.** After `upsert_contact` in `lf_executives.py`, no pattern inference, derivation, or SMTP validation happened.
2. **Pathway B (plugin) only validated when a popup email was supplied.** New plugin contacts with no email never got a derived email or validation.
3. **`discover_and_store_pattern` and `get_cached_validation` were dead code.** Pattern inference and cache reuse were not wired into either pathway.
4. **`_auto_validate_on_derive` was disabled in config.** Even the existing derivation hook was off.
5. **`validate-batch` only wrote to the cache, not to contact rows.** A background batch job could validate emails without ever updating the contact record.
6. **`pending_pushes` was audit-only with no dedup guard.** A double-fire from the plugin hit the `linkedin_slug` UNIQUE index and returned HTTP 500.
7. **`patch_contact` defaulted `is_manually_edited=1` for legacy callers.** Automated callers (including `lf_audit_backstop.py`) accidentally marked contacts as manually edited, causing the plugin t3.4 guard to trip on subsequent pushes.
8. **Client-side "Export CSV" exported all visible contacts,** including unvalidated derived emails.
9. **`CONTRACTS.md` listed 9 validation statuses** but the runtime stored only four (`NULL`, `"Okay to Send"`, `"Do Not Send"`, `"Maybe"`).

### What Phase 5 built

| Component | Files | Change |
|---|---|---|
| Unified email chain | `lf_email_patterns.py` | New `resolve_and_validate_email(contact_id, *, source, popup_email=None, force_revalidate=False)` — pattern → derive → cache pre-check → SMTP 2-probe → persist via `write_contact_validation`. Returns `ResolutionResult`. |
| Pattern inference fallback | `lf_email_patterns.py` | `_infer_pattern_v2` now falls back to `discover_email_pattern` (SearXNG-based) when `ai_infer_email_pattern_v2` times out or returns nothing. Also searches for a domain when a plugin-created company has no website. |
| Pathway A wiring | `lf_executives.py` | `resolve_and_validate_email` is called after every `upsert_contact` in search discovery. |
| Pathway B wiring | `lf_server.py` | `resolve_and_validate_email` is called after `update_existing`, `new_contact_existing_company`, and `new_company_new_contact` branches. |
| Dedup guard | `lf_server.py` | If matcher routes to create but `linkedin_slug` already exists, the request is rerouted to `update_existing`. |
| Cache pre-check | `lf_email_patterns.py` | The chain calls `get_cached_validation` before running SMTP and re-applies cached results. |
| Batch validator fix | `lf_server.py` | `/api/email/validate-batch` now also writes results to matching `contacts` rows via `write_contact_validation`. |
| Manual override refactor | `lf_server.py` | `/api/contact/{id}/manual-validate` now uses `write_contact_validation` with `validation_method='manual_override'`. |
| `is_manually_edited` foot-gun fix | `lf_db.py`, `lf_server.py`, `lf_audit_backstop.py` | `patch_contact` no longer defaults `is_manually_edited=1`. `api_patch_contact` explicitly defaults it to `1` for manual edits. Automated callers pass `0` or `ai_edited_at`. |
| Export filter fix | `static/contacts.html` | Client-side "Export CSV" now filters to `smtp_validation_status === "Okay to Send"` and `email_ready_for_export`. |
| Audit backstop extension | `lf_audit_backstop.py` | New `flag_stale_unvalidated_emails()` helper flags contacts with email but no `smtp_validation_status`. |
| Config | `lf_config.json` | `auto_validate_on_derive` set to `true`. |
| Tests | `tests/test_email_chain.py`, `tests/test_plugin_email_chain.py`, `scripts/session3_e2e.py`, `scripts/session3_t42_smtp_lifecycle.py` | Updated/added tests. |
| Docs | `docs/CONTRACTS.md`, `docs/PHASE_5_HARDENING_PLAN.md` | Updated to v1.2. |

### Verification

- `tests/test_email_chain.py` — 5 unit tests pass.
- `tests/test_plugin_email_chain.py` — 3 focused E2E tests pass.
- `scripts/session3_e2e.py` — 20/20 real-profile plugin tests pass (run with `--reset`).
- `scripts/session3_t42_smtp_lifecycle.py` — 3/3 lifecycle cases pass.
- All modified files compile with `python3 -m py_compile`.
- Server restarts cleanly and responds to `/api/health`.

### Known remaining risks

1. **Spamhaus IP block:** unchanged from Phase 1. Outlook-hosted MX servers may still return 550. Affects real-world deliverability but not the pipeline logic.
2. **AI/Ollama availability:** `_infer_pattern_v2` now falls back to SearXNG, but if both AI and SearXNG fail, the chain cannot derive an email and status remains `NULL`.
3. **Client-side export filter dependency:** relies on `/api/contacts` returning `smtp_validation_status` and `email_ready_for_export`. If the endpoint is changed, the filter must be updated.
4. **Database locked under concurrent plugin pushes:** existing SQLite limitation; `PRAGMA busy_timeout` is set but very fast double-fires can still collide. The slug dedup guard mitigates the symptom.

---

## Operational rules

### Agentic work
- **Sequential when:** the work modifies the same file, depends on the output of another task, or has a contract dependency.
- **Parallel when:** work is read-only, targets different files, or runs tests on independent components.
- **When unclear, sequential.** Safe over fast.

### Cross-session communication
- Any session that needs to change `CONTRACTS.md` MUST post a flag to PLAN.md `Open Questions` section and wait for the owner to resolve before proceeding.
- A session completes by updating `PLAN.json` and writing its post-mortem to `PLAN.md`.

### Subagent usage within a session
- Read-only exploration: parallel.
- File edits: only one agent edits any given file at a time.
- Schema changes: go through `CONTRACTS.md` review.
- The lead agent of each session owns merge decisions.

---

## Open questions

1. **Spamhaus block on probe IP (47.34.180.80):** The Phase 1 SMTP probes are systematically getting 550 responses from Outlook-hosted MX servers due to a Spamhaus block on the probe IP. This is an infrastructure issue, not a code issue. Action: investigate whether the block is temporary (request removal from https://www.spamhaus.org/query/ip/47.34.180.80) or persistent. If persistent, consider:
   - Enable `rotate_sender_identity: true` in `lf_config.json` and add more domains to the `sender_domains` pool to spread load across IPs.
   - Or use a different probe IP (e.g., from a different cloud provider or residential proxy).
   This affects Session 4 (integration) and any real-world validation by Session 2 (plugin).
2. **In-browser extension smoke test:** Session 3's E2E test exercised the server-side pipeline with real JSON payloads because the test environment is headless. The actual Chrome extension has not been loaded in a browser. The user (Anthony) should do a real-world smoke test: install the extension at `static/chrome-extension/`, browse 2-3 real Naval/Aerospace profiles, push them, confirm the data lands correctly in the admin view.
3. **Retention policy for `pending_pushes`:** The table accumulates forever. For the expected volume (a few pushes per day) this is fine for years, but a future `DELETE FROM pending_pushes WHERE created_at < ?` cron is recommended.

---

## System Status

**Status: READY FOR PRODUCTION with documented risks.**

The lead-finder v2 system is now an integrated whole. Two ingestion pathways (Lead Finder search and LinkedIn plugin) write to a shared database and feed a shared 2-probe SMTP validation pipeline. Every contact that can be pattern-matched/derived is validated; manual override is available; exports are filtered.

### What to read first

1. `docs/CONTRACTS.md` — source of truth for what every piece does (v1.4).
2. `docs/PHASE_5_HARDENING_PLAN.md` — detailed Phase 5 plan and decisions.
3. `docs/CONTACT_REJECTION_POSTMORTEM.md` — root-cause analysis of the 41 contacts rejected by identity verification, with ingest-time hardening recommendations (H-1…H-7) to prevent recurrence.
4. `.env` — local secrets (gitignored). `LF_API_KEY` is the server/SPA API key.
5. `docs/FINAL_REPORT.md` — the integrated system, both pathways, the validation state machine, end-to-end usage.
6. `docs/SESSION_3_E2E_RESULTS.md` — 20-profile plugin E2E results.
7. `docs/SESSION_3_T42_RESULTS.md` — SMTP lifecycle E2E results.

---

## Phase 1 — SMTP validation hardening (1.5 days, Session 1)

**Owner session:** A dedicated session that reads `PLAN.json` and picks up Phase 1 tasks.

**Status (as of 2026-07-24):** **COMPLETE — PARTIAL** (see `PHASE_1_REPORT.md`). 2-probe design works correctly. 22 of the 23 historical "Okay to Send" contacts are now correctly reclassified as Do Not Send / Maybe. The 2-probe design is sound; the live probe environment is rate-limited by Spamhaus, which depresses apparent accuracy on Outlook-hosted domains.

**Goal:** Turn the SMTP pathway from "shell that may hallucinate patterns are real" into "tested, documented, reliable validation that we trust the output of." Specifically: implement the 2-probe design and re-validate the existing 23 "Okay to Send" contacts.

**Tasks (updated based on Phase 0 findings):**
- Implement 2-probe validation flow (random + actual local-part)
- Persist `validation_confidence`, `validation_method`, `validation_checked_at`, `validation_mx_host`, `validation_response`, `validation_latency_ms` on every validation result
- Add the manual override mechanism: a contact marked "I know this works" bypasses SMTP and gets `validation_method=manual_override`
- Re-validate the 23 existing "Okay to Send" contacts with the new 2-probe design
- Re-run the 20-address test from Phase 0 and verify >= 90% accuracy
- Add a 100-address stress test
- Refactor for robustness: connection retry, graceful timeout, MX lookup fallback, parallel SMTP with backoff
- Add `POST /api/contact/{id}/manual-validate` endpoint

**Gate criteria:**
- `validation_status` enum documented in `CONTRACTS.md` ✓ (already done)
- 2-probe design implemented and tested
- Manual override works and is tested
- >= 90% accuracy on the 20-address test
- All 23 existing "Okay to Send" contacts re-validated
- Stress test passes

---

## Phase 2 — LinkedIn plugin v1 (2 days, Session 2)

**Owner session:** A dedicated session that reads `PLAN.json` and picks up Phase 2 tasks.

**Runs in parallel with Phase 1** after Phase 0 is complete. The two sessions do not share runtime state during this phase.

**Goal:** A working Chrome extension that extracts data from real LinkedIn profiles and pushes it to the lead-finder database, with a human-in-the-loop verification step at every push.

**What gets built:**

### Chrome extension
- Location: `~/lead-finder/static/chrome-extension/`
- Manifest v3
- Content script on `*.linkedin.com/in/*` URLs
- Reads the **Experience section** (NOT the headline — user explicitly noted headlines are unreliable)
- Extracts: full name, current title, current company, location, full experience history
- Sends the data to a popup

### Popup
- Editable form for every extracted field
- Match action: 4 cases
  - Update existing contact
  - Create new contact at existing company
  - Create new company + new contact
  - Discard
- **Always shows confirmation** (per user requirement — no silent updates)
- Send button POSTs to `/api/inject/linkedin-profile`

### Ingestion endpoint
- `POST /api/inject/linkedin-profile` in `lf_server.py`
- Persists to a `pending_pushes` table for audit before committing to `contacts`
- Schema migration: `contacts.linkedin_slug` UNIQUE, `contact_experience` table
- `linkedin_slug` is the primary disambiguation key (immutable per LinkedIn account)
- Name normalization and fuzzy company match (Levenshtein <= 3)

### Deployment
- Load unpacked in Chrome (developer mode)
- Install on the Chrome profile logged into the user's premium LinkedIn account
- Documented in `PLAN.md` for future reference

**Gate criteria:**
- Extension loads unpacked on the user's premium LinkedIn profile
- Experience section extraction works
- Confirmation popup shows every field, all editable
- 4-case matching engine works
- Always shows the confirmation popup

---

## Phase 3 — Plugin polish (1 day, Session 2 continued)

**Owner session:** Session 2 continues.

**Goal:** Make the plugin production-grade for daily use.

**Tasks:**
- Highlight-to-extract mode: user selects text on the page, plugin captures with context (which field it came from)
- Audit log table: every push records who/what/when/before/after
- Audit log UI in the lead-finder admin view
- `is_manually_edited` flag on contacts: protects manually-edited records from being overwritten without explicit confirmation

**Gate criteria:**
- Highlight mode works
- Audit log records every push
- `is_manually_edited=true` contacts cannot be silently overwritten

---

## Phase 4 — Integration and end-to-end (1 day, Session 3)

**Owner session:** A third dedicated session, started after both Phase 1 and Phase 3 are complete.

**Goal:** Verify the system works end-to-end with real data.

**Tasks:**
- Run end-to-end test with 20 real LinkedIn profiles from the existing Naval/Aerospace companies in the lead-finder session
- Verify SMTP validation status correctly appears on plugin-injected contacts
- Verify the export pipeline correctly distinguishes validated from unvalidated
- Refactor for robustness
- Write a post-mortem for each of the three sessions into this document

**Gate criteria:**
- 20 real profiles pushed and validated
- Export pipeline flags are correct
- All three sessions have written their post-mortems

---

## Chrome Extension Deployment

**Location:** `/home/anthonyturgman/lead-finder/static/chrome-extension/`

**Files in the extension directory:**
- `manifest.json` — Manifest v3. Permissions: `activeTab`, `storage`, `scripting`. Host: `https://www.linkedin.com/in/*`.
- `content.js` — content script. Finds the Experience section, extracts name, current title, current company, location, full experience history. Uses a `MutationObserver` with 500ms debounce to re-extract on DOM changes. Implements highlight-to-extract mode.
- `content.css` — styles for the highlight button.
- `background.js` — Manifest v3 service worker. Holds the API base URL and key in `chrome.storage.local` (the content script never sees them). Proxies extract / match / push / audit calls to the server.
- `popup.html`, `popup.css`, `popup.js` — the always-show confirmation popup. Editable form, 4-case match radios, Send button.
- `icons/icon{16,48,128}.png` — solid blue square.

**Install (developer mode):**
1. Open `chrome://extensions/`
2. Toggle "Developer mode" on (top right).
3. Click "Load unpacked"
4. Select `/home/anthonyturgman/lead-finder/static/chrome-extension/`
5. The extension appears in the toolbar.

**Install on the Chrome profile logged into the user's premium LinkedIn account.** The extension inherits whatever that profile has access to. For multi-account setups, install on each profile that has LinkedIn access.

**Update after code changes:** refresh the extension on `chrome://extensions/` after each edit. The "Service worker (invalid)" button appears if the manifest changed — click **Reload** on the extension card.

**Verify a clean install:** open `/finder/extension` on the Lead Finder dashboard — it lists every file in the install directory with byte counts. If anything is missing the load-unpacked will fail.

**Known limitations:**
- **No headline extraction** — the user's explicit requirement. Only the Experience section is read. LinkedIn headlines are unreliable personal descriptors.
- **No silent updates** — the popup always shows. Every push requires an explicit Send click.
- **No automated scraping** — the user is the rate limiter. No background tabs, no auto-discovery, no scheduled tasks.
- **Date parsing is heuristic** — LinkedIn's "Mar 2022 - Present" strings may be parsed imperfectly. Stored as-is so the data is recoverable even if the parser changes.
- **Premium LinkedIn required** for some profiles. If LinkedIn returns a "limited profile" page, the Experience section may not load and the popup will say "Could not read the Experience section".

**API endpoint contract:** the popup POSTs to `/api/inject/linkedin-profile` (defined in CONTRACTS.md section 4). Authentication via the `X-LF-Key` header. The matching engine runs server-side at `/api/inject/linkedin-profile/match` so the popup can show candidate matches before the user picks an action.

---

## Operational rules

### Agentic work
- **Sequential when:** the work modifies the same file, depends on the output of another task, or has a contract dependency.
- **Parallel when:** work is read-only, targets different files, or runs tests on independent components.
- **When unclear, sequential.** Safe over fast.

### Cross-session communication
- Any session that needs to change `CONTRACTS.md` MUST post a flag to PLAN.md `Open Questions` section and wait for the owner to resolve before proceeding.
- A session completes by updating `PLAN.json` and writing its post-mortem to `PLAN.md`.

### Subagent usage within a session
- Read-only exploration: parallel.
- File edits: only one agent edits any given file at a time.
- Schema changes: go through `CONTRACTS.md` review.
- The lead agent of each session owns merge decisions.

---

## Open questions

1. **Spamhaus block on probe IP (47.34.180.80):** The Phase 1 SMTP probes are systematically getting 550 responses from Outlook-hosted MX servers due to a Spamhaus block on the probe IP. This is an infrastructure issue, not a code issue. Action: investigate whether the block is temporary (request removal from https://www.spamhaus.org/query/ip/47.34.180.80) or persistent. If persistent, consider:
   - Enable `rotate_sender_identity: true` in `lf_config.json` and add more domains to the `sender_domains` pool to spread load across IPs.
   - Or use a different probe IP (e.g., from a different cloud provider or residential proxy).
   This affects Session 4 (integration) and any real-world validation by Session 2 (plugin).
2. **In-browser extension smoke test:** Session 3's E2E test exercised the server-side pipeline with real JSON payloads because the test environment is headless. The actual Chrome extension has not been loaded in a browser. The user (Anthony) should do a real-world smoke test: install the extension at `static/chrome-extension/`, browse 2-3 real Naval/Aerospace profiles, push them, confirm the data lands correctly in the admin view.
3. **Retention policy for `pending_pushes`:** The table accumulates forever. For the expected volume (a few pushes per day) this is fine for years, but a future `DELETE FROM pending_pushes WHERE created_at < ?` cron is recommended.

---

## Session spawn prompts

Ready-to-use prompts for spawning the three work sessions are in `docs/session-prompts/`:

- `SESSION_1_SMTP.md` — Phase 1: SMTP validation hardening. Spawn when ready, runs in parallel with Session 2.
- `SESSION_2_PLUGIN.md` — Phase 2 + 3: LinkedIn plugin v1 + polish. Spawn when ready, runs in parallel with Session 1.
- `SESSION_3_INTEGRATION.md` — Phase 4: integration and end-to-end. **DO NOT spawn until both Session 1 and Session 2 are complete.** Read their post-mortems (`PHASE_1_REPORT.md` and `PHASE_2_3_REPORT.md`) first.

Each session prompt is self-contained: it tells the agent which docs to read, what tasks to pick up, what to NOT touch, and what to print when done.

---

## System Status (added Session 3 / t4.4 — 2026-07-24)

**Status: READY FOR PRODUCTION with documented risks.**

The lead-finder v2 system is now an integrated whole. Two ingestion pathways (Lead Finder search and LinkedIn plugin) write to a shared database and feed a shared 2-probe SMTP validation pipeline. The export pipeline correctly distinguishes validated from unvalidated contacts.

### What was verified end-to-end

- **t4.1 — 20 real profiles pushed through `/api/inject/linkedin-profile`.** All 20 passed: 10 `update_existing` (existing companies), 3 `new_contact_existing_company`, 3 `new_company_new_contact`, 2 auto-matched by `linkedin_slug`, 2 `discard`. See `docs/SESSION_3_E2E_RESULTS.md`.
- **t4.2 — SMTP validation lifecycle.** Plugin-injected contacts with no email get `smtp_validation_status = NULL` (correct). Plugin-injected contacts with an email get `smtp_validation_status = NULL` and are then picked up by the Session 1 per-contact validator (`/api/contact/{id}/validate-email`), which writes the full 11-field validation record. Same for existing contacts updated with a new email. All 3 lifecycle cases passed. See `docs/SESSION_3_T42_RESULTS.md`.
- **t4.3 — Export filter fix.** The session-scoped export endpoint `/api/export/contacts/{session_key}` was returning unvalidated contacts in violation of `CONTRACTS.md §1`. Now fixed: default behavior is to include only `smtp_validation_status = "Okay to Send" AND email_ready_for_export = 1`. Opt-out via `?include_unvalidated=1` for debugging. See `docs/SESSION_3_T43_RESULTS.md`.

### What was built (all sessions)

| Component | Files | Status |
|---|---|---|
| 2-probe SMTP validation | `lf_email_validator.py`, `lf_db.py` | Production (with Spamhaus block as the only known issue) |
| Manual override | `lf_server.py:1078` (`/api/contact/{id}/manual-validate`) | Production |
| LinkedIn Chrome extension | `static/chrome-extension/` (8 files) | Production (in-browser smoke test pending) |
| Plugin ingest pipeline | `lf_server.py:3155` (`/api/inject/linkedin-profile`), `lf_matcher.py`, `lf_name_match.py` | Production (verified with 20-profile E2E test) |
| Audit log | `pending_pushes` table, `static/audit.html` | Production |
| `is_manually_edited` protection | `lf_server.py:3299` | Production |
| Filtered export | `/api/email/ready-for-export`, `/api/export/contacts/{key}` | Production (t4.3 fix applied) |

### What to read first

1. `docs/CONTRACTS.md` — source of truth for what every piece does.
2. `docs/FINAL_REPORT.md` — the integrated system, both pathways, the validation state machine, end-to-end usage.
3. `docs/PHASE_0_REPORT.md` — what was wrong with the SMTP pipeline before Session 1.
4. `docs/PHASE_1_REPORT.md` — the 2-probe design and what's still risky.
5. `docs/PHASE_2_3_REPORT.md` — the plugin, how to install it, what's still risky.
