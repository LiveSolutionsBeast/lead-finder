# Lead Finder v2 — Shared Contracts

**Version:** 1.5
**Created:** 2026-07-24
**Status:** Phase 1/2 complete — domain verification + full dataset scrub implemented; Phase 0 engagement proof hardening complete.
**Machine-readable plan:** `PLAN.json`
**Human-readable plan:** `docs/PLAN-domain-reverify.json`

This document is the source of truth for anything two sessions need to agree on. If a session needs to change something in this document, it must post a flag to `PLAN.md` Open Questions section and wait for the owner to resolve.

---

## Important: schema reality check

The current database has these email-validation-related fields on `contacts` (verified by reading `/home/anthonyturgman/lead-finder/lf.db`):

| Column | Type | Default | Existing values |
|---|---|---|---|
| `smtp_validation_status` | TEXT | NULL | `"Okay to Send"`, `"Do Not Send"`, NULL |
| `smtp_validated_at` | TEXT | NULL | ISO 8601 timestamps |
| `smtp_validation_code` | INTEGER | NULL | SMTP response code (e.g. 250, 550) |
| `email_ready_for_export` | INTEGER | 0 | 0 / 1 |
| `email_rejected_reason` | TEXT | NULL | e.g. `"Invalid Syntax"` |
| `email` | TEXT | NULL | The email address |
| `is_derived_email` | INTEGER | 0 | 0 / 1 |

**Current state:** verified contacts only; unverified contacts were backfilled or rejected. See §10 for change-control rules.

The contracts below extend the existing schema, not replace it. New fields are additive.

---

## 1. Validation states

The SMTP validation pipeline returns one of these `smtp_validation_status` values. These are the **actual strings stored in the database**.

| Status | Meaning | Set by | `validation_method` | Confidence | Exportable |
|---|---|---|---|---|---|
| `NULL` | Not validated (or chain could not derive a candidate) | insert, any path that skips validation | `NULL` | 0.0 | No |
| `"Okay to Send"` | SMTP confirmed the mailbox exists OR manual override | `resolve_and_validate_email()` / `check_email()` / `api_manual_validate_contact` | `smtp_live`, `smtp_cached`, or `manual_override` | 0.9+ / 1.0 | Only if `email_ready_for_export = 1` |
| `"Do Not Send"` | Mailbox does not exist, domain has no MX, disposable, or catch-all | `resolve_and_validate_email()` / `check_email()` | `smtp_live` or `smtp_cached` | 0.95 / 0.4 | No |
| `"Maybe"` | Soft SMTP response (greylist/defer), connection failure, or other transient | `resolve_and_validate_email()` / `check_email()` | `smtp_live` or `smtp_cached` | 0.3–0.5 | No |

**Note on Phase 0/1 documentation drift:** The original 9-state enum (`not_validated`, `smtp_testing`, `valid`, `catch_all`, `risky`, `invalid`, `timeout`, `manual_override`, `manual_invalid`) described intent, but the runtime stores only `NULL`, `"Okay to Send"`, `"Do Not Send"`, and `"Maybe"`. `catch_all`, `risky`, and `timeout` all collapse to `"Do Not Send"` or `"Maybe"` at persistence time; the distinction lives in `validation_response` and `validation_confidence`.

### Validation fields (11 columns on `contacts`)

- `smtp_validation_status` — actual status string (see table)
- `smtp_validated_at` — ISO 8601 timestamp
- `smtp_validation_code` — SMTP response code when applicable
- `email_ready_for_export` — `1` only when status is `"Okay to Send"`
- `email_rejected_reason` — human-readable reason when status is `"Do Not Send"`
- `validation_confidence` — 0.0..1.0
- `validation_method` — `smtp_live`, `smtp_cached`, `manual_override`
- `validation_checked_at` — ISO 8601 timestamp
- `validation_mx_host` — MX server used
- `validation_response` — SMTP response text / analysis summary
- `validation_latency_ms` — wall time of the validation step

### Single source of truth

`write_contact_validation()` (in `lf_db.py`) is the only function that may write the 11 validation columns. Callers pass a `ValidationResult` dataclass; the function maps the result to the columns and updates `contacts` in one statement. No other code writes these columns directly.

### Cache rules

- `get_cached_validation(email)` returns the most recent non-NULL result for that normalized email address (lowercased, stripped) within `cache_ttl_days`.
- A cache hit skips SMTP live probing and sets `validation_method='smtp_cached'`.
- Cache entries are keyed by the normalized full email address (lowercased, stripped) as the `email` PRIMARY KEY of `email_validation_cache`, and store `status`, `analysis`, `smtp_probed`, `smtp_code`, `mx_host`, `catch_all`, `validated_at`, plus the Phase 1 extended fields `validation_confidence`, `validation_method`, `validation_checked_at`, `validation_mx_host`, `validation_response`, `validation_latency_ms`.

