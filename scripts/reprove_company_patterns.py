#!/usr/bin/env python3
"""
reprove_company_patterns.py

Retrofit script for the Phase 2 pattern proof gate.

For companies that already have a stored email_pattern, this script:
  1. Picks a sample contact at the company.
  2. Re-runs the SMTP pattern proof engine over the global candidate pool.
  3. If a proven pattern is found, updates the company row.
  4. Re-derives all contacts at the company with the new proven pattern.
  5. Re-validates each derived contact via the unified email chain.

Use --dry-run to preview without writing to the database.
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
    derive_email,
    extract_domain_from_website,
    _prove_pattern_for_company,
    _store_proven_pattern,
    resolve_and_validate_email,
)


def _fmt_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _companies_to_reprove(
    cur: sqlite3.Cursor,
    min_do_not_send_ratio: float,
    limit: int | None,
    skip_already_proven: bool,
) -> list[dict]:
    """Return companies with stored patterns and enough derived contacts to audit."""
    rows = cur.execute("""
        SELECT
            c.id,
            c.name,
            c.website,
            c.email_pattern,
            c.email_pattern_source,
            c.email_pattern_confidence,
            c.email_pattern_proof_status,
            c.email_pattern_proven_at,
            COUNT(ct.id) AS total_with_email,
            SUM(CASE WHEN ct.smtp_validation_status = 'Do Not Send' THEN 1 ELSE 0 END) AS do_not_send,
            SUM(CASE WHEN ct.smtp_validation_status = 'Okay to Send' THEN 1 ELSE 0 END) AS okay,
            SUM(CASE WHEN ct.smtp_validation_status = 'Maybe' THEN 1 ELSE 0 END) AS maybe
        FROM companies c
        LEFT JOIN contacts ct ON ct.company_id = c.id AND ct.email IS NOT NULL AND ct.email != ''
        WHERE c.email_pattern IS NOT NULL
        GROUP BY c.id
        HAVING total_with_email > 0
        ORDER BY (do_not_send * 1.0 / MAX(total_with_email, 1)) DESC, c.id
    """).fetchall()

    companies: list[dict] = []
    for row in rows:
        if skip_already_proven and row["email_pattern_proven_at"]:
            continue
        total = row["total_with_email"]
        dns = row["do_not_send"] or 0
        ratio = dns / total if total else 0.0
        if ratio >= min_do_not_send_ratio:
            companies.append(dict(row))
        if limit and len(companies) >= limit:
            break
    return companies


def _rederive_company_contacts(
    company_id: int, pattern: str, cur: sqlite3.Cursor, *, overwrite_existing: bool = False
) -> list[int]:
    """
    Apply the proven pattern to contacts at a company; return affected contact IDs.

    By default only fills email-less contacts. When overwrite_existing=True, also
    overwrites existing derived emails (is_derived_email=1) so that bad patterns
    from earlier runs are corrected.
    """
    if overwrite_existing:
        contacts = cur.execute(
            "SELECT id, first_name, last_name FROM contacts "
            "WHERE company_id=? AND (email IS NULL OR email='' OR is_derived_email=1)",
            (company_id,),
        ).fetchall()
    else:
        contacts = cur.execute(
            "SELECT id, first_name, last_name FROM contacts "
            "WHERE company_id=? AND (email IS NULL OR email='')",
            (company_id,),
        ).fetchall()

    affected: list[int] = []
    for contact_id, first_name, last_name in contacts:
        email = derive_email(first_name or "", last_name or "", pattern)
        if email:
            cur.execute(
                "UPDATE contacts SET email=?, is_derived_email=1 WHERE id=?",
                (email, contact_id),
            )
            affected.append(contact_id)
    return affected


def main() -> int:
    parser = argparse.ArgumentParser(description="Retrofit SMTP-proven patterns for existing companies.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to DB.")
    parser.add_argument("--limit", type=int, default=None, help="Only process N companies.")
    parser.add_argument(
        "--min-do-not-send-ratio",
        type=float,
        default=0.0,
        help="Only re-prove companies with at least this ratio of Do Not Send (default 0.0 = all).",
    )
    parser.add_argument(
        "--skip-already-proven",
        action="store_true",
        default=True,
        help="Skip companies that already have email_pattern_proven_at set.",
    )
    parser.add_argument(
        "--no-skip-already-proven",
        dest="skip_already_proven",
        action="store_false",
        help="Re-prove even companies that already have a proven pattern.",
    )
    parser.add_argument(
        "--use-web-search",
        action="store_true",
        help="Include SearXNG/web-search pattern hints in the proof pool.",
    )
    parser.add_argument(
        "--max-probes",
        type=int,
        default=60,
        help="Max SMTP probes per company pattern proof.",
    )
    parser.add_argument(
        "--revalidate-all",
        action="store_true",
        help="Re-validate every contact at the company after re-derivation, not just new ones.",
    )
    args = parser.parse_args()

    init_db()
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    companies = _companies_to_reprove(cur, args.min_do_not_send_ratio, args.limit, args.skip_already_proven)
    print(f"[reprove] Found {len(companies)} companies to reprove (ratio >= {args.min_do_not_send_ratio})")
    if not companies:
        conn.close()
        return 0

    log: list[dict] = []
    upgraded = 0
    unchanged = 0
    failed = 0
    revalidated = 0
    start_all = time.time()

    for company in companies:
        company_id = company["id"]
        name = company["name"]
        website = company["website"]
        old_pattern = company["email_pattern"]
        old_source = company["email_pattern_source"]
        old_status = company["email_pattern_proof_status"]

        domain = extract_domain_from_website(website or "")
        if not domain:
            print(f"[reprove] [{company_id}] {name}: no domain from website; skipped")
            failed += 1
            log.append({
                "company_id": company_id,
                "name": name,
                "action": "skipped",
                "reason": "no_domain",
            })
            continue

        print(f"[reprove] [{company_id}] {name}: old={old_pattern} source={old_source} proof={old_status}")
        proof = _prove_pattern_for_company(
            company_id,
            domain,
            use_web_search=args.use_web_search,
            max_probes=args.max_probes,
            accept_catch_all=True,
        )

        entry: dict = {
            "company_id": company_id,
            "name": name,
            "domain": domain,
            "old_pattern": old_pattern,
            "old_source": old_source,
            "new_pattern": proof.pattern,
            "new_confidence": proof.confidence,
            "proof_status": proof.proof_status,
            "proof_email": proof.proof_email,
            "proof_source": proof.source,
            "reason": proof.reason,
            "probes": len(proof.candidate_results),
            "candidate_results": proof.candidate_results,
        }

        if not proof.pattern or proof.proof_status not in ("Okay to Send", "Catch-All"):
            print(f"  -> no proven pattern found ({proof.reason}); marking unproven")
            failed += 1
            entry["action"] = "unproven"
            if not args.dry_run:
                _store_proven_pattern(company_id, proof, source_label="unproven")
                conn.commit()
            log.append(entry)
            continue

        if proof.pattern == old_pattern and old_status in ("Okay to Send", "Catch-All"):
            print(f"  -> existing pattern {old_pattern} re-proven")
            unchanged += 1
            entry["action"] = "unchanged"
        else:
            print(f"  -> UPGRADE {old_pattern} -> {proof.pattern} ({proof.proof_status})")
            upgraded += 1
            entry["action"] = "upgraded"

        if not args.dry_run:
            _store_proven_pattern(company_id, proof, source_label=proof.source)
            affected = _rederive_company_contacts(
                company_id, proof.pattern, cur, overwrite_existing=True
            )
            conn.commit()

            # Re-validate every contact with an email at this company so that
            # contacts with stale/literal garbage emails are corrected.
            contact_rows = cur.execute(
                "SELECT id FROM contacts WHERE company_id=? AND email IS NOT NULL AND email != ''",
                (company_id,),
            ).fetchall()
            contact_ids = [r["id"] for r in contact_rows]

            for cid in contact_ids:
                try:
                    resolve_and_validate_email(cid, source="reprove_company_patterns", force_revalidate=True)
                    revalidated += 1
                except Exception as e:
                    print(f"    [warn] validation failed for contact {cid}: {e}")
        else:
            # Dry-run: simulate derivation count.
            preview = cur.execute(
                "SELECT COUNT(*) FROM contacts "
                "WHERE company_id=? AND (email IS NULL OR email='')",
                (company_id,),
            ).fetchone()[0]
            entry["dry_run_rederivation_count"] = preview

        log.append(entry)
        continue

        print(f"  -> UPGRADE {old_pattern} -> {proof.pattern} ({proof.proof_status})")
        upgraded += 1
        entry["action"] = "upgraded"

        if not args.dry_run:
            _store_proven_pattern(company_id, proof, source_label=proof.source)
            affected = _rederive_company_contacts(
                company_id, proof.pattern, cur, overwrite_existing=True
            )
            conn.commit()

            # Always re-validate every contact with an email at this company.
            contact_rows = cur.execute(
                "SELECT id FROM contacts WHERE company_id=? AND email IS NOT NULL AND email != ''",
                (company_id,),
            ).fetchall()
            contact_ids = [r["id"] for r in contact_rows]

            for cid in contact_ids:
                try:
                    resolve_and_validate_email(cid, source="reprove_company_patterns", force_revalidate=True)
                    revalidated += 1
                except Exception as e:
                    print(f"    [warn] validation failed for contact {cid}: {e}")
        else:
            # Dry-run: simulate derivation count.
            preview = cur.execute(
                "SELECT COUNT(*) FROM contacts "
                "WHERE company_id=? AND (email IS NULL OR email='')",
                (company_id,),
            ).fetchone()[0]
            entry["dry_run_rederivation_count"] = preview

        log.append(entry)

    conn.close()

    elapsed = time.time() - start_all
    summary = {
        "processed": len(companies),
        "upgraded": upgraded,
        "unchanged": unchanged,
        "failed": failed,
        "contacts_revalidated": revalidated,
        "elapsed_seconds": round(elapsed, 1),
        "dry_run": args.dry_run,
    }

    print("\n[reprove] Summary:")
    for k, v in summary.items():
        print(f"  {k}: {v}")

    log_path = BASE_DIR / "scripts" / f"reprove_company_patterns_log_{_fmt_ts()}.json"
    with open(log_path, "w") as f:
        json.dump({"summary": summary, "companies": log}, f, indent=2)
    print(f"[reprove] Log written: {log_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
