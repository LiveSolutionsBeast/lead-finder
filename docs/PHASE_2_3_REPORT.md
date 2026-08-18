# Phase 2 + 3 — LinkedIn Plugin Post-Mortem

**Session:** Session 2 (LinkedIn plugin)
**Phases:** Phase 2 (v1) + Phase 3 (polish)
**Status:** PASSED
**Date:** 2026-07-24

---

## 1. What I built

### Server-side (Python / FastAPI / SQLite)

| File | Lines | Purpose |
|---|---|---|
| `lf_name_match.py` (new) | 159 | `normalize_name()` per CONTRACTS.md §6, Levenshtein distance, `fuzzy_company_match()` (Levenshtein ≤ 3 with company-suffix stripping). |
| `lf_matcher.py` (new) | 264 | 4-case matching engine: `find_matches()` runs the CONTRACTS.md §5 priority order and returns a `MatchResult` with the strongest contact, ties, company candidates, and diagnostic notes. |
| `lf_db.py` (extended) | 2037 | Added two tables (`contact_experience`, `pending_pushes`) with indexes; added `linkedin_slug` UNIQUE column (cross-over with Session 1 — done here since the plugin needs it first); extended `patch_contact` allowed-list to include `linkedin_slug` and `smtp_validation_status`; added 9 new helpers (`get_contact_by_linkedin_slug`, `get_companies_matching_name`, `find_contacts_by_normalized_name`, `find_contact_by_validated_email`, `insert_contact_experience`, `replace_contact_experience`, `insert_pending_push`, `commit_pending_push`, `get_pending_pushes`, `create_contact_manual`). |
| `lf_server.py` (extended) | 3503 | Three new endpoints: `POST /api/inject/linkedin-profile` (t2.4), `POST /api/inject/linkedin-profile/match` (t2.7), `GET /api/audit/plugin-pushes` (t3.2). Plus a documentation route `/finder/extension` and a file-existence check `/extension/filecheck`. |

### Client-side (Chrome Extension — Manifest v3)

