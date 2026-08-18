# Session 1 Spawn Prompt — SMTP Validation Hardening

You are Session 1 of a coordinated 3-session build for the lead-finder system. Your job is to harden the SMTP email-validation pathway so we can trust its output. Do not start Session 2's work (LinkedIn plugin) or Session 3's work (integration). Stay in your lane.

## CRITICAL: Read these three files before doing anything else

1. `/home/anthonyturgman/lead-finder/docs/PLAN.json` — the machine-readable plan. Look at `phase_1_smtp_hardening`. Those are your tasks.
2. `/home/anthonyturgman/lead-finder/docs/PLAN.md` — the human-readable plan with rationale.
3. `/home/anthonyturgman/lead-finder/docs/CONTRACTS.md` — the shared schema and endpoint contracts. Your SMTP changes must conform to this.
4. `/home/anthonyturgman/lead-finder/docs/PHASE_0_REPORT.md` — what Phase 0 found. The 3 bugs I already fixed are documented there. Do not re-fix them; verify my fixes are present and continue from there.

## CRITICAL: SMTP validation does NOT send real emails. Ever.

**This validator only does RCPT TO probes.** The full SMTP conversation is:

```
C: HELO lead-finder.local
C: MAIL FROM:<verify@lead-finder.local>
C: RCPT TO:<random-or-actual-address>   ← this is all we do
C: QUIT
```

There is no `DATA` command. There is no message body. There is no subject line. There is no actual email sent. The MX server only tells us whether it would accept mail for that address — a read-only operation. This is the standard technique used by ZeroBounce, NeverBounce, Hunter, and every commercial email validator.

**Do NOT add a DATA command. Do NOT add message content. Do NOT add a "send test email" feature.** Those are out of scope. The validator is a non-destructive probe.

The user has previously sent real test emails and was frustrated that the content was poor. That's a separate concern from SMTP validation and lives in the user's actual campaign workflow, not in this module.

If any task seems to require sending a real email, STOP and ask the user. Do not implement it.

## The original you are extending

The SMTP validator was forked from `https://github.com/nimaaksoy/email-validator-app`. The original is 191 lines, single-probe catch-all design. Our current `lf_email_validator.py` is 477 lines and extends the original with: rate limiter, STARTTLS, jittered delay, sender rotation, opt-in flag, 6,776 disposable domains. The 191-line original is at `/home/anthonyturgman/lead-finder/email-validator-fork/email_checker_app.py` for reference.

## What you own (Phase 1 tasks from PLAN.json)

### Task t1.0 — Implement 2-probe validation flow

**Why this is the most important task.** The current design does ONE probe with a random 24-character local-part to detect catch-all. If the probe gets a 550, the system assumes the specific address is fine. This is wrong: the probe only proves the domain isn't catch-all. It does NOT prove the specific address exists. So a real lead like `rick@greenfieldpaper.com` (whose actual mailbox is no longer valid) would be marked "Okay to Send" alongside a fake `definitely-not-a-real-person-9999@gmail.com`.

