# Lead Finder — Revision Workflow

**Version:** 1.0
**Created:** 2026-08-18
**Status:** Active — applies to all future revisions on `refactor/enrichment-pipeline`
**Owner:** Anthony Turgman
**Companion:** [`REVISION_CHECKLIST.md`](../REVISION_CHECKLIST.md) (copy-paste pre-commit checklist)

This document is the source of truth for **how** to make and commit changes to lead-finder. It is intentionally separate from `CONTRACTS.md` (what the schema/contracts are) and `PLAN.md` (what we are building) — this is **how** we work.

If a session needs to change workflow rules, it must post a flag to `docs/PLAN.md` Open Questions section and wait for the owner to resolve.

---

## 1. Ground rules

1. **Default branch is `refactor/enrichment-pipeline`** — every PR targets it.
2. **All non-trivial work happens on a topic branch** (`feat/`, `fix/`, `refactor/`, `docs/`, `chore/`).
3. **Conventional commits** — every commit message uses `type(scope): summary`. PR titles match the conventional format.
4. **One workstream per branch** — never mix unrelated changes. If a single edit touches two workstreams, split the commit.
5. **The repo must be clean before switch** — `git status` is empty before checkout, branch, or push.
6. **Backup discipline is in the repo, not in the working tree** — `.bak-*` files belong in `backups/` (gitignored), never in branch diffs.
7. **CI is local** — there is no GitHub Actions runner configured. All checks (`py_compile`, `pytest`, `ruff`, `git diff --check`) run on the developer's machine before push.

---

## 2. Branch naming

| Prefix | When to use | Example |
|---|---|---|
| `feat/` | New user-facing functionality | `feat/send-ready-pagination` |
| `fix/` | Bug fixes | `fix/smtp-catch-all-rejection` |
| `refactor/` | Internal cleanup, no behavior change | `refactor/extract-pattern-scorer` |
| `docs/` | Documentation only | `docs/revision-workflow` |
| `chore/` | Tooling, gitignore, build, deps | `chore/gha-pytest-action` |
| `test/` | New tests only | `test/coverage-engagement-verify` |

Date suffix is optional. Use it when the same workstream spans multiple sessions.

---

## 3. Pre-flight — before any code change

```bash
# 1. Confirm clean state
git status                                   # must be empty
git diff --check                              # must be empty

# 2. Confirm we are on the base branch
git branch --show-current                     # should be refactor/enrichment-pipeline

# 3. Pull latest
git pull --ff-only origin refactor/enrichment-pipeline

# 4. Confirm tests pass on the base
python3 -m pytest tests/ -x --tb=short        # must be green

# 5. Create the topic branch
git switch -c fix/your-branch-name
```

**If any of these fails, fix it before starting new work.** Do not pile new edits on a broken base.

---

## 4. While working — local hygiene

### 4.1 Never commit backup files

Backup discipline: keep `.bak-*` files in `backups/` (gitignored), never in the tracked tree. If a tool emits a `.bak` file into the working tree, move it before committing:

```bash
mv lf_*.bak-* backups/
mv lf.db.bak-* backups/
```

### 4.2 Never commit database files

`*.db` and `*.db.bak*` are gitignored. The only DB that should ever be committed is `sample-files/*.db` (if any).

### 4.3 Never commit generated scripts

`scripts/*_log_*.json` and `scripts/*_run_*.log` are gitignored. If a script you wrote emits one, do **not** unignore it — its logs belong in `logs/` (also gitignored).

### 4.4 Never commit `.env` or secrets

`.env` is gitignored. Confirm `.gitignore` covers it before every commit:

```bash
git status --ignored | grep -E '\.env'        # should show .env as ignored
```

