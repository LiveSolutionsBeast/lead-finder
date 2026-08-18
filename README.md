# Lead Finder

**Status:** Active. Default branch: `refactor/enrichment-pipeline`.

## Read these first

1. **[docs/REVISION_WORKFLOW.md](docs/REVISION_WORKFLOW.md)** — how to make and commit changes. **Start here.**
2. **[REVISION_CHECKLIST.md](REVISION_CHECKLIST.md)** — copy-paste pre-commit / PR-body checklist.
3. **[docs/CONTRACTS.md](docs/CONTRACTS.md)** — shared schema and contracts. Source of truth for the database.
4. **[docs/PLAN.md](docs/PLAN.md)** — current project plan and open questions.
5. **[docs/PLAN-smtp-repair-2026-08-17.md](docs/PLAN-smtp-repair-2026-08-17.md)** — the SMTP repair workstream that was merged via PR #1.

## Repo layout

```
lf_*.py                # core modules (server, db, patterns, validator, search, ai, etc.)
scripts/               # CLI / batch scripts (engagement_verify, regression_check, ...)
tests/                 # pytest suite
static/                # HTML UI (contacts, emails, companies, audit, sessions, index)
docs/                  # plans, contracts, post-mortems, workflow
sample-files/          # import templates
backups/, .bak-*       # gitignored — never tracked
logs/                  # gitignored — runtime logs
exports/               # gitignored — CSV exports
.env                   # gitignored — local secrets
```

## Quick health check

```bash
git status                                    # must be clean
git pull --ff-only origin refactor/enrichment-pipeline
python3 -m pytest tests/ -x --tb=short       # must be green
```

## Last significant workstream

**PR #1** (merged 2026-08-18): SMTP repair — domain-first pattern proof, `email_send_queue` table, 15s throttle, send-ready UI. Commit `9db34e2` on `refactor/enrichment-pipeline`.
