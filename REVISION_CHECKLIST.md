# Lead Finder — Revision Checklist

**Companion to [`docs/REVISION_WORKFLOW.md`](docs/REVISION_WORKFLOW.md).** Copy-paste this into the PR body and fill in each section.

---

## PR Title

```
<type>(<scope>): <short summary, max 72 chars>
```

Use `feat`, `fix`, `refactor`, `docs`, `chore`, `test`, `perf`, or `style`.

---

## Section 1 — User Intent

What does the user (or the agent session) want? 1–3 sentences. Use the user's words verbatim if possible.

Example:
> User wants the Slides page to no longer emit a runtime error when the deck has no image. The error is `TypeError: cannot unpack non-iterable NoneType object` in `lf_server.py:1842`.

---

## Section 2 — Current State

What is happening right now? Include:

- Files / functions affected (with `file:line` refs).
- The exact error or behavior, with a stack trace if relevant.
- Why the current behavior is wrong.

---

## Section 3 — Changes

What did this PR change? Use bullet points, one per logical edit. Group by file.

Example:

```
### lf_db.py
- Add `email_send_queue` table (id, contact_id, email, status, ...)
- Add `insert_email_send_queue()`, `get_send_ready_contacts()`, `remove_from_send_queue()`

### lf_email_patterns.py
- Stage `Okay to Send` derived emails into `email_send_queue` via `resolve_and_validate_email()`
- Add `validate dates before caching` check to prevent stale 2026-07 entries

### lf_server.py
- Add `POST /api/contacts/queue-smtp-validation` (background job, 15s spacing)
- Mark `POST /api/contacts/bulk-validate-emails` as deprecated
```

---

## Section 4 — Tests

How was this verified? List commands and their output.

```
- [x] `python3 -m py_compile lf_db.py lf_email_patterns.py lf_server.py` — silent
- [x] `python3 -m pytest tests/` — 9 passed
- [x] `git diff --check` — clean
- [x] `ruff check` — no new issues
```

If a test was updated intentionally (e.g., to mock a new module), call it out.

---

## Section 5 — Risk

What could break? Tag the risk level.

- **Low** — additive only, no behavior change for existing callers.
- **Medium** — schema change, new endpoint, behavior change in an existing function.
- **High** — deletes data, changes contracts, requires migration.

For Medium/High, list the affected callers and their migration path.

---

## Section 6 — Backout

How do we revert this PR? Single sentence.

Example:
> Revert the merge commit; no schema migration needed because the new table is empty and the new columns are NULL-able.

Or:
> `git revert <merge-commit-sha>` — drops the new endpoints and the new columns. Existing email-validation pipeline is unaffected.

---

## Section 7 — Checklist (copy-paste)

- [ ] Branch is `feat/`, `fix/`, `refactor/`, `docs/`, `chore/`, or `test/` named.
- [ ] Each commit uses conventional-commit format.
- [ ] `python3 -m py_compile` is silent on all changed `.py` files.
- [ ] `git diff --check` is empty.
- [ ] No `.bak-*`, `.db`, `.db.bak-*`, `.json` log, or `.env` files in `git status`.
- [ ] `python3 -m pytest tests/` passes locally.
- [ ] If `CONTRACTS.md` was changed, owner was pinged in `docs/PLAN.md` Open Questions.
- [ ] If `lf_config.json` was changed, new keys are zeroed (no leaked secrets).
- [ ] If a new `pip install` was added, owner approved.
- [ ] PR title is `<type>(<scope>): <summary>`.
- [ ] PR body has all 6 sections above filled in.

---

## Section 8 — Squash-Merge Default

For PRs with a single commit or where the diff is the meaningful unit:

```
gh pr merge <n> --squash --delete-branch
```

For multi-commit workstreams where each commit is independently meaningful (e.g., the SMTP repair commit `9db34e2` covers domain-first proof + send queue + 15s throttle + UI), use merge commit instead:

```
gh pr merge <n> --merge --delete-branch
```

Use your judgement. Default to squash unless the commit history is the documentation of the work.
