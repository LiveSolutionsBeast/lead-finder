# Chrome Extension — Revision Log

**Purpose:** Track every change to the Lead Finder Chrome extension. **Read this before editing any file in `static/chrome-extension/`.** The previous approach (edit → test → repeat) caused multiple regressions because we had no record of what was working.

**How to use this file:**
1. Before any change, add a new entry under "Open Revisions" with what you plan to do and why
2. After the change works, move the entry to "Closed Revisions" with the actual result
3. If a change regresses something, write a "Regression" entry that points back to the change that caused it
4. Never delete old entries. This is an append-only log.

---

## Open Revisions

### rev-20260725-230000-group-location (grouped-role group-level location fallback)
- **What changed:** Update `parseGroupedEntry` in `static/chrome-extension/content.js` to also scan the company group text link for a location string when the individual role `<li>` does not contain one.
  - For some grouped/multi-role profiles (e.g., Darragh), LinkedIn places the work location in the company group header alongside the company name and total tenure, not inside each role `<li>`.
  - After extracting `company` from the group text link, the parser now also runs `leafTextElements(textLink)` and checks each leaf with `looksLikeLocation` and `stripWorkMode`.
  - The role-level location still takes precedence if present (Nicole/Jane behavior unchanged). If the role `<li>` has no location, the group-level location is used as a fallback.
- **Why:** Layer 1 of the two-layer location fix. Without this, the raw location string `"Los Angeles Metropolitan Area"` is never sent to the server, so no server-side resolver can act on it.
- **Files touched:** `static/chrome-extension/content.js`, `CHANGELOG.md`.
- **BUILD_ID:** `parser-20260725-2300`.
- **Safety / isolation:** This only affects grouped-role entries. `parseFlatEntry` is untouched. The role-level location is preferred over the group-level location, so Nicole/Jane remain unchanged.
- **Expected impact:**
  - Nicole LaMotte: unchanged — `Hawthorne, California, United States` from role `<li>`.
  - Jane M.: unchanged — `El Segundo, California, United States` from role `<li>`.
  - Darragh: `location` should now be `"Los Angeles Metropolitan Area"` (group header fallback) instead of `""`.
- **Status:** SHIPPED for live verification.

### rev-20260725-220000-popup-rematch (manual edits re-run matcher)
- **What changed:** Update `popup.js` so that manual edits to **Full name** and **Current company** re-run the server matcher and refresh the match options.
  - `runMatcher()` now reads `full_name` and `current_company` from the live form fields, with optional overrides, while keeping `linkedin_url`, `linkedin_slug`, and `email` from the original extraction.
  - Added a 400 ms debounced `refreshMatches()` handler on the `input` events for `full_name` and `current_company`.
  - When the user types, the popup calls `/api/inject/linkedin-profile/match` with the current form values and re-renders the 4-case match options (update existing, new contact at existing company, new company + new contact, discard).
  - `buildPayload()` continues to use the latest `matchResult` so the chosen IDs stay in sync with the displayed options.
- **Why:** If LinkedIn's initial scan identified the wrong company, the user can correct `current_company` to an existing company name and the popup must find that match. The same applies to `full_name`. Previously the matcher only ran once on popup open; manual edits had no effect until the user clicked Send, which could create duplicate contacts or companies.
- **Files touched:** `static/chrome-extension/popup.js`, `CHANGELOG.md`.
- **BUILD_ID:** bumped to `parser-20260725-2200` to mark the new extension build.
- **Syntax check:** `node --check static/chrome-extension/popup.js` passes; `node --check static/chrome-extension/content.js` passes.
- **Status:** SHIPPED for live verification.

### rev-20260725-210100-popup-rematch (manual edits re-run matcher)
- **Superseded by rev-20260725-220000-popup-rematch.** Kept as a placeholder to preserve revision numbering continuity. See the closed entry above for the implemented fix.

### rev-20260724-220106-followup (unified latest-role selector, content.js)
- **What changed:** Replaced the two-strategy `findLatestExperienceItem` with a single, DOM-shape-agnostic selector.
  - Collects all leaf-most dated elements inside the Experience section (`<li>`, `<div>`, `<article>`) that do not contain a dated descendant.
  - Rejects outer wrappers that aggregate multiple roles by counting date tokens.
  - Scores candidates by parsed date range, prefers roles ending in "Present", and picks the latest start date.
  - Passes the discovered `/company/` link text into `parseExperienceItem` as `knownCompany`.
- **What changed in `parseExperienceItem`:**
  - When `knownCompany` is supplied, use the entire pre-date text as the title and strip the known company if LinkedIn concatenated it (e.g. "Mission Manager, Rideshare ProgramSpaceX").
  - Removed `/i` flag from the duration-stripping regex; the `/i` flag caused "yrS" to match and eat the first letter of location names that start with "S" (e.g. "Seattle" → "eattle").
  - Location is now taken **only** from the role text, immediately after the date/duration. Removed the top-card `extractLocation()` fallback because the top card is unreliable and other sections (Recommendations, Education) were leaking into the location field.
