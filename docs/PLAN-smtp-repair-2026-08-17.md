# SMTP Repair Plan — 2026-08-17

## User Intent
1. **Contacts page is the pattern/SMTP control plane.**
   - Selecting contacts and validating should:
     - discover / prove the company's email pattern,
     - derive the contact's email from the proven pattern,
     - run SMTP validation on the derived email,
     - queue the contact as "ready for human send".
2. **Email Validation tab becomes a flat send-ready list.**
   - It lists contacts that have been pattern-generated.
   - Each row shows a queued email + the template that will be used.
   - The user can pick a body template and trigger human send.
3. **Chron spacing must avoid spam flags.**
   - Lower the minimum spacing from 60 s to **15 s** for SMTP probe and send batches.
4. **Domain-first pattern proof.**
   - All pattern inference must first validate the real company domain via web search ("Company name" manufacturer "city, state" + relevant details) before any SMTP proof is attempted. This should be the canonical process.

## Current State
- `lf_email_patterns.py` already has the pattern-proof pipeline (`_infer_pattern_v2`, `_prove_pattern_for_company`, `_prove_pattern_from_search`) and `resolve_and_validate_email()` for the pattern→derive→SMTP chain.
- `lf_search.py` already has `verify_company_domain()` and helpers `_search_published_emails()`, `_homepage_has_domain_emails()`, `_mx_record_exists()` that use Google Places + web search + MX + homepage emails to confirm a domain.
- The **Contacts** page (`static/contacts.html`) currently bulk-validates by running SMTP directly against whatever `contacts.email` is present; it does **not** prove the company pattern first.
- The **Email Validation** page (`static/emails.html`) currently has Quick Validate / Validate Derived / Ready for Export / Cache tabs. The Ready-for-Export list is mostly read-only CSV export.
- Throttle defaults are **60 s** in `scripts/engagement_verify.py` (`throttle_seconds=60.0`) and the `EVSendBody` model in `lf_server.py`.

## Proposed Changes

### 1. Contacts page becomes pattern-proof + derive + SMTP queue endpoint

#### A. New server endpoint
Add `POST /api/contacts/queue-smtp-validation` in `lf_server.py`.

Responsibilities:
1. Accept a list of contact IDs and an optional `force_reprove` flag.
2. For each contact:
   - Load company (`website`, `name`, `city`, `state`, `email_domain_confirmed`, `email_pattern`, `email_pattern_proof_status`).
   - If no confirmed domain, run `verify_company_domain(company_id)` first.
   - If pattern not proven (`email_pattern_proof_status` not in `Okay to Send` / `Catch-All`), run `_infer_pattern_v2(company_id, domain)`.
   - If a proven pattern exists, derive the contact's email with `derive_email()`.
   - If no email could be derived, mark `smtp_validation_status='Do Not Send'` with `email_rejected_reason='no_pattern'`.
   - Run SMTP validation on the derived email using `resolve_and_validate_email(contact_id, source='contact_queue', force_revalidate=False)`.
   - If validation returns `Okay to Send`, set `email_ready_for_export=1` and stage the contact for the send queue (see section 3).
3. Run the whole loop in a background task with a **15 s** sleep between contacts to avoid spam/rate flags.
4. Return `{job_id, total}` immediately.

#### B. Contacts UI changes
In `static/contacts.html`:
- Replace the bulk-bar button label from "Validate Emails" to **"Queue Pattern + SMTP"**.
- Change `bulkValidate()` to POST to `/api/contacts/queue-smtp-validation` and poll the existing validation-job endpoint `/api/email/validate-job/{job_id}`.
- Change the single-contact action button from "SMTP-validate this email" to **"Queue for Send"** and route it through the same endpoint (single-element array).
- Add a toast message summarizing how many contacts were queued / proven / ready.

### 2. All patterns must prove against a real, verified domain

#### A. Domain verification as gate
Modify `lf_email_patterns.py`:
- In `_infer_pattern_v2()`:
  - Keep the existing `email_domain_confirmed` check, but if it is empty, **do not fall back silently to the stored website domain**.
  - Call `verify_company_domain(company_id)` from `lf_search.py` first, then use the returned `confirmed_domain`.
  - If `verify_company_domain` returns no confirmed domain (or `corroborated=False` when `require_corroboration=True`), store the company as unproven and return `(None, 0.0)`.
- In `derive_emails_for_company()`:
  - Same rule: if `email_domain_confirmed` is empty, call `verify_company_domain(company_id)` before running `_prove_pattern_for_company`.
  - Abort derivation if the domain cannot be confirmed.

#### B. Domain search must include company-specific context
`verify_company_domain()` already uses `name`, `city`, `state`, `business_type` in Places queries and web search. We will additionally pass `business_type` and relevant details into `_search_published_emails()` by changing its query list to include:
- `"<company name>" "<city>, <state>" "<industry>" email`
- `"<company name>" manufacturer "<city>, <state>" email`
- `"@{domain}" "<company name>" email`
This makes the process match the user's described search strategy.

