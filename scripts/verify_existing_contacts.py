#!/usr/bin/env python3
"""
scripts/verify_existing_contacts.py
=====================================
Contact identity verification backfill. Runs every non-verified contact
through the hardened escalator (lf_agent_verify._escalate_one_contact) so
that each contact's name + title + company get double-confirmation before
any email validation happens.

Selection criteria:
  - pipeline_stage != 'verified' (the 214 'discovered' / 'needs_review' /
    'escalator_no_commit' contacts)
  - Exclude synthetic test names: 'Test%', 'e2e-%', 'Verified Via Plugin%'
    (those are scaffold fixtures, not real people to audit)

The escalator (M1 AI -> M2 SearXNG URL gate -> M2b title snippet gate ->
M3 website directory -> M4 AI refinement -> M5 email) is the double
confirmation: a contact is only marked 'verified' if at least two
independent sources corroborate. The search-provider fallback fix
(lf_search_providers.search now rotates when the preferred provider
returns empty) is what makes M2/M2b usable in this environment.

Resilience:
  - Per-contact try/except: one failure never aborts the batch.
  - Search providers fall back across degoog/fourget/searxng/brave/serpapi.
  - SQLite busy_timeout=10s (set in lf_db.get_db) absorbs lock contention.
  - Optional --limit for smoke tests; --dry-run to preview the selection.

Usage:
    python3 scripts/verify_existing_contacts.py --limit 5    # smoke test
    python3 scripts/verify_existing_contacts.py --dry-run    # list only
    python3 scripts/verify_existing_contacts.py              # full run
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lf_db import get_db  # noqa: E402
import lf_agent_verify as av  # noqa: E402


def fetch_contacts_needing_verification(limit: int | None = None) -> list[dict]:
    """Select contacts not yet through the escalator, excluding test fixtures."""
    conn = get_db()
    rows = conn.execute(
        """
        SELECT c.id, c.full_name, c.ai_verified_title AS title, c.company_id,
               c.linkedin_url, c.email, c.phone, c.location,
               c.confidence_score, c.pipeline_stage, c.data_provenance,
               co.name AS company_name, co.website AS company_website,
               co.email_pattern AS company_email_pattern,
               co.state AS company_state, co.lat AS company_lat, co.lng AS company_lng
        FROM contacts c
        JOIN companies co ON c.company_id = co.id
        WHERE c.pipeline_stage != 'verified'
          AND c.full_name IS NOT NULL AND c.full_name != ''
          AND c.full_name NOT LIKE 'Test%'
          AND c.full_name NOT LIKE 'e2e-%'
          AND c.full_name NOT LIKE 'Verified Via Plugin%'
          AND c.full_name NOT LIKE 'T42 %'
        ORDER BY c.confidence_score ASC, c.id ASC
        """,
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        # Skip rows whose company join produced no company_name (orphaned)
        if not d.get("company_name"):
            continue
        out.append(d)
    if limit:
        out = out[:limit]
    return out


def verify_batch(contacts: list[dict], dry_run: bool = False) -> dict:
    total = len(contacts)
    print(f"=== Verifying {total} contacts through the hardened escalator ===")
    if dry_run:
        for i, d in enumerate(contacts, 1):
            print(f"  [{i}/{total}] id={d['id']} {d['full_name']!r} @ {d['company_name']!r} "
                  f"(stage={d['pipeline_stage']}, conf={d['confidence_score']})")
        print("\n[DRY RUN] No escalator runs performed.")
        return {"total": total, "dry_run": True}

    contact_ids = [d["id"] for d in contacts]
    # Use the existing verify-batch infra: it creates a job row, adds items,
    # and runs them through _escalate_one_contact with ThreadPoolExecutor
    # (concurrency from config agent_verify_concurrency, default 3). This is
    # the same path the API uses, so the double-confirmation gate is intact.
    job_id = av.create_verify_batch_job(contact_ids)
    if not job_id:
        print("ERROR: create_verify_batch_job returned None (no contacts matched).")
        return {"total": total, "error": "no job created"}

    print(f"Created verify job {job_id} for {len(contact_ids)} contacts.")
    print("Running escalator (ThreadPoolExecutor, concurrency from config)...")
    started = time.monotonic()
    av.run_verify_batch_job(job_id)
    elapsed = time.monotonic() - started

    # Read the job summary from the DB
    conn = get_db()
    job = conn.execute(
        "SELECT status, results, total, done, failed FROM discovery_jobs WHERE job_id=?",
        (job_id,),
    ).fetchone()
    conn.close()

    summary: dict = {"total": total, "job_id": job_id, "elapsed_seconds": round(elapsed, 1)}
    if job:
        summary["job_status"] = job["status"]
        try:
            results = __import__("json").loads(job["results"]) if job["results"] else {}
        except Exception:
            results = {}
        summary.update(results)
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Job id:             {job_id}")
        print(f"Job status:         {job['status']}")
        print(f"Total processed:    {results.get('total', total)}")
        print(f"Done:               {results.get('done', '?')}")
        print(f"Verified:           {results.get('verified', '?')}")
        print(f"Skipped:            {results.get('skipped', '?')}")
        print(f"Failed (no conf):   {results.get('failed', '?')}")
        print(f"Errors:             {results.get('errors', '?')}")
        print(f"Total elapsed:      {summary['elapsed_seconds']}s")
    else:
        print("WARNING: job row not found after run.")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="List the selected contacts but do not run the escalator")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only verify first N contacts (smoke test)")
    args = ap.parse_args()

    contacts = fetch_contacts_needing_verification(limit=args.limit)
    if not contacts:
        print("No contacts need verification. All real contacts are already 'verified'.")
        return 0

    verify_batch(contacts, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())