#!/usr/bin/env python3
"""
apply_proven_patterns.py

Phase 2 follow-up: apply every company's SMTP-proven pattern to all of its
contacts, then re-validate each contact through the unified email chain.

This script does NOT discover new patterns; it only acts on companies where
email_pattern_proof_status is 'Okay to Send' or 'Catch-All'.
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
from lf_email_patterns import derive_email, resolve_and_validate_email


def _fmt_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _companies_with_proven_patterns(cur: sqlite3.Cursor) -> list[dict]:
    return [
        dict(row) for row in cur.execute("""
            SELECT id, name, email_pattern, email_pattern_proof_status,
                   email_pattern_proof_email
            FROM companies
            WHERE email_pattern IS NOT NULL
              AND email_pattern_proof_status IN ('Okay to Send', 'Catch-All')
            ORDER BY id
        """)
    ]


def _clear_derived_at_unproven_companies(cur: sqlite3.Cursor, *, dry_run: bool) -> int:
    """Remove derived emails from companies that no longer have a proven pattern."""
    cur.execute("""
        SELECT ct.id
        FROM contacts ct
        JOIN companies c ON c.id = ct.company_id
        WHERE ct.is_derived_email = 1
          AND ct.is_manually_edited = 0
          AND (ct.email IS NOT NULL AND ct.email != '')
          AND (c.email_pattern IS NULL
               OR c.email_pattern_proof_status NOT IN ('Okay to Send', 'Catch-All'))
    """)
    ids = [r["id"] for r in cur.fetchall()]
    if not dry_run and ids:
        placeholders = ",".join("?" * len(ids))
        cur.execute(f"""
            UPDATE contacts
            SET email = NULL,
                is_derived_email = 0,
                smtp_validation_status = NULL,
                smtp_validated_at = NULL,
                smtp_validation_code = NULL,
                email_ready_for_export = 0,
                email_rejected_reason = NULL,
                validation_confidence = 0.0,
                validation_method = NULL,
                validation_checked_at = NULL,
                validation_mx_host = NULL,
                validation_response = NULL,
                validation_latency_ms = 0
            WHERE id IN ({placeholders})
        """, ids)
    return len(ids)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply proven patterns to all contacts and re-validate."
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write to DB.")
    parser.add_argument("--limit", type=int, default=None, help="Only process N companies.")
    parser.add_argument(
        "--clear-unproven",
        action="store_true",
        default=True,
        help="Clear derived emails at companies without a proven pattern.",
    )
    parser.add_argument(
        "--no-clear-unproven",
        dest="clear_unproven",
        action="store_false",
        help="Do not clear derived emails at unproven companies.",
    )
    args = parser.parse_args()
    start_all = time.time()

    init_db()
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    companies = _companies_with_proven_patterns(cur)
    if args.limit:
        companies = companies[:args.limit]

    print(f"[apply] {len(companies)} companies have proven patterns")

    total_cleared = 0
    if args.clear_unproven:
        total_cleared = _clear_derived_at_unproven_companies(cur, dry_run=args.dry_run)
        print(f"[apply] {total_cleared} derived emails at unproven companies will be cleared")
        if not args.dry_run and total_cleared:
            conn.commit()

    if not companies:
        conn.close()
        elapsed = time.time() - start_all
        summary = {
            "companies": 0,
            "contacts_rederived": 0,
            "contacts_revalidated": 0,
            "contacts_cleared": total_cleared,
            "elapsed_seconds": round(elapsed, 1),
            "dry_run": args.dry_run,
        }
        print("\n[apply] Summary:")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        log_path = BASE_DIR / "scripts" / f"apply_proven_patterns_log_{_fmt_ts()}.json"
        with open(log_path, "w") as f:
            json.dump({"summary": summary, "companies": []}, f, indent=2)
        print(f"[apply] Log written: {log_path}")
        return 0

    log: list[dict] = []
    total_rederived = 0
    total_revalidated = 0

    for company in companies:
        company_id = company["id"]
        pattern = company["email_pattern"]
        status = company["email_pattern_proof_status"]
        print(f"[apply] [{company_id}] {company['name']}: pattern={pattern} proof={status}")

        contacts = cur.execute(
            "SELECT id, first_name, last_name, email, is_derived_email "
            "FROM contacts WHERE company_id=? AND (email IS NULL OR email='' OR is_derived_email=1)",
            (company_id,),
        ).fetchall()

        rederived: list[int] = []
        if not args.dry_run:
            for row in contacts:
                email = derive_email((row["first_name"] or "").strip(), (row["last_name"] or "").strip(), pattern)
                if email and email != (row["email"] or "").strip().lower():
                    cur.execute(
                        "UPDATE contacts SET email=?, is_derived_email=1 WHERE id=?",
                        (email, row["id"]),
                    )
                    rederived.append(row["id"])
            conn.commit()
        else:
            rederived = [row["id"] for row in contacts]

        total_rederived += len(rederived)

        # Re-validate every contact at the company that now has an email.
        all_email_contacts = cur.execute(
            "SELECT id FROM contacts WHERE company_id=? AND email IS NOT NULL AND email != ''",
            (company_id,),
        ).fetchall()

        revalidated = 0
        for row in all_email_contacts:
            cid = row["id"]
            if not args.dry_run:
                try:
                    resolve_and_validate_email(cid, source="apply_proven_patterns", force_revalidate=True)
                    revalidated += 1
                except Exception as e:
                    print(f"    [warn] validation failed for contact {cid}: {e}")
            else:
                revalidated += 1

        total_revalidated += revalidated

        log.append({
            "company_id": company_id,
            "name": company["name"],
            "pattern": pattern,
            "proof_status": status,
            "rederived_count": len(rederived),
            "revalidated_count": revalidated,
        })

    conn.close()

    elapsed = time.time() - start_all
    summary = {
        "companies": len(companies),
        "contacts_rederived": total_rederived,
        "contacts_revalidated": total_revalidated,
        "contacts_cleared": total_cleared,
        "elapsed_seconds": round(elapsed, 1),
        "dry_run": args.dry_run,
    }

    print("\n[apply] Summary:")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    log_path = BASE_DIR / "scripts" / f"apply_proven_patterns_log_{_fmt_ts()}.json"
    with open(log_path, "w") as f:
        json.dump({"summary": summary, "companies": log}, f, indent=2)
    print(f"[apply] Log written: {log_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
