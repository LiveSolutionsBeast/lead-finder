#!/usr/bin/env python3
"""
scripts/bulk_validate_unvalidated.py
====================================
Bulk SMTP-validate all contacts that currently lack a definitive
smtp_validation_status (NULL, 'Unknown', or 'Maybe') and are not marked as
manually edited.

Uses the unified email chain's `resolve_and_validate_email()` path when a
contact record is available, and falls back to `check_email()` + direct DB
writes for any contacts that the chain would skip.

PROOF GATE: before any DB write we verify the validation came from a real
probe or a definitive pre-SMTP filter:
  - `smtp_probed=True` (an SMTP conversation actually happened), OR
  - status is 'Do Not Send' with analysis in {Invalid Syntax, No MX, Disposable}

Anything that does not meet the proof gate is reported as skipped and NOT
written.

Output: per-contact verdict, proof-gate summary, and a JSON log written to
scripts/bulk_validate_unvalidated_log_<ts>.json.

Usage:
    python3 scripts/bulk_validate_unvalidated.py
    python3 scripts/bulk_validate_unvalidated.py --limit 10   # smoke test
    python3 scripts/bulk_validate_unvalidated.py --dry-run   # no writes
"""
import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lf_config import get as _get_config  # noqa: E402
from lf_db import (  # noqa: E402
    get_all_derived_emails_to_validate,
    init_db,
    set_cached_validation,
    write_contact_validation,
)
from lf_email_validator import check_email  # noqa: E402

DB_PATH = ROOT / "lf.db"
LOG_DIR = ROOT / "scripts"

# A result is considered "proven" if we either did real SMTP work or reached a
# definitive pre-SMTP rejection.
PROVEN_PRESMTP_ANALYSES = {"Invalid Syntax", "No MX", "Disposable"}


def is_proven(result: dict) -> bool:
    """Return True if the validation result is safe to persist."""
    if result.get("smtp_probed"):
        return True
    if result.get("status") == "Do Not Send" and result.get("analysis") in PROVEN_PRESMTP_ANALYSES:
        return True
    return False


def fallback_validate(email: str) -> dict:
    """Direct SMTP probe for contacts the unified chain won't touch."""
    res = check_email(email)
    d = res.to_dict()
    d.setdefault("email", email)
    return d


def run(limit: int | None = None, dry_run: bool = False) -> dict:
    init_db()
    cfg = _get_config("email_validation", {})
    max_per_batch = cfg.get("max_per_batch", 200)

    contacts = get_all_derived_emails_to_validate(max_count=limit or max_per_batch)
    if limit:
        contacts = contacts[:limit]

    total = len(contacts)
    print(f"=== Bulk validating {total} unvalidated contacts ===")
    if not contacts:
        print("No unvalidated contacts to process.")
        return {"total": 0, "proven": 0, "skipped": 0, "details": []}

    proven = 0
    skipped = 0
    skipped_ids: list[int] = []
    by_status: dict[str, int] = {}
    details: list[dict] = []
    started = time.monotonic()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    for i, ct in enumerate(contacts, 1):
        cid = ct["id"]
        email = (ct.get("email") or "").strip()
        print(f"\n[{i}/{total}] id={cid} email={email}")
        if not email:
            print("    SKIP: empty email")
            skipped += 1
            details.append({"id": cid, "email": email, "skipped": True, "reason": "empty email"})
            continue

        # Honor manual-edit guard: never re-probe a manually edited email unless
        # the operator explicitly forces it. For backlog clearing we use direct
        # SMTP probes so we always exercise the live network path instead of
        # relying on the unified chain's skip logic.
        if ct.get("is_manually_edited"):
            print("    SKIP: manually edited contact")
            skipped += 1
            details.append({"id": cid, "email": email, "skipped": True, "reason": "manual_edit"})
            continue

        try:
            result = check_email(email)
            result = result.to_dict()
            result.setdefault("email", email)
        except Exception as e:
            print(f"    PROBE ERROR: {e}")
            skipped += 1
            details.append({"id": cid, "email": email, "skipped": True, "reason": f"probe_error:{e}"})
            continue

        status = result.get("status", "Unknown")
        analysis = result.get("analysis", "")
        smtp_probed = bool(result.get("smtp_probed"))
        smtp_code = result.get("smtp_code")
        latency = result.get("validation_latency_ms") or 0
        mx_host = result.get("validation_mx_host") or result.get("mx_host") or ""
        confidence = result.get("validation_confidence", 0.0)
        method = result.get("validation_method", "")

        print(f"    status={status} analysis={analysis}")
        print(f"    smtp_probed={smtp_probed} code={smtp_code} mx={mx_host} latency={latency}ms")
        print(f"    method={method} confidence={confidence}")

        if not is_proven(result):
            print("    SKIP: not proven (no SMTP probe and not a definitive pre-SMTP rejection)")
            skipped += 1
            skipped_ids.append(cid)
            details.append({
                "id": cid, "email": email, "skipped": True,
                "reason": "not_proven", "status": status, "analysis": analysis,
            })
            continue

        by_status[status] = by_status.get(status, 0) + 1
        proven += 1

        if not dry_run:
            try:
                write_contact_validation(cid, result)
                set_cached_validation(result)
                print("    DB WRITE OK")
            except Exception as e:
                print(f"    DB WRITE FAILED: {e}")
                details.append({
                    "id": cid, "email": email, "skipped": True,
                    "reason": f"db_write_error:{e}",
                })
                continue

        details.append({
            "id": cid,
            "email": email,
            "skipped": False,
            "status": status,
            "analysis": analysis,
            "smtp_probed": smtp_probed,
            "smtp_code": smtp_code,
            "mx_host": mx_host,
            "latency_ms": latency,
            "method": method,
            "confidence": confidence,
        })

    elapsed = round(time.monotonic() - started, 1)
    summary = {
        "ts": ts,
        "total": total,
        "proven": proven,
        "skipped": skipped,
        "by_status": by_status,
        "elapsed_seconds": elapsed,
        "dry_run": dry_run,
        "skipped_ids": skipped_ids,
        "details": details,
    }

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total processed:   {total}")
    print(f"Proven + written:  {proven}")
    print(f"Skipped:           {skipped}")
    print(f"By status:         {by_status}")
    print(f"Elapsed:           {elapsed}s")
    if dry_run:
        print("[DRY RUN] No database writes were performed.")

    log_path = LOG_DIR / f"bulk_validate_unvalidated_log_{ts}.json"
    with open(log_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Log written: {log_path}")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Only validate first N contacts")
    ap.add_argument("--dry-run", action="store_true", help="Run checks but do not write")
    args = ap.parse_args()

    run(limit=args.limit, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
