# Email Pipeline Hardening — Plan & Strategy

**Date:** 2026-07-26
**Status:** Planning only — no code changes yet.
**Scope:** Verify and harden the email pattern + SMTP verification workstream for the
two lead ingestion pathways: (A) lead-finder search, (B) LinkedIn Chrome-extension import.
**Goal:** 100% confidence that every contact leaving the system has been (a) pattern-matched
or derived, (b) SMTP-verified or manually-validated, with no duplicated work across the
two pathways.

---

## 1. Background — what prior sessions already built

Phases 0–4 are complete and documented in `docs/PHASE_*_REPORT.md` and `docs/FINAL_REPORT.md`.
The pipeline architecture that exists today:

| Layer | File | Status |
|---|---|---|
| 2-probe SMTP validator | `lf_email_validator.py:390` `check_email()` | ✅ Built & tested |
| Validation result cache | `email_validation_cache` table + `set_cached_validation` (`lf_db.py:1047`) | ✅ Built, but **read-path is dead** |
| Per-contact persistence | `write_contact_validation` (`lf_db.py:1072`) | ✅ Single source of truth for 11 fields |
| Pattern discovery (AI) | `ai_infer_email_pattern_v2` (called from 4 server endpoints) | ✅ Built |
| Pattern discovery (SearXNG+AI) | `discover_email_pattern` (`lf_email_patterns.py:92`) | ✅ Built |
| Pattern + derive in one call | `discover_and_store_pattern` (`lf_email_patterns.py:311`) | ❌ **Dead code — defined, never called** |
| Bulk derive from stored pattern | `derive_emails_for_company` (`lf_email_patterns.py:404`) | ✅ Built, called from 5 endpoints |
| Auto-validate after derive | `_auto_validate_derived` (`lf_email_patterns.py:450`) | ⚠️ **Wired but gated off** (`auto_validate_on_derive: false`) |
| Manual override endpoint | `POST /api/contact/{id}/manual-validate` (`lf_server.py:1145-1221`) | ✅ Idempotent |
| LinkedIn plugin ingest | `POST /api/inject/linkedin-profile` (`lf_server.py:3509-3827`) | ✅ Built — but **does no email work** |

---

## 2. The spec we're hardening toward (user's words, restated)

> For each pathway (search, LinkedIn import), check if a confident pattern exists. If
> it does, match the contact to the pattern. If it does not, derive the pattern via the
> established pathways. When found, match the contact, then verify via SMTP and persist
> documented results. Ensure no duplication of effort. For LinkedIn, this happens as
> soon as enrichment is completed (i.e., after location is validated).

What this means in implementation terms:

```
For every newly-created contact:
  1. Pattern step
     a. Look up company.email_pattern
     b. If present and confidence >= threshold: use it
     c. Else: call AI/SearXNG to infer pattern, store it
  2. Derive step
     a. Apply pattern to (first, last, domain) -> email
     b. If contact.email already set: skip (idempotency)
  3. Verify step
     a. SMTP 2-probe (check_email)
     b. Persist via write_contact_validation (single source of truth)
     c. Also write to email_validation_cache
  4. Audit step
     a. Record who/what triggered the chain and the final state
```

**This chain runs in exactly one place per pathway. No double-fires, no skipped steps.**

---

## 3. Current state vs spec — gap analysis

### 3.1 Pathway A — Lead-finder search (`api_discover_executives`)

**Current behavior:**
1. `discover_executives_ai_first` produces a verified candidate
2. `upsert_contact` writes the row (`lf_executives.py:1770`)
3. **Stop.** No pattern lookup, no derivation, no SMTP.

**Spec gap:** ❌ Chain is not implemented for this pathway at all.

**Where the chain would hook in:** after the contact row is committed in
`api_discover_executives` (search `lf_server.py` for the route handler that calls
`discover_executives_ai_first`), or — cleaner — at the bottom of
`discover_executives_ai_first` itself so every code path that calls it benefits.

### 3.2 Pathway B — LinkedIn plugin (`api_inject_linkedin_profile`)

**Current behavior:**
1. Step 0: `_resolve_location` (Google Places) — ✅
2. Step 1: `insert_pending_push` (audit)
3. Step 2: `find_linkedin_matches` (4-case, read-only)
4. Step 3-4: create/update contact via 3 branches (`update_existing`,
   `new_contact_existing_company`, `new_company_new_contact`)