---

## 2. Email pattern derivation and storage

### Where patterns come from

1. **Manual / plugin push:** a contact arrives with an explicit email. No pattern derivation is needed.
2. **AI inference (`_infer_pattern_v2`):** uses `ai_infer_email_pattern_v2()` and falls back to SearXNG + website discovery. Discovered website is persisted to the company row.
3. **Search discovery (`discover_email_pattern`):** SearXNG-only fallback when AI is unavailable.

### Pattern storage

- Patterns are stored on `companies.email_pattern`, `email_pattern_confidence`, `email_pattern_source`.
- Confidence 1.0 means independently validated (SMTP or multiple corroborating sources); <1.0 is estimated.
- `derive_emails_for_company()` reads the pattern and generates candidate emails. It does NOT validate. Validation is the responsibility of the unified chain (§3).

### Domain verification (Phase 1)

The company's **confirmed email domain** is the source of truth for all pattern inference and email derivation. The following columns record the domain verification state:

- `companies.email_domain_confirmed` — bare domain confirmed as the real email domain.
- `companies.email_domain_source` — `google_places` or `stored`.
- `companies.email_domain_checked_at` — ISO 8601 timestamp.
- `companies.email_domain_evidence` — JSON evidence object (Google Places candidates, MX result, homepage emails, search emails, corroboration count).
- `companies.email_domain_mismatch` — `1` when the confirmed domain differs from the stored `website` domain; the row is updated to `https://<confirmed_domain>` and the old value is logged in `manual_notes`.

Rules:

1. `_infer_pattern_v2()` and `derive_emails_for_company()` use `email_domain_confirmed` first; if it is empty they fall back to `website`.
2. A domain is confirmed only when it has at least one corroboration: MX record, published `@domain` email on homepage, or published `@domain` email found via web search.
3. Aggregator / 3rd-party domains (Yelp, LinkedIn, Facebook, Crunchbase, etc.) are rejected as email domains.
4. No pattern may be stored for a company whose domain cannot be confirmed; such companies must be resolved via engagement testing on the lowest-ranked employee after explicit human approval.

### Derived email rules

- `is_derived_email=1` when the email was generated from a pattern.
- `is_derived_email=0` when the email came from a plugin push, manual entry, or popup override.
- Changing a derived email via `api_patch_contact` clears `is_derived_email=0` and sets `is_manually_edited=1`.

---

## 3. Unified email validation chain

`resolve_and_validate_email(contact_id, *, source, popup_email=None, force_revalidate=False)` is the single entry point for all email validation.

### Chain steps

1. Load contact + company.
2. Guard against manual edits: if `is_manually_edited=1` and `force_revalidate=False`, return the existing status without re-probing.
3. Pre-check existing state / cache.
4. Determine the email pattern: use existing company pattern or infer via `_infer_pattern_v2`.
5. Derive the candidate email.
6. Check `get_cached_validation` for a fresh cached result.
7. Run `_smtp_validate()` (2-probe: random local part + actual local part).
8. Persist via `write_contact_validation()` and `set_cached_validation()`.

### Who calls it

- `lf_server.py` plugin ingest paths (after `update_existing`, `new_contact_existing_company`, `new_company_new_contact`).
- `lf_server.py` `/api/email/validate-batch` background job.
- `lf_server.py` `api_manual_validate_contact` for `manual_override`.
- `lf_executives.py` search ingest after `upsert_contact`.

---

## 4. Plugin ingest contract

`/api/inject/linkedin-profile` accepts a LinkedIn profile pushed by the Chrome extension and resolves to one of four actions:

| Action | When it happens |
|---|---|
| `update_existing` | LinkedIn slug matches an existing contact, OR name+company match is strong. |
| `new_contact_existing_company` | No contact match, but the company matches an existing company. |
| `new_company_new_contact` | No contact and no company match. |
| `discard` | User chose to discard; only `pending_pushes` row is written. |

### Required fields

`linkedin_url`, `linkedin_slug`, `full_name`, `first_name`, `last_name`, `current_title`, `current_company`, `location`, `experience` (array of current-role entries). Optional: `email`, `phone`.

### Deduplication

If the matcher selects `new_contact_existing_company` or `new_company_new_contact` but `get_contact_by_linkedin_slug(slug)` already exists, the server reroutes to `update_existing` for that existing contact.