**Implementation:**
- Keep the existing random-local-part probe to detect catch-all (it's the correct way to do that).
- If the random probe returns 550 (not catch-all), do a SECOND probe with the ACTUAL local-part.
- If the second probe returns 250, the address exists. Status: "Okay to Send".
- If the second probe returns 550 or 553, the address does not exist. Status: "Do Not Send".
- If the second probe returns a soft code (450, 451, 452, 4xx), mark as "Maybe" (risky).
- This is the standard email-validation flow used by ZeroBounce, NeverBounce, etc.

Add a function `verify_specific_address(email, mx_host, ehlo_host, mail_from) -> (code, message)` that does the second probe, then call it from `check_email()` after a successful 550 random-probe.

### Task t1.1 — Already done by me. Verify and move on.

The `validation_status` enum is already documented in CONTRACTS.md section 1. Read CONTRACTS.md to confirm, then move on.

### Task t1.2 — Persist new validation fields

The `contacts` table already has `smtp_validation_status`, `smtp_validated_at`, `smtp_validation_code`, `email_ready_for_export`, `email_rejected_reason`. CONTRACTS.md section 1 says we need ADDITIONAL fields. Run a migration in `lf_db.py` (or wherever schema migrations live) to add:

```sql
ALTER TABLE contacts ADD COLUMN validation_confidence REAL NOT NULL DEFAULT 0.0;
ALTER TABLE contacts ADD COLUMN validation_method TEXT;  -- 'smtp_live' | 'smtp_cached' | 'manual_override'
ALTER TABLE contacts ADD COLUMN validation_mx_host TEXT;
ALTER TABLE contacts ADD COLUMN validation_response TEXT;
ALTER TABLE contacts ADD COLUMN validation_latency_ms INTEGER;
```

Update `ValidationResult` dataclass in `lf_email_validator.py` to populate these. Update the contact-update paths (find them by searching for `smtp_validation_status` in the codebase) to write all the new fields.

### Task t1.3 — Re-validate the 23 existing "Okay to Send" contacts

The current database has 23 contacts with `smtp_validation_status = "Okay to Send"`. These were marked by the buggy single-probe design BEFORE Phase 0 fixed the bugs. **Most or all of them are not actually proven deliverable.** Write a script `scripts/revalidate_existing_contacts.py` that:

- Selects all contacts where `smtp_validation_status = "Okay to Send"`
- Runs each through the new 2-probe `check_email()` 
- Updates the contact's `smtp_validation_status` and friends based on the new result
- Prints a summary: how many went from "Okay to Send" to "Do Not Send", how many stayed "Okay to Send", how many went to "Maybe"

This is your first real-world validation of the new design. The results will tell you how bad the old data was.

### Task t1.4 — Manual override mechanism

Add a way to mark a contact as known-good or known-bad without SMTP. Two cases:
- User knows the address works because they sent an email and got a reply (not just an open — a reply)
- User knows the address doesn't work because they got a hard bounce

Add an endpoint `POST /api/contact/{contact_id}/manual-validate` with body `{"status": "valid" | "invalid", "reason": "..."}`. Set `validation_method = "manual_override"`, `validation_confidence = 1.0`, `validation_status = "Okay to Send"` or `"Do Not Send"` based on input, and store the reason in `validation_response`.

Add a button on `static/contacts.html` per row for "Mark valid" / "Mark invalid" with a tiny reason field.

### Task t1.5 — Re-run the 20-address Phase 0 test

The same 20 addresses from PHASE_0_REPORT.md (10 real, 5 proven-deliverable, 5 invalid). After your 2-probe changes, run them again. Target: >= 90% accuracy. Specifically:
- All 5 proven-deliverable addresses should be "Okay to Send"
- All 5 invalid addresses should be "Do Not Send"
- Real-leads: at least 7/10 should be "Okay to Send" (some real leads in the test set had invalid mailboxes)

If accuracy is below 90%, find the failing cases and fix. Do not declare done until >= 90%.

### Task t1.6 — 100-address stress test

Sample 100 addresses from `/home/anthonyturgman/lead-seeker/leads.db` (random) plus 20 from `/home/anthonyturgman/lead-seeker/track.db` opens. Run the new design. Print: how many were validated, how many timed out, how many failed, distribution of validation_confidence. Time the whole thing. Per-address latency should be < 5 seconds including jitter. Total batch time should be < 10 minutes for 120 addresses.

### Task t1.7 — Refactor for robustness

- Wrap each SMTP probe in a try/except that catches ALL exceptions (the current code has good coverage but verify).
- Add a per-MX retry with backoff: if the first attempt to a specific MX fails, wait 2 seconds and try once more before giving up.
- Add a graceful timeout: if a single probe takes more than 10 seconds, kill it and mark as "Maybe".
- Verify the existing `MXRateLimiter` actually rate-limits (it's in the current code; check it does what it claims).

### Task t1.8 — POST /api/contact/{contact_id}/manual-validate endpoint

Already covered in t1.4. Just make sure it's wired in `lf_server.py` and `static/contacts.html`.

## Sequential / parallel work rules

- **Read-only work** (reading files, grep, search): parallelize as much as you want.
- **File edits**: only one agent edits any given file at a time.
- **Schema changes**: go through `lf_db.py`. Do not edit the database directly. Use ALTER TABLE migrations.
- **Test runs**: parallel is fine for tests of independent components.

When unclear, do it sequentially. We are not in a race; we are building something we will trust with our sender reputation.

## Don't touch

- `lf_executives.py` (Session 2 owns plugin-adjacent code)
- `static/chrome-extension/` (Session 2 builds this)
- The two new tables in CONTRACTS.md section 3 (`contact_experience`, `pending_pushes`) — these are Session 2's responsibility, but DO add the contacts table columns from CONTRACTS.md section 2 (`linkedin_slug`, `is_manually_edited` already exists) so Session 2 can write to them.

Wait, that last one is a cross-over. **Add the `linkedin_slug` column** as part of t1.2 (or as its own micro-task). The plugin needs to write to it. CONTRACTS.md says:

```sql
ALTER TABLE contacts ADD COLUMN linkedin_slug TEXT UNIQUE;
```

Add it now, even though Session 2 is the one that will populate it. The migration is part of the schema work for both sessions.

## When you are done

Update `/home/anthonyturgman/lead-finder/docs/PLAN.json` to mark Phase 1 tasks as completed. Then write a post-mortem to `/home/anthonyturgman/lead-finder/docs/PHASE_1_REPORT.md` covering:

1. **What you built.** Brief.
2. **Test results.** The 20-address re-run with pass/fail per address. The 100-address stress test summary. The re-validation of 23 existing contacts with the count of how many flipped.
3. **What you found.** Any new bugs, any surprising SMTP server behavior, any rate limiting issues.
4. **What's still risky.** Anything you'd want the user to know before they trust this with their real sender reputation.
5. **Recommended next step.** Whether Session 4 (integration testing) can proceed, or whether more hardening is needed.

When you finish the post-mortem, print a clear summary to the user:

> "Session 1 complete. Phase 1 [PASSED/PARTIAL/FAILED]. See `docs/PHASE_1_REPORT.md`. Ready for Session 2 to start in parallel, and Session 3 to start when Session 2 is also done."

The user (Anthony) will then decide whether to start Session 3.

## Tools you can use

- `bash` for running tests, scripts, and the SMTP pipeline
- File reading and editing tools
- `webfetch` if you need to check the original repo or any documentation
- `task` to spawn sub-agents for parallel read-only exploration (use sparingly; coordinate file edits through the lead agent)

Begin by reading the four docs files, then verify my Phase 0 fixes are present in `lf_email_validator.py`, then proceed to task t1.0 (2-probe flow).