5. Step 5: `replace_contact_experience`
6. Step 6: `commit_pending_push`
7. **Stop.** No pattern, no derive, no SMTP.

**Spec gap:** ❌ Chain is not implemented. The user's "as soon as enrichment is completed
(after location is validated)" maps to inserting the chain after Step 4 (contact is in
DB) but before Step 6 (commit). The matcher at Step 2 is read-only, so the chain runs
AFTER the matcher returns and an action is chosen.

**Special case:** when the popup supplies an `email` (the `update_existing` carve-out at
`lf_server.py:3723-3736`), the 11 validation fields are already nulled. The chain must
**use the popup email** as the candidate (not derive from pattern), run SMTP, then
persist. For all other cases the chain derives the candidate from the pattern.

### 3.3 Cross-pathway duplicates

| Concern | Risk | Evidence |
|---|---|---|
| Two `discover_email_pattern` calls for same company | Low | Every caller pre-checks `email_pattern` first |
| Two `derive_emails_for_company` for same contact | None | `WHERE email IS NULL OR email=''` filter (`lf_email_patterns.py:425`) |
| Two SMTP probes for same contact | **High** | `get_cached_validation` is dead code (`lf_db.py:1018`); 4 validation endpoints all re-probe |
| LinkedIn double-fire | **High** | No dedup gate in inject handler; would surface as HTTP 500 on slug UNIQUE |
| `is_manually_edited=1` foot-gun | Medium | `patch_contact` default (`lf_db.py:702-703`) |
| `validate-batch` writes cache but not contact rows | **High** | `lf_server.py:2993-3040` — divergence |

### 3.4 Other gaps

- `discover_and_store_pattern` (`lf_email_patterns.py:311`) is imported at `lf_server.py:2594`
  but never called. Either remove it (dead code) or wire it into both pathways.
- `_auto_validate_derived` is gated by `auto_validate_on_derive: false`
  (`lf_config.json:48`). The gate is appropriate for opt-in, but the spec says it must
  happen automatically — so the gate will need to flip to `true` (with safeguards).
- `email_validation.rotate_sender_identity: false` (`lf_config.json:49`). The sender
  domain pool is one IP (`validator.livesolutionsnow.com`), which is why Spamhaus is
  blocking Outlook targets. This is a Phase 1 carryover and not in scope for this
  hardening pass — but it should be on the Phase 5 list.

---

## 4. Strategy

### 4.1 Principles

1. **One chain, one writer.** The "pattern → derive → SMTP" chain will be implemented
   once as `_resolve_and_validate_email(contact_id, source)` in `lf_email_patterns.py`
   (or a new `lf_email_resolver.py` if size warrants). Every pathway calls it.
2. **`write_contact_validation` is the single writer** for the 11 contact-row
   fields. No endpoint bypasses it. `api_manual_validate_contact` will be
   refactored to call `write_contact_validation(contact_id, result,
   validation_method='manual_override')` (per decision §5.2).
3. **DB cache is consulted before every probe.** `get_cached_validation` becomes
   a real pre-check in `_resolve_and_validate_email`. If a fresh cache hit exists,
   we re-apply the result via `write_contact_validation` (cheap) and skip SMTP.
4. **Idempotency at the contact level.** If a contact already has a non-NULL
   `smtp_validation_status` and the source is not "force_revalidate", the chain
   short-circuits.
5. **The chain returns a structured result** so each caller can log/audit it.
6. **No background-job magic for the LinkedIn path.** The user said "as soon as
   enrichment is completed" — i.e., synchronously, in the same request
   (per decision §5.1). Acceptable worst case is ~11s p95 (3s pattern inference
   + 8s 2-probe SMTP). The chain must report timing so the front end can show
   progress if needed.
7. **Refactor as we go (user directive, 2026-07-26).** This is not a "patch and
   ship" pass. When a gap is fixed, the surrounding code should be left cleaner
   than we found it: dead code removed (not just orphaned imports), divergent
   writers unified (not just the new one added), foot-gun defaults fixed at the
   definition site (not just at the call sites that trip them), endpoint
   duplication consolidated where multiple endpoints do the same thing under
   different flags. Each stage's acceptance criteria includes a "no newly-dead
   code, no newly-orphaned imports" check. If a refactor crosses an AOM §16
   escalation line (3+ services, secrets, irreversible), stop and ask first.

### 4.2 The unified chain (proposed signature)