- **Why:** The previous two-strategy approach (prefer `<ul>/<li>`, fallback to smallest Present `<div>`) failed on nested company-group structures and could return a wrapper containing multiple roles. This makes the latest-role identification one-size-fits-all across flat `<li>`, nested `<div>`, and div-only 3rd-degree profiles. The location fix prevents garbage from Recommendations/Education from being picked up.
- **Test cases:**
  - **Nicole LaMotte** — flat `<li>` per promotion at SpaceX. Expected: Role=`Manager, Technical Recruiting`, Company=`SpaceX`, Location=`Hawthorne, California, United States`.
  - **Steven Maa** — current role in nested `<div>`s inside a company-group `<li>`. Expected: Role=`Mission Manager, Rideshare Program`, Company=`SpaceX`, Location=`Hawthorne, California, United States`.
  - **Jay Malave** — 3rd-degree, flat `<div>` blocks, no `<ul>/<li>`. Expected: Role=`Chief Financial Officer`, Company=`Boeing`, Location=`Seattle, Washington, United States`.
- **Files touched:** `static/chrome-extension/content.js` only. `manifest.json`, `background.js`, `popup.*` unchanged.
- **Status:** SHIPPED, but live tests showed regressions on all four profiles. A new fix is being attempted; see the regression note below.

### rev-20260724-220106-followup-regression (live results)
- **Symptom:** After shipping rev-20260724-220106-followup, live tests on Nicole, Jane M., Steven, and Jay showed `current_company` populated with unparsed role text and `current_title` including the company name. Locations were blank or missing.
- **Likely cause:** The unified container walk-up picked wrappers whose text was not parseable, and the single-word company fallback in `parseExperienceItem` failed for companies like "Boeing".
- **Fix attempted:** Reverted to a hybrid selector (proven `<ul>/<li>` strategy first, container walk-up only for div-only profiles), added single-word company fallback for split-text profiles, and added an embedded DOM-shape detector plus safe-mode guard.
- **Status:** in progress, awaiting user console output from live profiles.

### rev-20260724-220106-followup-debug (profile structure capture)
- **What changed:** Added a debug snapshot pipeline so we can inspect real LinkedIn DOM shapes without manual console copy/paste.
  - `lf_server.py`: new `POST /api/debug/profile-structure` writes `debug/profile-<slug>-<timestamp>.json`.
  - `background.js`: new `LF_DEBUG_STRUCTURE` message handler keeps the service worker alive until the snapshot POST completes.
  - `content.js`: snapshot is now sent when the popup requests extraction (`LF_EXTRACT` / `LF_RE_EXTRACT`), because the service worker is awake. Also supports a manual `LF_SEND_DEBUG` message.
  - `popup.html` / `popup.js`: added a "Send debug snapshot" button as a manual fallback.
- **Why:** Automatic snapshots on popup open failed because the Manifest v3 service worker was not reliably alive during initial page-load extraction. Moving the trigger to the popup request path and adding a manual button gives us two ways to capture the real DOM.
- **Files touched:** `content.js`, `background.js`, `popup.html`, `popup.js`, `lf_server.py`.
- **Status:** SHIPPED. Snapshots received for Nicole, Steven, Jay, and Jane.

### rev-20260724-220106-followup-rewrite (sibling-based role extraction)
- **What changed:** Rewrote `findLatestExperienceItem` to match the real DOM captured in the snapshots.
  - Discovered that the Experience section has NO `<ul>/<li>` lists; every profile is flat `<div>` roles.
  - New algorithm: find leaf-most date-bearing elements, walk up to the role container (smallest ancestor with a `/company/` link), and extract title/company/location from sibling elements.
  - Company is read from the `/company/` link's `aria-label` or child span text.
  - `extractExperience()` now uses the structured pieces from the selector first, only falling back to `parseExperienceItem` for missing fields.
- **Why:** The previous hybrid approach (Strategy A `<ul>/<li>`, Strategy B container text) failed because it assumed wrong DOM shapes. The real DOM keeps title, company link, dates, and location as siblings inside a role container.
- **Files touched:** `content.js`.
- **Test cases (harness):**
  - Nicole LaMotte: PASS — Manager, Technical Recruiting / SpaceX / Hawthorne, California
  - Steven Maa: PASS — Mission Manager, Rideshare Program / SpaceX / Hawthorne, California
  - Jane M.: PASS — Bus and Payload Design Engineering Manager / Boeing / El Segundo
  - Jay Malave: PASS — Chief Financial Officer / Boeing / Seattle
- **Status:** SHIPPED. Live tests after nuclear reload showed the same concatenation regression; see regression note below.

