# Session 2 Spawn Prompt — LinkedIn Plugin v1

You are Session 2 of a coordinated 3-session build for the lead-finder system. Your job is to build a Chrome extension that lets the user (Anthony, with a premium LinkedIn membership) push real LinkedIn profile data into the lead-finder database. Do not start Session 1's work (SMTP hardening) or Session 3's work (integration). Stay in your lane.

## CRITICAL: Read these three files before doing anything else

1. `/home/anthonyturgman/lead-finder/docs/PLAN.json` — the machine-readable plan. Look at `phase_2_plugin_v1` and `phase_3_plugin_polish`. Those are your tasks.
2. `/home/anthonyturgman/lead-finder/docs/PLAN.md` — the human-readable plan with rationale.
3. `/home/anthonyturgman/lead-finder/docs/CONTRACTS.md` — the shared schema and endpoint contracts. The plugin must conform to this.

The Phase 0 SMTP report and Phase 1 SMTP report (when Session 1 finishes) may inform you about validation state behavior. Read them if they exist.

## The system you are extending

The lead-finder has a JSON HTTP API at `http://localhost:<port>` (find the port in `/home/anthonyturgman/lead-finder/lf_server.py` or check the running service). The API key is stored in `.env` as `LF_API_KEY` and loaded by `lf_config.py`; the SPA fetches it from `GET /api/config` at runtime. You'll be hitting existing endpoints AND adding new ones.

The user has a premium LinkedIn membership (until at least September 2026, possibly longer). They will be browsing real LinkedIn profiles manually and clicking the extension icon to push data. **You are not building a scraper. You are building a tool the user drives.**

## The user explicitly does not want these things

- **No headline extraction.** The user has explicitly said LinkedIn headlines are unreliable (people use them as personal descriptors, not role descriptors). Read the Experience section instead.
- **No silent updates.** Per the user's requirement, the confirmation popup ALWAYS shows. The user must click "Send" every time. There is no "auto-update same person" mode in v1.
- **No automated scraping.** The user manually browses and manually clicks. No background tab scraping, no auto-discovery, no scheduled tasks. The user is the rate limiter.

## What you own (Phase 2 and Phase 3 tasks from PLAN.json)

### Task t2.1 — Chrome extension manifest.json

Create `/home/anthonyturgman/lead-finder/static/chrome-extension/manifest.json`. Manifest v3. Required permissions:
- `activeTab` — access to the current tab when the user clicks the extension icon
- `storage` — to remember the API endpoint URL and key
- `host_permissions: ["https://www.linkedin.com/in/*"]` — content script runs only on profile pages

Three components:
- `content_scripts` — runs on `/in/*` pages, reads Experience section
- `action` (popup) — opens when user clicks the extension icon
- `background` (service worker) — handles API calls (keeps API key out of content script)

### Task t2.2 — Content script: extract Experience section

Create `content.js`. On page load (and on DOM changes that may indicate Experience section loaded), find the Experience section. **The user explicitly said the Experience section, NOT the headline.** The selector strategy:

- LinkedIn's DOM is highly variable. Don't rely on specific class names.
- Look for the section that contains the heading "Experience" (case-insensitive, partial match).
- Within that section, find role entries. Each role has: company name, title, date range, sometimes location.
- The CURRENT role is the first entry (most recent) in the Experience list.
- Also extract: name (from the top of the profile), location (from the intro section).

Return this structured data via `chrome.runtime.sendMessage` to the popup:

```javascript
{
  linkedin_url: window.location.href,
  linkedin_slug: extractSlug(window.location.href),  // the /in/<slug> portion
  full_name: "...",
  first_name: "...",
  last_name: "...",
  current_title: "...",
  current_company: "...",
  location: "...",
  experience: [
    { company: "...", title: "...", started_at: "...", ended_at: "...", is_current: true },
    ...
  ]
}
```

LinkedIn uses dynamic class names and may re-render. Implement a `MutationObserver` to re-extract when the DOM changes. Debounce re-extraction to 500ms to avoid hammering the DOM.

### Task t2.3 — Popup: editable form + 4-case match + Send

Create `popup.html`, `popup.css`, `popup.js`. When user clicks the extension icon, the popup shows the extracted data. Every field is EDITABLE. Below the form, show 4 radio buttons:

