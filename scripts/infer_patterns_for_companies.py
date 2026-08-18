#!/usr/bin/env python3
"""
infer_patterns_for_companies.py

Phase 2 extension: run the SMTP-proof pattern inference engine against companies
that have contacts but no stored email_pattern. This lets the proof gate discover
and store a pattern where AI/web-search can suggest one and SMTP proves it.

Workflow:
  1. Find companies with contacts but no email_pattern.
  2. Extract the email domain from the company website (fallback to SearXNG search).
  3. Run _infer_pattern_v2() which internally calls _prove_pattern_for_company().
  4. If a pattern proves, derive emails for all email-less contacts at the company.
  5. Re-validate every contact at the company through the unified email chain.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from lf_db import get_db, init_db
from lf_email_patterns import (
    _infer_pattern_v2,
    derive_email,
    extract_domain_from_website,
    resolve_and_validate_email,
)


def _fmt_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _companies_without_patterns(
    cur: sqlite3.Cursor,
    limit: int | None,
    min_contacts: int = 1,
) -> list[dict]:
    """Return companies with contacts and a website but no stored email_pattern."""
    rows = cur.execute("""
        SELECT
            c.id,
            c.name,
            c.website,
            COUNT(ct.id) AS contact_count,
            SUM(CASE WHEN ct.email IS NOT NULL AND ct.email != '' THEN 1 ELSE 0 END) AS contacts_with_email
        FROM companies c
        JOIN contacts ct ON ct.company_id = c.id
        WHERE c.email_pattern IS NULL
          AND c.website IS NOT NULL AND c.website != ''
        GROUP BY c.id
        HAVING contact_count >= ?
        ORDER BY contact_count DESC, c.id
    """, (min_contacts,)).fetchall()

    companies = [dict(row) for row in rows]
    if limit:
        companies = companies[:limit]
    return companies


def _derive_contacts_for_company(company_id: int, pattern: str, cur: sqlite3.Cursor) -> list[int]:
    """Derive emails for all email-less contacts at the company."""
    rows = cur.execute(
        "SELECT id, first_name, last_name FROM contacts "
        "WHERE company_id=? AND (email IS NULL OR email='')",
        (company_id,),
    ).fetchall()
    affected: list[int] = []
    for contact_id, first_name, last_name in rows:
        email = derive_email(first_name or "", last_name or "", pattern)
        if email:
            cur.execute(
                "UPDATE contacts SET email=?, is_derived_email=1 WHERE id=?",
                (email, contact_id),
            )
            affected.append(contact_id)
    return affected


def main() -> int:
    parser = argparse.ArgumentParser(description="Infer SMTP-proven patterns for companies without one.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to DB.")
    parser.add_argument("--limit", type=int, default=None, help="Only process N companies.")
    parser.add_argument("--min-contacts", type=int, default=1, help="Only process companies with at least this many contacts.")
    parser.add_argument("--use-web-search", action="store_true", help="Allow SearXNG/web-search hints during proof.")
    parser.add_argument("--max-probes", type=int, default=60, help="Max SMTP probes per company pattern proof.")
    args = parser.parse_args()

    init_db()
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    companies = _companies_without_patterns(cur, args.limit, args.min_contacts)
    print(f"[infer] Found {len(companies)} companies without patterns (min_contacts={args.min_contacts})")
    if not companies:
        conn.close()
        return 0

    log: list[dict] = []
    proven = 0
    unproven = 0
    skipped = 0
    derived_total = 0
    revalidated_total = 0
    start_all = time.time()

    for company in companies:
        company_id = company["id"]
        name = company["name"]
        website = company["website"]

        domain = extract_domain_from_website(website or "")
        if not domain:
            print(f"[infer] [{company_id}] {name}: no domain from website; skipped")
            skipped += 1
            log.append({
                "company_id": company_id,
                "name": name,
                "action": "skipped",
                "reason": "no_domain",
            })
            continue

        print(f"[infer] [{company_id}] {name}: domain={domain}")
        pattern: str | None = None
        confidence = 0.0
        try:
            # _infer_pattern_v2 handles AI/web search, SMTP proof, and persistence.
            pattern, confidence = _infer_pattern_v2(
                company_id,
                domain,
                use_web_search=args.use_web_search,
            )
        except Exception as e:
            print(f"  -> inference failed: {e}")
            unproven += 1
            log.append({
                "company_id": company_id,
                "name": name,
                "domain": domain,
                "action": "unproven",
                "reason": f"inference_exception: {e}",
            })
            continue

        entry: dict = {
            "company_id": company_id,
            "name": name,
            "domain": domain,
            "pattern": pattern,
            "confidence": confidence,
        }

        if not pattern:
            print(f"  -> no pattern could be proven")
            unproven += 1
            entry["action"] = "unproven"
            log.append(entry)
            continue

        print(f"  -> PROVEN pattern={pattern} confidence={confidence:.2f}")
        proven += 1
        entry["action"] = "proven"

        if not args.dry_run:
            affected = _derive_contacts_for_company(company_id, pattern, cur)
            conn.commit()
            derived_total += len(affected)
            print(f"     derived {len(affected)} emails")

            # Re-validate every contact with an email at this company.
            contact_rows = cur.execute(
                "SELECT id FROM contacts WHERE company_id=? AND email IS NOT NULL AND email != ''",
                (company_id,),
            ).fetchall()
            for row in contact_rows:
                cid = row["id"]
                try:
                    resolve_and_validate_email(cid, source="infer_patterns_for_companies", force_revalidate=True)
                    revalidated_total += 1
                except Exception as e:
                    print(f"    [warn] validation failed for contact {cid}: {e}")
        else:
            preview = cur.execute(
                "SELECT COUNT(*) FROM contacts WHERE company_id=? AND (email IS NULL OR email='')",
                (company_id,),
            ).fetchone()[0]
            entry["dry_run_derivation_count"] = preview

        log.append(entry)

    conn.close()

    elapsed = time.time() - start_all
    summary = {
        "processed": len(companies),
        "proven": proven,
        "unproven": unproven,
        "skipped": skipped,
        "contacts_derived": derived_total,
        "contacts_revalidated": revalidated_total,
        "elapsed_seconds": round(elapsed, 1),
        "dry_run": args.dry_run,
    }

    print("\n[infer] Summary:")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    log_path = BASE_DIR / "scripts" / f"infer_patterns_for_companies_log_{_fmt_ts()}.json"
    with open(log_path, "w") as f:
        json.dump({"summary": summary, "companies": log}, f, indent=2)
    print(f"[infer] Log written: {log_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