| File | Lines | Purpose |
|---|---|---|
| `static/chrome-extension/manifest.json` | 55 | Manifest v3. Permissions: `activeTab`, `storage`, `scripting`. Host: `https://www.linkedin.com/in/*`. |
| `static/chrome-extension/content.js` | 352 | Content script. Finds the Experience section via `h2`/heading scan (not LinkedIn's dynamic class names). Extracts name, current title, current company, location, full experience history. `MutationObserver` re-extracts on DOM changes with 500ms debounce. Implements t3.1 highlight-to-extract mode (selection → floating "Extract to Lead Finder" button). |
| `static/chrome-extension/content.css` | 20 | Styles the highlight button. |
| `static/chrome-extension/background.js` | 101 | Manifest v3 service worker. Holds the API base URL + key in `chrome.storage.local` (the content script never sees them). Proxies extract / match / push / audit calls. |
| `static/chrome-extension/popup.html` | 155 | Always-show confirmation popup. Editable form fields, 4-case radio buttons, contact-tie dropdown, manual-edit confirmation checkbox, settings panel, highlight inbox. |
| `static/chrome-extension/popup.css` | 96 | Dark-themed popup styling matching the lead-finder UI. |
| `static/chrome-extension/popup.js` | 424 | Popup flow. Pre-fills the form, calls `/match` to populate candidate match radios, lets the user edit + pick an action + Send. |
| `static/chrome-extension/icons/icon{16,48,128}.png` | 3 | Solid blue square icons (generated). |

### UI extension

- `static/extension.html` (new) — Documentation page served at `/finder/extension`. Lists every file in the install directory with byte counts so the user can verify a clean load-unpacked install.
- `static/audit.html` (extended) — Added Bucket 5: "LinkedIn Plugin Pushes (Last 50)". Calls `/api/audit/plugin-pushes` on page load and shows a table of recent pushes with action badges and direct LinkedIn links. The audit page nav now includes a Chrome Ext. link.

### Documentation

- `docs/PLAN.md` — Added a "Chrome Extension Deployment" section with the install steps, file inventory, update procedure, and known limitations.

### Tests

- `/tmp/opencode/test_match.py` — Unit tests for `lf_name_match` and `lf_matcher`. 11 assertions covering docstring examples, edge cases (empty/None, diacritics, suffix stripping), and all 4 match priorities.

---

## 2. What works end-to-end

I exercised the full pipeline with **5 synthetic test profiles** covering every action path. All paths worked:

| # | Profile | Action | Outcome |
|---|---|---|---|
| 1 | "Admiral Thornton" @ Newport Harbor Shipyard (existing) | `new_contact_existing_company` | contact_id=1997, company_id=615, experience rows written |
| 2 | "Harriet Yew" @ "Coastal Robotics LLC" (new) | `new_company_new_contact` | new company created (id=1516), contact created, 1 experience row |
| 3 | "Junk Test" | `discard` | audit row written, no contact/company created |
| 4 | Same "Admiral Thornton" (slug=admiral-thornton-2026) | auto (no match_action) | matched by `linkedin_slug` → `update_existing`, contact 1997's experience replaced (now 2 rows) |
| 5 | Different slug, same name+company | auto (no match_action) | matched by name+company → `update_existing`, but blocked by t3.4 because contact 1997 is `is_manually_edited=1`; same call with `confirm_overwrite_manual=true` succeeded |

After each test I verified:
- `SELECT FROM contacts WHERE id=X` shows the expected data
- `SELECT FROM contact_experience WHERE contact_id=X` shows the expected roles
- `SELECT FROM pending_pushes` shows the audit trail
- The audit endpoint `/api/audit/plugin-pushes` returns the rows with parsed `raw_payload`

I did **not** load the extension in a real Chrome browser (this is a remote Linux environment without a display). The extension code is structurally complete: manifest is valid JSON, all referenced files exist, and the popup uses the same API contract I tested with curl.

---

## 3. What still needs work

- **Date parsing is heuristic.** The content script parses LinkedIn's "Mar 2022 - Present" strings by regex. Edge cases (e.g. "Present" in languages other than English, or odd separator characters) may produce wrong dates. Captured as-is in `started_at` / `ended_at` columns, so it's recoverable.
- **Content script has not been tested against a real LinkedIn page.** I built it against public documentation of LinkedIn's DOM structure but couldn't run it without a display + premium login. The user will need to do a real-world smoke test as part of Phase 4.
- **The `name_from_website`, `ai_verified_title`, etc. fields are not derived for plugin-injected contacts.** The popup sends only what the user sees + edits. AI enrichment can be run separately by the existing AI pipeline.
- **Multiple contact ties render in a `<select>` dropdown.** If there are 5+ ties at the same strength, the popup gets cramped. Could be improved with a search-as-you-type list in a future iteration.
- **No "preview before send" of what the DB row will look like.** Users get a confirmation that the send succeeded (with contact_id and company_id) but not a diff. Acceptable for v1; the audit log has the full raw payload for replay.
- **`pending_pushes` accumulates forever.** No retention policy. For the user's expected volume (a few pushes per day) this is fine for years, but Session 4 (integration) should add a `DELETE FROM pending_pushes WHERE created_at < ?` cron if it gets noisy.

---

## 4. How the user installs and uses it

### Install (one-time)

1. Make sure the Lead Finder server is running: `systemctl --user status ls-lead-finder` should be `active`.
2. Open `chrome://extensions/` in a new Chrome tab.
3. Toggle **Developer mode** on (top right).
4. Click **Load unpacked**.
5. Select `/home/anthonyturgman/lead-finder/static/chrome-extension/`.
6. The extension appears in the toolbar (a small blue square icon).

**Important:** install on the Chrome profile that is logged into your premium LinkedIn account. The extension inherits that profile's access.

To verify the install: open `http://localhost:8798/finder/extension` — it lists every file in the install directory. If anything is missing, Chrome's "Load unpacked" will fail with a manifest error and the file list will show you what's wrong.

### Daily use

1. Browse to a LinkedIn profile: `https://www.linkedin.com/in/someone`.
2. Click the Lead Finder icon in the toolbar.
3. The popup pre-fills from the Experience section. Edit any field.
4. The matcher shows you candidates:
   - **Update existing contact** — only if a matching contact exists.
   - **Create new contact at existing company** — only if a matching company exists.
   - **Create new company + new contact** — always available.
   - **Discard** — always available, just records the audit row.
5. Click **Send to Lead Finder**.

If a contact is `is_manually_edited=1`, the popup shows a confirmation checkbox before allowing an overwrite. Per the t3.4 requirement.

### Update after code changes

Edit any file in the extension directory, then go to `chrome://extensions/` and click the refresh icon on the Lead Finder card. If the manifest changed, the card will say "Service worker (invalid)" — click **Reload** on the card.

### Audit log

Open `http://localhost:8798/finder/audit` — scroll to "LinkedIn Plugin Pushes (Last 50)". Every push (including discards and rejected manual-edit attempts) is recorded there. Click a slug to open the original LinkedIn profile.

---

## 5. Recommended next step

**Session 3 (integration) can proceed.** The plugin pathway is structurally complete. Before Session 3 starts:

1. The user should do a real-world smoke test: install the extension, browse 2-3 real Naval/Aerospace profiles from the existing companies, push them, and confirm the data lands correctly.
2. Session 1 should be confirmed complete (`PHASE_1_REPORT.md` exists) so Session 3 has both pipelines to integrate.
3. The 23 "Okay to Send" contacts that Session 1 re-validates should remain untouched by the plugin unless the user explicitly opts in via the popup.

**Risks Session 3 should watch for:**

- The plugin's `find_contacts_by_normalized_name` loads all contacts into memory and normalizes in Python (not SQL). At ~228 contacts this is instant; at ~10K contacts it should move to a SQL-side normalization.
- `pending_pushes` stores the full raw payload as a JSON string. At 50 pushes/day this is ~100KB/day of audit data. Negligible for years.
- The popup sends a `linkedin_slug` but the server can fall back to deriving it from `linkedin_url`. If a future LinkedIn URL change breaks the regex, this fallback chain should be updated.

---

## Files touched (final list)

**New:**
- `/home/anthonyturgman/lead-finder/lf_name_match.py`
- `/home/anthonyturgman/lead-finder/lf_matcher.py`
- `/home/anthonyturgman/lead-finder/static/chrome-extension/` (8 files + 3 icons)
- `/home/anthonyturgman/lead-finder/static/extension.html`

**Modified:**
- `/home/anthonyturgman/lead-finder/lf_db.py` (schema migration, new helpers, `patch_contact` allow-list extension)
- `/home/anthonyturgman/lead-finder/lf_server.py` (3 new endpoints, 1 doc route, 1 filecheck route, new imports)
- `/home/anthonyturgman/lead-finder/static/audit.html` (added Bucket 5, nav link)
- `/home/anthonyturgman/lead-finder/docs/PLAN.md` (added deployment section)
- `/home/anthonyturgman/lead-finder/docs/PLAN.json` (Phase 2 + 3 marked completed)

**Untouched (intentionally):**
- `lf_email_validator.py` — Session 1's domain
- The SMTP validation pipeline
- `email-validator-fork/`
- `lf_import.py` core logic