- ⃝ Update existing contact (only shown if the matcher found a match — see t2.7)
- ⃝ Create new contact at existing company (only shown if the matcher found a company match)
- ⃝ Create new company + new contact (always available)
- ⃝ Discard (always available, just closes the popup)

A "Send to Lead Finder" button. Disabled until the user picks a match action. When clicked, POST to `/api/inject/linkedin-profile` with the data and the chosen `match_action`.

Per user requirement: the popup ALWAYS shows. There is no silent-update path. If the user has seen this contact before, the popup still shows with "Update existing" pre-selected. The user can still see what's about to be written and click Send.

### Task t2.4 — POST /api/inject/linkedin-profile endpoint

Add to `/home/anthonyturgman/lead-finder/lf_server.py`. The request/response shape is in CONTRACTS.md section 4. Implementation:

1. Validate the API key from the `X-LF-Key` header.
2. Parse the payload.
3. Run the matching engine (t2.7) to determine the match action IF the popup didn't specify one.
4. Write to `pending_pushes` table first (audit).
5. Apply the action:
   - `update_existing` → UPDATE contacts WHERE id = matched_contact_id
   - `new_contact_existing_company` → INSERT INTO contacts
   - `new_company_new_contact` → INSERT INTO companies + INSERT INTO contacts
6. If the contact has an email (rare for plugin-injected), set `smtp_validation_status = NULL` (not_validated). Session 1's pipeline picks it up.
7. Insert the experience records into `contact_experience` table.
8. Mark the `pending_pushes` row as committed.
9. Return `{ok: true, contact_id, company_id, matched_action, smtp_validation_status}`.

All hard deletes, no soft deletes. (User explicitly disabled soft deletes in a prior session.)

### Task t2.5 — Schema migration

Add to the schema migration script (find it in `lf_db.py` or wherever migrations live):

```sql
ALTER TABLE contacts ADD COLUMN linkedin_slug TEXT UNIQUE;
```

The `linkedin_slug` is the `/in/<slug>` portion of the URL. It is the immutable primary disambiguation key per CONTRACTS.md section 5.

**Note:** Session 1 is also adding columns in parallel. Before adding this column, check that Session 1 hasn't already added it. If it has, skip. If it hasn't, add it. The CONTRACTS.md migration should be done by whoever gets to it first.

Also create the two new tables from CONTRACTS.md section 3:

```sql
CREATE TABLE IF NOT EXISTS contact_experience (
  id INTEGER PRIMARY KEY,
  contact_id INTEGER NOT NULL,
  company_name TEXT,
  title TEXT,
  started_at TEXT,
  ended_at TEXT,
  is_current INTEGER DEFAULT 0,
  linkedin_slug TEXT,
  source TEXT,
  captured_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_contact_experience_contact ON contact_experience(contact_id);

CREATE TABLE IF NOT EXISTS pending_pushes (
  id INTEGER PRIMARY KEY,
  linkedin_slug TEXT NOT NULL,
  raw_payload TEXT NOT NULL,
  matched_action TEXT,
  matched_contact_id INTEGER,
  matched_company_id INTEGER,
  committed_at TEXT,
  committed_by TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_pushes_slug ON pending_pushes(linkedin_slug);
```

### Task t2.6 — Name normalization + fuzzy company match

Create `lf_name_match.py` (or add to an existing module). Two helpers:

```python
def normalize_name(name: str) -> str:
    """Lowercase, strip credentials, remove punctuation, collapse whitespace."""
    # Implementation per CONTRACTS.md section 7

def fuzzy_company_match(company_name: str, candidates: list[dict], max_distance: int = 3) -> list[dict]:
    """Return candidates with Levenshtein distance <= max_distance from company_name, sorted by distance."""
    # Use rapidfuzz or write a simple Levenshtein
```

### Task t2.7 — 4-case matching engine

The matching engine runs server-side when the plugin POSTs a profile. Per CONTRACTS.md section 5, priority order:

1. `linkedin_slug` match — SELECT FROM contacts WHERE linkedin_slug = ?
2. Normalized name + same company
3. Normalized name + fuzzy company match (Levenshtein <= 3)
4. Email match (only if existing has validated email)

Return a list of candidate matches with their match strength. The popup shows them as radio buttons. The user picks one (or "create new").

If multiple candidates match at the same strength, return all of them and the popup shows them as a dropdown.

If the plugin already specified `match_action` explicitly (the user picked in the popup), the engine respects that and doesn't override.

