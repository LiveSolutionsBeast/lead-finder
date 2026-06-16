#!/usr/bin/env python3
"""
Safe, non-destructive email pattern audit script.

This script audits every company that currently has an email_pattern and
compares it against what ai_infer_email_pattern_v2 would discover today.
It NEVER blindly clears patterns. By default it runs in DRY-RUN mode and
only prints a CSV-style report of suggestions. Use --apply to commit
high-confidence corrections.

Rules for auto-apply (--apply):
  - New v2 confidence must be >= 0.85 (direct evidence)
  - New pattern/domain must differ from the existing one
  - Existing pattern confidence must be < 1.0 OR new confidence is higher
  - A backup snapshot is written to lf.db before any changes

Usage:
  python3 scripts/audit_email_patterns.py              # dry run report
  python3 scripts/audit_email_patterns.py --apply      # commit corrections
  python3 scripts/audit_email_patterns.py --company 807 --apply
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lf_db import get_db
from lf_email_patterns import extract_domain_from_website
from lf_ai_enrich import ai_infer_email_pattern_v2


def _backup_db():
    """Snapshot the DB before applying changes."""
    src = Path("lf.db")
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = src.with_suffix(f".bak-audit-{ts}.db")
    dst.write_bytes(src.read_bytes())
    return dst


def audit_company(company: dict) -> dict | None:
    """Run v2 inference for one company and return a suggestion dict."""
    website = (company.get("website") or "").strip()
    if not website:
        return None

    domain = extract_domain_from_website(website)
    if not domain:
        return None

    result = ai_infer_email_pattern_v2(
        domain=domain,
        company_name=company.get("name", ""),
        industry=company.get("business_type", ""),
        city=company.get("city", ""),
        state=company.get("state", ""),
    )
    if not result or not result.get("pattern"):
        return None

    old_pattern = (company.get("email_pattern") or "").strip()
    old_conf = float(company.get("email_pattern_confidence") or 0.0)
    new_pattern = result["pattern"]
    new_conf = float(result.get("confidence", 0.0))

    return {
        "id": company["id"],
        "name": company["name"],
        "website": website,
        "domain": domain,
        "old_pattern": old_pattern,
        "old_confidence": old_conf,
        "new_pattern": new_pattern,
        "new_confidence": new_conf,
        "reasoning": result.get("reasoning", ""),
        "pattern_index": result.get("pattern_index"),
        "same": old_pattern.lower() == new_pattern.lower(),
    }


def should_apply(suggestion: dict) -> bool:
    """High-confidence correction gate."""
    if suggestion["same"]:
        return False
    if suggestion["new_confidence"] < 0.85:
        return False
    # Don't overwrite manually-perfect 1.0 patterns unless v2 is also 1.0
    if suggestion["old_confidence"] >= 1.0 and suggestion["new_confidence"] < 1.0:
        return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Audit email patterns safely")
    parser.add_argument("--apply", action="store_true", help="Commit high-confidence corrections")
    parser.add_argument("--company", type=int, help="Audit only this company ID")
    parser.add_argument("--min-conf", type=float, default=0.85, help="Minimum confidence to apply (default 0.85)")
    args = parser.parse_args()

    conn = get_db()
    cur = conn.cursor()

    if args.company:
        rows = cur.execute(
            "SELECT id, name, website, business_type, city, state, email_pattern, email_pattern_confidence "
            "FROM companies WHERE id=? AND email_pattern IS NOT NULL AND email_pattern != ''",
            (args.company,),
        ).fetchall()
    else:
        rows = cur.execute(
            "SELECT id, name, website, business_type, city, state, email_pattern, email_pattern_confidence "
            "FROM companies WHERE email_pattern IS NOT NULL AND email_pattern != '' "
            "ORDER BY id"
        ).fetchall()
    conn.close()

    print(f"Auditing {len(rows)} companies... (dry_run={not args.apply})")
    print(
        "id|name|website|domain|old_pattern|old_conf|new_pattern|new_conf|apply|reasoning"
    )

    suggestions = []
    for row in rows:
        company = dict(row)
        suggestion = audit_company(company)
        if not suggestion:
            # Could not infer — keep but mark
            print(
                f"{company['id']}|{company['name']}||||||no v2 result|skip|"
            )
            continue
        suggestions.append(suggestion)
        apply_ok = should_apply(suggestion) and suggestion["new_confidence"] >= args.min_conf
        if not args.apply:
            apply_ok = False
        marker = "APPLY" if apply_ok else "review"
        print(
            f"{suggestion['id']}|{suggestion['name']}|{suggestion['website']}|"
            f"{suggestion['domain']}|{suggestion['old_pattern']}|"
            f"{suggestion['old_confidence']:.2f}|{suggestion['new_pattern']}|"
            f"{suggestion['new_confidence']:.2f}|{marker}|{suggestion['reasoning']}"
        )

    apply_count = sum(
        1 for s in suggestions
        if should_apply(s) and s["new_confidence"] >= args.min_conf
    )

    if not args.apply:
        print(f"\nDry run complete. {apply_count} high-confidence corrections would be applied.")
        print("Run with --apply to commit them.")
        return

    if apply_count == 0:
        print("\nNo high-confidence corrections to apply.")
        return

    backup_path = _backup_db()
    print(f"\nBackup created: {backup_path}")

    conn = get_db()
    cur = conn.cursor()
    applied = 0
    for s in suggestions:
        if should_apply(s) and s["new_confidence"] >= args.min_conf:
            cur.execute(
                "UPDATE companies SET email_pattern=?, email_pattern_confidence=? WHERE id=?",
                (s["new_pattern"], s["new_confidence"], s["id"]),
            )
            applied += 1
    conn.commit()
    conn.close()
    print(f"Applied {applied} corrections.")


if __name__ == "__main__":
    main()
