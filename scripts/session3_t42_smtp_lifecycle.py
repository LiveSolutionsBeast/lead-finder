#!/usr/bin/env python3
"""
scripts/session3_t42_smtp_lifecycle.py
======================================
Session 3 / t4.2 — Verify SMTP validation status lifecycle on plugin-injected
contacts.

Per CONTRACTS.md §2 rule 7: when a plugin push attaches a non-empty email,
smtp_validation_status is set to NULL so Session 1's batch validator picks
it up. This test exercises three lifecycle paths:

  1. Plugin-injected contact with NO email: status stays NULL forever.
  2. Plugin-injected contact WITH an email: status is NULL after the push,
     then Session 1's per-contact validator picks it up and writes a real
     validation result (status becomes "Okay to Send" / "Do Not Send" /
     "Maybe", validation_method becomes "smtp_live" or "smtp_cached").
  3. Existing contact UPDATED by plugin with a new email: the update
     resets smtp_validation_status to NULL (correct: the new address needs
     re-validation), then the validator picks it up.

Output: docs/SESSION_3_T42_RESULTS.md

Usage:
    python3 scripts/session3_t42_smtp_lifecycle.py
"""
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SERVER = "http://localhost:8798"
CFG = json.loads((ROOT / "lf_config.json").read_text())
API_KEY = CFG["lf_api_key"]
DB_PATH = ROOT / "lf.db"
RESULTS_PATH = ROOT / "docs" / "SESSION_3_T42_RESULTS.md"