### Task t2.8 — Always-show confirmation popup

Already covered by t2.3. Just confirm there's no silent path. Review the code to ensure every push goes through the popup, no background auto-updates.

### Task t2.9 — Document deployment in PLAN.md

Add a section to `/home/anthonyturgman/lead-finder/docs/PLAN.md` titled "Chrome Extension Deployment" with:

- Where the extension files live (`/home/anthonyturgman/lead-finder/static/chrome-extension/`)
- How to install (developer mode, load unpacked, select the folder)
- Which Chrome profile to install on (the one logged into the user's premium LinkedIn)
- How to update after code changes (refresh the extension on `chrome://extensions/`)
- Known limitations (no headline extraction, no silent updates, etc.)

### Phase 3 tasks (t3.1, t3.2, t3.3, t3.4)

- **t3.1 — Highlight-to-extract mode.** In `content.js`, add a listener for `mouseup` events. If the user has selected text on the page, show a small floating button "Extract to Lead Finder". On click, capture the selected text + the closest field-name context (e.g., the nearest `<dt>` or label).
- **t3.2 — Audit log.** Every `pending_pushes` row is the audit log. Add a queryable endpoint `GET /api/audit/plugin-pushes?from=...&to=...` that returns recent pushes.
- **t3.3 — Audit log UI.** Add a section to `/home/anthonyturgman/lead-finder/static/audit.html` showing recent plugin pushes with: timestamp, user (initially just "plugin"), linkedin_slug, matched action, contact_id, company_id. Read-only view.
- **t3.4 — `is_manually_edited` protection.** The column already exists in `contacts`. The plugin must check it: if `is_manually_edited = 1` and the user picks "update_existing", the popup shows a warning and requires explicit confirmation before the send proceeds.

## Sequential / parallel work rules

- **Read-only work**: parallelize.
- **File edits**: only one agent edits any given file at a time.
- **Schema changes**: coordinate with Session 1 via CONTRACTS.md. If you need a column that Session 1 might add, check first.
- **Test runs**: parallel for independent components.

When unclear, sequential. Don't race.

## Don't touch

- `lf_email_validator.py` (Session 1's domain)
- The SMTP validation pipeline
- The `email-validator-fork/` directory
- `lf_import.py`'s core import logic (it has its own gap_fill that may overlap with the plugin's, but that's a Phase 4 integration concern)

## When you are done

Update `/home/anthonyturgman/lead-finder/docs/PLAN.json` to mark Phase 2 and Phase 3 tasks as completed. Then write a post-mortem to `/home/anthonyturgman/lead-finder/docs/PHASE_2_3_REPORT.md` covering:

1. **What you built.** Brief, with screenshots or file listings.
2. **What works end-to-end.** Did you actually load the extension in Chrome and push a real test profile? Did the data land in the contacts table? Did the experience history land in `contact_experience`? Did `pending_pushes` record it?
3. **What still needs work.** Anything you deferred.
4. **How the user installs and uses it.** Step-by-step from a clean Chrome.
5. **Recommended next step.** Whether Session 3 (integration) can proceed.

When you finish the post-mortem, print to the user:

> "Session 2 complete. Phase 2 + Phase 3 [PASSED/PARTIAL/FAILED]. See `docs/PHASE_2_3_REPORT.md`. Ready for Session 3 (integration testing) when Session 1 is also done."

The user will then decide whether to start Session 3.

## Test data the user has approved

The user said: "we can pick a company with a lot of potential contacts — like big Naval or Aerospace companies in the area. We can start with existing companies, target them and move on it. I have a bunch of contacts we can start with, but I'd rather start with the data we already have."

So your test set is the existing Naval/Aerospace companies in the lead-finder session `import-naval-contractors-cmmc-06ddcd`. Pick 5-10 of them, browse their executives' LinkedIn profiles (or use any test profile you have access to), and verify the plugin extracts and pushes correctly. You don't need to push 20 profiles — 5 is enough to prove it works.

## Tools you can use

- `bash` for running tests, scripts, and the lead-finder server
- File reading and editing tools
- `task` to spawn sub-agents for parallel read-only exploration
- `webfetch` for documentation lookups (Chrome extension docs, LinkedIn DOM references)

Begin by reading the three docs files, then verify the lead-finder server is running (and what port), then start with task t2.1 (manifest.json).
