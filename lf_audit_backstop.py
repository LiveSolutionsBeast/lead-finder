#!/usr/bin/env python3
"""
lf_audit_backstop.py - Nightly safety net for AI-generated contact data.

Scans the contacts table for rows that look "verified" (confidence >= 0.7)
and runs them through the hardened escalator in audit-only mode. Any row
whose evidence doesn't survive the escalator's gates is downgraded:
  - confidence_score is set to the escalator's honest confidence
  - pipeline_stage is set to 'audit_failed'
  - linkedin_url is cleared if the escalator couldn't re-verify it
  - title is cleared if the escalator couldn't re-verify it
  - manual_notes is appended with a dated audit trail

Run nightly via cron, or on demand:
    python3 lf_audit_backstop.py
    python3 lf_audit_backstop.py --contact-ids 1538 1539 1540 1990
    python3 lf_audit_backstop.py --min-confidence 0.7 --limit 200
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from lf_db import get_db, patch_contact
from lf_executives import (
    cascading_linkedin_search,
    verified_linkedin_url,
    search_linkedin_verification,
)

logger = logging.getLogger("lf_audit_backstop")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def audit_contact(contact_id: int, dry_run: bool = True) -> dict:
    """Re-verify a single contact's title and LinkedIn URL with fresh evidence.

    Returns a dict with:
      - audited: bool
      - downgraded: bool
      - reasons: list[str]
      - cleared: list[str]   # fields that were cleared
      - new_confidence: float
    """
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT ct.id, ct.full_name, ct.title, ct.linkedin_url, ct.email,
               ct.confidence_score, ct.ai_title_confidence, ct.ai_edited_at,
               ct.pipeline_stage, ct.company_id,
               c.name AS company_name, c.website AS company_website,
               c.state AS company_state, c.email_pattern AS company_email_pattern
        FROM contacts ct JOIN companies c ON c.id = ct.company_id
        WHERE ct.id = ?
    """, (contact_id,)).fetchone()
    conn.close()
    if not row:
        return {"audited": False, "reason": "not_found"}

    ct = dict(row)
    if (ct.get("confidence_score") or 0) < 0.7 and (ct.get("ai_title_confidence") or 0) < 0.7:
        return {"audited": False, "reason": "below_threshold"}

    full_name = ct["full_name"] or ""
    company_name = ct["company_name"] or ""
    cleared = []
    reasons = []

    # Re-verify the LinkedIn URL
    linkedin_url = ct.get("linkedin_url")
    if linkedin_url:
        verify = verified_linkedin_url(
            candidate_url=linkedin_url,
            full_name=full_name,
            company_name=company_name,
            search_state=ct.get("company_state") or "CA",
            timeout=12,
        )
        if not verify.get("confirmed"):
            cleared.append("linkedin_url")
            reasons.append(f"linkedin_url re-verification failed: {verify.get('reason')}")

    # Re-verify the title by running a fresh cascading search
    cascade = cascading_linkedin_search(
        full_name=full_name,
        company_name=company_name,
        company_website=ct.get("company_website") or "",
        search_state=ct.get("company_state") or "CA",
        timeout=10,
    )
    if not cascade.get("company_match"):
        if ct.get("title"):
            cleared.append("title")
            reasons.append("cascade search could not confirm company match")

    # Look at SearXNG corroboration
    searx_hits = []
    try:
        searx_hits = search_linkedin_verification(full_name, company_name, ct.get("company_state") or "CA")
    except Exception as e:
        reasons.append(f"searxng error: {e}")

    if not searx_hits and ct.get("title"):
        cleared.append("title")
        reasons.append("no SearXNG corroboration for title")

    new_confidence = ct.get("confidence_score") or 0.0
    if cleared:
        new_confidence = min(new_confidence, 0.5)

    downgraded = bool(cleared) and not dry_run
    if downgraded:
        update = {}
        if "linkedin_url" in cleared:
            update["linkedin_url"] = None
            update["source_linkedin_verified"] = 0
            update["source_linkedin_unverified"] = 0
        if "title" in cleared:
            update["title"] = None
            update["ai_verified_title"] = None
            update["title_from_linkedin"] = None
        update["confidence_score"] = new_confidence
        update["ai_title_confidence"] = new_confidence
        update["pipeline_stage"] = "audit_failed"
        update["is_manually_edited"] = 0  # automated audit, not a human edit
        note = f" [audit {now_iso()[:10]}: cleared {','.join(cleared)}. {('; '.join(reasons))[:200]}]"
        update["manual_notes"] = (ct.get("manual_notes") or "") + note
        patch_contact(contact_id, update)

    return {
        "audited": True,
        "contact_id": contact_id,
        "full_name": full_name,
        "company_name": company_name,
        "cleared": cleared,
        "reasons": reasons,
        "new_confidence": new_confidence,
        "downgraded": downgraded,
        "dry_run": dry_run,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contact-ids", nargs="*", type=int, default=None, help="Specific contact IDs to audit (default: scan all)")
    ap.add_argument("--min-confidence", type=float, default=0.7, help="Minimum current confidence to audit")
    ap.add_argument("--limit", type=int, default=200, help="Max contacts to scan")
    ap.add_argument("--apply", action="store_true", help="Apply the audit (default: dry run)")
    args = ap.parse_args()

    conn = get_db()
    conn.row_factory = sqlite3.Row
    if args.contact_ids:
        ids = args.contact_ids
    else:
        rows = conn.execute("""
            SELECT id FROM contacts
            WHERE COALESCE(confidence_score,0) >= ? OR COALESCE(ai_title_confidence,0) >= ?
            ORDER BY id DESC LIMIT ?
        """, (args.min_confidence, args.min_confidence, args.limit)).fetchall()
        ids = [r["id"] for r in rows]
    conn.close()

    print(f"Auditing {len(ids)} contacts (min_conf={args.min_confidence}, apply={args.apply})")
    summary = {"total": len(ids), "downgraded": 0, "skipped": 0, "errors": 0}
    for cid in ids:
        try:
            res = audit_contact(cid, dry_run=not args.apply)
            if not res.get("audited"):
                summary["skipped"] += 1
                continue
            if res.get("downgraded"):
                summary["downgraded"] += 1
                print(f"  DOWNGRADE  ct={cid}  '{res['full_name']}'  cleared={res['cleared']}  new_conf={res['new_confidence']:.2f}")
                for r in res["reasons"]:
                    print(f"      reason: {r}")
            else:
                print(f"  OK         ct={cid}  '{res['full_name']}'  conf={res['new_confidence']:.2f}")
        except Exception as e:
            summary["errors"] += 1
            print(f"  ERROR      ct={cid}  {e}")
    print()
    print("Summary:", summary)
    return 0


def flag_stale_unvalidated_emails(hours: int = 24, dry_run: bool = True) -> list[dict]:
    """
    Phase 5 backstop: find contacts that have an email but no
    smtp_validation_status, older than `hours`.

    Returns a list of dicts with id, email, created_at, company_name so a
    human or downstream job can re-run the chain or investigate.
    """
    from datetime import timedelta
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT ct.id, ct.email, c.name AS company_name
        FROM contacts ct
        JOIN companies c ON c.id = ct.company_id
        WHERE ct.email IS NOT NULL AND ct.email != ''
          AND ct.smtp_validation_status IS NULL
        ORDER BY ct.id
    """).fetchall()
    conn.close()
    flagged = [dict(r) for r in rows]
    if flagged:
        print(f"[audit] flagged {len(flagged)} contacts with unvalidated emails")
        for r in flagged[:10]:
            print(f"  ct={r['id']} email={r['email']} company={r['company_name']}")
        if len(flagged) > 10:
            print(f"  ... and {len(flagged) - 10} more")
    else:
        print(f"[audit] no unvalidated emails")
    return flagged


if __name__ == "__main__":
    sys.exit(main())