### rev-20260725-071200-regression (live results after sibling rewrite)
- **Symptom:** After the user deleted old files, removed the extension, re-synced from the server, loaded the unpacked extension fresh, hard-refreshed each profile, and clicked the Lead Finder icon, all four profiles still returned concatenated garbage:
  - Nicole: `current_company` = `"Manager, Technical RecruitingFull-timeJan 2026 - Present · 7 mosHawthorne, California, United States"`, `current_title` = `"Manager, Technical Recruiting"`.
  - Steven: `current_company` = `"Mission Manager, Rideshare ProgramSpaceX · Full-timeJan 2026 - Present · 7 mosHawthorne, California, United States · On-site"`, `current_title` = `"Mission Manager, Rideshare ProgramSpaceX"`.
  - Jane: `current_company` = `"Bus and Payload Design Engineering ManagerOct 2023 - Present · 2 yrs 10 mosEl Segundo, California, United States"`, `current_title` = `"Bus and Payload Design Engineering Manager"`.
  - Jay: `current_company` = `"Chief Financial OfficerBoeingAug 2025 - Present · 1 yrSeattle, Washington, United States"`, `current_title` = `"Chief Financial OfficerBoeing"`.
- **Root cause discovered by inspecting the 07:12 debug snapshots:**
  1. The `/company/` link used as the "company source" is actually a **logo-only anchor**. Its `aria-label` is `"SpaceX logo"` / `"Boeing logo"`, not the company name. The visible company name lives in a **separate text-bearing `/company/` link** that is a sibling of the logo link.
  2. The title, dates, and location are **deeply nested** inside a text-column `<div>`, not direct children of the role container. My sibling filter operated on `container.children`, which never contained the actual title/dates/location text blocks.
  3. Jane's profile uses a **grouped structure**: one company link at the group level, with multiple roles inside a `<ul>/<li>` list. My code assumed no `<ul>/<li>` lists and stopped at the group container instead of the role `<li>`.
  4. Because the company name was never found, `roleText` was built as `[title, "", dateText, location]`, omitting the company entirely. The fallback `parseExperienceItem` then made false splits (e.g. treating "Recruiting" as a company).
- **Impact:** The parser regressed entirely; separation of title, company, dates, and location is broken on all tested profiles.
- **Recovery plan:**
  1. Rewrite `findLatestExperienceItem` to treat the `/company/` link as two possible anchors: a logo link and a text link. Extract the company name only from a text link (or from the visible text element associated with the `/company/` slug).
  2. Walk up from each date element to find the **role item container** (the smallest element that is a distinct role, e.g. an `<li>` or a role `<div>`), then continue up to find the **company source container** for grouped roles.
  3. Build an ordered list of text-bearing field containers inside the role item and identify: title (before dates), dates, location (after dates, location-pattern), company (from text link or process of elimination).
  4. Harden `parseExperienceItem` so it never treats a trailing job-title word as a company.
  5. Update the offline test harness fixtures to match the real DOM shapes (logo link + text link + nested text column; grouped `<ul>/<li>`).
- **Files touched:** `content.js` (parser), `/tmp/opencode/test-latest-role.js` (harness), `CHANGELOG.md`.
- **Fix implemented:**
  1. `findLatestExperienceItem` now separates **role-item** detection from **company-source** detection. It walks up to the smallest distinct role (`<li>` or a div that is a direct child of the LazyColumn / entity-collection-item / Experience section), then walks up again to find the nearest ancestor with a *text-bearing* `/company/` link.
  2. `extractCompanyName` ignores logo-only links (aria-label ends in "logo", or link has no visible text) and reads the company name from the visible text-bearing `/company/` link.
  3. Title/dates/location are read from ordered fields inside the role's field container (the wrapper div that holds the date and multiple field-like children). The title is the field before the date, location is the first field after the date that matches a location pattern.
  4. `parseExperienceItem` now inserts whitespace where LinkedIn concatenates words (`Full-timeJan 2026` → `Full-time Jan 2026`) and no longer splits trailing job-title words into false company names.
  5. Test harness fixtures rebuilt with logo link + text link + nested text column, plus Jane's grouped `<ul>/<li>` structure.
- **Test cases (harness):**
  - Nicole LaMotte: PASS — Manager, Technical Recruiting / SpaceX / Hawthorne, California, United States
  - Steven Maa: PASS — Mission Manager, Rideshare Program / SpaceX / Hawthorne, California, United States
  - Jane M.: PASS — Bus and Payload Design Engineering Manager / Boeing / El Segundo, California, United States
  - Jay Malave: PASS — Chief Financial Officer / Boeing / Seattle, Washington, United States