### 3. Email Validation tab → flat send-ready list with templates

#### A. New send-queue table
Add `email_send_queue` table in `lf_db.py::init_db()`:
```sql
CREATE TABLE IF NOT EXISTS email_send_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contact_id INTEGER NOT NULL REFERENCES contacts(id),
    company_id INTEGER NOT NULL REFERENCES companies(id),
    queued_email TEXT NOT NULL,
    pattern_template TEXT,
    subject TEXT,
    body_template_path TEXT,
    status TEXT DEFAULT 'queued',   -- queued | sent | failed | skipped
    created_at TEXT NOT NULL,
    sent_at TEXT,
    send_error TEXT
);
CREATE INDEX idx_esq_status ON email_send_queue(status);
CREATE INDEX idx_esq_contact ON email_send_queue(contact_id);
CREATE INDEX idx_esq_company ON email_send_queue(company_id);
```

When `resolve_and_validate_email()` (or the new queue endpoint) marks a contact as `Okay to Send`, also insert a row into `email_send_queue` with `status='queued'`.

#### B. Server API additions
- `GET /api/email/send-ready` — returns flat list of queued rows joined with contacts + companies, optionally filtered by session or company, sorted by `created_at`.
- `POST /api/email/send-ready/{queue_id}/remove` — mark a row `skipped` or delete it (human can remove before sending).
- `POST /api/email/send-batch` — accepts `queue_ids[]` and `body_template_path`, then calls `engagement_verify.send_approved_batch()` or directly sends via the existing `live_solutions_outreach` module, sleeping 15 s between sends. Defaults to `dry_run=True`.

#### C. UI changes in `static/emails.html`
- Remove the old Quick Validate and Validate Derived tabs (or keep them behind a collapsible "Advanced" section).
- Default tab becomes **"Send Queue"** with a flat table:
  - Contact name, title, company, city/state, queued email, pattern template, status, queued_at.
  - Checkboxes to select rows.
  - Template picker (`<select>`) populated from a new `GET /api/email/templates` endpoint that lists files in `LS_TEMPLATE_DIR`.
  - Buttons: **"Refresh"**, **"Dry-Run Send"**, **"Send for Real"**, **"Remove Selected"**.
- Update stats to show counts: queued, ready, sent, failed.

### 4. Relax chron spacing from 60 s to 15 s

Files to update:
- `scripts/engagement_verify.py`: change default `throttle_seconds=60.0` to `throttle_seconds=15.0` in `send_approved_batch()`, `send_cornerstone_batch()`, and the CLI `--throttle` help/default.
- `lf_server.py`: change `EVSendBody.throttle_seconds` default from `60.0` to `15.0`.
- `lf_config.json`: add an optional `send_throttle_seconds` key under `email_validation` defaulting to `15.0`.
- New SMTP validation background job in section 1 should sleep `15.0` seconds between contacts (read from config, minimum 15).

## Implementation Order
1. **Schema**: add `email_send_queue` table to `lf_db.py`.
2. **Domain-first gate**: update `_infer_pattern_v2()` and `derive_emails_for_company()` in `lf_email_patterns.py` to call `verify_company_domain()` when no confirmed domain exists.
3. **Queue write**: modify `resolve_and_validate_email()` in `lf_email_patterns.py` to insert a `email_send_queue` row when status is `Okay to Send`.
4. **Contacts API**: add `POST /api/contacts/queue-smtp-validation` in `lf_server.py` with 15 s spacing.
5. **Contacts UI**: update `static/contacts.html` actions and bulk button.
6. **Email Validation UI**: rewrite `static/emails.html` Send Queue tab and add endpoints.
7. **Throttle**: update 60 s → 15 s defaults across engagement_verify and server.
8. **Test & lint**: run `pytest` and `ruff`.

## Acceptance Criteria
- [ ] Selecting a contact and clicking "Queue for Send" proves the company pattern from a verified domain, derives the email, and SMTP-validates it.
- [ ] Bulk "Queue Pattern + SMTP" on contacts page works and shows progress.
- [ ] Email Validation tab shows a flat list of contacts with derived/validated emails ready for human send.
- [ ] User can select a body template and run dry-run/real send; sends are spaced by at least 15 s.
- [ ] No pattern can be stored without first verifying the real company domain via search.
- [ ] Existing tests pass; new tests added for the queue endpoint and domain-first gate.

## Risks & Notes
- `verify_company_domain()` calls Google Places; ensure API key is present. If not, the function already falls back to stored domain, but we may want to make that fall-back explicit and auditable.
- The existing engagement workflow (`scripts/engagement_verify.py`) has its own queue. We should not duplicate sends; the new `email_send_queue` is for the simpler "human send" path only.
- The 15 s spacing is a minimum; the SMTP validator already has per-MX rate limiting (`per_mx_rate_limit: 5`). Combined, this should avoid spam flags.
- Keep `lf_email_patterns.py` import of `lf_search` lazy or at top level? `lf_search` does not import `lf_email_patterns`, so top-level is safe.