### Identity verification

Plugin-pushed contacts receive `pipeline_stage='discovered'` by default. They must pass the identity verification escalator before email validation is considered authoritative. Test fixtures must set `is_test=1`.

---

## 5. Matching engine priorities

`lf_matcher.py` resolves plugin pushes in this priority order:

1. **LinkedIn slug match** — immutable, strongest.
2. **Normalized name + same company** — exact company-name match.
3. **Normalized name + fuzzy company match** — Levenshtein distance ≤ 3.
4. **Validated email match** — only if the existing contact has a validated email matching the pushed email.

The popup (`static/chrome-extension/popup.js`) calls `/api/inject/linkedin-profile/match` to get candidates and pre-selects the strongest action. Manual edits to `full_name` or `current_company` re-run the matcher.

---

## 6. Export rules

- `/api/export/contacts/{session_key}` returns contacts in a session.
- CSV export in `static/contacts.html` filters to `smtp_validation_status === 'Okay to Send' && email_ready_for_export`.
- `/api/email/ready-for-export` returns the same filtered set.

---

## 7. Manual override

`POST /api/contact/{id}/manual-validate` marks an email as known-good or known-bad with `validation_method='manual_override'`. It requires a reason, which is stored in `manual_notes`. The row then counts as validated and is exportable if marked valid.

---

## 8. Chrome extension distribution

The extension lives at `static/chrome-extension/`. The user installs it unpacked from `chrome://extensions/`. The server serves:

- `/finder/extension` — installation docs page.
- `/extension/manifest.json` — sanity-check link.
- `/extension/filecheck` — lists files in the extension directory.

**Notes:**
- Instant updates during development: refresh the extension on `chrome://extensions/` after each code change.
- "Developer mode" warning is harmless and persistent.
- For multi-account setups, install on each profile that has LinkedIn access.

---

## 9. Lead Seeker data reference (Phase 0) and engagement proof rules

For future reference, the Lead Seeker data is at:

- **`/home/anthonyturgman/lead-seeker/leads.db`** — 2838 leads, all with emails.
  - Schema: `leads(FirstName, LastName, Title, Company, Street, City, State, PostalCode, Email, Website, LinkedIn_URL, ...)`
- **`/home/anthonyturgman/lead-seeker/track.db`** — `sends` table. After Phase 0 hardening the `sends` table includes `open_type TEXT` (values `human` or `bot`). Pixel opens from Microsoft OneOutlook / SafeLinks / link-preview bots, curl, and known bot IP ranges are classified as `bot` and are **never** counted as human proof.
  - Schema: `sends(token, email, first_name, last_name, company, subject, hub, sent_at, opened_at, open_count, open_ip, open_ua, open_type, bounce_status, bounce_detail, bounce_at, internet_message_id, poll_until, last_poll_at, delivery_status, delivery_detail)`
- **`/home/anthonyturgman/lead-seeker/SF_leads_v2_with_linkedin.csv`** — 658KB CSV with LinkedIn URLs.

These are the canonical source of "real, deliverable" emails for the SMTP reality check.

### Engagement proof hardening (Phase 0)

A contact may be marked `email_ready_for_export=1` from engagement only when **one** of the following is true:

1. The tracking pixel recorded a **human** open (`open_type='human'` in `track.db` and at least one open event), **and** no bounce was recorded for that token/email.
2. Microsoft Graph delivery polling reports `delivery_status='delivered'` or a non-bounce Sent Items confirmation, **and** no NDR/bounce was recorded.

Bot-only opens (`open_type='bot'`) keep the contact in a pending state; the company pattern is **not** promoted. Bounces override opens and mark the candidate `Do Not Send`.

The engagement verification bridge (`scripts/engagement_verify.py`) enforces this by reading `track.db.open_type` and `bounce_status`/`delivery_status` before promoting any pattern or contact.

---

## 10. Change-control contract (added v1.5)

The codebase contains a **PRIME baseline** for each core file: the earliest `.bak-pre-*` backup created before any hardening session. These baselines represent a known-good state. Any edit that drops a PRIME import or top-level definition can silently break a feature (e.g. dropping `from lf_matcher import find_matches as find_linkedin_matches` broke `/api/inject/linkedin-profile/match`). This section prevents recurrence.

### 10.1 Protected core files