- **Live verification (2026-07-25 07:28 UTC):** After the user deleted the extension folder, resynced (content.js = 44451 bytes, matching server), removed the plugin, reinstalled it, and refreshed all pages, the debug snapshots still showed the **old concatenated output** and the same broken parsed results. This indicates the new code was not actually executing in the browser — Chrome is caching the old content script despite the nuclear reload. Added a `BUILD_ID` to content.js and the debug payload to confirm which version runs next time.
- **Status:** code updated, harness passing; next step is to confirm Chrome is running the new build and capture fresh snapshots.

### rev-20260725-080000-parser-rewrite (entity-item role extraction)
- **What changed:** Rewrote `findLatestExperienceItem` from the ground up based on the real DOM captured in the 07:12 debug snapshots.
  - Entries are found by `section.querySelectorAll('[componentkey^="entity-collection-item-"]')`.
  - Each entry has a **logo-only** `/company/` link and a separate **text-bearing** `/company/` link.
  - **Flat roles** (Nicole, Steven, Jay): the text link has three direct child blocks: title block (job title + company/employment-type), dates block, location block. The parser identifies the date block, takes the first leaf of the preceding block as the title and the last leaf as the company, and strips work-mode bullets from the following block to get the location.
  - **Grouped roles** (Jane): the entry has a company text link at the group level plus a `<ul>` of roles. The company name comes from the group text link (ignoring the duration tenure line); each `<li>` provides its own title/dates/location.
  - `extractExperience()` now uses `titleFromSibling`, `companyFromLink`, and `locationFromSibling` first, only falling back to `parseExperienceItem` if a piece is missing.
  - `BUILD_ID` bumped to `parser-20260725-0800`.
- **Why:** The previous parser was operating on the wrong DOM assumptions (leaf-most dated elements, sibling children of role container). The real DOM nests title/dates/location inside a text-column `<a>` and uses grouped `<ul>/<li>` for some profiles, so the previous extraction returned concatenated garbage.
- **Files touched:** `static/chrome-extension/content.js`, `/tmp/opencode/test-latest-role.js`, `CHANGELOG.md`.
- **Test cases (harness):**
  - Nicole LaMotte: PASS — Manager, Technical Recruiting / SpaceX / Hawthorne, California, United States
  - Steven Maa: PASS — Mission Manager, Rideshare Program / SpaceX / Hawthorne, California, United States
  - Jane M.: PASS — Bus and Payload Design Engineering Manager / Boeing / El Segundo, California, United States
  - Jay Malave: PASS — Chief Financial Officer / Boeing / Seattle, Washington, United States
- **Syntax check:** `node --check static/chrome-extension/content.js` passes.
- **Status:** SHIPPED for verification in Chrome.

### rev-20260725-080000-parser-rewrite-verification (live results)
- **When:** 2026-07-25 15:53–21:01 UTC (latest debug snapshots).
- **Confirmed fixed (do not regress):**
  - **Steven Maa** — `title` = `Mission Manager, Rideshare Program`, `company` = `SpaceX`, `location` = `Hawthorne, California, United States`.
  - **Jay Malave** — `title` = `Chief Financial Officer`, `company` = `Boeing`, `location` = `Seattle, Washington, United States`.
  - These are flat-role profiles. The new entity-item parser correctly identifies the title block, company line, date block, and location block.
- **Still broken (multi-role at same company):**
  - **Nicole LaMotte** — `title` = `Full-time` (expected `Manager, Technical Recruiting`), `company` = `SpaceX`, `location` = `Hawthorne, California, United States`.
  - **Jane M.** — `title` = `Bus and Payload Design Engineering Manager`, `company` = `Boeing8 yrs` (expected `Boeing`), `location` = `El Segundo, California, United States`.
- **Root cause discovered by DOM analysis (agents):**
  - **Nicole** is actually a **grouped role** at SpaceX (multiple promotions). The first `<li>` contains leaves `[Manager, Technical Recruiting, Full-time, Jan 2026 - Present · 7 mos, Hawthorne, California, United States]`. The parser took `leaves[dateIdx - 1]`, which is the employment-type line `Full-time`, as the title.
  - **Jane** is also grouped. The company group text link has leaf elements `<p>Boeing</p>` and `<p>8 yrs</p>`, but `extractGroupCompany` scanned wrapper `<div>`s before leaf elements and returned the concatenated text `"Boeing8 yrs"` because the combined string was not recognized as a duration.
- **Constraint:** Any fix for Nicole/Jane must keep Steven/Jay passing.
- **Status:** fixes implemented; awaiting live verification.

