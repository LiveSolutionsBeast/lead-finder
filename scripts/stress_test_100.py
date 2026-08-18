#!/usr/bin/env python3
"""
scripts/stress_test_100.py
==========================
Phase 1 / t1.6 — 100-address stress test against the new 2-probe design.

Mix:
  - 100 random addresses from ~/lead-seeker/leads.db
  - 20 from ~/lead-seeker/track.db opens (proven deliverable)

Targets (from SESSION_1_SMTP.md):
  - Per-address latency < 5 seconds (including jitter)
  - Total batch time < 10 minutes for 120 addresses
  - Report count of validated, timed-out, failed, and distribution of validation_confidence.

Usage:
    python3 scripts/stress_test_100.py
    python3 scripts/stress_test_100.py --limit 20   # smoke test
"""
import argparse
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lf_email_validator import check_email  # noqa: E402

LEAD_SEEKER_LEADS = Path("/home/anthonyturgman/lead-seeker/leads.db")
LEAD_SEEKER_TRACK = Path("/home/anthonyturgman/lead-seeker/track.db")


def fetch_lead_seeker_addresses(n: int) -> list[str]:
    conn = sqlite3.connect(str(LEAD_SEEKER_LEADS))
    rows = conn.execute(
        "SELECT DISTINCT Email FROM leads "
        "WHERE Email IS NOT NULL AND Email != '' "
        "ORDER BY RANDOM() LIMIT ?",
        (n,),
    ).fetchall()
    conn.close()
    return [r[0].strip() for r in rows if r[0]]


def fetch_track_opens(n: int) -> list[str]:
    conn = sqlite3.connect(str(LEAD_SEEKER_TRACK))
    rows = conn.execute(
        "SELECT DISTINCT email FROM sends "
        "WHERE opened_at IS NOT NULL AND email IS NOT NULL "
        "ORDER BY RANDOM() LIMIT ?",
        (n,),
    ).fetchall()
    conn.close()
    return [r[0].strip() for r in rows if r[0]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=100,
                    help="Number of random lead-seeker addresses (default 100)")
    ap.add_argument("--opens", type=int, default=20,
                    help="Number of proven-deliverable opens to include (default 20)")
    ap.add_argument("--no-db", action="store_true",
                    help="Skip lead-seeker DB lookup; use hard-coded test set")
    args = ap.parse_args()

    if args.no_db:
        addresses = ["test@gmail.com"] * 5  # tiny smoke set
    else:
        random_emails = fetch_lead_seeker_addresses(args.limit)
        open_emails = fetch_track_opens(args.opens)
        addresses = random_emails + open_emails

    print("=" * 70)
    print(f"STRESS TEST — {len(addresses)} addresses")
    print("=" * 70)

    started = time.monotonic()
    results = []
    for i, email in enumerate(addresses, 1):
        t0 = time.monotonic()
        try:
            r = check_email(email)
            err = None
        except Exception as e:
            r = None
            err = str(e)
        latency = time.monotonic() - t0
        results.append({
            "email": email,
            "status": r.status if r else "ERROR",
            "code": r.smtp_code if r else None,
            "analysis": r.analysis if r else "exception",
            "confidence": r.validation_confidence if r else 0.0,
            "latency": round(latency, 2),
            "error": err,
        })
        if i % 10 == 0 or i == len(addresses):
            elapsed = time.monotonic() - started
            print(f"  [{i}/{len(addresses)}] elapsed={elapsed:.1f}s avg={elapsed/i:.2f}s")

    total_elapsed = time.monotonic() - started
    print(f"\nTotal elapsed: {total_elapsed:.1f}s ({total_elapsed/len(addresses):.2f}s/address)")
    if total_elapsed > 600:
        print(f"✗ FAIL: > 10 minutes")
    else:
        print(f"✓ PASS: < 10 minutes")

    # Distribution
    by_status = Counter(r["status"] for r in results)
    by_analysis = Counter(r["analysis"] for r in results)
    confidences = [r["confidence"] for r in results if r["confidence"] is not None]
    latencies = [r["latency"] for r in results]

    print("\nStatus distribution:")
    for k, v in by_status.most_common():
        print(f"  {k:<25} {v:>3} ({100*v/len(results):.0f}%)")

    print("\nAnalysis distribution:")
    for k, v in by_analysis.most_common():
        print(f"  {k:<35} {v:>3} ({100*v/len(results):.0f}%)")

    print("\nLatency (seconds):")
    print(f"  min={min(latencies):.2f}  p50={sorted(latencies)[len(latencies)//2]:.2f}  "
          f"p95={sorted(latencies)[int(len(latencies)*0.95)]:.2f}  max={max(latencies):.2f}")
    over_5s = sum(1 for l in latencies if l > 5)
    print(f"  over 5s: {over_5s} ({100*over_5s/len(latencies):.1f}%)")

    if confidences:
        print(f"\nConfidence: min={min(confidences):.2f}  max={max(confidences):.2f}  "
              f"mean={sum(confidences)/len(confidences):.2f}")
        bins = {"0.0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
        for c in confidences:
            for k in bins:
                lo, hi = k.split("-")
                if float(lo) <= c < float(hi) or (k == "0.8-1.0" and c <= 1.0):
                    bins[k] += 1
                    break
        print("Confidence distribution:")
        for k, v in bins.items():
            print(f"  {k}    {v:>3} ({100*v/len(confidences):.0f}%)")

    errors = [r for r in results if r["error"]]
    if errors:
        print(f"\nErrors: {len(errors)}")
        for r in errors[:5]:
            print(f"  {r['email']}: {r['error']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
