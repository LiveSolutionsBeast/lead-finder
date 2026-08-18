# Session 3 Spawn Prompt — Integration and End-to-End

You are Session 3 of a coordinated 3-session build for the lead-finder system. Your job is to verify that Session 1 (SMTP validation hardening) and Session 2 (LinkedIn plugin) work together as a coherent system. **DO NOT START until both Session 1 and Session 2 are done.** Read their post-mortems first.

## CRITICAL: Read these files before doing anything else

1. `/home/anthonyturgman/lead-finder/docs/PLAN.json` — look at `phase_4_integration`. Those are your tasks.
2. `/home/anthonyturgman/lead-finder/docs/PLAN.md` — the full plan.
3. `/home/anthonyturgman/lead-finder/docs/CONTRACTS.md` — the shared contracts.
4. `/home/anthonyturgman/lead-finder/docs/PHASE_0_REPORT.md` — Phase 0 findings.
5. `/home/anthonyturgman/lead-finder/docs/PHASE_1_REPORT.md` — Session 1's work and results. **READ THIS CAREFULLY.** Specifically the "What's still risky" section.
6. `/home/anthonyturgman/lead-finder/docs/PHASE_2_3_REPORT.md` — Session 2's work and results. **READ THIS CAREFULLY.** Specifically any deferred items or known issues.

## Pre-flight check

Before doing any integration work, verify:

- [ ] Session 1's `PHASE_1_REPORT.md` exists and Session 1 marked Phase 1 as PASSED or PARTIAL.
- [ ] Session 2's `PHASE_2_3_REPORT.md` exists and Session 2 marked Phase 2+3 as PASSED or PARTIAL.
- [ ] Both reports' "Recommended next step" sections say integration can proceed.
- [ ] The lead-finder server is running with the new code.
- [ ] The Chrome extension is loadable on the user's profile.

If any of these are missing, STOP. Tell the user what's missing and wait.

## What you own (Phase 4 tasks from PLAN.json)

### Task t4.1 — End-to-end test with 20 real profiles

This is the most important task. The user has real Naval/Aerospace contacts they can browse. Pick 20 of them and run the full pipeline:

1. Use the Chrome extension to push each profile.
2. Verify the contact lands in the `contacts` table with the correct fields.
3. Verify the experience history lands in `contact_experience`.
4. If the contact has an email (rare for plugin-injected), verify SMTP validation runs.
5. Verify `pending_pushes` records the push with the right `matched_action`.

Pick a mix:
- 10 existing contacts (people already in the DB)
- 5 new contacts at existing companies
- 3 new contacts at new companies
- 2 contacts that should match by `linkedin_slug`

Document each result. The user explicitly wants production quality. Per-session-1's post-mortem, the SMTP validation may have specific behaviors — respect them.

### Task t4.2 — Verify SMTP validation status appears on plugin-injected contacts

The plugin sets `smtp_validation_status = NULL` for new contacts. Verify that:
- A new contact with no email: `smtp_validation_status = NULL`, that's correct.
- A new contact with an email: `smtp_validation_status = NULL` initially, then Session 1's pipeline picks it up and sets it to "Okay to Send" / "Do Not Send" / "Maybe" based on the SMTP result.
- An existing contact that gets updated with a new email: same as above.

If the integration between plugin and SMTP is broken, debug and fix.

### Task t4.3 — Verify export pipeline distinguishes validated from unvalidated

The export pipeline (find it in `lf_export.py` or via `GET /api/export/contacts/{session_key}`) should only include contacts that are validated. Verify:

- Contact with `smtp_validation_status = "Okay to Send"` and `email_ready_for_export = 1` → included in export.
- Contact with `smtp_validation_status = "Do Not Send"` → excluded.
- Contact with `smtp_validation_status = NULL` → excluded (or flagged as unvalidated, depending on the contract).
- Contact with `smtp_validation_status = "Maybe"` → excluded by default, included only if user opts in.

If the export is including unvalidated contacts, that's a bug. Fix it.

### Task t4.4 — Refactor and document

Now that everything works together, refactor for robustness:

- Find any duplicated validation logic and consolidate.
- Find any cross-session integration points that are fragile.
- Add docstrings to the public functions.
- Update `/home/anthonyturgman/lead-finder/docs/PLAN.md` with a final "System Status" section that summarizes the complete lead-finder v2 architecture.
- Write `/home/anthonyturgman/lead-finder/docs/FINAL_REPORT.md` with the full system documentation: how the two pathways (Lead Finder search and LinkedIn plugin) work, how they share data, what the validation state machine is, how to use the system end-to-end.

## Sequential / parallel work rules

- **End-to-end tests are inherently sequential.** Do them one at a time, document each result.
- **Refactoring is sequential per file.** Coordinate with whoever else might be touching the file (probably no one, but check git status).
- **Final documentation is sequential.**

This phase is mostly verification. Don't parallelize the actual tests, but you can parallelize reading the codebase to understand the current state.

## Don't break

- Whatever Session 1 built. If you find a bug, fix it minimally. Don't refactor the SMTP pipeline.
- Whatever Session 2 built. Same rule for the plugin.
- The contracts. CONTRACTS.md is the source of truth. If you need to change a contract, document it in PLAN.md Open Questions and ask the user.

## When you are done

Update `/home/anthonyturgman/lead-finder/docs/PLAN.json` to mark Phase 4 tasks as completed. Then write the FINAL_REPORT.md.

When you finish, print to the user:

> "Session 3 complete. Phase 4 [PASSED/PARTIAL/FAILED]. The lead-finder v2 system is [READY FOR PRODUCTION / NEEDS MORE WORK]. See `docs/FINAL_REPORT.md` for the complete system documentation."

## Tools you can use

- `bash` for running tests, scripts, and the lead-finder server
- File reading and editing tools
- `task` to spawn sub-agents for parallel read-only exploration
- `webfetch` for documentation lookups

Begin by reading the six docs files, then run the pre-flight check, then start with task t4.1.