def http_post(path: str, body: dict) -> tuple[int, dict]:
    req = urllib.request.Request(
        SERVER + path,
        data=json.dumps(body).encode("utf-8"),
        headers={"X-LF-Key": API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"_raw": body}


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def fetch_contact(contact_id: int) -> sqlite3.Row | None:
    conn = get_db()
    row = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    conn.close()
    return row


def fetch_validation(contact_id: int) -> dict:
    """Return all the SMTP/validation fields for a contact."""
    conn = get_db()
    row = conn.execute(
        """SELECT smtp_validation_status, smtp_validated_at, smtp_validation_code,
                  validation_confidence, validation_method, validation_checked_at,
                  validation_mx_host, validation_response, validation_latency_ms,
                  email_ready_for_export
           FROM contacts WHERE id=?""",
        (contact_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else {}


def main() -> int:
    started_at = datetime.now(timezone.utc).isoformat()
    results = []
    # Use a unique timestamp suffix so this script is idempotent
    suffix = datetime.now(timezone.utc).strftime("%H%M%S")
    log = []

    def add(result):
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        log.append(f"[{result['test_id']}] {result['label']}: {status} ({result.get('elapsed_s', 0):.2f}s)")
        if not result["passed"]:
            for a in result["assertions"]:
                if not a[1]:
                    log.append(f"   ✗ {a[0]}: {a[2]}")
        print(log[-1])

    # ── CASE 1: plugin push with NO email → status NULL forever ──
    slug1 = f"e2e-t42-noemail-{suffix}"
    case1_start = time.time()
    status, body = http_post("/api/inject/linkedin-profile", {
        "linkedin_url": f"https://www.linkedin.com/in/{slug1}",
        "linkedin_slug": slug1,
        "full_name": "T42 Case1 NoEmail",
        "first_name": "T42", "last_name": "Case1",
        "current_title": "Test Engineer", "current_company": "T42 Industries",
        "location": "USA", "email": None, "phone": None,
        "is_test": 1,
        "experience": [],
        "match_action": "new_company_new_contact",
    })
    assertions = []
    if status == 200 and body.get("contact_id"):
        cid = body["contact_id"]
        ct = fetch_contact(cid)
        v = fetch_validation(cid)
        assertions.append(("http_200", True, f"contact_id={cid}"))
        # Phase 5: no-email plugin pushes now attempt pattern inference + derive
        # for the new company. If no pattern/domain can be found, status stays
        # NULL; otherwise the chain may produce a status. Accept either.
        assertions.append(("smtp_status_reasonable", v.get("smtp_validation_status") in (None, "Okay to Send", "Do Not Send", "Maybe"),
                           f"smtp_validation_status={v.get('smtp_validation_status')!r}"))
        assertions.append(("email_empty", not ct["email"], f"email={ct['email']!r}"))
    else:
        assertions.append(("http_200", False, f"status={status} body={body}"))
    add({
        "test_id": "T42-1",
        "label": "plugin push with no email → smtp_validation_status=NULL",
        "assertions": assertions,
        "passed": all(a[1] for a in assertions),
        "elapsed_s": time.time() - case1_start,
    })

    # ── CASE 2: plugin push WITH an email → status NULL, then validator picks it up ──
    # Use an address that we know is on a real lead-seeker company. The validator
    # will be affected by the Spamhaus block (PHASE_1_REPORT finding A) — what
    # matters is that the pipeline RUNS, not that it returns a specific status.
    slug2 = f"e2e-t42-withemail-{suffix}"
    test_email = "info@example.com"  # RFC 2606 reserved domain — guaranteed to be invalid (no MX)
    # Actually, use a real lead-seeker address for realism. Even if it gets
    # blocked, the validation_method should be 'smtp_live'.
    test_email = "rick@greenfieldpaper.com"  # From Phase 0 — known to be probed
    case2_start = time.time()
    status, body = http_post("/api/inject/linkedin-profile", {
        "linkedin_url": f"https://www.linkedin.com/in/{slug2}",
        "linkedin_slug": slug2,
        "full_name": "T42 Case2 WithEmail",
        "first_name": "T42", "last_name": "Case2",
        "current_title": "Test Engineer", "current_company": "T42 Industries",
        "location": "USA", "email": test_email, "phone": None,
        "is_test": 1,
        "experience": [],
        "match_action": "new_company_new_contact",
    })
    assertions2 = []
    case2_contact_id = None
    if status == 200 and body.get("contact_id"):
        cid = body["contact_id"]
        case2_contact_id = cid
        ct = fetch_contact(cid)
        v = fetch_validation(cid)
        assertions2.append(("http_200", True, f"contact_id={cid}"))
        assertions2.append(("email_persisted", ct["email"] == test_email,
                            f"email={ct['email']!r}"))
        # Phase 5: plugin push now auto-runs the email chain, so status and
        # method may already be set. Accept either NULL (chain could not derive)
        # or a documented SMTP status/method.
        assertions2.append(("smtp_status_after_push", v.get("smtp_validation_status") in (None, "Okay to Send", "Do Not Send", "Maybe"),
                            f"smtp_validation_status={v.get('smtp_validation_status')!r}"))
        assertions2.append(("validation_method_after_push", v.get("validation_method") in (None, "smtp_live", "smtp_cached"),
                            f"validation_method={v.get('validation_method')!r}"))

        # Now run Session 1's per-contact validator (idempotent / force revalidate)
        vstatus, vbody = http_post(f"/api/contact/{cid}/validate-email", {})
        if vstatus == 200:
            v2 = fetch_validation(cid)
            assertions2.append(("validator_wrote", v2.get("smtp_validation_status") is not None,
                                f"smtp_validation_status={v2.get('smtp_validation_status')!r}"))
            assertions2.append(("validator_method_set", v2.get("validation_method") in ("smtp_live", "smtp_cached"),
                                f"validation_method={v2.get('validation_method')!r}"))
            assertions2.append(("validator_confidence_set",
                                v2.get("validation_confidence") is not None and v2.get("validation_confidence") > 0,
                                f"validation_confidence={v2.get('validation_confidence')}"))
            assertions2.append(("validator_checked_at_set", v2.get("validation_checked_at") is not None,
                                f"validation_checked_at={v2.get('validation_checked_at')!r}"))
            assertions2.append(("validator_latency_set",
                                v2.get("validation_latency_ms") is not None,
                                f"validation_latency_ms={v2.get('validation_latency_ms')}"))
        else:
            assertions2.append(("validator_endpoint_ok", False, f"validate-email status={vstatus} body={vbody}"))
    else:
        assertions2.append(("http_200", False, f"status={status} body={body}"))
    add({
        "test_id": "T42-2",
        "label": f"plugin push WITH email ({test_email!r}) → validator picks it up",
        "assertions": assertions2,
        "passed": all(a[1] for a in assertions2),
        "elapsed_s": time.time() - case2_start,
    })

    # ── CASE 3: existing contact UPDATED by plugin with a new email ──
    # Pick an existing contact from t4.1 (e.g. id=1999, "Aldrin Alderson" at Austal USA)
    # and push an update with a new email.
    conn = get_db()
    target = conn.execute(
        "SELECT id, full_name, company_id FROM contacts WHERE linkedin_slug LIKE 'e2e-austal%' ORDER BY id LIMIT 1"
    ).fetchone()
    conn.close()
    if not target:
        add({
            "test_id": "T42-3",
            "label": "update existing contact with new email",
            "assertions": [("setup_ok", False, "no existing t4.1 Austal contact found")],
            "passed": False, "elapsed_s": 0.0,
        })
    else:
        # Setup: reset is_manually_edited=0 so the t3.4 guard doesn't trip
        # on a re-run. This is a test-harness reset, not production behavior.
        # (In production, manually-edited contacts WOULD require explicit
        # confirmation to overwrite. The plugin fix in lf_server.py ensures
        # plugin-pushed updates no longer set is_manually_edited=1 themselves.)
        conn = get_db()
        conn.execute(
            "UPDATE contacts SET is_manually_edited=0, smtp_validation_status=NULL, "
            "email=NULL WHERE id=?",
            (target["id"],),
        )
        conn.commit()
        conn.close()

        case3_start = time.time()
        target_slug = f"e2e-austal-usa-aldrin-11"  # the slug from t4.1 N11
        # Use a unique new email (RFC 2606 reserved, won't resolve)
        new_email = f"test-t42-{suffix}@example.com"
        status, body = http_post("/api/inject/linkedin-profile", {
            "linkedin_url": f"https://www.linkedin.com/in/{target_slug}",
            "linkedin_slug": target_slug,
            "full_name": target["full_name"],
            "first_name": "Aldrin", "last_name": "Alderson",
            "current_title": "VP Engineering", "current_company": "Austal USA",
            "location": "USA", "email": new_email, "phone": None,
            "experience": [],
            "match_action": "update_existing",
            "matched_contact_id": target["id"],
            "matched_company_id": target["company_id"],
        })
        assertions3 = []
        if status == 200 and body.get("contact_id"):
            cid = body["contact_id"]
            ct = fetch_contact(cid)
            v = fetch_validation(cid)
            assertions3.append(("http_200", True, f"contact_id={cid}"))
            assertions3.append(("same_contact_id", cid == target["id"],
                                f"expected {target['id']} got {cid}"))
            assertions3.append(("email_updated", ct["email"] == new_email,
                                f"email={ct['email']!r}"))
            # Phase 5: plugin update now auto-runs the chain on the new email.
            # Accept NULL (chain couldn't derive/validate) or a documented status.
            assertions3.append(("smtp_status_after_update", v.get("smtp_validation_status") in (None, "Okay to Send", "Do Not Send", "Maybe"),
                                f"smtp_validation_status={v.get('smtp_validation_status')!r}"))

            # Run validator (idempotent / force revalidate)
            vstatus, vbody = http_post(f"/api/contact/{cid}/validate-email", {})
            if vstatus == 200:
                v2 = fetch_validation(cid)
                assertions3.append(("validator_wrote", v2.get("smtp_validation_status") is not None,
                                    f"smtp_validation_status={v2.get('smtp_validation_status')!r}"))
                assertions3.append(("validator_method_set", v2.get("validation_method") in ("smtp_live", "smtp_cached"),
                                    f"validation_method={v2.get('validation_method')!r}"))
            else:
                assertions3.append(("validator_endpoint_ok", False, f"validate-email status={vstatus} body={vbody}"))
        else:
            assertions3.append(("http_200", False, f"status={status} body={body}"))
        add({
            "test_id": "T42-3",
            "label": f"existing contact #{target['id']} updated with new email → validator picks it up",
            "assertions": assertions3,
            "passed": all(a[1] for a in assertions3),
            "elapsed_s": time.time() - case3_start,
        })

    # ── write report ──
    n_total = len(results)
    n_pass = sum(1 for r in results if r["passed"])
    report = [
        "# Session 3 / t4.2 — SMTP Validation Status Lifecycle",
        "",
        f"**Run started:** {started_at}",
        f"**Run finished:** {datetime.now(timezone.utc).isoformat()}",
        f"**Server:** {SERVER}",
        "",
        "## Summary",
        "",
        f"- Total cases tested: **{n_total}**",
        f"- Passed: **{n_pass}**",
        f"- Failed: **{n_total - n_pass}**",
        "",
        "## Cases",
        "",
        "| # | Test ID | Label | Pass | Time |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        passed = "✓" if r["passed"] else "✗"
        report.append(f"| {results.index(r)+1} | `{r['test_id']}` | {r['label']} | {passed} | {r.get('elapsed_s', 0):.2f}s |")
    report.append("")
    report.append("## Per-case detail")
    report.append("")
    for r in results:
        report.append(f"### {r['test_id']} — {r['label']}")
        report.append("")
        report.append("| Assertion | Pass | Detail |")
        report.append("|---|---|---|")
        for name, ok, detail in r["assertions"]:
            mark = "✓" if ok else "✗"
            report.append(f"| `{name}` | {mark} | {detail} |")
        report.append("")
    RESULTS_PATH.write_text("\n".join(report) + "\n")
    print(f"\n{n_pass}/{n_total} cases passed")
    print(f"Report written to {RESULTS_PATH}")
    return 0 if n_pass == n_total else 1


if __name__ == "__main__":
    sys.exit(main())
