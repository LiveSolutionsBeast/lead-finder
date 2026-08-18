#!/usr/bin/env python3
"""
scripts/revalidate_existing_contacts.py
======================================
Phase 1 / t1.3 — Re-validate the 23 existing 'Okay to Send' contacts with the
new 2-probe design. These were marked 'Okay to Send' by the old single-probe
catch-all design BEFORE Phase 0. Most or all are not actually proven deliverable.

Output: per-contact verdict (kept/changed) and a summary flip count.

Usage:
    python3 scripts/revalidate_existing_contacts.py
    python3 scripts/revalidate_existing_contacts.py --dry-run   # show what would change, no writes
    python3 scripts/revalidate_existing_contacts.py --limit 5   # only first 5 for quick smoke test
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lf_db import init_db, get_db, write_contact_validation  # noqa: E402
from lf_email_validator import check_email  # noqa: E402

DB_PATH = ROOT / "lf.db"


def fetch_okay_contacts(limit: int | None = None) -> list[dict]:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    sql = """
        SELECT id, full_name, email, company_id, smtp_validation_status,
               smtp_validation_code, smtp_validated_at
        FROM contacts
        WHERE smtp_validation_status = 'Okay to Send'
          AND email IS NOT NULL AND email != ''
        ORDER BY id ASC
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def revalidate(limit: int | None = None, dry_run: bool = False) -> dict:
    contacts = fetch_okay_contacts(limit=limit)
    print(f"=== Revalidating {len(contacts)} 'Okay to Send' contacts ===")
    if not contacts:
        print("No contacts to revalidate.")
        return {"flipped_to_do_not_send": 0, "kept_okay": 0, "flipped_to_maybe": 0,
                "flipped_to_catch_all": 0, "details": []}

    flipped_to_do_not_send = 0
    flipped_to_maybe = 0
    flipped_to_catch_all = 0
    kept_okay = 0
    details = []
    started = time.monotonic()

    for i, ct in enumerate(contacts, 1):
        email = ct["email"].strip()
        print(f"\n[{i}/{len(contacts)}] id={ct['id']} email={email}")
        print(f"    name={ct.get('full_name')!r}")
        print(f"    old: status={ct['smtp_validation_status']} code={ct['smtp_validation_code']}")

        try:
            result = check_email(email)
        except Exception as e:
            print(f"    ERROR: {e}")
            details.append({"id": ct["id"], "email": email, "error": str(e)})
            continue

        result_dict = result.to_dict()
        new_status = result.status
        new_code = result.smtp_code
        print(f"    new: status={new_status} code={new_code} analysis={result.analysis}")
        print(f"         conf={result.validation_confidence} method={result.validation_method} "
              f"mx={result.validation_mx_host} latency={result.validation_latency_ms}ms")

        if new_status == "Okay to Send":
            kept_okay += 1
            category = "kept_okay"
        elif new_status == "Do Not Send":
            if "Catch-All" in result.analysis:
                flipped_to_catch_all += 1
                category = "flipped_to_catch_all"
            else:
                flipped_to_do_not_send += 1
                category = "flipped_to_do_not_send"
        else:
            flipped_to_maybe += 1
            category = "flipped_to_maybe"

        if not dry_run:
            try:
                write_contact_validation(ct["id"], result_dict)
            except Exception as e:
                print(f"    DB WRITE FAILED: {e}")

        details.append({
            "id": ct["id"],
            "email": email,
            "name": ct.get("full_name"),
            "old_status": ct["smtp_validation_status"],
            "new_status": new_status,
            "new_code": new_code,
            "new_analysis": result.analysis,
            "category": category,
            "mx_host": result.validation_mx_host,
            "latency_ms": result.validation_latency_ms,
        })

    elapsed = time.monotonic() - started
    summary = {
        "total": len(contacts),
        "kept_okay": kept_okay,
        "flipped_to_do_not_send": flipped_to_do_not_send,
        "flipped_to_catch_all": flipped_to_catch_all,
        "flipped_to_maybe": flipped_to_maybe,
        "elapsed_seconds": round(elapsed, 1),
        "details": details,
    }

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total revalidated:       {summary['total']}")
    print(f"Kept 'Okay to Send':     {summary['kept_okay']}")
    print(f"Flipped to 'Do Not Send' (not found): {summary['flipped_to_do_not_send']}")
    print(f"Flipped to 'Do Not Send' (catch-all): {summary['flipped_to_catch_all']}")
    print(f"Flipped to 'Maybe':      {summary['flipped_to_maybe']}")
    print(f"Total elapsed:           {summary['elapsed_seconds']}s")
    if summary['total'] > 0:
        flip_pct = 100.0 * (summary['flipped_to_do_not_send'] + summary['flipped_to_catch_all']
                            + summary['flipped_to_maybe']) / summary['total']
        print(f"Flip rate:               {flip_pct:.0f}% (lower is better; reflects old data quality)")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Run checks but do not write to DB")
    ap.add_argument("--limit", type=int, default=None,
                    help="Only revalidate first N contacts (smoke test)")
    args = ap.parse_args()
    init_db()
    summary = revalidate(limit=args.limit, dry_run=args.dry_run)
    if args.dry_run:
        print("\n[DRY RUN] No database writes were performed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
