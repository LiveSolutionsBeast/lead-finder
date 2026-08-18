#!/usr/bin/env python3
"""
scripts/test_phase0_20.py
=========================
Phase 1 / t1.5 — Re-run the 20-address Phase 0 test with the new 2-probe
design. Tests known-good, known-bad, and proven-deliverable addresses.

Targets (from PHASE_0_REPORT.md):
  - All 5 proven-deliverable addresses should be "Okay to Send"
  - All 5 invalid addresses should be "Do Not Send"
  - Real leads: at least 7/10 should be "Okay to Send"
  - Overall accuracy >= 90%

Usage:
    python3 scripts/test_phase0_20.py
    python3 scripts/test_phase0_20.py --print-all
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lf_email_validator import check_email  # noqa: E402

# ── Test set ───────────────────────────────────────────────────────────────
# Proven-deliverable (5): from ~/lead-seeker/track.db opens
PROVEN_DELIVERABLE = [
    "alaina@kairosaerospace.com",
    "tdegrasse@appliedengineering.com",
    "elyce@cocotutti.com",
    "erica@tpcionline.com",
    "ewolff@owensdesign.com",
]

# Invalid controls (5): bad syntax, no MX, fake local-part, disposable, etc.
INVALID_CONTROLS = [
    "not-an-email",                                              # invalid syntax
    "test@this-domain-definitely-does-not-exist-12345.com",      # no MX
    "fakeuser1234567@mailinator.com",                            # disposable
    "noreply@google.com",                                        # known no-reply (catch-all)
    "noonexyzabc9999@gmail.com",                                 # random fake on real domain
]

# Real leads (10): from ~/lead-seeker/leads.db (random)
REAL_LEADS = [
    "davide@castertech.com",
    "acelestine@ppg.com",
    "jhsieh@clref.com",
    "johna@aero-mechanical.com",
    "shannon@onceinalicetime.com",
    "raykokopath@aol.com",
    "jsteele@generallinear.com",
    "epolanco@ppg.com",
    "sam@magicjump.com",
    "tom@spinlaunch.com",
]

CATEGORIES = {
    "proven_deliverable": (PROVEN_DELIVERABLE, "Okay to Send"),
    "real_leads":         (REAL_LEADS, "Okay to Send"),
    "invalid_controls":   (INVALID_CONTROLS, "Do Not Send"),
}


def expected(category: str, idx: int) -> str:
    """Return the expected status for an address in a category."""
    return CATEGORIES[category][1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--print-all", action="store_true",
                    help="Print every address verdict (default: only print failures)")
    args = ap.parse_args()

    print("=" * 70)
    print("PHASE 0 20-ADDRESS RE-RUN — Phase 1 / t1.5")
    print("=" * 70)

    results = []
    correct = 0
    total = 0
    started = time.monotonic()

    for category in ("proven_deliverable", "real_leads", "invalid_controls"):
        addrs, want = CATEGORIES[category]
        print(f"\n--- {category.replace('_', ' ').upper()} (expected: {want}) ---")
        for email in addrs:
            total += 1
            try:
                r = check_email(email)
            except Exception as e:
                print(f"  ERROR {email}: {e}")
                results.append((category, email, "ERROR", "?", ""))
                continue
            got = r.status
            ok = (got == want)
            mark = "✓" if ok else "✗"
            if ok:
                correct += 1
            if args.print_all or not ok:
                print(f"  {mark} {email:<48} got={got!r:<20} expected={want!r:<20} "
                      f"code={r.smtp_code} conf={r.validation_confidence} "
                      f"analysis={r.analysis}")
            results.append((category, email, got, r.smtp_code, r.analysis))

    elapsed = time.monotonic() - started

    # Per-category accuracy
    print("\n" + "=" * 70)
    print("RESULTS BY CATEGORY")
    print("=" * 70)
    for category in CATEGORIES:
        addrs, want = CATEGORIES[category]
        cat_results = [r for r in results if r[0] == category]
        cat_correct = sum(1 for r in cat_results if r[2] == want)
        print(f"  {category:<25} {cat_correct}/{len(cat_results)} correctly classified "
              f"(expected {want})")

    overall_pct = 100.0 * correct / total if total else 0
    print(f"\nOVERALL: {correct}/{total} correct ({overall_pct:.0f}%)")
    print(f"TIME:    {elapsed:.1f}s (avg {elapsed/total:.2f}s/address)")
    if overall_pct >= 90:
        print("✓ PASS: >= 90% accuracy target met.")
    else:
        print(f"✗ FAIL: {overall_pct:.0f}% < 90% target.")
    return 0 if overall_pct >= 90 else 1


if __name__ == "__main__":
    sys.exit(main())
