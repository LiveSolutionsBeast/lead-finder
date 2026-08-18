#!/usr/bin/env python3
"""
tests/test_plugin_email_chain.py
================================
Focused E2E test for the LinkedIn plugin email chain.

Requires a running lead-finder server on http://127.0.0.1:8798 and an
API key in lf_config.json.

Strategy: pre-seed a company with a real website in the DB, then push a
profile pointing at that company. The matcher routes to
new_contact_existing_company, and the unified chain derives + SMTP-validates
an email for the new contact.
"""

import json
import os
import random
import sqlite3
import string
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SERVER = "http://127.0.0.1:8798"
CFG = json.loads((ROOT / "lf_config.json").read_text())
# Mirror lf_config.py:_resolve_secret() — env var wins, then the envvar
# pointer, then the config value. lf_config.json ships with an empty
# "lf_api_key" and points to "LF_API_KEY" via "lf_api_key_envvar".
API_KEY = (
    os.environ.get("LF_API_KEY")
    or os.environ.get(CFG.get("lf_api_key_envvar", "LF_API_KEY"))
    or CFG.get("lf_api_key", "")
)
DB_PATH = ROOT / "lf.db"


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


def random_slug():
    return "t5-" + "".join(random.choices(string.ascii_lowercase + string.digits, k=12))


def cleanup_company_contacts(company_id: int, slug: str):
    conn = get_db()
    cur = conn.cursor()
    rows = cur.execute("SELECT id FROM contacts WHERE company_id=? OR linkedin_slug=?", (company_id, slug)).fetchall()
    for r in rows:
        cur.execute("DELETE FROM contact_experience WHERE contact_id=?", (r["id"],))
        cur.execute("DELETE FROM contacts WHERE id=?", (r["id"],))
    cur.execute("DELETE FROM pending_pushes WHERE linkedin_slug=?", (slug,))
    conn.commit()
    conn.close()


def seed_company(name: str, website: str) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO companies (name, website, source, data_provenance) VALUES (?, ?, ?, ?)",
        (name, website, "TEST", "test_plugin_email_chain"),
    )
    company_id = cur.lastrowid
    conn.commit()
    conn.close()
    return company_id


def delete_company(company_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM companies WHERE id=?", (company_id,))
    conn.commit()
    conn.close()


def build_payload(slug: str, company: str, email: str | None = None):
    return {
        "linkedin_url": f"https://www.linkedin.com/in/{slug}/",
        "linkedin_slug": slug,
        "full_name": "Testy McTestface",
        "first_name": "Testy",
        "last_name": "McTestface",
        "current_title": "Director of Testing",
        "current_company": company,
        "location": "Los Angeles, CA",
        "email": email,
        "is_test": 1,
        "experience": [
            {
                "title": "Director of Testing",
                "company": company,
                "start_date": "2024-01",
                "end_date": None,
                "is_current": True,
            }
        ],
    }


def main():
    failures = []

    # Use a real company domain to test full chain.
    unique_name = f"TestCo {datetime.now(timezone.utc).isoformat()}"
    company_id = seed_company(unique_name, "https://cloudflare.com")

    try:
        # Test 1: new contact at existing company with real website
        ct = None  # bound here so Test 2's guard works even if Test 1 fails
        slug1 = random_slug()
        payload1 = build_payload(slug1, unique_name)
        print(f"Test 1: new contact at existing company company_id={company_id} slug={slug1}")
        status, body = http_post("/api/inject/linkedin-profile", payload1)
        if status != 200:
            failures.append(("t1_http", f"status={status} body={body}"))
        else:
            action = body.get("matched_action")
            smtp = body.get("smtp_validation_status")
            print(f"  action={action} smtp_validation_status={smtp}")
            if action not in ("new_contact_existing_company", "update_existing"):
                failures.append(("t1_action", f"expected new_contact_existing_company/update_existing got {action}"))
            if smtp is None:
                failures.append(("t1_smtp", "smtp_validation_status is None — chain did not run"))

            conn = get_db()
            ct = conn.execute("SELECT * FROM contacts WHERE linkedin_slug=?", (slug1,)).fetchone()
            conn.close()
            if not ct:
                failures.append(("t1_contact", "contact row not found"))
            else:
                print(f"  email={ct['email']} status={ct['smtp_validation_status']} method={ct['validation_method']} ready={ct['email_ready_for_export']}")
                if not ct["email"]:
                    failures.append(("t1_email", "contact email is NULL"))
                if not ct["validation_method"]:
                    failures.append(("t1_method", "validation_method is NULL"))

        # Test 2: same slug again — should dedup to update_existing
        if ct is not None:
            print(f"Test 2: re-push same slug slug={slug1}")
            status2, body2 = http_post("/api/inject/linkedin-profile", payload1)
            if status2 != 200:
                failures.append(("t2_http", f"status={status2} body={body2}"))
            else:
                action2 = body2.get("matched_action")
                print(f"  action={action2}")
                if action2 != "update_existing":
                    failures.append(("t2_dedup", f"expected update_existing got {action2}"))

        # Cleanup test 1+2
        cleanup_company_contacts(company_id, slug1)

        # Test 3: update_existing with popup email
        slug2 = random_slug()
        # Pre-create a contact at the same company to route to update_existing.
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO contacts (company_id, first_name, last_name, full_name, title, linkedin_slug, source_primary, is_test) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (company_id, "Pop", "Up", "Pop Up", "Tester", slug2, "test"),
        )
        pre_contact_id = cur.lastrowid
        conn.commit()
        conn.close()

        payload2 = build_payload(slug2, unique_name, email="support@example.com")
        print(f"Test 3: update_existing with popup email slug={slug2}")
        status3, body3 = http_post("/api/inject/linkedin-profile", payload2)
        if status3 != 200:
            failures.append(("t3_http", f"status={status3} body={body3}"))
        else:
            smtp3 = body3.get("smtp_validation_status")
            print(f"  action={body3.get('matched_action')} smtp_validation_status={smtp3}")
            if smtp3 is None:
                failures.append(("t3_smtp", "smtp_validation_status is None for popup email"))
            conn = get_db()
            ct3 = conn.execute("SELECT * FROM contacts WHERE id=?", (pre_contact_id,)).fetchone()
            conn.close()
            if ct3 and ct3["email"] != "support@example.com":
                failures.append(("t3_email", f"expected support@example.com got {ct3['email']}"))

        cleanup_company_contacts(company_id, slug2)

    finally:
        delete_company(company_id)

    if failures:
        print("\nFAILURES:")
        for name, detail in failures:
            print(f"  {name}: {detail}")
        sys.exit(1)
    print("\nAll plugin email-chain tests passed.")


if __name__ == "__main__":
    main()