### rev-20260725-210100-multirole-fix (grouped-role fixes)
- **What changed:** Two localized fixes in `findLatestExperienceItem` (`static/chrome-extension/content.js`).
  1. **`extractGroupCompany` (line ~264):** now uses `leafTextElements(textLink)` instead of `querySelectorAll("p, span, div")`. This prevents wrapper `<div>` text concatenations like `"Boeing8 yrs"` from being returned as the company name. It iterates only actual text-bearing `<p>`/`<span>` descendants and skips duration/date leaves.
  2. **`parseGroupedEntry` (line ~385):** when choosing the title from leaves before the date line, it now skips known employment-type labels (`Full-time`, `Part-time`, etc.) and duration-only strings. This handles LinkedIn's layout where an employment-type line is inserted between the real job title and the date line.
- **Why:** The entity-item parser handled flat roles correctly, but grouped/multi-role entries at the same company had two remaining failure modes: (a) wrapper-div concatenation of company + tenure, and (b) an employment-type leaf sitting between title and date.
- **Files touched:** `static/chrome-extension/content.js`, `CHANGELOG.md`.
- **Safety:** Both changes are localized to grouped-role handling. `parseFlatEntry` (used by Steven and Jay) is untouched. The Nicole fix only affects grouped entries with an employment-type leaf before the date; Steven/Jay flat entries do not reach this path.
- **BUILD_ID:** bumped to `parser-20260725-2101`.
- **Test cases:**
  - Nicole LaMotte: expected `Manager, Technical Recruiting / SpaceX / Hawthorne, California, United States`.
  - Jane M.: expected `Bus and Payload Design Engineering Manager / Boeing / El Segundo, California, United States`.
  - Steven Maa: expected unchanged `Mission Manager, Rideshare Program / SpaceX / Hawthorne, California, United States`.
  - Jay Malave: expected unchanged `Chief Financial Officer / Boeing / Seattle, Washington, United States`.
- **Syntax check:** `node --check static/chrome-extension/content.js` passes.
- **Status:** SHIPPED for live verification.

### rev-20260725-210100-PRIME (stable release baseline)
- **Designation:** **PRIME** — the first release-ready, fully-verified version of the Lead Finder Chrome extension.
- **What this version represents:**
  - LinkedIn latest-current-role parsing works across all four live test profiles:
    - Flat roles (Steven Maa, Jay Malave)
    - Grouped / multi-role at the same company (Nicole LaMotte, Jane M.)
  - Title, company, start date, current flag, and location are all cleanly separated.
- **BUILD_ID:** `parser-20260725-2101`.
- **Files state:**
  - `static/chrome-extension/content.js` — parser-20260725-2101.
  - `static/chrome-extension/CHANGELOG.md` — includes this PRIME entry.
  - All other extension files (`manifest.json`, `background.js`, `popup.*`, `content.css`, `icons/`) are consistent with this release.
- **Fallback policy:** This revision is the **default rollback target** for any future bug or regression in LinkedIn parsing. If a later change breaks parsing, revert to this version unless a subsequent revision has explicitly earned a new PRIME designation through the same four-profile live verification.
- **Note:** The popup rematch feature added in `rev-20260725-220000-popup-rematch` is intentionally **not** part of the PRIME baseline because it is a UI/workflow change, not a parser change. If it causes issues, revert only `popup.js`; the parser PRIME baseline remains unchanged.
- **How to revert:**
  1. Identify the file state at this revision (content.js BUILD_ID `parser-20260725-2101`).
  2. Restore `static/chrome-extension/content.js` to that state.
  3. Re-sync the extension via `sync-extension.ps1`, remove the extension in Chrome, load unpacked, and hard-refresh each profile.
- **Status:** RELEASE-READY. No further parser changes should be made without first adding a new Open Revisions entry and, if the change fails live verification, rolling back to this PRIME version.

### rev-20260725-230000-group-location (group-level location fallback)
- **What changed:** `content.js` `parseGroupedEntry` now falls back to scanning the **company group text link** for a location string when the individual role `<li>` has no location element.
- **Why:** Darragh De Stonn-Dun's profile exposed a grouped-role layout where the location (`Los Angeles Metropolitan Area · Hybrid`) lived at the company-group level, not inside the role `<li>`. Nicole and Jane had location inside the role `<li>`, so they worked without this fallback.
- **Files touched:** `static/chrome-extension/content.js`.
- **BUILD_ID:** bumped to `parser-20260725-2300`.
- **Status:** closed; verified against Darragh snapshot.

### rev-20260725-230100-location-enrichment (Google Places address enrichment)
- **What changed:** Server-side location enrichment for every LinkedIn plugin push.
  - `lf_server.py`: new `_resolve_location()` runs a Google Places Text Search for `current_company + core_city` on every push. Returns full formatted address, city, state, lat, lng, place_id.
  - `contacts.location` is updated to the full Google Places formatted address (with `, USA` stripped).
  - Raw LinkedIn string preserved in `contacts.linkedin_location_raw`.
  - New DB columns on `contacts` for export/Salesforce: `linkedin_resolved_city`, `linkedin_resolved_state`, `linkedin_resolved_country`, `linkedin_resolved_lat`, `linkedin_resolved_lng`, `linkedin_resolved_formatted_address`, `linkedin_resolved_place_id`, `linkedin_resolved_source`, `linkedin_resolved_at`.
  - New `location_resolution_cache` table keyed by `(raw_location, company_name)` to avoid repeated Google Places API calls.
  - `_enrich_company_from_resolution()` fills empty `companies.city/state/lat/lng` from the resolved address for all three plugin actions (`update_existing`, `new_contact_existing_company`, `new_company_new_contact`).
