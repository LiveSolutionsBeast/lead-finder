# Lead Finder — LinkedIn Plugin Context & Prompt

## 1. What this project is

Lead Finder is a lead-generation/data-enrichment system built around a Python FastAPI server (`lf_server.py`) and a Chrome extension (`static/chrome-extension/`). The extension runs on `https://www.linkedin.com/in/*` profile pages, extracts profile and experience data from the live DOM, and POSTs it to the server's `/api/inject/linkedin-profile` endpoint. The server stores/updates contacts, companies, and a full `contact_experience` audit trail in SQLite.

This document captures the **current, scoped state of the LinkedIn experience parser** so future sessions can resume without re-explaining the whole system.

## 2. What the Chrome extension is supposed to do

- Run **only** on LinkedIn `/in/*` profile pages.
- Extract from the **live DOM** (user is logged in with Premium; no reliance on meta tags).
- **Always show a confirmation popup** before pushing data — no silent path.
- Extract exactly **one** experience entry: the **latest / current role**.
- Build a payload with: `linkedin_url`, `linkedin_slug`, `full_name`, `first_name`, `last_name`, `current_title`, `current_company`, `location`, `email`, `phone`, `experience` (array of one entry).
- The `experience` array entry has shape:
  - `company`
  - `title`
  - `started_at`
  - `ended_at`
  - `is_current`
  - `source: "plugin"`
  - `linkedin_slug`
- POST the payload to the configured Lead Finder API (Tailscale hostname by default).

## 3. Scoped functions / files

The only extension file that changes during parser work is:

- `static/chrome-extension/content.js` — content-script extraction logic.

Supporting files (do **not** change without explicit reason):

- `static/chrome-extension/popup.js` — renders extracted data, sends payload.
- `static/chrome-extension/background.js` — proxies API calls.
- `static/chrome-extension/manifest.json` — permissions and run-at settings.
- `static/chrome-extension/CHANGELOG.md` — append-only revision log.

Server side involved but normally untouched during extension parser work:

- `lf_server.py` — `/api/inject/linkedin-profile`, `/api/inject/linkedin-profile/match`.
- `lf_db.py` — `contact_experience`, `replace_contact_experience`, `insert_contact_experience`.

## 4. Current extraction algorithm (as of rev-20260724-220106-followup)

1. Find the Experience section heading (`<h2>`) matching `Experience`, `Experience & …`, or `Experience, …`.
2. Inside that section, collect every **atomic dated element** (`<li>`, `<div>`, `<article>`).
   - Must contain a date matching `Month YYYY` or `YYYY`.
   - Text length must be ≤ 1200 chars.
   - Must not contain a child/descendant that also contains a date.
   - Must not contain > 2 date tokens (rejects outer wrappers aggregating multiple roles).
3. Parse the date range from each candidate.
4. Prefer roles ending in **Present**; among those, pick the **latest start date** (DOM order tie-break).
5. Extract company from the nearest `/company/` link inside the atomic element (or its host `<li>`).
6. `parseExperienceItem(text, knownCompany)` splits the text:
   - Uses `knownCompany` when available; strips it from title if concatenated.
   - Otherwise falls back to camelCase boundary split or capitalized company-name regex.
   - Strips employment type (`Full-time`, etc.) from the pre-date title.
    - Strips duration from post-date text. **Duration regex intentionally has no `/i` flag** to avoid `yrS` matching and eating the first letter of locations like `Seattle`.
    - Extracts location from post-duration text. Location comes **first**, immediately after the duration, before any work-mode bullet (`· Hybrid/On-site/Remote`) or description sentence.
 7. Return a single-entry `experience` array for the most recent role.
 8. **Location does NOT fall back to the top card or any other section.** If the role text has no location, the field is left blank.

## 5. Known DOM shape classes

| Profile | URL | Shape class | Expected output |
|---|---|---|---|
| Nicole LaMotte | https://www.linkedin.com/in/nicole-lamotte-63a21555/ | Flat `<li>` per promotion at same company | Role: `Manager, Technical Recruiting`, Company: `SpaceX`, Location: `Hawthorne, California, United States`, Started: `Jan 2026`, Current: true |
| Steven Maa | https://www.linkedin.com/in/steven-maa/ | Current role in nested `<div>`s inside a company-group `<li>` | Role: `Mission Manager, Rideshare Program`, Company: `SpaceX`, Location: `Hawthorne, California, United States`, Started: `Jan 2026`, Current: true |
| Jay Malave | https://www.linkedin.com/in/jay-malave-69a33748/ | 3rd-degree, flat `<div>` blocks, no `<ul>/<li>` | Role: `Chief Financial Officer`, Company: `Boeing`, Location: `Seattle, Washington, United States`, Started: `Aug 2025`, Current: true |

## 6. Profile sample data (for offline regression testing)

Use `/tmp/opencode/test-latest-role.js` or a copy in `scripts/` for regression. The three fixtures cover all known shapes.

## 7. Rules for future parser edits

- **Scope:** Only `content.js` unless the user explicitly asks otherwise.
- **Always add a CHANGELOG entry** under Open Revisions before editing; move to Closed Revisions once verified.
- **Do not send full history.** The popup and server expect a single current-role entry in `experience`.
- **Keep one-size-fits-all:** do not add per-profile class-name selectors; use structural signals (date, atomic element, `/company/` link).
- **Duration regex must not use `/i`.**
- **When a `/company/` link is present, trust it** and only split the title to remove the concatenated company name.
- **Test all three sample profiles** before declaring a fix done.

## 8. User workflow

1. User opens a LinkedIn profile.
2. Clicks the extension icon → popup extracts and shows data.
3. User reviews/edits fields and clicks Send.
4. Extension POSTs to `/api/inject/linkedin-profile`.
5. User runs `& "$env:USERPROFILE\sync-extension.ps1"` to pull the new zip from the server via Tailscale.

## 9. Current status

Parser refactor to unified atomic-dated-element selector is implemented and passes offline tests for Nicole, Steven, and Jay. Awaiting live-profile verification.