```python
# lf_email_patterns.py (or new lf_email_resolver.py)
def resolve_and_validate_email(
    contact_id: int,
    *,
    source: str,                 # "search_ingest" | "linkedin_plugin" | "manual" | "backfill"
    popup_email: str | None = None,  # LinkedIn path: user-supplied email if any
    force_revalidate: bool = False,
) -> ResolutionResult:
    """One chain for both pathways. Returns a structured result."""

@dataclass
class ResolutionResult:
    contact_id: int
    pattern_source: Literal["existing", "ai_inferred", "search_searxng", "none"]
    pattern: str | None
    derived_email: str | None
    validation_method: str       # "smtp_2probe" | "manual_override" | "cache_hit" | "skipped"
    smtp_validation_status: str | None
    email_ready_for_export: bool
    duration_ms: int
    notes: list[str]
```

### 4.3 Order of work

The work is staged so each phase produces something independently testable. We do not
advance to a phase until the previous one's tests are green.

| Stage | What | Test surface |
|---|---|---|
| **H0** | Read & verify | No code. Confirm the audit is right, re-read the two `backup-20260724` files, re-grep for every name in §3. |
| **H1** | Repurpose `discover_and_store_pattern` as chain primitive | Replace its body with the chain logic (pattern → derive → SMTP). Delete `_auto_validate_derived`. Hook `get_cached_validation` as the pre-check. Per decision §5.3. |
| **H2** | Build & unit-test `resolve_and_validate_email` | Pure function + unit tests with a mocked `check_email`. Covers: existing pattern, no pattern, popup email, cache hit, manual override short-circuit, is_manually_edited=1 guard. |
| **H3** | Wire Pathway A (search) | Hook into the route that returns from `discover_executives_ai_first`. Flip `auto_validate_on_derive` to `true` in `lf_config.json` per decision §5.4. |
| **H4** | Wire Pathway B (LinkedIn, sync all steps) | Hook into `api_inject_linkedin_profile` after Step 4 (contact in DB), before Step 6 (commit). Branch on `update_existing` with `popup_email` vs. without. Worst case ~11s p95. |
| **H5** | Dedup gates | `pending_pushes` consulted as a 30s dedup gate (per decision §5.5). `get_cached_validation` pre-check at the top of every `check_email` call site. `validate-batch` patched to also write contact rows (per decision §5.6). |
| **H6** | Idempotency for `is_manually_edited=1` | Chain skips entirely if flag is set (per decision §5.7). The flag is only cleared by explicit user action or by the plugin's `update_existing` (which already sets it to 0 correctly). **H0 finding:** `lf_audit_backstop.py:143` is a live victim — it currently gets `is_manually_edited=1` stamped on every audit-downgraded contact because it calls `patch_contact` without passing the flag. Fix the foot-gun at the definition site (`lf_db.py:700-703`: remove the `elif` fallback, require explicit) AND fix `lf_audit_backstop.py` to pass `is_manually_edited=0` (it's an automated pipeline, not a manual edit). |
| **H7** | Refactor `api_manual_validate_contact` to use `write_contact_validation` | Per decision §5.2. Pure refactor; behavior unchanged. Verify t4.2 lifecycle test still 3/3. |
| **H8** | End-to-end tests | Re-run the t4.1 (20-profile E2E) and t4.2 (SMTP lifecycle) suites. Add a t5.1 search-ingest-E2E and t5.2 plugin-E2E that exercise the full chain. |
| **H9** | Backstop | `lf_audit_backstop.py` extension to flag any contact with `email IS NOT NULL AND smtp_validation_status IS NULL` that is older than N hours. |
| **H10** | Docs | Update `CONTRACTS.md` to v1.2: add `resolve_and_validate_email` to the API surface, document the new dedup rules, document the `pending_pushes` 30s dedup window. Update `PLAN.md` to mark Phase 5 (hardening) complete. |

### 4.4 Risks we'll be watching

- **Latency budget (resolved: sync all steps).** A pattern inference call
  (`ai_infer_email_pattern_v2`) can take 1-3s (AI call), plus a 2-probe SMTP
  can take 3-8s. Worst case for the LinkedIn plugin ingest is 11s. We accept
  this per decision §5.1 — the chain reports timing so the front end can show
  progress. We may revisit if the popup UX suffers.
