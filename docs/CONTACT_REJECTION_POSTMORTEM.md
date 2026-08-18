# Contact Rejection Postmortem — Identity Verification Backfill

**Date:** 2026-07-26
**Workstream:** Contact identity verification (Phase 5 prerequisite)
**Trigger:** User suspected many of the 243 contacts were "invalid and hallucinated from early on when the contacts were just being dumped in by the weak search and validation criteria (the self-validating scripts and search)."
**Scope:** 41 contacts rejected by the hardened escalator (`pipeline_stage = 'escalator_no_commit'`, `confidence_score = 0.0`) during the verification backfill of 188 eligible contacts.
**Related code:** `lf_agent_verify.py` (escalator), `lf_executives.py` (ingest path), `lf_db.py:upsert_contact` (insert), `lf_search_providers.py` (web search).
**Companion docs:** `PLAN.md`, `CONTRACTS.md`, `PHASE_5_HARDENING_PLAN.md`.

---

## 1. Executive summary

Of 243 contacts, 188 were run through the double-confirmation escalator. **41 were rejected** — the escalator could not establish that the person currently holds the stated role at the stated company using two independent sources. The rejects fall into four root-cause categories:

| # | Root cause | Count | Ingest defect |
|---|---|---|---|
| A | **Departed / retired / former employees** ("retired 2023", "departed 2024", "Former President") accepted at ingest | 11 | No recency/current-employment check at ingest |
| B | **AI-only contacts with no corroborating source** (no LinkedIn URL, no snippet, no website mention) | 18 | AI primary research trusted as sole source; no corroboration gate before insert |
| C | **Wrong-LinkedIn-URL attachments** — URL slug belongs to a different person (e.g. "Kris Young" → `/in/ramone-andrade-analyst`) | 4 | URL accepted from search snippet without slug-vs-name match |
| D | **Snippet/name contamination** — `linkedin_snippet` describes a different person's tenure (e.g. "Eva Behrend" row carries a snippet about a Tesla cinematographer) | 8 | Snippet stored verbatim without name-token verification |
| E | **Synthetic / test / manual garbage** (`e2e-*`, `MANUAL_EDIT`, ` v2` names) | 3 | Test fixtures and one manual edit mixed into the live corpus |

Categories overlap: a single reject can be both "departed" and "AI-only", or both "wrong URL" and "snippet contamination". The counts above are by primary cause; 41 total rejects.

**All 41 have `confidence_score = 0.0`** — the escalator's final gate sets overall confidence to the min of written-field confidences, and since no field cleared the 0.55 bar, every written field was rolled back (`pipeline_stage = 'escalator_no_commit'`).

**The escalator worked.** The defects below were ingest-time defects that let bad rows in; the verification pass is what caught them. The fixes proposed in §4 are about preventing ingestion in the first place, so the corpus never needs a 35-minute reconciliation pass again.

---

## 2. Detailed findings by category

### Category A — Departed / retired / former employees (11 contacts)

**IDs:** 1521, 1523, 1525, 1629, 1638, 1639, 1640, 1647, 1722, 1724, 1735, 1970, 1973, 1985 (some also in B).