- **Files touched:** `lf_server.py`, `lf_db.py`, `lf_search.py`.
- **Why:** LinkedIn region strings like "Los Angeles Metropolitan Area" don't give a usable city for Salesforce export. We now resolve them to real addresses. The same enrichment also applies to clean city/state strings, giving full street addresses when Google Places has them.
- **Bugs fixed during live verification:**
  1. `find_nearby_cities` returned tuples, but the resolver treated them as dicts → `AttributeError`. Fixed by indexing tuple elements.
  2. `create_contact_manual` INSERT had 33 placeholders for 32 columns after adding resolved columns → `sqlite3.OperationalError: 33 values for 32 columns`. Removed one duplicate `?`.
  3. Multiple stale `lf_server.py` processes were listening on port 8798, so code changes didn't take effect. Killed old PIDs and restarted.
  4. `contacts.location` ended in `, USA` and `, CA, USA`. Fixed `_format_location` and cached-result path to strip trailing country.
- **Live verification:**
  - **Darragh De Stonn-Dun** (`darraghdestonndun`): `location` = `10842 Noel St Unit 102, Los Alamitos, CA 90720`; `linkedin_resolved_city` = `Los Alamitos`; company `Automated Industrial Robotics` geocoded to `Los Alamitos, CA`.
  - **Victor Coronado** (`victor-coronado-a691566`): `location` = `17800 Laguna Canyon Rd Suite 300, Irvine, CA 92603`; `linkedin_resolved_city` = `Irvine`; company `Ventura Foods` geocoded to `Irvine, CA`.
  - Steven, Nicole, Jane, Jay database records reviewed: all four have correct title/company/location from the PRIME parser.
- **Server process:** must be restarted for schema migrations and code changes. Use `kill $(lsof -ti :8798) && cd /home/anthonyturgman/lead-finder && nohup python3 lf_server.py > logs/lf_server_$(date +%Y%m%d).log 2>&1 &`.
- **Status:** closed; running on port 8798 (PID changes with each restart).

---

## Next Session Focus: Email Pattern Recognition & Validation
- **Priority:** highest.
- **Goal:** Before importing any contact into Salesforce, validate the email pattern (first/last → company domain) and test the email for deliverability.
- **Two entry points to cover:**
  1. **LinkedIn plugin pathway** — currently sends `email: null` for most profiles. We need to derive the email from `first_name`, `last_name`, and the matched company's domain, then validate it.
  2. **Lead-finder search pathway** — existing contacts/companies from search have company domains; we need to generate candidate emails and validate them before Salesforce export.
- **Likely work areas:**
  - `lf_server.py` plugin push endpoint: call email derivation + validation after contact/company creation.
  - Existing email validation module in `lf_server.py` / helpers.
  - `lf_db.py`: store validation status, candidate email patterns, and validation response on contacts.
  - Potential UI: popup shows derived email and validation result before user clicks Send.
- **Why now:** The LinkedIn parser and location enrichment are stable. The next blocker for Salesforce import is making sure the email address is real, not just guessed.

---

## Closed Revisions

### rev-20260724-185300 (targeted fix, content.js = 569 lines)
- **What changed (2 small edits to content.js):**
  1. **Line ~294 in parseExperienceItem:** `\\d{{4}}` → `\\d{4}`. The double-brace was a template-literal typo that produced a broken regex (`{4}` was being parsed as a quantifier on the previous capture group, not as a literal "4 times"). Result: the date-range match always failed, so the entire experience blob was returned as `title` with no other fields populated. This is what caused the Steven Maa symptom: `"Mission Manager, Rideshare ProgramSpaceX · Full-timeJan 2026 - Present · 7 mosHawthorne, California, United States · On-site"` came back as one line.
  2. **Lines ~399-410 in extract():** location discovery now prefers the parsed experience item's location (`experience[0].location`) over the top card's primary location (`extractLocation()`). Previously the top card always won, which gave the wrong location when LinkedIn's "primary location" differs from the work location of the current role.