| File | Why it is protected |
|---|---|
| `lf_server.py` | All API routes, static serving, auth, plugin ingest live here. |
| `lf_executives.py` | Executive discovery + ingest pipeline. |
| `lf_db.py` | Database schema and all CRUD operations. |
| `lf_matcher.py` | Plugin contact/company matching engine. |
| `lf_email_patterns.py` | Email derivation + SMTP validation chain. |
| `lf_agent_verify.py` | Identity verification escalator. |
| `lf_search_providers.py` | Web-search circuit breaker and providers. |
| `lf_config.py` | Configuration loader; env var handling. |

### 10.2 Rules for editing a protected file

1. **Create a backup first** named `.bak-pre-<workstream>-YYYYMMDD-HHMMSS`.
2. **Run `scripts/regression_check.py`** and verify no PRIME imports or top-level definitions were dropped. If the script reports a regression, restore the symbol or create an explicit deprecation shim.
3. **Run `python3 -m py_compile` on the file**.
4. **Run smoke tests** for any endpoint the file touches. Minimum set:
   - `GET /api/health`
   - `GET /api/config`
   - `GET /api-key-loader.js`
   - `GET /api/contacts?limit=5`
   - `POST /api/inject/linkedin-profile/match`
   - `POST /api/inject/linkedin-profile` (E2E fixture)
   - `POST /api/search` (if `lf_search_providers.py` changed)
   - `POST /api/contact/{id}/derive-email` (if `lf_email_patterns.py` changed)
   - `POST /api/contact/{id}/verify` (if `lf_agent_verify.py` changed)
5. **Never remove an import solely because an IDE flags it as unused** without proving the symbol is not referenced via string dispatch, reflection, or an endpoint.
6. **If a symbol is genuinely obsolete**, mark it deprecated in code and remove it in a dedicated cleanup session, not as a side effect of another change.

### 10.3 Regression-check script

`scripts/regression_check.py` compares the current core files against their earliest backup. It fails if any top-level import or top-level function/class definition present in the PRIME baseline is missing in the current file.

Run it before declaring any workstream complete:

```bash
python3 scripts/regression_check.py
```

### 10.4 What to do if a regression is detected

1. Do **not** proceed with the new feature until the regression is fixed.
2. Restore the dropped import/definition, or add a compatibility shim that re-exports the same symbol.
3. Re-run the regression check and the smoke tests.
4. Document the regression and fix in the changelog for the affected workstream.

### 10.5 Practical consequence

A single-line deletion in `lf_server.py` (removing the `lf_matcher` import) disabled the plugin popup's re-match feature and caused duplicate companies. That regression would have been caught by §10.2 rule 2 and rule 4. Future core-file edits must pass the same gate.

---

## 11. Versioning

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-07-24 | Initial contracts. Phase 0 in progress. |
| 1.1 | 2026-07-24 | Aligned validation status names to actual DB column (`smtp_validation_status`, `"Okay to Send"`, `"Do Not Send"`). |
| 1.2 | 2026-07-26 | Phase 5 hardening. Documented `resolve_and_validate_email`, unified chain, actual four stored statuses, cache/dedup rules, `validate-batch` contact-row writes, export filter fix, and plugin slug dedup. |
| 1.3 | 2026-07-26 | Identity verification backfill. Documented `pipeline_stage` lifecycle (`discovered` → `verified` / `escalator_no_commit` / `needs_review`) and the double-confirmation escalator (M1–M5). 41 rejected contacts analyzed and deleted; see `CONTACT_REJECTION_POSTMORTEM.md` for root causes and ingest-time hardening recommendations (H-1…H-7). |
| 1.4 | 2026-07-26 | API key moved from `lf_config.json` and `static/*.html` to `.env` (`LF_API_KEY`). Server exposes `/api/config` and `/api-key-loader.js`; SPA loads key at runtime. Added `static/api-key-loader.js`. `docs/session-prompts/SESSION_2_PLUGIN.md` updated to reference `.env`. |
| 1.5 | 2026-07-27 | Change-control contract added (§10). Core-file edits must pass `scripts/regression_check.py`, preserve PRIME baseline imports/definitions, and include smoke tests. LinkedIn people-search button added to contacts table for rows without `linkedin_url`. Pierre Pethrus duplicate merged; `lf_matcher` import restored after regression. |
| 1.6 | 2026-08-12 | Domain verification hardening (Phase 1/2). Added `companies.email_domain_confirmed/source/checked_at/evidence/mismatch`, `verify_company_domain()` in `lf_search.py`, MX + published-email corroboration, aggregator rejection, and wired confirmed domain into `_infer_pattern_v2()` / `derive_emails_for_company()`. Reset bot-open false positives (companies 603/641) and wrote diagnostics to `discovery_jobs`/`discovery_job_items`. Updated engagement proof rules in §9 and domain rules in §2.
