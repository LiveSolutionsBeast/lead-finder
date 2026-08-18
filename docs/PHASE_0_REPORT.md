# Phase 0 — SMTP Reality Check Report

**Date:** 2026-07-24
**Status:** Complete, **HONEST FINDINGS**
**Verdict:** The SMTP validation pathway is a shell. It does not work end-to-end. Fixing it is the single highest-priority blocker for the entire system.

---

## TL;DR

Three concrete bugs prevent real email validation today. Even after fixing all three, the validation design is incomplete: it only does a catch-all probe, never a probe of the specific local-part. A 2-probe design is required.

**Until SMTP validation is fixed, the export pipeline will produce contacts that may not actually be deliverable.** This puts the user's sender reputation at risk on every send.

---

## What I tested

| Set | Count | Source | Expected |
|---|---|---|---|
| Real leads | 10 | `~/lead-seeker/leads.db` (random sample) | Should validate as Okay to Send |
| Proven deliverable | 5 | `~/lead-seeker/track.db` (distinct emails with `opened_at`) | Should validate as Okay to Send |
| Invalid (control) | 5 | Synthetic: bad syntax, no MX, fake local-part, disposable | Should validate as Do Not Send |

Test method: `check_email()` from `lf_email_validator.py` for each address.

---

## Finding #1: SMTP was disabled in config

**Severity:** CRITICAL — the entire system was a no-op for SMTP.

`lf_config.json` had `"email_validation": { "enabled": false, ... }`. With SMTP disabled, the function short-circuits at line 365-372 and returns `"Okay to Send"` for any address that has an MX record — even `definitely-not-a-real-person-9999@gmail.com`. The 23 "Okay to Send" records in the contacts table are not actually proven deliverable; they are "the domain has an MX record."

**Action taken:** Enabled `"enabled": true` in `lf_config.json`. (Session 1 will need to verify this is the desired state before running real validation.)

---

## Finding #2: Missing `string` import

**Severity:** HIGH — bug, not a design issue.

`lf_email_validator.py` line 267 used `string.ascii_lowercase` but `import string` was missing. With SMTP enabled, every probe threw `name 'string' is not defined`.

**Action taken:** Added `import string` at the top of the file.

---

## Finding #3: `smtplib.SMTP.starttls()` empty hostname error

**Severity:** HIGH — bug, not a design issue.

`smtplib.SMTP(mx_host, timeout=...)` connects but never sets `self._host` if you call `connect()` separately. The `starttls()` method uses `self._host` for SNI, and an empty hostname causes OpenSSL to error: `server_hostname cannot be an empty string or start with a leading dot.`

**Action taken:** Refactored to `smtplib.SMTP(mx_host, timeout=cfg)` (let `__init__` handle connect). `self._host` is now set correctly.

---

## Finding #4: Single-probe design does not validate the specific address

**Severity:** CRITICAL — design issue, not a bug.

`is_catch_all()` does ONE probe with a random 24-character local-part. If the probe gets a 550, the system assumes the domain is "Okay to Send" and infers that the specific address is fine. This is **wrong**: the probe only proves the domain isn't catch-all. It does not prove the specific address exists.

**Test evidence (with all 3 bugs above fixed):**

| Address | Random probe | Specific probe | Correct verdict |
|---|---|---|---|
| `rick@greenfieldpaper.com` (real lead) | 550 | **550 (does not exist)** | Do Not Send |
| `definitely-not-a-real-person-9999@gmail.com` (fake) | 550 | 550 (does not exist) | Do Not Send |
| `james.stachowiak@thermofisher.com` (real) | **250 (catch-all)** | n/a | Do Not Send (catch-all) |
| `noreply@google.com` (control) | **250 (catch-all)** | n/a | Do Not Send (catch-all) |

The current system would have marked `rick@greenfieldpaper.com` as "Okay to Send" — but the actual address does not exist. This is the failure mode the user warned about.

**Required design change:** 2-probe flow. If the random probe returns 550 (domain not catch-all), do a SECOND probe with the actual local-part. If the second probe returns 250, the address exists. If 550/553, it does not. This is the standard email-validation flow used by ZeroBounce, NeverBounce, and similar services.

---

## Recommendations (handoff to Session 1)

In order of priority:

1. **Implement 2-probe validation in `is_catch_all()` or a new `verify_address()` function.** The first probe is the existing random probe for catch-all detection. The second probe tests the actual local-part. Update `check_email()` to use both.
2. **Persist `validation_confidence`, `validation_method`, `validation_checked_at`, `validation_mx_host`, `validation_response`, `validation_latency_ms` on every validation result.** The current `ValidationResult` dataclass has `smtp_code` and `smtp_probed` but not these. See CONTRACTS.md section 1.
3. **Add the manual override mechanism** for cases where SMTP can't reach the server but the user knows the address works. Add a `manual_override` field and a `POST /api/contact/{id}/manual-validate` endpoint.
4. **Re-run the 20-address test from Phase 0 after the 2-probe fix and verify >= 90% accuracy.**
5. **Re-validate the 23 existing "Okay to Send" contacts in the DB with the 2-probe design.** Some of them are probably not real.
6. **Add a 100-address stress test (mix of valid, invalid, catch-all, disposable, malformed).**
7. **Refactor for robustness:** connection retry, graceful timeout, parallel SMTP with backoff, sender-domain rotation (already in config but not exercised).

---

## Files changed in Phase 0

- `/home/anthonyturgman/lead-finder/lf_email_validator.py` — added `import string`, refactored SMTP connection to pass `mx_host` to `__init__`.
- `/home/anthonyturgman/lead-finder/lf_config.json` — `"email_validation.enabled": true`.

These are minimal targeted fixes. The full 2-probe refactor is Session 1's job.