If you accidentally staged a secret, follow the [Secret-spill recovery](#9-secret-spill-recovery) section.

### 4.5 Commit atomically

Each commit should compile and pass tests independently. If you are doing a multi-step edit, **commit after each step**:

```bash
git add lf_db.py
git commit -m "feat(db): add email_send_queue table"

git add lf_email_patterns.py
git commit -m "feat(patterns): wire resolve_and_validate_email to stage into queue"
```

### 4.6 Run lint before commit

```bash
# Compile check (must be silent)
python3 -m py_compile lf_*.py scripts/*.py tests/*.py

# Whitespace check (must be silent)
git diff --check

# Prefer ruff if installed
ruff check lf_*.py scripts/*.py tests/*.py
```

---

## 5. Make-your-changes checklist

Run this **before** every commit:

- [ ] `python3 -m py_compile` on every modified `.py` file is silent.
- [ ] `git diff --check` is empty.
- [ ] No `.bak-*`, `.db`, `.db.bak-*`, `.json` log, or `.env` files in `git status`.
- [ ] New public functions/classes have type hints.
- [ ] New SQL goes through `lf_db.py` helper functions — no raw SQL embedded in routes or scripts.
- [ ] New API endpoints have a request/response model in `lf_server.py`.
- [ ] New DB columns are documented in `docs/CONTRACTS.md` §1 (validation fields) or the appropriate section.
- [ ] Tests added for new behavior in `tests/test_<area>.py`.
- [ ] `pytest tests/` passes locally before push.

---

## 6. Commit message format

Use **Conventional Commits** strictly. Allowed types: `feat`, `fix`, `refactor`, `docs`, `chore`, `test`, `perf`, `style`.

### Format

```
<type>(<scope>): <short summary, max 72 chars>

<optional body — why, not what>

<optional footer — BREAKING CHANGE: ..., Refs: ..., Closes: ...>
```

### Scope — short, kebab-case subsystem

| Scope | When |
|---|---|
| `db` | `lf_db.py` schema, helpers, queries |
| `patterns` | `lf_email_patterns.py` |
| `validator` | `lf_email_validator.py` |
| `search` | `lf_search.py`, search provider routing |
| `server` | `lf_server.py` API endpoints |
| `ai` | `lf_ai_enrich.py`, AI chain / model routing |
| `executives` | `lf_executives.py` |
| `ui` | `static/*.html` |
| `scripts` | `scripts/*.py` |
| `tests` | `tests/*.py` |
| `config` | `lf_config.json`, `lf_config.py` |
| `repo` | `.gitignore`, tooling, CI |

### Examples — good

```
feat(smtp): domain-first pattern proof, send queue, 15s throttle, send-ready UI
fix(email): use the params list, not the implicit loop variable, in _infer_pattern_v2
refactor(enrichment): split ai_infer_email_pattern_v2 into quick + deep modes
chore(repo): ignore chrome-extension internal revision snapshots
docs(workflow): add REVISION_WORKFLOW.md and companion checklist
fix(enrichment): repair undefined-name regressions in lf_email_patterns and lf_executives
```

### Examples — bad

```
update                # no type
fix stuff             # no scope, vague summary
WIP                   # not a commit type
asdfasdf              # not meaningful
feat(server): I added some new logic                          # body is "what", not "why"
feat(server): feat(server): ...                              # double prefix
```

### Multi-paragraph bodies

For complex changes, write 2–4 short paragraphs explaining the **why** (not the diff). Reference bugs by PR/issue number. Drop the diff into the PR body, not the commit.

---

## 7. PR workflow

### 7.1 Pre-PR audit

Before pushing and opening a PR, run this from the topic branch:

```bash
# 1. Diff is what you expect
git diff --stat refactor/enrichment-pipeline..HEAD
git log --oneline refactor/enrichment-pipeline..HEAD

# 2. Compile all changed files
git diff --name-only refactor/enrichment-pipeline..HEAD -- '*.py' | xargs python3 -m py_compile

# 3. Whitespace check on the branch
git diff --check $(git merge-base HEAD refactor/enrichment-pipeline)..HEAD

# 4. Run tests
python3 -m pytest tests/ -x --tb=short

# 5. Confirm gitignore is sane
git status --ignored | head -40
```

### 7.2 Push and open PR

```bash
git push -u origin <branch-name>
gh pr create --base refactor/enrichment-pipeline --head <branch-name> \
  --title "<conventional-commit-style title>" \
  --body-file <pr-body.md>   # copy template from REVISION_CHECKLIST.md into pr-body.md, fill in, then `--body-file`
```

### 7.3 PR body — use the checklist

The PR body is the human-readable contract. Use [`REVISION_CHECKLIST.md`](../REVISION_CHECKLIST.md) as the template — it has the sections (`User Intent`, `Current State`, `Changes`, `Tests`, `Risk`, `Backout`) that reviewers expect.

### 7.4 Reviewer rules

- **One reviewer** required for `fix/`, `chore/`, `docs/`, `test/`.
- **Two reviewers** required for `feat/`, `refactor/` that touches `lf_db.py` schema, or anything that changes `CONTRACTS.md`.
- Reviewer must run `pytest tests/` and `git diff --check` locally before approving.
- Reviewer must NOT amend the PR author's commits. Request changes instead.

### 7.5 Merge

```bash
# Squash and merge for single-commit workstreams
gh pr merge <n> --squash --delete-branch

# OR merge commit for multi-commit workstreams where each commit is independently meaningful
gh pr merge <n> --merge --delete-branch
```

Default to **squash** unless the branch has multiple commits that each compile and pass tests independently (e.g., the SMTP repair commit).

### 7.6 Post-merge

```bash
git switch refactor/enrichment-pipeline
git pull --ff-only origin refactor/enrichment-pipeline
git branch -d <branch-name>          # local cleanup
```

---

## 8. Conflict resolution

When `refactor/enrichment-pipeline` moves ahead of your topic branch:

```bash
git switch <topic-branch>
git fetch origin
git rebase origin/refactor/enrichment-pipeline

# If rebase conflicts:
#   1. Fix the file
#   2. git add <fixed-file>
#   3. git rebase --continue
#   4. Repeat until done
#   5. Re-run py_compile + pytest
```

**Never** use `git merge refactor/enrichment-pipeline` into a topic branch — rebase only. Topic branches should be linear.

---

## 9. Secret-spill recovery

If a secret is committed (or staged and committed):

```bash
# 1. Rewind the commit (kept the changes, dropped the commit)
git reset --soft HEAD~1

# 2. Remove the secret from staging
git restore --staged <file-with-secret>
# OR remove the line from the file if the file is otherwise worth keeping

# 3. Reword the commit, recommit without the secret
git commit -m "fix(<scope>): <original summary>"

# 4. Rotate the leaked secret IMMEDIATELY (API key, password, etc.)
# 5. Add the secret-equivalent name to .gitignore if it is a config-key-style field
# 6. Push (force is fine for a private repo, ask before force-pushing a public one)
git push --force-with-lease origin <branch>
```

If the secret was already pushed to `origin`, rotate it AND contact the team. The PR-comment history retains the secret even after rebase.

---

## 10. Common pitfalls

| Pitfall | Symptom | Fix |
|---|---|---|
| Mixed workstreams in one branch | PR review is impossible | Split into separate branches / commits |
| `.bak-*` files in diff | `git diff` shows 100+ deletions | `mv <file>.bak-* backups/` then re-add |
| Database file committed | `git push` is rejected or repo bloats | Confirm `*.db` is in `.gitignore`; remove from index |
| `tests/test_engagement_verify.py` collection fails | `pytest` errors with `ModuleNotFoundError: engagement_verify` | Use `pytest.importorskip("engagement_verify")` — module lives at `scripts/engagement_verify.py` |
| `F821 undefined name` in modified file | `ruff check` flags it | Check that the function is imported in scope before use; reviewer subagent catches this |
| Trailing blank line at EOF | `git diff --check` complains | Remove trailing blank line |
| `lf_config.json` divergence | One branch has `send_throttle_seconds: 15.0`, another has `60.0` | Confirm change applies across all branches before merge |

---

## 11. Tooling reference

| Tool | Command | Purpose |
|---|---|---|
| `py_compile` | `python3 -m py_compile <file>` | Syntax check |
| `pytest` | `python3 -m pytest tests/` | Run all tests |
| `pytest` (single) | `python3 -m pytest tests/test_email_chain.py -v` | Run one file |
| `ruff` | `ruff check <files>` | Lint (F821, F811, unused imports, etc.) |
| `mypy` | `mypy lf_*.py` | Type check (if configured) |
| `git diff --check` | `git diff --check` | Whitespace audit |
| `gitleaks` | `gitleaks detect --source .` | Secret scan (if installed) |
| `gh pr view` | `gh pr view <n>` | PR status |
| `gh pr merge` | `gh pr merge <n> --squash --delete-branch` | Merge and cleanup |

---

## 12. Quick-reference — the full happy path

```bash
# 1. Pre-flight
git status                                                # clean
git pull --ff-only origin refactor/enrichment-pipeline
python3 -m pytest tests/ -x --tb=short                   # green

# 2. Branch
git switch -c fix/my-fix

# 3. Edit + test iteratively
git add -p
python3 -m py_compile <changed>.py
python3 -m pytest tests/test_relevant.py -v

# 4. Commit (with conventional message)
git commit -m "fix(scope): clear summary"

# 5. Push + PR
git push -u origin fix/my-fix
gh pr create --base refactor/enrichment-pipeline \
  --title "fix(scope): clear summary" \
  --body-file <(cat <<'EOF'
## User Intent
...

## Current State
...

## Changes
...

## Tests
...

## Risk
...

## Backout
...
EOF
)

# 6. Review + merge
gh pr merge <n> --squash --delete-branch

# 7. Cleanup
git switch refactor/enrichment-pipeline
git pull --ff-only
git branch -d fix/my-fix
```

---

## 13. When to escalate

Escalate to the owner (post a flag in `docs/PLAN.md` Open Questions) when:

- A change touches `CONTRACTS.md` (schema or contract change).
- A change touches `lf_config.json` provider keys.
- A change introduces a new external dependency (new `pip install`).
- A change breaks an existing test for "intentional" reasons (the test needs to be updated).
- A change requires a database migration (the schema differs from what is on disk).
- A change touches `scripts/engagement_verify.py` and `tests/test_engagement_verify.py` together (modpath resolution).

---

## 14. Version history

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-08-18 | Initial workflow doc — covers pre-flight, branch naming, commit format, PR workflow, conflict resolution, secret spill, common pitfalls. Drafted from the SMTP-repair branch isolation session. |