- **Spamhaus block.** Probe IP 47.34.180.80 is on Spamhaus. Outlook-hosted targets
  return a `5.7.1 Service unavailable; Client host blocked` regardless of mailbox
  existence. This is a known Phase 1 carryover. Out of scope for this hardening.
  But: a high percentage of false-invalid results is the failure mode we need to
  flag in the audit backstop (H9).
- **Catch-all domains.** Cannot be SMTP-validated. The `validation_method='catch_all'`
  result is correct, but `email_ready_for_export` is `False`. We need to make sure
  the chain writes this honestly and not as a validation failure.
- **Rate limits.** `MXRateLimiter` is per-MX-host. If we fire the chain on every
  plugin push, we'll burst on big companies' MX hosts. The chain must serialize
  per-MX (the existing rate limiter does this) and we must add a circuit breaker
  if the per-second budget is exceeded (return `validation_method='skipped_rate_limit'`
  and queue for a backfill pass).

---

## 5. Open decisions (RESOLVED 2026-07-26)

1. **H4 latency budget — sync, all steps.** The LinkedIn inject request will
   synchronously run pattern (if missing) → derive → SMTP. Worst case ~11s p95
   (3s pattern inference + 8s 2-probe SMTP). Matches the user's spec ("as soon
   as enrichment is completed"). The popup UX already handles long HTTP responses
   (the location resolution step can take ~2s today).
2. **`api_manual_validate_contact` — refactor to `write_contact_validation`.**
   Manual override becomes a regular call to `write_contact_validation(contact_id,
   result, validation_method='manual_override')`. Single writer everywhere.
3. **`discover_and_store_pattern` — repurpose as the chain primitive.** Confirmed
   with user: LinkedIn never supplies an email, so the chain is what populates
   the `email` column. The current function name is kept (it does what its name
   says, just with the SMTP step appended) and its body is replaced with the
   chain logic. `_auto_validate_derived` is deleted.
4. **`auto_validate_on_derive` — flip to `true` in H3/H4.** The chain is the
   user's intent; the flag becomes a backstop for the legacy derive path.
5. **`pending_pushes` dedup window — 30 seconds.** Catches accidental
   double-clicks and very short replays. A reload a minute later is a legitimate
   new enrichment (location/title may have updated).
6. **`validate-batch` — patch to also write contact rows.** Removes the silent
   divergence. ~20-30 lines of change (H0 refinement: the handler takes email
   strings, not contact IDs, so patching it requires either a contact
   lookup-by-email or an API change to accept contact IDs).
7. **`is_manually_edited=1` — skip the entire chain.** Hand-curated contacts
   are hand-curated. Chain only runs on explicitly-requested
   `/api/contact/{id}/validate-email` for those.

---

## 6. What I'm NOT doing in this session

- No code changes.
- No config changes (`auto_validate_on_derive` stays `false` until H3/H4).
- No DB schema changes (everything we need is already there from Phase 1).
- No new dependencies.

## 7. What I AM doing in this session (and next)

- This plan document (saved to `docs/PHASE_5_HARDENING_PLAN.md`).
- The audit in §3, which is the basis for the gap analysis.

**Session boundary:** This session is review + planning only. Per your instruction
("No code changes should take place at this junction"), H1 onward is deferred to
the next session.

**Next session will start with H0 → H1:**
- H0: re-read the two `backup-20260724` files for any code paths I missed in the
  audit, and re-grep for every name in §3 to confirm the call-site inventory.
- H1: replace the body of `discover_and_store_pattern` with the chain logic,
  delete `_auto_validate_derived`, wire `get_cached_validation` into the chain
  as the pre-check. Backup the file first (we already have
  `lf_email_patterns.py` as of 2026-07-24, but a fresh pre-H1 backup is good
  hygiene).

**H1 acceptance criteria:**
- `discover_and_store_pattern(contact_id, source=..., popup_email=..., force_revalidate=False)`
  exists with the signature in §4.2 and is the single entry point for both pathways.
- `_auto_validate_derived` is gone (or is a no-op shim that delegates to the chain).
- `get_cached_validation` is called at the top of the chain.
- Existing callers of `derive_emails_for_company` (5 endpoints) still work
  (they call the underlying derive logic, not the chain).
- `lf_email_patterns.py` has a fresh `*.bak-pre-phase5` companion.
- Unit tests pass for: existing pattern path, no-pattern path, popup email path,
  cache hit path, manual override short-circuit, is_manually_edited=1 skip.