- **What this should produce on Steven Maa's profile:**
  - `name`: Steven Maa (from `main h2` or og:title)
  - `title`: `Mission Manager, Rideshare Program` (split off from `SpaceX` via camelCase boundary in `parseExperienceItem`)
  - `company`: `SpaceX` (from camelCase split, OR from inner `<a href="/company/spacex">` link if the strategy-2 `<li>` path wins)
  - `started_at`: `Jan 2026`, `ended_at`: ``, `is_current`: true
  - `location`: `Hawthorne, California, United States` (from the experience item's text after the date)
- **Files touched:** `static/chrome-extension/content.js` only. `manifest.json`, `background.js`, `popup.*` unchanged.
- **Verification:** Run on Steven's profile. Console should show `name=Steven Maa`, `title=Mission Manager, Rideshare Program`, `company=SpaceX`, `location=Hawthorne, California, United States`, `1 role(s)`. If anything is wrong, add a regression entry and revert with `tar xzf .revisions/baseline-20260724-185032.tar.gz`.

### baseline-20260724-185032 (baseline snapshot, content.js = 567 lines)
- **What:** Tarball of the current state of all extension files, saved before any further parser work.
- **Why:** Establish a known-good starting point so future regressions can be diffed against this.
- **File:** `static/chrome-extension/.revisions/baseline-20260724-185032.tar.gz`
- **Known problems with this baseline:**
  - **Steven Maa profile (https://www.linkedin.com/in/steven-maa/)**: parser returns the entire experience item as one unparsed string: `"Mission Manager, Rideshare ProgramSpaceX · Full-timeJan 2026 - Present · 7 mosHawthorne, California, United States · On-site"`. No newline/split between title, company, employment type, dates, location, work mode. Form fields stay empty because the title/company aren't extracted from the blob.
  - **Before this baseline (per user):** there was a version that returned `roles=1` and got name + experience count right, but couldn't find the company. The user says: "your previous perfect code (except for the identification of the position for experience content) is now gone. Let's try to get it back and reset. And then make the location change on the discovery for the experience section."
  - **We do not have a copy of the working-but-company-bad version.** It was edited through and the file in this baseline is the regressed state.

### content.js current state (the "regressed" version)
- **Symptom:** `[lead-finder/cs] Initial extract partial: name=true company=false roles=1`
- **Field set by the parser:** name only. title, company, location, experience all empty (the form shows them as blank because the parser joins everything into one blob and never splits it).
- **What broke:** the most recent rewrite attempted to extract from `data-section="currentPositionsDetails"` and `og:` meta tags, but the actual logged-in DOM uses neither pattern for the experience data. The experience items live inside an `<h2>Experience</h2>` section with `<li>` entries, but the parser now joins all text within an item into one string without splitting on newlines or bullet separators.

---

## Known DOM patterns (for reference when designing the next iteration)

| Profile | Connection degree | Experience section structure |
|---|---|---|
| Steven Maa | 1st | `<h2>Experience</h2>` → `<ul class="...">` → `<li>` per role. Each `<li>` has nested divs with title, company link, dates, location, work mode. |
| Jay Malave | 3rd | `<h2>Experience & Education</h2>` → flat `<div>` blocks (no `<ul><li>`). Per user, "third-degree LinkedIn connections show experience data as flat `<div>` blocks". |

The parser must handle BOTH formats. Use the strategy: find the `<h2>Experience</h2>`, then look for either `<ul><li>` children OR direct `<div>` children that contain "Present" in their text.

---

## Revert / recovery

If a new revision breaks things:

1. **Stop.** Don't keep editing.
2. Extract the baseline tarball:

### rev-20260724-213819 (latest-role-only fix, ~80 lines)
- **What:** Reordered `findLatestExperienceItem` to prefer the first `<li>` (Strategy 2) when an `<ul>` exists, with Strategy 1 (leaf-most div with Present) as a fallback for div-only profiles. Added `<li>` walk-up in Strategy 1 so the company link is reachable when the picked div is nested inside an `<li>`.
- **Why:** On Nicole LaMotte's profile (8 SpaceX roles, `<ul><li>` format), Strategy 1 was picking a nested `<div>` whose text was identical to its parent — the leaf-most filter didn't strip it, the sort by length picked the inner div, and the company link (which lives on the `<li>`, not the inner div) was unreachable. Result: popup showed `Role: Manager,`, `Company: Technical Recruiting,`, `Location: Lead Technical Recruiter, Engineering` (two roles joined into one parse).
- **Test cases:**
  - **Nicole LaMotte (https://www.linkedin.com/in/nicole-lamotte-63a21555/)** — Strategy 2 path. Should produce: Role="Manager, Technical Recruiting", Company="SpaceX", Location="Hawthorne, California, United States".
  - **Steven Maa (https://www.linkedin.com/in/steven-maa/)** — Strategy 2 path. Should still produce: Role="Mission Manager, Rideshare Program", Company="SpaceX", Location="Hawthorne, California, United States" (no regression).
  - **Jay Malave (https://www.linkedin.com/in/jay-malave-69a33748/)** — 3rd-degree, div-only. Strategy 1 path. No `<ul>` exists, so it falls through to the leaf-most div. Should still work.
- **Risk:** Low. Two strategy reordering + walk-up. No new selectors. Existing Steven Maa / Jay Malave behavior preserved.
- **Status:** shipped, awaiting user verification on Nicole + Steven + Jay.

### rev-20260724-214346 (Strategy 2 — sub-list guard, ~30 lines)
- **What:** Changed `findLatestExperienceItem` Strategy 2 to use `:scope > ul, :scope > ol` (direct children only) and to skip `<ul>`s whose first `<li>` has neither a date substring nor a `/company/` link.
- **Why:** rev-20260724-213819 didn't fix Nicole. The first `<ul>` walked up from `<h2>Experience</h2>` is a SKILLS or HIGHLIGHTS sub-list, not the experience list. The old code returned its first `<li>` (which has no date, no company link) and then `parseExperienceItem` was being fed the wrong text. Result: popup showed `Location: Technical Recruiting` (end of `<li>` #1's title, picked up as the location because the parse splits on the date) and `Company: Lead Technical Recruiter, Engineering` (the full title of `<li>` #2 because the parse kept reading past the date boundary).
- **Fix details:**
  - `:scope > ul, :scope > ol` — only direct-child `<ul>`s of the current ancestor. Stops the search from picking nested lists inside `<li>`s (like a sub-list of skills within a role).
  - Heuristic gate: at each candidate `<ul>`, the first `<li>` must contain BOTH a date (e.g. "Jan 2026") AND a `/company/` link. If not, it's a skills/highlights/about list — skip it.
  - Loops through all direct-child `<ul>`s at each ancestor level (in case of multiple sibling lists).
- **Test cases:**
  - **Nicole LaMotte** — should now pick the experience `<ul>` (first `<li>` has "Jan 2026 - Present" and an `<a href="/company/spacex">`). Expected popup: Role=`Manager, Technical Recruiting`, Company=`SpaceX`, Location=`Hawthorne, California, United States`.
  - **Steven Maa** — same expected output as rev-20260724-213819 (regression check).
  - **Jay Malave** — div-only profile, Strategy 1 path, no change in behavior.
- **Risk:** Low. Only Strategy 2 changed. Strategy 1 unchanged.
- **Status:** shipped, awaiting user verification.


1. **Stop.** Don't keep editing.
2. Extract the baseline tarball:
   ```bash
   cd /home/anthonyturgman/lead-finder/static/chrome-extension
   tar xzf .revisions/baseline-20260724-185032.tar.gz
   ```
3. Add a Regression entry here pointing to the new revision that broke things.
4. The `/extension/download` endpoint serves whatever is on disk, so after the revert, the user re-syncs and is back to known state.

---

## Next iteration — to be designed

User's stated goal:
1. Get the parser back to the state where it returned `roles=1` (correctly finding the experience item)
2. THEN make one targeted fix: improve **location discovery** inside the experience section

We do not have the previous working version's source. We need either:
- The user to paste the full content.js of the working version, OR
- The user to paste the popup output (form fields populated) from the working version, OR
- A rebuild from the high-level strategy documented in this file

**DO NOT** start a new iteration without one of the above. Confirm with the user before editing.

### rev-20260724-220106 (heading check fix, 1 line changed)
- **What:** Changed `findLatestExperienceItem` line 141 from `=== "Experience"` to accept `"Experience"`, `"Experience & ..."`, or `"Experience, ..."` (any heading that starts with "Experience").
- **Why:** LinkedIn renders the Experience section heading as `"Experience & Education"` on some profiles (when the section is combined with Education). The exact equality check only matched the bare `"Experience"` heading. On Nicole LaMotte's profile, the heading was `"Experience & Education"`, so the check returned null. The parser then fell through to Strategy 1 (the div-based fallback), which picked a div whose text was multiple roles joined — causing the "Manager, / Technical Recruiting" symptom.
- **Root cause analysis:** The 5 summary lines (`Name:`, `Title:`, etc.) were printing because those run at the end of `extract()` regardless. The depth= diagnostic lines were NOT printing because the entire Strategy 2 loop was skipped when `expH2` was null. This explained the "5 lines but no depth= lines" symptom — the parser was reaching the post-extract logging without ever running Strategy 2.
- **Test cases:**
  - **Nicole LaMotte (https://www.linkedin.com/in/nicole-lamotte-63a21555/)** — heading is "Experience & Education". Should now match and pick the first <li> via Strategy 2.
  - **Steven Maa (https://www.linkedin.com/in/steven-maa/)** — heading is bare "Experience". Should still match. No regression.
  - **Jay Malave (https://www.linkedin.com/in/jay-malave-69a33748/)** — heading is bare "Experience" per user, but DOM has "Experience & Education" per curl. Either way, both should now match.
- **Status:** shipped, awaiting user verification.