Representative rows:
- **1523 Alan Defrancis** — title `Senior Director, Global Head of Real Estate (retired 2023)`, Boeing. The word "retired" is literally in the stored title.
- **1521 Bob Burtch** — title `VP, Supply Chain, Commercial Airplanes (departed 2024)`, Boeing. Snippet says `Experience: Self-employed`.
- **1629 Dave Carver** — title `Former President (retired)`, NASSCO.
- **1973 Tony Wood** — title `Former Chief Executive Officer`, Meggitt.
- **1985 Greg Hayes** — title `Former Chairman & CEO`, filed under `Hamilton Sundstrand Corporation` (a UTC subsidiary that hasn't existed under that name since 2020) but the email is `@collinsaerospace.com`.

**Why they slipped in:** The original ingest path (`discover_executives_ai_first`, `lf_executives.py:1998+`) and `upsert_contact` (`lf_db.py:431`) never checked whether the person is a **current** employee. The AI research prompt does ask for `is_current_employee` (`lf_ai_enrich.py:1507`), and the escalator's M1 checks it (`lf_agent_verify.py`), but at **ingest time** that signal was not a gate — a title like "Former President" was stored verbatim and the contact was inserted with `confidence_score` defaulting high.

**Hardening gap:** No ingest-time regex/keyword filter for `retired|departed|former|past|emeritus|self-employed`, and no ingest-time use of the AI's `is_current_employee=false` signal to reject (only the escalator uses it, after the fact).

### Category B — AI-only contacts, no corroboration (18 contacts)

**IDs:** 1629, 1638, 1639, 1640, 1647, 1737, 1955, 1966, 1970, 1971, 1973, 1976, 1977, 1978, 1980, 1985, 1986, 1989.

These have `data_provenance` starting with `AI_PRIMARY` or `AI_PRIMARY:ai_search`, **no `linkedin_url`, no `linkedin_snippet`**, and no `title_from_website`. They were created purely from an AI completion. Representative rows:
- **1640 Todd Bishop** — `VP of Engineering, General Dynamics NASSCO`, no URL, no snippet. AI said so, and that was enough to insert.
- **1955 Greg Manuel** — `Vice President, Strategic Deterrent Systems`, Northrop Grumman. AI-only.
- **1989 Tony Favaloro** — `CFO, Circor International`. AI-only. (Circor Aerospace was filed as the company; the AI's "Circor International" is a related-but-different entity.)

**Why they slipped in:** `discover_executives_ai_first` treats an AI completion as a sufficient insert signal. The Layer-2 press search (`lf_executives.py:2130`) and Layer-3 AI ping are **best-effort and silent on failure** (`except: pass` at lines 2115 and 2144). When search is down or returns nothing, the contact is still inserted with `data_provenance='AI_PRIMARY'` and a default-high confidence. There was **no "minimum two sources" gate at insert**.

**Hardening gap:** `upsert_contact` accepts a row with `source_primary='ai'` alone. The double-confirmation rule (≥2 independent sources) was enforced only by the escalator, never by the ingest path.

### Category C — Wrong LinkedIn URL attachments (4 contacts)

**IDs:** 1591, 1594, 1597, 1599 (all Vast).

| Contact | Stored URL slug | Name tokens |
|---|---|---|
| Kris Young | `/in/ramone-andrade-analyst` | kris, young |
| Benjamin Leeds | `/in/julian-breen-bba378209` | benjamin, leeds |
| Eva Behrend | `/in/corbincox` | eva, behrend |
| Tom Shelley | `/in/meghan-everett-phd-81943030` | tom, shelley |

In every case the URL slug contains **none** of the contact's name tokens — these are four different real people's LinkedIn profiles attached to four different contacts.

**Why they slipped in:** `cascading_linkedin_search` (`lf_executives.py:2103`) runs the query `"{full_name}" "{company_name}" site:linkedin.com/in` and accepts the **first** result whose URL passes `is_valid_linkedin_profile(url, snippet)`. That validator checks URL shape and snippet length but **does not check that the slug matches the queried name**. When search returns a wrong-but-shape-valid profile (common for rare names where the search engine broadens the match), it is stored as the contact's `linkedin_url`. The escalator's M2 (`verified_linkedin_url`) later re-verifies and rejects it, but by then the row exists.

**Hardening gap:** `is_valid_linkedin_profile` (and the ingest path generally) has no **slug-name token match** check. A URL like `/in/ramone-andrade-analyst` should never be written to a "Kris Young" row.

### Category D — Snippet / name contamination (8 contacts)

**IDs:** 1521, 1523, 1525, 1532, 1572, 1597, 1599, 1664.

The `linkedin_snippet` column carries text that does **not** mention the contact's own name, yet it was stored as that contact's snippet. Examples:
- **1597 Eva Behrend** — snippet: `Senior Manager, Media Production. VAST. Jul 2024 - Present … Cinematographer. Tesla.` — this is a different Vast employee's tenure, attached to the Eva Behrend row.
- **1572 Marsha Shoushtari** — snippet: `Felbro Food Products, Inc. Education. California State University-Dominguez Hills Graphic. General Manager at J.W.Treuth & Sons inc.` — name "Marsha Shoushtari" does not appear in the snippet; it describes someone at a different company.
- **1521 Bob Burtch** — snippet: `Experience: Self-employed` — a strong "departed" signal that was stored but never acted on.

**Why they slipped in:** Same root cause as C — the first shape-valid search result wins, and `_extract_title_from_linkedin_snippet` (`lf_executives.py:1580`) extracts a title from snippet text without confirming the snippet is **about the queried person**. A snippet that mentions the company but not the person is treated as corroboration.

**Hardening gap:** No name-token-in-snippet check before storing `linkedin_snippet` / `title_from_linkedin`.

### Category E — Synthetic / test / manual garbage (3 contacts)

**IDs:** 1990, 2045, 2046.
- **1990 Breslin Juan** — `data_provenance='MANUAL_EDIT'`, no title, company "Advanced Cosmetic Research". Looks like a manual one-off.
- **2045 "Dana Discovery v2"** / **2046 "Edmund Eagleton v2"** — `plugin_pushed`, `linkedin_slug` starts with `e2e-`, title `Updated Title via Auto-Match`. These are E2E test fixtures that leaked into the live corpus because the test cleanup (`reset_e2e_test_data`) only removes `linkedin_slug LIKE 'e2e-%'` rows created by the test runner, not ones pushed by the plugin E2E.

**Why they slipped in:** Test fixtures and manual edits share the production `contacts` table with no isolation. The verification script excludes `e2e-%` and `Test%` names, but the escalator itself does not — these three reached `escalator_no_commit` only because their names are obviously fake; the escalator rejected them on the merits.

**Hardening gap:** No `is_test` / `is_synthetic` flag on contacts; test fixtures live in the production table and rely on naming conventions to be filtered out.

---

## 3. Cross-cutting observations

1. **Company concentration.** Rejects cluster: Vast (4), Parker Hannifin (4), Boeing (3), General Dynamics ×2 companies (6), Hitachi Vantara (3), JFrog (2), Ducommun (2). The early "dump" runs hit the same defense companies repeatedly and accepted every AI-suggested executive without corroboration. The NASSCO / General Dynamics cluster is almost entirely former executives.

2. **The `data_provenance` and `source_primary` columns are unreliable as quality signals on their own.** A row labelled `AI_PRIMARY+WEBSITE+LINKEDIN:searxng` (the "strongest" provenance) still contains wrong-URL rejects (1591, 1594, 1597, 1599). The provenance string records which layers *ran*, not whether they *agreed*.

3. **The escalator is the correct place for the double-confirmation gate, and it works.** Every defect above is an **ingest** defect. The escalator caught all of them. The lesson is not "make the escalator stricter" — it is "stop accepting uncorroborated rows at ingest so the escalator has less garbage to sieve."

4. **Search provider reliability drove the original mess and still drives it.** When the free search providers (degoog/fourget/searxng) are down, the ingest path's Layer-2 press search silently fails (`except: pass`), so the AI completion becomes the sole source and a row is inserted anyway. The circuit breaker added in this workstream (`lf_search_providers.py`) fixes the *latency* problem but not the *silent-accept* problem.

5. **`confidence_score` defaults are too permissive.** `upsert_contact` defaults `confidence_score` to `1.0` (`lf_db.py:540`) when the caller doesn't supply one. Several early ingests let that default ride, so uncorroborated AI contacts entered at confidence 1.0 and were only corrected when the escalator ran. A single-source AI contact should never enter at 1.0.

---

## 4. Recommended hardening (ingest-time guards)

These are the preventive fixes implied by the findings. **All seven (H-1..H-7)
were IMPLEMENTED on 2026-07-26** — file:line citations are inline below. The
escalator (`lf_agent_verify.py`) remains the safety net; these guards stop bad
rows at the front door so the escalator has less garbage to sieve.

### H-1: Reject departed/retired/former at ingest  *(Category A)* — IMPLEMENTED
**File:** `lf_executives.py`, `discover_executives_ai_first` (line 1998+) and the non-AI `discover_executives` (line 1671).
**Change:** Before calling `upsert_contact`, run a title/keyword gate:
```python
DEPARTED_RE = re.compile(r'\b(retired|former|departed|past|emeritus|self[- ]?employed|ex[- ])\\b', re.I)
if DEPARTED_RE.search(title or '') or DEPARTED_RE.search(ai_verified_title or ''):
    # do not insert; log and skip
```
Also honor the AI's `is_current_employee=False` signal: when the AI explicitly says the person is not a current employee of the target company, **do not insert** (the escalator already uses this to move or reject; ingest should reject).

**Implementation:**
- `DEPARTED_RE` regex and `_is_departed_title()` helper: `lf_executives.py:44`, `lf_executives.py:50`.
- Title/`ai_verified_title` gate in `_verify_and_enrich_person_impl` (the body of `verify_and_enrich_person`, called by both `discover_executives_ai_first` paths and `_enrich_from_linkedin_fallback`): `lf_executives.py:2298-2306`.
- AI `is_current_employee=False` honored **unconditionally** (previous gate only dropped when no LinkedIn + no press evidence): `lf_executives.py:2279-2288`.
- Legacy `discover_executives` save loop (does not route through `verify_and_enrich_person`): `lf_executives.py:1822-1830`.

### H-2: Require two independent sources before insert  *(Category B)* — IMPLEMENTED
**File:** `lf_executives.py:discover_executives_ai_first`, `lf_db.py:upsert_contact`.
**Change:** An AI-only contact (no LinkedIn URL, no website mention, no press snippet) must **not** be inserted at confidence > 0.0. Either:
- Skip the insert entirely and write to a `pending_review` queue, or
- Insert with `confidence_score` capped at 0.40 and `pipeline_stage='needs_review'` so it is visibly provisional and must clear the escalator before export.
The escalator's double-confirmation gate then becomes the only path to `pipeline_stage='verified'`.

**Implementation:** AI-only cap in `_verify_and_enrich_person_impl`: `lf_executives.py:2333-2345`. When `not linkedin_url and not web_evidence`, `confidence_score` is capped at 0.40 and `pipeline_stage='needs_review'` is set. The escalator's double-confirmation gate remains the only path to `verified`.

### H-3: Slug-vs-name match for LinkedIn URLs  *(Category C)* — IMPLEMENTED
**File:** `lf_executives.py:2109` (the `is_valid_linkedin_profile` call) or a new helper.
**Change:** Before storing `linkedin_url`, extract the slug and require at least one name token (length ≥ 3) from `full_name` to appear in the slug:
```python
def slug_matches_name(url: str, full_name: str) -> bool:
    m = re.search(r'/in/([a-z0-9\-]+)', url.lower())
    if not m: return False
    slug = m.group(1)
    toks = [re.sub(r'[^a-z]','',p) for p in full_name.lower().split()]
    return any(len(t) >= 3 and t in slug for t in toks)
```
If `slug_matches_name` is False, do **not** store the URL; treat the search result as a miss and continue the query loop. This single check would have prevented all four Category-C rejects.

**Implementation:** `slug_matches_name()` helper at `lf_executives.py:59`. Gate in the LinkedIn search loop of `_verify_and_enrich_person_impl`: `lf_executives.py:2173-2180` (mismatch → `continue` the result loop, treating as a miss).

### H-4: Name-token check before storing snippet / `title_from_linkedin`  *(Category D)* — IMPLEMENTED
**File:** `lf_executives.py:2108-2111` and `_extract_title_from_linkedin_snippet` (line 1580).
**Change:** Before storing `linkedin_snippet` or deriving `title_from_linkedin` from it, require the contact's first **and** last name tokens to appear in the snippet text (case-insensitive). If the snippet does not mention the person, it is about someone else — discard it and do not set `title_from_linkedin`. This prevents the "Eva Behrend row carries a Tesla cinematographer's snippet" class of corruption.

**Implementation:** `_snippet_mentions_person()` helper at `lf_executives.py:77`. In the LinkedIn search loop, a snippet that fails the check blanks `linkedin_snippet` and the URL is recorded without a snippet: `lf_executives.py:2181-2199`. The title/location extraction block is also guarded so `_extract_title_from_linkedin_snippet` is only called when the snippet mentions the person: `lf_executives.py:2210-2226`.

### H-5: Lower the default `confidence_score`  *(Cross-cutting)* — IMPLEMENTED
**File:** `lf_db.py:540`.
**Change:** Default `confidence_score` from `1.0` to `0.0` (or at most `0.50`). A caller that has actually corroborated the contact should pass an explicit score. Letting the default ride at 1.0 is how uncorroborated AI contacts entered the corpus looking authoritative. This is a one-line change with broad effect — audit callers first (`grep -n 'confidence_score' lf_executives.py lf_server.py`).

**Implementation:** All three `data.get("confidence_score", 1.0)` defaults in `upsert_contact` changed to `0.0`: `lf_db.py:478`, `lf_db.py:514`, `lf_db.py:545`. Caller audit confirmed every ingest path in `lf_executives.py` passes an explicit `confidence_score` (lines 772, 1467, 1525, 1892, 1925, 2388, 2417, 2442), and the manual-create endpoint in `lf_server.py:1453` passes `0.5`. The plugin path (`create_contact_manual`, `lf_db.py:2145`) keeps its `1.0` default because plugin pushes are human-confirmed in the browser extension and pass an explicit value from the server; it is a separate function from `upsert_contact` and out of the H-5 scope.

### H-6: Isolate test fixtures  *(Category E)* — IMPLEMENTED
**File:** schema (`lf_db.py` migrations) + test runners.
**Change:** Add an `is_test INTEGER DEFAULT 0` column to `contacts` and have the E2E/plugin-test runners set it. Filter `is_test=0` in all production queries (`/api/contacts`, export, escalator selection). This replaces the fragile `name LIKE 'Test%' / 'e2e-%'` filtering in `scripts/verify_existing_contacts.py` with a hard schema boundary.

**Implementation:**
- Schema column in `CREATE TABLE contacts`: `lf_db.py:91`.
- Migration via `_add_column_if_missing`: `lf_db.py:226-228` (runs `ALTER TABLE contacts ADD COLUMN is_test INTEGER DEFAULT 0` if missing; migration verified on the live `lf.db`).
- `create_contact_manual` (plugin path) accepts and persists `is_test`: `lf_db.py:2120` (column) and `lf_db.py:2157` (value).
- Production query filter `AND COALESCE(ct.is_test, 0) = 0` in `/api/contacts`: `lf_server.py:794`.
- Plugin inject endpoint reads `is_test` from the payload and threads it into both `create_contact_manual` call sites: `lf_server.py:3570-3572` (parsing), `lf_server.py:3802` (`new_contact_existing_company`), `lf_server.py:3853` (`new_company_new_contact`).
- Test runners set `is_test=1`:
  - `tests/test_plugin_email_chain.py`: `build_payload` (`tests/test_plugin_email_chain.py:107`) and the direct `INSERT INTO contacts` (`tests/test_plugin_email_chain.py:176-179`).
  - `scripts/session3_e2e.py`: new-contact payloads N11-N13 (`scripts/session3_e2e.py:204`), C14-C16 (`scripts/session3_e2e.py:244`), A17-A18 (`scripts/session3_e2e.py:280`), D19-D20 (`scripts/session3_e2e.py:314`). The `update_existing` U01-U10 targets are intentionally left `is_test=0` because they are real production contacts being borrowed by the test.
  - `scripts/session3_t42_smtp_lifecycle.py`: case 1 (`scripts/session3_t42_smtp_lifecycle.py:117`) and case 2 (`scripts/session3_t42_smtp_lifecycle.py:160`). Case 3 is `update_existing` on a real Austal USA contact and is left `is_test=0`.

### H-7: Make ingest-time search failure non-silent  *(Cross-cutting)* — IMPLEMENTED
**File:** `lf_executives.py:2115` and `2144`.
**Change:** The `except: pass` blocks hide search failure. At minimum, when Layer-2 press search returns nothing **and** there is no LinkedIn URL, mark the contact `pipeline_stage='needs_review'` and cap confidence at 0.40 (ties into H-2). Do not let a silent search failure promote an AI-only contact to a high-confidence row.

**Implementation:**
- LinkedIn search loop `except Exception:` now logs the failure: `lf_executives.py:2203-2208`.
- Layer-2 press search `except Exception:` now logs and sets `press_search_failed=True`: `lf_executives.py:2248-2254`.
- The H-2 cap (`lf_executives.py:2333-2345`) uses `press_search_failed` to distinguish "no press evidence" from "press search failed" in its log message, and either way caps confidence at 0.40 + `needs_review` when there is no LinkedIn URL. A silent search failure can no longer promote an AI-only contact to a high-confidence row.

---

## 5. What the escalator already does right (do not weaken)

These are the gates that **did** catch the 41 rejects. They should be preserved:

- **M1** (`ai_research_contact`): requires `is_current_employee=True` **and** company match for a title confidence ≥ 0.70. All 11 departed contacts failed here.
- **M2** (`cascading_linkedin_search` → `verified_linkedin_url`): independent URL re-verification gate. The 4 wrong-URL contacts failed here — the URL was not written because re-verification could not confirm it.
- **M2b** (`search_linkedin_verification`): name **and** company must appear in a search snippet; without it, title confidence is lowered to 0.55. The snippet-contamination contacts failed the name-in-snippet check here.
- **M3** (company website directory scrape): an independent source that does not depend on web search. This is why verification still worked when the free search providers were down.
- **Final gate** (`lf_agent_verify.py:535`): `overall_conf = min(written field confidences)`, success requires `overall_conf ≥ 0.55`. Every one of the 41 rejects had `overall_conf = 0.0` because no field cleared the bar.
- **`escalator_no_commit` stage**: the escalator never deletes or overwrites on failure — it simply does not commit, leaving the row at `confidence_score=0.0` for a later decision (this deletion).

The escalator is the safety net. The fixes in §4 are about making the net catch less by letting less through the front door.

---

## 6. Disposition

**Decision (user, 2026-07-26):** Delete the 41 rejected contacts.

Rationale: they are confirmed non-current, uncorroborated, mis-attributed, or synthetic. Keeping them in the corpus risks exporting bad leads and pollutes future verification runs. The data provenance for each is preserved in this document and in the `discovery_job_items` table (job `verify_a834f6c36d0d`) for audit.

The 8 General Atomics orphans (`company_id=1098`, company row missing) are **not** in the 41 rejects — they were excluded from verification because their company does not exist. They are out of scope for this deletion and remain in the corpus pending a separate decision on re-creating General Atomics.

---

## 7. Reference index

- Escalator entry point: `lf_agent_verify.py:run_verify_batch_job` / `create_verify_batch_job`.
- Escalator per-contact: `lf_agent_verify.py:_escalate_one_contact` (M1–M5, lines 260–580).
- Ingest path (AI-first): `lf_executives.py:discover_executives_ai_first` (line 1998).
- Ingest path (legacy): `lf_executives.py:discover_executives` (line 1671).
- Insert: `lf_db.py:upsert_contact` (line 431), default `confidence_score=1.0` at line 540.
- LinkedIn URL acceptance: `lf_executives.py:2109` (`is_valid_linkedin_profile`).
- Snippet title extraction: `lf_executives.py:_extract_title_from_linkedin_snippet` (line 1580).
- Circuit breaker: `lf_search_providers.py` (module-level `_BREAKER_*` state, wired into `search()`).
- Backfill script: `scripts/verify_existing_contacts.py`.
- Verification job record: `discovery_jobs.job_id='verify_a834f6c36d0d'`, `discovery_job_items` (188 rows).
- Final DB state before deletion: 243 contacts → 182 verified, 41 `escalator_no_commit`, 20 `discovered` (12 test + 8 GA orphans).