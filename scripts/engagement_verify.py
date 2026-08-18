#!/usr/bin/env python3
"""
engagement_verify.py — Lead Finder ↔ Lead-Seeker engagement verification bridge
================================================================================

This module safely bridges lead-finder (lf.db) and lead-seeker (track.db) for
proof-by-engagement email validation.

Two modes
---------
1. Legacy "spray" mode: stage a batch of contacts, approve, send, then check
   results. Kept for backward compatibility.
2. Cornerstone mode (new): pick ONE low-level contact per unproven company, send
   a small number of pattern candidates, wait for a delivery/open proof, then
   auto-apply the proven pattern to every sibling contact and stage a Salesforce
   push.

Cornerstone workflow
--------------------
1. Pick the safest cornerstone contact per company (low-level title; senior
   titles are blocked).
2. Stage ≤3 proof emails for that contact in `engagement_verify_queue`.
3. Approve + send via `--send plan.json` (uses live Graph; throttle default 8s).
4. A cron watcher polls `lead-seeker/track.db` every 2h and, on proof, marks the
   company pattern proven, derives emails for all sibling contacts, and stages
   Salesforce payloads.
5. A separate `--confirm` step (or the cron with `ENGAGEMENT_VERIFY_AUTOSEND=1`)
   actually pushes to Salesforce.

Safety
------
- DEFAULT dry_run=True. Real sends require explicit approval + batch_id.
- Cornerstone auto-send requires `ENGAGEMENT_VERIFY_AUTOSEND=1`.
- `--watchdog --once` (cron default) is dry-run for SF; add `--confirm` to push.
- Senior titles (CEO, VP, Director, President, etc.) are never chosen as
  cornerstones.

Quick commands
--------------
    # Pick + stage one company
    python3 scripts/engagement_verify.py --pick-cornerstone --company-id 599
    python3 scripts/engagement_verify.py --stage-cornerstone --company-id 599 -o /tmp/c599.json

    # Send after review (approves automatically when using --send)
    python3 scripts/engagement_verify.py --send /tmp/c599.json --throttle 8.0

    # Check results for that company after 24h
    python3 scripts/engagement_verify.py --check-company --company-id 599 --wait-hours 24

    # Manual status / dry-run SF push / real SF push
    python3 scripts/engagement_verify.py --status --company-id 599
    python3 scripts/engagement_verify.py --push-sf --company-id 599 --dry-run
    python3 scripts/engagement_verify.py --push-sf --company-id 599 --confirm

    # Cron watcher (dry-run by default; add --confirm only after review)
    python3 scripts/engagement_verify.py --watchdog --once
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

# Ensure we import lead-finder modules from this workspace
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from lf_db import get_db, init_db, patch_company, patch_contact, write_contact_validation, upsert_contact, get_company, create_company_manual
from lf_email_patterns import generate_candidates, extract_domain_from_website, derive_email, EMAIL_PATTERNS

# lead-seeker lives next door to this repo
LEAD_SEEKER_DIR = Path(os.environ.get("LEAD_SEEKER_DIR", "/home/anthonyturgman/lead-seeker"))
TRACK_DB_PATH = Path(os.environ.get("LEAD_SEEKER_TRACK_DB", LEAD_SEEKER_DIR / "track.db"))

# Add lead-seeker to import path for live_solutions_outreach
sys.path.insert(0, str(LEAD_SEEKER_DIR))

# Default tracking hub per user spec
HUB = "lead-finder-verify"

# Table name for the bridge queue
QUEUE_TABLE = "engagement_verify_queue"

# ── Cornerstone workflow constants ──────────────────────────────────────────

# Lower rank number = preferred cornerstone (lower-level, safer-to-prove title)
LOW_LEVEL_TITLE_RANK = {
    "assistant": 1,
    "coordinator": 2,
    "specialist": 3,
    "analyst": 4,
    "admin": 5,
    "administrator": 6,
    "engineer": 7,
    "manager": 8,
    "supervisor": 9,
    "associate": 10,
    "representative": 11,
}

# These titles are too senior / high-stakes to use as a pattern-proof cornerstone.
SENIOR_TITLE_BLOCKLIST = {
    "ceo", "chief", "president", "owner", "founder", "partner",
    "chairman", "chairwoman", "chairperson", "vice chairman",
    "cfo", "coo", "cto", "cmo", "chro", "clo", "cio", "ciso", "cao", "cpo",
    "vp", "vice president",
    "director", "executive director",
    "general manager", "gm",
    "senior vice president", "svp", "evp", "executive vice president",
    "managing director",
    "principal", "founding partner",
    "former executive", "former president", "former vice president",
    "former director", "former senior vice president", "former chief",
}

MAX_CANDIDATES_PER_CORNERSTONE = 3
MAX_BATCH_SEND = 20
CORNERSTONE_LOCK_HOURS = 96
PATTERN_PROOF_WAIT_HOURS = 24
AUTO_APPLY_SIBLING_PROOF_WAIT_HOURS = 24


SF_MODULE_PATH = Path("/home/anthonyturgman/ai-stack/scripts/ops/b7_salesforce_module.py")
ESPOCRM_BRIDGE_PATH = Path("/home/anthonyturgman/espocrm/scripts/lead-finder-bridge.py")
# Allow override via env, e.g. http://172.18.0.25/api/v1 if DNS name isn't resolvable.
ESPOCRM_API_URL = os.environ.get("ESPOCRM_API_URL", "http://espocrm:80/api/v1")


# ── Schema ──────────────────────────────────────────────────────────────────

def _ensure_queue_table() -> None:
    """Create engagement_verify_queue table if it doesn't exist, and migrate it."""
    from lf_db import _add_column_if_missing
    conn = get_db()
    cur = conn.cursor()
    cur.executescript(f"""
        CREATE TABLE IF NOT EXISTS {QUEUE_TABLE} (
            token               TEXT PRIMARY KEY,
            contact_id          INTEGER NOT NULL REFERENCES contacts(id),
            company_id          INTEGER NOT NULL REFERENCES companies(id),
            candidate_email     TEXT NOT NULL,
            pattern_name        TEXT,
            pattern_template    TEXT,
            subject             TEXT,
            sent_at             TEXT,
            dry_run             INTEGER DEFAULT 1,
            approved            INTEGER DEFAULT 0,
            approval_batch      TEXT,
            result_status       TEXT,      -- 'pending' | 'opened' | 'delivered' | 'bounced' | 'expired'
            result_checked_at   TEXT,
            result_open_count   INTEGER,
            result_bounce_status TEXT,
            result_bounce_detail TEXT,
            contact_updated_at  TEXT,
            cornerstone         INTEGER DEFAULT 0,
            sf_pushed           INTEGER DEFAULT 0,
            sf_pushed_at        TEXT,
            sf_lead_id          TEXT,
            notes               TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_evq_contact ON {QUEUE_TABLE}(contact_id);
        CREATE INDEX IF NOT EXISTS idx_evq_company ON {QUEUE_TABLE}(company_id);
        CREATE INDEX IF NOT EXISTS idx_evq_batch ON {QUEUE_TABLE}(approval_batch);
        CREATE INDEX IF NOT EXISTS idx_evq_status ON {QUEUE_TABLE}(result_status);
    """)
    _add_column_if_missing(cur, QUEUE_TABLE, "cornerstone", "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, QUEUE_TABLE, "sf_pushed", "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, QUEUE_TABLE, "sf_pushed_at", "TEXT")
    _add_column_if_missing(cur, QUEUE_TABLE, "sf_lead_id", "TEXT")
    _add_column_if_missing(cur, QUEUE_TABLE, "tracker_token", "TEXT")
    # EspoCRM push tracking (added 2026-08-11). EspoCRM is the default
    # destination; sf_* columns remain for legacy / rollback paths.
    _add_column_if_missing(cur, QUEUE_TABLE, "espo_pushed", "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, QUEUE_TABLE, "espo_pushed_at", "TEXT")
    _add_column_if_missing(cur, QUEUE_TABLE, "espo_lead_id", "TEXT")
    _add_column_if_missing(cur, QUEUE_TABLE, "espo_lead_url", "TEXT")
    conn.commit()
    conn.close()


# ── Helpers ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_eligible_contacts(limit: int = 500) -> list[dict]:
    """
    Contacts at companies that have a website but no proven email pattern,
    where the contact itself has no validated email.

    Excludes test contacts.
    """
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT c.id, c.first_name, c.last_name, c.full_name, c.title,
               co.id AS company_id, co.name AS company_name, co.website,
               co.email_pattern AS stored_pattern,
               co.email_pattern_proof_status AS proof_status
        FROM contacts c
        JOIN companies co ON co.id = c.company_id
        WHERE (c.email IS NULL OR c.email = '')
          AND (co.website IS NOT NULL AND co.website != '')
          AND (co.email_pattern IS NULL OR co.email_pattern_proof_status NOT IN ('Okay to Send', 'Catch-All'))
          AND c.is_test = 0
        ORDER BY co.name, c.last_name, c.first_name
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _clean_name_part(name: str) -> str:
    """Strip suffixes, middle initials, and punctuation from a name for pattern generation."""
    if not name:
        return ""
    name = name.strip().lower()
    # Remove common suffixes
    for suffix in ("jr", "sr", "ii", "iii", "iv", "v", "phd", "md", "cpa", "pe"):
        if name.endswith(f" {suffix}") or name.endswith(f",{suffix}"):
            name = name[: name.rfind(suffix)].rstrip(", ")
    # Strip characters that don't belong in an email localpart
    cleaned = "".join(ch for ch in name if ch.isalpha() or ch.isspace())
    return cleaned.strip()


def _split_first_last(full_name: str) -> tuple[str, str]:
    """Best-effort first/last split from a full name."""
    parts = full_name.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def _priority_patterns() -> list[str]:
    """Highest-likelihood global patterns, ordered by popularity."""
    return [
        "first.last",
        "firstlast",
        "flast",
        "f.last",
        "first_last",
        "first",
        "last",
        "firstl",
    ]


def _build_candidates_for_contact(contact: dict, max_candidates: int = 4) -> list[dict]:
    """
    Generate top email candidates for a contact.

    Uses the stored unproven pattern first if present, then falls back to the
    global pattern pool ordered by _priority_patterns().
    """
    full_name = (contact.get("full_name") or f"{contact.get('first_name', '')} {contact.get('last_name', '')}").strip()
    first_raw = contact.get("first_name") or ""
    last_raw = contact.get("last_name") or ""
    if not first_raw or not last_raw:
        first_raw, last_raw = _split_first_last(full_name)

    first = _clean_name_part(first_raw)
    last = _clean_name_part(last_raw)
    if not first or not last:
        return []

    domain = extract_domain_from_website(contact.get("website") or "")
    if not domain:
        return []

    all_candidates = generate_candidates(first, last, domain)
    priority = _priority_patterns()
    name_to_template = {name: tpl for name, tpl in EMAIL_PATTERNS}

    # Try the stored pattern first (if any)
    stored = contact.get("stored_pattern") or ""
    stored_email: Optional[str] = None
    stored_template: Optional[str] = None
    if stored:
        # If the stored pattern is already a template, use it directly.
        if "{" in stored or "@" in stored:
            stored_template = stored if "@" in stored else stored + f"@{domain}"
            stored_template = stored_template.replace("{domain}", domain)
            stored_email = derive_email(first, last, stored_template)
        else:
            # Bare localpart-style stored pattern; derive a candidate email the
            # legacy way but do NOT promote this as a reusable template.
            stored_email = derive_email(first, last, stored + f"@{domain}")
            stored_template = None

    ordered: list[dict] = []
    seen = set()

    if stored_email and stored_email.lower() not in seen and stored_template:
        seen.add(stored_email.lower())
        ordered.append({
            "pattern_name": "stored_unproven",
            "pattern_template": stored_template,
            "email": stored_email,
            "priority": -1,
        })

    for cand in all_candidates:
        email = cand["email"]
        if email.lower() in seen:
            continue
        try:
            prio = priority.index(cand["pattern"])
        except ValueError:
            prio = 99
        seen.add(email.lower())
        bare_template = name_to_template.get(cand["pattern"], cand["pattern"])
        if "@" not in bare_template:
            full_template = bare_template + f"@{domain}"
        else:
            full_template = bare_template.replace("{domain}", domain)
        ordered.append({
            "pattern_name": cand["pattern"],
            "pattern_template": full_template,
            "email": email,
            "priority": prio,
        })

    ordered.sort(key=lambda x: x["priority"])
    return ordered[:max_candidates]


# ── Preview / Stage ─────────────────────────────────────────────────────────

def preview_candidates(limit: int = 500) -> list[dict]:
    """Return the staged send plan without doing any real work."""
    _ensure_queue_table()
    contacts = _get_eligible_contacts(limit=limit)
    plan: list[dict] = []
    for contact in contacts:
        candidates = _build_candidates_for_contact(contact)
        if not candidates:
            continue
        plan.append({
            "contact_id": contact["id"],
            "company_id": contact["company_id"],
            "full_name": contact.get("full_name") or f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip(),
            "title": contact.get("title") or "",
            "company_name": contact.get("company_name") or "",
            "website": contact.get("website") or "",
            "candidates": candidates,
        })
    return plan


def stage_batch(plan: list[dict], batch_id: Optional[str] = None) -> tuple[str, int]:
    """Persist a staged plan to the queue table (dry_run=1, approved=0)."""
    _ensure_queue_table()
    if not batch_id:
        batch_id = f"ev-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
    conn = get_db()
    cur = conn.cursor()
    inserted = 0
    for entry in plan:
        for cand in entry["candidates"]:
            token = str(uuid4())
            cur.execute(f"""
                INSERT OR IGNORE INTO {QUEUE_TABLE}
                (token, contact_id, company_id, candidate_email, pattern_name, pattern_template,
                 subject, dry_run, approved, approval_batch, result_status, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, 'pending', ?)
            """, (
                token,
                entry["contact_id"],
                entry["company_id"],
                cand["email"],
                cand["pattern_name"],
                cand["pattern_template"],
                _default_subject(entry),
                batch_id,
                json.dumps({"staged_at": _now_iso()}),
            ))
            if cur.rowcount > 0:
                inserted += 1
    conn.commit()
    conn.close()
    return batch_id, inserted


def _default_subject(entry: dict) -> str:
    """Compose a human subject line from the company name."""
    company = entry.get("company_name") or "your company"
    return f"Live Solutions × {company}"


# ── Send ────────────────────────────────────────────────────────────────────

def _load_live_solutions_outreach():
    """Lazy import of lead-seeker's outreach module."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("live_solutions_outreach", LEAD_SEEKER_DIR / "live_solutions_outreach.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_signature() -> str:
    """Read the Live Solutions signature template."""
    tmpl_dir = Path(os.environ.get("LS_TEMPLATE_DIR", "/home/anthonyturgman/ai-stack/templates/email"))
    return (tmpl_dir / "live-solutions-signature.html").read_text(encoding="utf-8")


def _load_body(path: Optional[str] = None) -> str:
    """Read the Live Solutions outreach body template."""
    if path:
        return Path(path).read_text(encoding="utf-8")
    tmpl_dir = Path(os.environ.get("LS_TEMPLATE_DIR", "/home/anthonyturgman/ai-stack/templates/email"))
    default = os.environ.get("LS_BODY_TEMPLATE", "live-solutions-outreach.html")
    return (tmpl_dir / default).read_text(encoding="utf-8")


def send_approved_batch(batch_id: str, dry_run: bool = True, throttle_seconds: float = 15.0,
                         body_template_path: Optional[str] = None) -> dict:
    """
    Send (or dry-run) every pending row in an approved batch.

    dry_run=True: renders + records tokens but does NOT call Graph or tracker.
    dry_run=False: sends real email via live_solutions_outreach, registers tracker.

    Default throttle reduced to 15s (was 60s) to improve throughput while still
    avoiding spam/rate-limit flags.
    """
    _ensure_queue_table()
    lso = _load_live_solutions_outreach() if not dry_run else None
    body_template = _load_body(body_template_path)
    signature_html = _load_signature()

    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(f"""
        SELECT * FROM {QUEUE_TABLE}
        WHERE approval_batch=? AND approved=1 AND result_status='pending'
        ORDER BY rowid
    """, (batch_id,)).fetchall()
    conn.close()

    results = {"sent": 0, "dry_run": dry_run, "batch_id": batch_id, "rows": []}
    for row in rows:
        token = row["token"]
        contact_id = row["contact_id"]
        email = row["candidate_email"]
        subject = row["subject"] or _default_subject({"company_name": row["company_id"]})

        contact = _get_contact(contact_id)
        if not contact:
            _update_queue(token, result_status="error", notes=f"Contact {contact_id} not found")
            continue

        merge_vars = {
            "first_name": contact.get("first_name") or contact.get("full_name", "").split()[0],
            "signature_html": signature_html,
        }
        contact_fields = {
            "id": None,
            "first_name": contact.get("first_name"),
            "last_name": contact.get("last_name"),
            "company": contact.get("company_name"),
            "title": contact.get("title"),
            "email": email,
        }

        if dry_run:
            _update_queue(token, sent_at=_now_iso(), notes=f"dry-run preview for {email}")
            results["sent"] += 1
            results["rows"].append({
                "token": token,
                "email": email,
                "contact_id": contact_id,
                "subject": subject,
                "dry_run": True,
            })
            continue

        # Real send
        try:
            res = lso.send_email(
                to_addr=email,
                subject=subject,
                body_html=body_template,
                to_name=contact.get("full_name") or "",
                hub=HUB,
                merge_vars=merge_vars,
                contact_fields=contact_fields,
                create_task=False,  # We create/update SF lead separately if needed
                dry_run=False,
                append_signature=False,  # signature is passed via merge_vars
            )
            _update_queue(
                token,
                sent_at=_now_iso(),
                dry_run=0,
                notes=json.dumps({
                    "graph_status": res.get("send", {}).get("graph_status"),
                    "tracker_registered": res.get("tracking", {}).get("tracker_registered"),
                    "internet_message_id": res.get("send", {}).get("internet_message_id"),
                    "warnings": res.get("warnings"),
                }),
            )
            # The tracker token generated inside send_email differs from the
            # staging token. Store it so check_results can find the track.db row.
            real_token = res.get("tracking", {}).get("token")
            if real_token and real_token != token:
                conn2 = get_db()
                conn2.execute(
                    f"UPDATE {QUEUE_TABLE} SET tracker_token=? WHERE token=?",
                    (real_token, token),
                )
                conn2.commit()
                conn2.close()
            results["sent"] += 1
            results["rows"].append({
                "token": real_token or token,
                "email": email,
                "contact_id": contact_id,
                "subject": subject,
                "graph_status": res.get("send", {}).get("graph_status"),
                "internet_message_id": res.get("send", {}).get("internet_message_id"),
                "tracker_token": real_token,
            })
            time.sleep(throttle_seconds)
        except Exception as e:
            _update_queue(token, result_status="error", notes=f"send failed: {e}")
            results["rows"].append({"token": token, "email": email, "error": str(e)})

    return results


def _get_contact(contact_id: int) -> Optional[dict]:
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    row = cur.execute("""
        SELECT c.*, co.name AS company_name, co.website
        FROM contacts c
        JOIN companies co ON co.id = c.company_id
        WHERE c.id=?
    """, (contact_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _update_queue(token: str, **kwargs) -> None:
    """Update one or more columns on a queue row."""
    allowed = {
        "sent_at", "dry_run", "approved", "result_status", "result_checked_at",
        "result_open_count", "result_bounce_status", "result_bounce_detail",
        "contact_updated_at", "notes", "cornerstone",
        "sf_pushed", "sf_pushed_at", "sf_lead_id",
        # EspoCRM push tracking (added 2026-08-11). Default destination.
        "espo_pushed", "espo_pushed_at", "espo_lead_id", "espo_lead_url",
    }
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return
    conn = get_db()
    cur = conn.cursor()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    cur.execute(f"UPDATE {QUEUE_TABLE} SET {set_clause} WHERE token=?", list(updates.values()) + [token])
    conn.commit()
    conn.close()


# ── Check Results ───────────────────────────────────────────────────────────

def check_results(batch_id: Optional[str] = None, wait_hours: int = 24) -> dict:
    """
    Query lead-seeker track.db for every pending token, then update lead-finder.

    wait_hours is a soft filter: only tokens sent at least wait_hours ago are
    evaluated; newer tokens are left as pending.
    """
    _ensure_queue_table()
    if not TRACK_DB_PATH.exists():
        return {"error": f"track.db not found at {TRACK_DB_PATH}"}

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=wait_hours)).isoformat()

    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    if batch_id:
        rows = cur.execute(f"""
            SELECT * FROM {QUEUE_TABLE}
            WHERE approval_batch=? AND result_status IN ('pending', 'sent')
            AND (sent_at IS NULL OR sent_at <= ?)
            ORDER BY rowid
        """, (batch_id, cutoff)).fetchall()
    else:
        rows = cur.execute(f"""
            SELECT * FROM {QUEUE_TABLE}
            WHERE result_status IN ('pending', 'sent')
            AND (sent_at IS NULL OR sent_at <= ?)
            ORDER BY rowid
        """, (cutoff,)).fetchall()
    conn.close()

    if not rows:
        return {"checked": 0, "message": "No pending tokens past the wait window"}

    track_conn = sqlite3.connect(str(TRACK_DB_PATH))
    track_conn.row_factory = sqlite3.Row
    track_cur = track_conn.cursor()

    summary = {"checked": 0, "opened": 0, "human_opened": 0, "bot_opened": 0, "delivered": 0, "bounced": 0, "still_pending": 0}
    for row in rows:
        token = row["token"]
        # The tracker token (generated inside send_email) may differ from the
        # staging token. Prefer tracker_token if present.
        track_token = row["tracker_token"] if "tracker_token" in row.keys() and row["tracker_token"] else token
        track_row = track_cur.execute("""
            SELECT opened_at, open_count, open_type, bounce_status, bounce_detail
            FROM sends WHERE token=?
        """, (track_token,)).fetchone()
        if not track_row:
            continue

        summary["checked"] += 1
        opened_at = track_row["opened_at"]
        open_count = track_row["open_count"] or 0
        open_type = track_row["open_type"] or "human"  # legacy rows w/o open_type treated as human-safe
        bounce_status = track_row["bounce_status"]
        bounce_detail = track_row["bounce_detail"]

        # Bounces override everything: hard fail.
        if bounce_status == "bounced":
            _mark_contact_result(row, "Do Not Send", open_count, bounce_status, bounce_detail)
            summary["bounced"] += 1
            continue

        # A confirmed human open is proof.
        if opened_at and open_type == "human":
            _mark_contact_result(row, "Okay to Send", open_count, bounce_status, bounce_detail)
            summary["opened"] += 1
            summary["human_opened"] += 1
            continue

        # A bot-only open must never count as proof. Keep pending.
        if opened_at and open_type == "bot":
            _update_queue(token, result_status="pending", result_checked_at=_now_iso(),
                          notes="bot-only open recorded; not counted as proof")
            summary["bot_opened"] += 1
            summary["still_pending"] += 1
            continue

        # No open yet, but Graph delivery confirmation is acceptable proof.
        if bounce_status == "delivered":
            _mark_contact_result(row, "Okay to Send", open_count, bounce_status, bounce_detail)
            summary["delivered"] += 1
            continue

        # Nothing decisive yet.
        _update_queue(token, result_status="pending", result_checked_at=_now_iso())
        summary["still_pending"] += 1

    track_conn.close()
    return summary


def _mark_contact_result(
    queue_row: sqlite3.Row,
    status: str,
    open_count: int,
    bounce_status: Optional[str],
    bounce_detail: Optional[str],
) -> None:
    """Persist engagement result back to the contact and optionally the company pattern."""
    token = queue_row["token"]
    contact_id = queue_row["contact_id"]
    company_id = queue_row["company_id"]
    email = queue_row["candidate_email"]
    pattern_name = queue_row["pattern_name"]
    pattern_template = queue_row["pattern_template"]
    now = _now_iso()

    # Update contact email + validation status
    ready = 1 if status == "Okay to Send" else 0
    rejected_reason = None
    if status == "Do Not Send":
        rejected_reason = bounce_detail or "Engagement verification: hard bounce"

    patch_contact(contact_id, {
        "email": email,
        "is_derived_email": 0,
        "email_ready_for_export": ready,
        "email_rejected_reason": rejected_reason,
        "smtp_validation_status": status,
        "smtp_validated_at": now,
        "validation_method": "engagement_verify",
        "validation_checked_at": now,
        "pipeline_stage": "validated",
    })

    # Update queue
    result_status = "opened" if status == "Okay to Send" and open_count else ("delivered" if status == "Okay to Send" else "bounced")
    _update_queue(
        token,
        result_status=result_status,
        result_checked_at=now,
        result_open_count=open_count,
        result_bounce_status=bounce_status,
        result_bounce_detail=bounce_detail,
        contact_updated_at=now,
    )

    # If this pattern succeeded on HUMAN evidence, promote it on the company row as proven.
    is_human = result_status == "opened" and open_count > 0
    if status == "Okay to Send" and pattern_template and "@" in pattern_template:
        _promote_company_pattern(company_id, pattern_template, email, open_count, is_human=is_human)


def _promote_company_pattern(company_id: int, pattern_template: str, proof_email: str, open_count: int, is_human: bool = True) -> None:
    """
    Mark a pattern as proven via engagement evidence.

    Hardened: only promotes on a human open (open_type='human') plus at least
    one open_count. Bot-only opens never promote the pattern.
    """
    if not is_human or open_count < 1:
        return
    confidence = 0.85 if open_count > 0 else 0.75
    patch_company(company_id, {
        "email_pattern": pattern_template,
        "email_pattern_confidence": confidence,
        "email_pattern_source": "engagement_proven",
        "email_pattern_proof_email": proof_email,
        "email_pattern_proof_status": "Okay to Send",
        "email_pattern_proven_at": _now_iso(),
        "email_pattern_unproved_reason": None,
    })


# ── Cornerstone workflow helpers ──────────────────────────────────────────────

def _title_rank(title: Optional[str]) -> int:
    """
    Return the cornerstone-suitability rank for a title.
    Lower = better (safer, lower-level). Blocklisted titles return infinity.
    """
    if not title:
        return 999
    t = title.lower().strip()
    # Strip punctuation
    t = re.sub(r"[^a-z0-9\s]", "", t)
    tokens = set(t.split())
    # Block any title containing a blocklisted senior token.
    # Multi-word blocklist entries must match as a phrase; single-word blocklist tokens
    # must appear as whole words so "Coordinator" doesn't match "coo".
    for senior in SENIOR_TITLE_BLOCKLIST:
        if " " in senior:
            if senior in t:
                return 9999
        elif senior in tokens:
            return 9999
    # C-suite abbreviations must be whole words only.
    c_suite_abbreviations = {"ceo", "cfo", "coo", "cto", "cmo", "chro", "clo", "cio", "ciso", "cao", "cpo", "svp", "evp", "gm", "vp"}
    for token_l in tokens:
        if token_l in c_suite_abbreviations:
            return 9999
    # Exact match in low-level rank
    if t in LOW_LEVEL_TITLE_RANK:
        return LOW_LEVEL_TITLE_RANK[t]
    # Partial token match for multi-word titles
    for token_l in tokens:
        if token_l in LOW_LEVEL_TITLE_RANK:
            return LOW_LEVEL_TITLE_RANK[token_l]
    return 100


def _is_unvalidated_contact(contact: dict) -> bool:
    """A contact is eligible for cornerstone selection if not already proven ready."""
    return not contact.get("email_ready_for_export")


def _get_company_contacts(company_id: int) -> list[dict]:
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT c.*, co.name AS company_name, co.website
        FROM contacts c
        JOIN companies co ON co.id = c.company_id
        WHERE c.company_id=? AND c.is_test=0
        ORDER BY c.id
    """, (company_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _get_unproven_companies() -> list[dict]:
    """Return companies eligible for cornerstone proof: website, no proven pattern."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT id, name, website, email_pattern, email_pattern_proof_status
        FROM companies
        WHERE (website IS NOT NULL AND website != '')
          AND (email_pattern IS NULL OR email_pattern_proof_status NOT IN ('Okay to Send', 'Catch-All'))
        ORDER BY name
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _is_cornerstone_batch(batch_id: str) -> bool:
    """Return True if any row in this batch is a cornerstone send."""
    conn = get_db()
    cur = conn.cursor()
    count = cur.execute(f"SELECT COUNT(*) FROM {QUEUE_TABLE} WHERE approval_batch=? AND cornerstone=1", (batch_id,)).fetchone()[0]
    conn.close()
    return count > 0


def _get_active_company_run(company_id: int) -> Optional[dict]:
    """Return the most recent engagement_company_runs row for a company."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    row = cur.execute("""
        SELECT * FROM engagement_company_runs
        WHERE company_id=?
        ORDER BY queued_at DESC
        LIMIT 1
    """, (company_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _get_all_active_runs() -> list[dict]:
    """Return all engagement_company_runs rows in non-terminal states."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT r.*, co.name AS company_name, co.website,
               c.full_name AS cornerstone_name, c.title AS cornerstone_title,
               c.email AS cornerstone_email
        FROM engagement_company_runs r
        JOIN companies co ON co.id = r.company_id
        LEFT JOIN contacts c ON c.id = r.cornerstone_contact_id
        WHERE r.state IN ('queued', 'sent', 'proof_succeeded')
        ORDER BY r.queued_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _create_company_run(company_id: int, cornerstone_contact_id: int, pattern_template: str,
                        state: str = "queued", notes: str = "") -> str:
    """Create an engagement_company_runs row and return the run_id."""
    run_id = f"ecr-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO engagement_company_runs
        (run_id, company_id, cornerstone_contact_id, pattern_template, state, queued_at, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (run_id, company_id, cornerstone_contact_id, pattern_template, state, _now_iso(), notes))
    conn.commit()
    conn.close()
    return run_id


def _update_company_run(run_id: str, **kwargs) -> None:
    """Update columns on an engagement_company_runs row."""
    allowed = {"state", "sent_at", "proven_at", "sf_pushed_at", "apply_count", "sf_push_count", "last_error", "notes"}
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return
    conn = get_db()
    cur = conn.cursor()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    cur.execute(f"UPDATE engagement_company_runs SET {set_clause} WHERE run_id=?", list(updates.values()) + [run_id])
    conn.commit()
    conn.close()


def pick_cornerstone(company_id: int, *, dry_run: bool = False,
                     preferred_contact_id: Optional[int] = None) -> Optional[dict]:
    """
    Pick the single best cornerstone contact for a company.

    If `preferred_contact_id` is provided and the contact is eligible, it becomes
    the cornerstone regardless of title rank. This supports the "user added this
    contact" flow.

    Returns the chosen contact dict (with keys id, full_name, title, etc.), or None
    if no eligible contact exists. In dry-run mode no DB writes occur.
    """
    contacts = _get_company_contacts(company_id)
    eligible = [c for c in contacts if _is_unvalidated_contact(c)]
    if not eligible:
        return None

    # If caller explicitly requested a contact and it's eligible, honor it.
    if preferred_contact_id:
        preferred = next((c for c in eligible if c.get("id") == preferred_contact_id), None)
        if preferred:
            chosen = preferred
        else:
            # Fallback: rank normally
            eligible.sort(key=lambda c: (_title_rank(c.get("title")), c.get("title") == "", c.get("id")))
            chosen = eligible[0]
    else:
        # Rank by title rank, then prefer contacts that already have titles
        eligible.sort(key=lambda c: (_title_rank(c.get("title")), c.get("title") == "", c.get("id")))
        chosen = eligible[0]

    if dry_run:
        return chosen

    now = _now_iso()
    locked_until = (datetime.now(timezone.utc) + timedelta(hours=CORNERSTONE_LOCK_HOURS)).isoformat()
    patch_contact(chosen["id"], {
        "is_cornerstone": 1,
        "cornerstone_picked_at": now,
        "pipeline_stage": "cornerstone_picked",
    })
    patch_company(company_id, {
        "cornerstone_contact_id": chosen["id"],
        "cornerstone_locked_until": locked_until,
    })
    return chosen


def _company_send_count_last_days(company_id: int, days: int = 7) -> int:
    """Count how many engagement_verify_queue rows this company has sent in the last N days."""
    conn = get_db()
    cur = conn.cursor()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    count = cur.execute(f"""
        SELECT COUNT(*) FROM {QUEUE_TABLE}
        WHERE company_id=? AND sent_at IS NOT NULL AND sent_at >= ?
    """, (company_id, since)).fetchone()[0]
    conn.close()
    return count


def enqueue_cornerstone_verification(
    company_id: int,
    *,
    max_candidates: int = MAX_CANDIDATES_PER_CORNERSTONE,
    dry_run: bool = False,
) -> dict:
    """
    Pick (or reuse) a cornerstone contact for a company and stage ≤max_candidates
    proof emails in the queue.

    Returns dict {batch_id, run_id, contact_id, candidates, inserted, dry_run}.
    """
    _ensure_queue_table()

    company = get_company(company_id)
    if not company:
        raise ValueError(f"Company {company_id} not found")
    if not company.get("website"):
        raise ValueError(f"Company {company_id} has no website")

    # Re-use an active cornerstone pick if still locked
    locked_until = company.get("cornerstone_locked_until")
    cornerstone_id = company.get("cornerstone_contact_id")
    chosen = None
    if locked_until and datetime.fromisoformat(locked_until) > datetime.now(timezone.utc):
        chosen = _get_contact(cornerstone_id) if cornerstone_id else None

    if not chosen:
        chosen = pick_cornerstone(company_id, dry_run=dry_run)
        if not chosen:
            raise ValueError(f"No eligible cornerstone contact for company {company_id}")

    # Cap per-company sends
    recent_sends = _company_send_count_last_days(company_id, days=7)
    allowed = max(0, MAX_CANDIDATES_PER_CORNERSTONE - recent_sends)
    if allowed == 0:
        raise ValueError(f"Company {company_id} already hit weekly proof-send cap")

    candidates = _build_candidates_for_contact({**chosen, "website": company["website"], "stored_pattern": company.get("email_pattern")}, max_candidates=allowed)
    if not candidates:
        raise ValueError(f"No email candidates for cornerstone contact {chosen['id']}")

    batch_id = f"ev-cornerstone-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"
    run_id = _create_company_run(company_id, chosen["id"], candidates[0]["pattern_template"], state="queued", notes=f"batch_id={batch_id}")

    if dry_run:
        return {
            "batch_id": batch_id,
            "run_id": run_id,
            "contact_id": chosen["id"],
            "candidates": candidates,
            "inserted": 0,
            "dry_run": True,
        }

    conn = get_db()
    cur = conn.cursor()
    inserted = 0
    for cand in candidates:
        token = str(uuid4())
        cur.execute(f"""
            INSERT OR IGNORE INTO {QUEUE_TABLE}
            (token, contact_id, company_id, candidate_email, pattern_name, pattern_template,
             subject, dry_run, approved, approval_batch, result_status, cornerstone, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, 'pending', 1, ?)
        """, (
            token,
            chosen["id"],
            company_id,
            cand["email"],
            cand["pattern_name"],
            cand["pattern_template"],
            _default_subject({"company_name": company["name"]}),
            batch_id,
            json.dumps({"staged_at": _now_iso(), "run_id": run_id, "cornerstone": True}),
        ))
        if cur.rowcount > 0:
            inserted += 1
    conn.commit()
    conn.close()

    patch_company(company_id, {
        "pattern_apply_count": 0,
        "pattern_apply_batch_id": None,
    })

    return {
        "batch_id": batch_id,
        "run_id": run_id,
        "contact_id": chosen["id"],
        "candidates": candidates,
        "inserted": inserted,
        "dry_run": False,
    }


def _insert_cornerstone_queue_rows(batch_id: str, run_id: str, company_id: int, contact_id: int,
                                   candidates: list[dict], company_name: str) -> int:
    """Insert queue rows for a cornerstone run; return inserted count."""
    _ensure_queue_table()
    conn = get_db()
    cur = conn.cursor()
    inserted = 0
    for cand in candidates:
        token = str(uuid4())
        cur.execute(f"""
            INSERT OR IGNORE INTO {QUEUE_TABLE}
            (token, contact_id, company_id, candidate_email, pattern_name, pattern_template,
             subject, dry_run, approved, approval_batch, result_status, cornerstone, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, 'pending', 1, ?)
        """, (
            token, contact_id, company_id, cand["email"], cand["pattern_name"], cand["pattern_template"],
            _default_subject({"company_name": company_name}), batch_id,
            json.dumps({"staged_at": _now_iso(), "run_id": run_id, "cornerstone": True}),
        ))
        if cur.rowcount > 0:
            inserted += 1
    conn.commit()
    conn.close()
    return inserted


def stage_cornerstone_batch(company_id: int, *, output: Optional[str] = None,
                            max_candidates: int = MAX_CANDIDATES_PER_CORNERSTONE) -> dict:
    """
    Stage a cornerstone verification run for one company. This is the dry-run-safe
    entrypoint: it writes queue/run rows but leaves approved=0.
    """
    result = enqueue_cornerstone_verification(company_id, max_candidates=max_candidates, dry_run=False)
    plan = {
        "batch_id": result["batch_id"],
        "run_id": result["run_id"],
        "company_id": company_id,
        "contact_id": result["contact_id"],
        "candidates": result["candidates"],
        "inserted": result["inserted"],
    }
    if output:
        with open(output, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2, default=str)
    return plan


def approve_batch(batch_id: str) -> int:
    """Approve all pending rows in a batch."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"UPDATE {QUEUE_TABLE} SET approved=1 WHERE approval_batch=? AND approved=0", (batch_id,))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected


def _queue_rows_for_batch(batch_id: str) -> list[dict]:
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(f"SELECT * FROM {QUEUE_TABLE} WHERE approval_batch=? ORDER BY rowid", (batch_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def send_cornerstone_batch(batch_id: str, *, dry_run: bool = False,
                            throttle_seconds: float = 15.0,
                            body_template_path: Optional[str] = None) -> dict:
    """
    Send an approved cornerstone batch. Writes sent_at on queue rows and transitions
    the company run to 'sent'.
    """
    rows = _queue_rows_for_batch(batch_id)
    if not rows:
        return {"sent": 0, "batch_id": batch_id, "error": "No rows found"}

    # Enforce batch send cap
    if len(rows) > MAX_BATCH_SEND:
        return {"sent": 0, "batch_id": batch_id, "error": f"Batch exceeds MAX_BATCH_SEND ({MAX_BATCH_SEND})"}

    run_id = None
    try:
        notes = json.loads(rows[0].get("notes") or "{}")
        run_id = notes.get("run_id")
    except Exception:
        pass

    results = send_approved_batch(batch_id, dry_run=dry_run, throttle_seconds=throttle_seconds,
                                   body_template_path=body_template_path)

    if run_id and not dry_run:
        _update_company_run(run_id, state="sent", sent_at=_now_iso())

    # Mark cornerstone queue rows as 'sent' status so the watcher evaluates them.
    if not dry_run:
        for row in rows:
            if row.get("result_status") == "pending" and row.get("sent_at"):
                _update_queue(row["token"], result_status="sent")

    return {**results, "run_id": run_id, "batch_id": batch_id}


# ── Add / inject a contact for verification ─────────────────────────────────

def add_contact_for_verification(
    *,
    contact_id: Optional[int] = None,
    company_id: Optional[int] = None,
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    title: Optional[str] = None,
    linkedin_url: Optional[str] = None,
    auto_send: bool = False,
    dry_run: bool = False,
) -> dict:
    """
    Add an existing contact (by id) or create a new contact at a company, mark it as
    the cornerstone if none is active, and optionally stage + send immediately.

    Safety: auto_send requires environment ENGAGEMENT_VERIFY_AUTOSEND=1.
    """
    _ensure_queue_table()

    if contact_id:
        contact = _get_contact(contact_id)
        if not contact:
            raise ValueError(f"Contact {contact_id} not found")
        company_id = contact["company_id"]
    elif company_id and first_name and last_name:
        contact_id = upsert_contact(company_id, {
            "first_name": first_name,
            "last_name": last_name,
            "title": title or "",
            "linkedin_url": linkedin_url or "",
            "data_provenance": "engagement_verify_manual_add",
        })
        contact = _get_contact(contact_id)
    else:
        raise ValueError("Need either contact_id or (company_id + first_name + last_name)")

    company = get_company(company_id)
    if not company:
        raise ValueError(f"Company {company_id} not found")
    if not company.get("website"):
        raise ValueError(f"Company {company_id} has no website; cannot verify pattern")

    # Re-use an active locked cornerstone; only pick a new one if none is locked.
    locked_until = company.get("cornerstone_locked_until")
    existing_cornerstone_id = company.get("cornerstone_contact_id")
    chosen = None
    if locked_until and datetime.fromisoformat(locked_until) > datetime.now(timezone.utc):
        chosen = _get_contact(existing_cornerstone_id) if existing_cornerstone_id else None

    if not chosen:
        chosen = pick_cornerstone(company_id, dry_run=dry_run, preferred_contact_id=contact_id)
    if not chosen:
        raise ValueError(f"No eligible cornerstone contact for company {company_id}")

    result = enqueue_cornerstone_verification(company_id, dry_run=dry_run)

    if auto_send:
        autosend_ok = os.environ.get("ENGAGEMENT_VERIFY_AUTOSEND") == "1"
        if not autosend_ok:
            raise RuntimeError("auto_send requires ENGAGEMENT_VERIFY_AUTOSEND=1")
        approve_batch(result["batch_id"])
        send_cornerstone_batch(result["batch_id"], dry_run=dry_run, throttle_seconds=15.0,
                               body_template_path=os.environ.get("LS_BODY_TEMPLATE"))

    return {
        "contact_id": contact["id"],
        "company_id": company_id,
        "batch_id": result["batch_id"],
        "run_id": result["run_id"],
        "candidates": result["candidates"],
        "auto_sent": auto_send and not dry_run,
    }


# ── Pattern apply to siblings ────────────────────────────────────────────────

def apply_proven_pattern(company_id: int, *, dry_run: bool = False, force: bool = False) -> dict:
    """
    Once a company's pattern is proven, derive emails for every other unvalidated
    contact at the company and mark them as derived (but not yet export-ready).

    Idempotent: if pattern_apply_count > 0, the function returns early unless
    force=True.

    Returns {derived, skipped, pattern_template}.
    """
    company = get_company(company_id)
    if not company:
        raise ValueError(f"Company {company_id} not found")
    pattern = company.get("email_pattern")
    if not pattern:
        raise ValueError(f"Company {company_id} has no proven email_pattern")
    if company.get("email_pattern_proof_status") not in ("Okay to Send", "Catch-All"):
        raise ValueError(f"Company {company_id} pattern is not proven")
    if not force and company.get("pattern_apply_count", 0) > 0:
        return {
            "company_id": company_id,
            "pattern_template": pattern,
            "derived": 0,
            "skipped": 0,
            "dry_run": dry_run,
            "already_applied": True,
        }

    contacts = _get_company_contacts(company_id)
    derived = 0
    skipped = 0
    run_id = None
    active_run = _get_active_company_run(company_id)
    if active_run:
        run_id = active_run["run_id"]

    for contact in contacts:
        if contact.get("id") == company.get("cornerstone_contact_id"):
            continue
        # If the contact already has a derived email, just mark it ready.
        if contact.get("email") and contact.get("is_derived_email"):
            if not dry_run:
                patch_contact(contact["id"], {
                    "email_ready_for_export": 1,
                    "email_pattern_note": f"derived_from_proven_pattern:{run_id or 'manual'}",
                    "pipeline_stage": "derived_from_proven_pattern",
                })
            derived += 1
            continue
        # Skip contacts that already have a validated/proven email.
        if contact.get("email_ready_for_export"):
            skipped += 1
            continue
        email = derive_email(contact.get("first_name") or "", contact.get("last_name") or "", pattern)
        if not email:
            skipped += 1
            continue
        if dry_run:
            derived += 1
            continue
        patch_contact(contact["id"], {
            "email": email,
            "is_derived_email": 1,
            "email_ready_for_export": 1,
            "email_pattern_note": f"derived_from_proven_pattern:{run_id or 'manual'}",
            "pipeline_stage": "derived_from_proven_pattern",
        })
        derived += 1

    if not dry_run:
        patch_company(company_id, {
            "pattern_apply_count": derived,
            "pattern_apply_batch_id": run_id,
        })
        if active_run:
            _update_company_run(active_run["run_id"], apply_count=derived)

    return {
        "company_id": company_id,
        "pattern_template": pattern,
        "derived": derived,
        "skipped": skipped,
        "dry_run": dry_run,
        "run_id": run_id,
        "already_applied": False,
    }


# ── Salesforce push ──────────────────────────────────────────────────────────

def _load_salesforce_module():
    """Lazy import of the ai-stack Salesforce module."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("b7_salesforce_module", SF_MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_espocrm_bridge():
    """Lazy import of the EspoCRM bridge module."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("lead_finder_bridge", ESPOCRM_BRIDGE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(ESPOCRM_BRIDGE_PATH.parent))
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _salesforce_account_payload(company: dict) -> dict:
    return {
        "Name": company.get("name") or "Unknown Company",
        "Website": company.get("website") or "",
        "BillingCity": company.get("city") or "",
        "BillingState": _sf_state_code(company.get("state") or ""),
        "BillingCountry": "US",
        "Phone": company.get("phone") or "",
    }


# ISO-3166 alpha-2 country codes Salesforce expects for state/country picklists.
_SF_COUNTRY_MAP = {
    "usa": "US", "united states": "US", "united states of america": "US",
    "canada": "CA", "mexico": "MX", "uk": "GB", "united kingdom": "GB",
}


def _sf_country_code(country: str) -> str:
    return _SF_COUNTRY_MAP.get(country.lower().strip(), country.strip() or "US")


def _normalize_company_location_for_sf(company_id: int) -> None:
    """Normalize company country/state to Salesforce picklist values in lf.db."""
    company = get_company(company_id)
    if not company:
        return
    updates: dict[str, str] = {}
    country = (company.get("country") or "").strip()
    if country:
        code = _sf_country_code(country)
        if code != country:
            updates["country"] = code
    state = (company.get("state") or "").strip()
    if state:
        code = _sf_state_code(state)
        if code != state:
            updates["state"] = code
    if updates:
        patch_company(company_id, updates)



def _sf_state_code(state: str) -> str:
    """Return a 2-letter state code if the input looks like a US state."""
    s = state.strip().upper()
    if len(s) == 2 and s.isalpha():
        return s
    # Common full names → codes
    full_to_code = {
        "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
        "CALIFORNIA": "CA", "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE",
        "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
        "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
        "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
        "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
        "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
        "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
        "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
        "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
        "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT",
        "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV",
        "WISCONSIN": "WI", "WYOMING": "WY", "DISTRICT OF COLUMBIA": "DC",
    }
    return full_to_code.get(s, s)


def _espocrm_upsert_account(bridge, company: dict) -> tuple[Optional[str], Optional[str]]:
    """Find or create an EspoCRM Account for the company. Returns (account_id, status).

    Dedup by website (exact match). Returns (None, "error") on failure. In
    dry-run contexts the caller must not invoke this helper.
    """
    client = bridge.EspoCRMClient(base_url=ESPOCRM_API_URL)
    website = (company.get("website") or "").strip().lower()
    name = (company.get("name") or "").strip() or "Unknown Company"

    payload: dict[str, object] = {
        "name": name,
        "website": website or None,
        "billingCity": company.get("city") or None,
        "billingState": company.get("state") or None,
        "billingCountry": company.get("country") or None,
        "phone": company.get("phone") or None,
        "source": "Lead Finder",
        "leadFinderAccountId__c": str(company.get("id")),
    }

    existing_id = None
    if website:
        try:
            existing = client.client.find_one(
                "Account",
                [{"type": "equals", "attribute": "website", "value": website}],
            )
            if existing:
                existing_id = existing.get("id")
        except Exception as e:
            return None, f"error: find Account failed: {e}"

    try:
        if existing_id:
            client.client.update("Account", existing_id, payload)
            return existing_id, "updated"
        result = client.client.create("Account", payload)
        return result.get("id"), "created"
    except Exception as e:
        return None, f"error: {e}"


def push_company_to_espocrm(
    company_id: int,
    *,
    dry_run: bool = True,
    confirm: bool = False,
) -> dict:
    """
    Push the company Account plus every email-ready contact at the company as an
    EspoCRM Lead. EspoCRM is the default push destination.

    dry_run=True: prints payloads, no external calls.
    confirm=True + dry_run=False: actually creates records in EspoCRM.

    Writes to the new espo_* contact / company columns and transitions the active
    engagement_company_runs row to 'espocrm_pushed' / 'espocrm_failed'. The legacy
    sf_* columns remain untouched.
    """
    if not dry_run and not confirm:
        raise RuntimeError("Real EspoCRM push requires --confirm")

    company = get_company(company_id)
    if not company:
        raise ValueError(f"Company {company_id} not found")

    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT * FROM contacts
        WHERE company_id=? AND is_test=0
          AND email_ready_for_export=1
          AND (espo_push_status IS NULL OR espo_push_status='error')
        ORDER BY id
    """, (company_id,)).fetchall()
    conn.close()

    contacts = [dict(r) for r in rows]
    bridge = _load_espocrm_bridge() if (not dry_run and contacts) else None

    result = {
        "company_id": company_id,
        "destination": "espocrm",
        "dry_run": dry_run,
        "confirm": confirm,
        "contacts_total": len(contacts),
        "pushed": 0,
        "already_exists": 0,
        "errors": 0,
        "espocrm_ids": [],
        "account": None,
        "payloads": [],
    }

    account_id = None
    account_status = None
    if not dry_run and contacts:
        account_id, account_status = _espocrm_upsert_account(bridge, company)
        result["account"] = {"account_id": account_id, "status": account_status}
        if account_id:
            patch_company(company_id, {
                "espo_account_id": account_id,
                "espo_account_pushed_at": _now_iso(),
                "espo_account_push_status": account_status,
            })
        if account_status == "error":
            result["errors"] += 1

    for contact in contacts:
        email = contact.get("email")
        if not email:
            continue
        payload = {
            "first_name": contact.get("first_name") or "",
            "last_name": contact.get("last_name") or "Unknown",
            "email": email,
            "company": company.get("name") or "Unknown Company",
            "title": contact.get("title") or "",
            "lead_finder_id": str(contact.get("id")),
            "validation_status": contact.get("smtp_validation_status") or "validated",
            "validation_method": contact.get("validation_method") or "engagement_proven",
            "validation_confidence": contact.get("validation_confidence"),
            "linkedin_url": contact.get("linkedin_url") or "",
            "linkedin_slug": contact.get("linkedin_slug") or "",
            "source_primary": contact.get("source_primary") or "lead-finder",
            "title_from_linkedin": contact.get("title_from_linkedin") or "",
            "ai_verified_title": contact.get("ai_verified_title") or "",
            "ai_title_confidence": contact.get("ai_title_confidence"),
            "location": contact.get("location") or company.get("city") or "",
            "phone": contact.get("phone") or company.get("phone") or "",
            "address_street": company.get("street") or "",
            "address_city": company.get("city") or "",
            "address_state": company.get("state") or "",
            "address_postal_code": company.get("postal_code") or "",
            "address_country": company.get("country") or "",
            "company_website": company.get("website") or "",
            "industry": company.get("business_type") or "",
            "email_pattern": company.get("email_pattern") or "",
            "quality_grade": company.get("quality_grade") or "",
            "quality_score": company.get("quality_score"),
            "linkedin_snippet": contact.get("linkedin_snippet") or "",
            "imported_at": _now_iso(),
        }
        if dry_run:
            result["payloads"].append({"contact_id": contact["id"], **payload})
            continue

        try:
            client = bridge.EspoCRMClient(base_url=ESPOCRM_API_URL)
            espocrm_id = client.push_lead(payload)
            # Use the public Espo URL for the lead link, not the internal bridge IP.
            public_base = os.environ.get("ESPOCRM_PUBLIC_URL", "https://sales.livesolutionsnow.com")
            lead_url = f"{public_base.rstrip('/')}/#Lead/view/{espocrm_id}" if espocrm_id else None
            if espocrm_id:
                # push_lead returns the id of an existing lead if it deduped (update)
                # We treat any successful non-None id as a successful push.
                status = "created"
                result["pushed"] += 1
                patch_contact(contact["id"], {
                    "espo_lead_id": espocrm_id,
                    "espo_lead_url": lead_url,
                    "espo_pushed_at": _now_iso(),
                    "espo_push_status": status,
                    "espo_push_error": None,
                    "espo_push_source": "espocrm.lead-finder-bridge.push_lead",
                })
                result["espocrm_ids"].append({
                    "contact_id": contact["id"],
                    "lead_id": espocrm_id,
                    "lead_url": lead_url,
                })
            else:
                result["errors"] += 1
                patch_contact(contact["id"], {
                    "espo_push_status": "error",
                    "espo_push_error": "EspoCRM push returned no lead id",
                })
        except Exception as e:
            result["errors"] += 1
            patch_contact(contact["id"], {
                "espo_push_status": "error",
                "espo_push_error": str(e)[:500],
            })

    active_run = _get_active_company_run(company_id)
    if active_run and not dry_run:
        _update_company_run(
            active_run["run_id"],
            state="espocrm_pushed" if result["errors"] == 0 else "espocrm_failed",
            sf_pushed_at=_now_iso() if result["errors"] == 0 else None,
            sf_push_count=result["pushed"] + result["already_exists"],
            last_error=(None if result["errors"] == 0 else f"{result['errors']} EspoCRM push errors"),
        )

    return result



# ── Results check (company-scoped) ───────────────────────────────────────────

def check_results_for_company(company_id: int, wait_hours: int = PATTERN_PROOF_WAIT_HOURS) -> dict:
    """
    Check engagement results scoped to a single company. If the cornerstone proves
    the pattern, auto-apply it to siblings and transition the run state.
    """
    _ensure_queue_table()
    if not TRACK_DB_PATH.exists():
        return {"error": f"track.db not found at {TRACK_DB_PATH}"}

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=wait_hours)).isoformat()

    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(f"""
        SELECT * FROM {QUEUE_TABLE}
        WHERE company_id=? AND result_status IN ('pending', 'sent')
          AND sent_at IS NOT NULL AND sent_at <= ?
        ORDER BY rowid
    """, (company_id, cutoff)).fetchall()
    conn.close()

    if not rows:
        return {"company_id": company_id, "checked": 0, "message": "No pending tokens past the wait window"}

    track_conn = sqlite3.connect(str(TRACK_DB_PATH))
    track_conn.row_factory = sqlite3.Row
    track_cur = track_conn.cursor()

    summary = {"company_id": company_id, "checked": 0, "opened": 0, "delivered": 0, "bounced": 0, "still_pending": 0}
    pattern_proven = False
    proven_template = None
    proven_email = None
    proven_open_count = 0

    for row in rows:
        token = row["token"]
        # The tracker token (generated inside send_email) may differ from the
        # staging token. Prefer tracker_token if present.
        track_token = row["tracker_token"] if row["tracker_token"] else token
        track_row = track_cur.execute("""
            SELECT opened_at, open_count, bounce_status, bounce_detail
            FROM sends WHERE token=?
        """, (track_token,)).fetchone()
        if not track_row:
            continue

        summary["checked"] += 1
        opened_at = track_row["opened_at"]
        open_count = track_row["open_count"] or 0
        bounce_status = track_row["bounce_status"]
        bounce_detail = track_row["bounce_detail"]

        if bounce_status == "bounced":
            _mark_contact_result(row, "Do Not Send", open_count, bounce_status, bounce_detail)
            summary["bounced"] += 1
        elif opened_at:
            _mark_contact_result(row, "Okay to Send", open_count, bounce_status, bounce_detail)
            summary["opened"] += 1
            pattern_proven = True
            proven_template = row["pattern_template"]
            proven_email = row["candidate_email"]
            proven_open_count = open_count
        elif bounce_status == "delivered":
            _mark_contact_result(row, "Okay to Send", open_count, bounce_status, bounce_detail)
            summary["delivered"] += 1
            pattern_proven = True
            proven_template = row["pattern_template"]
            proven_email = row["candidate_email"]
            proven_open_count = open_count
        else:
            _update_queue(token, result_status="pending", result_checked_at=_now_iso())
            summary["still_pending"] += 1

    track_conn.close()

    if pattern_proven:
        active_run = _get_active_company_run(company_id)
        if active_run:
            _update_company_run(
                active_run["run_id"],
                state="proof_succeeded",
                proven_at=_now_iso(),
                notes=f"pattern proven via {proven_email} (template: {proven_template})",
            )
        apply_proven_pattern(company_id, dry_run=False)

    return summary


# ── Watchdog / orchestrator ─────────────────────────────────────────────────

def _push_destination(dry_run: bool, confirm: bool) -> dict:
    """
    Choose the CRM destination based on environment.

    Env ``USE_SALESFORCE=1`` keeps the legacy Salesforce path; otherwise leads go to EspoCRM.
    """
    use_salesforce = os.environ.get("USE_SALESFORCE", "").strip() == "1"
    if use_salesforce:
        # Legacy SF path was retired from this file on 2026-08-11 because the
        # duplicate definition became corrupted during field-enrichment edits.
        # To re-enable, restore push_company_to_salesforce() from the git
        # history or from scripts/.deprecated-20260811-sf-tool.
        raise NotImplementedError(
            "Salesforce push path has been removed from this build. "
            "To roll back, restore the legacy engagement_verify.py from git "
            "and set USE_SALESFORCE=1. "
            "Migration log: ~/ai-stack-logs/espo-migration-20260811/PROGRESS.md"
        )
    return {"func": push_company_to_espocrm, "name": "espocrm"}


def run_watchdog_cycle(*, wait_hours: int = PATTERN_PROOF_WAIT_HOURS,
                       dry_run: bool = True,
                       confirm: bool = False) -> dict:
    """
    Run one watcher cycle: for every company with an active engagement run, check
    results; on proof, apply the proven pattern to sibling contacts; then stage (or
    confirm) a CRM push for any email-ready contacts.

    - dry_run=True (default): never writes to CRM; pattern apply still writes to
      lf.db because that is the expected auto-apply behavior on proof.
    - confirm=True + dry_run=False: actually pushes to the configured CRM.

    Destination is controlled by ``USE_SALESFORCE=1`` (Salesforce) or the default
    EspoCRM bridge.
    """
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT DISTINCT company_id FROM engagement_company_runs
        WHERE state IN ('queued', 'sent', 'proof_succeeded')
    """).fetchall()
    conn.close()

    dest = _push_destination(dry_run, confirm)
    summary = {
        "companies": len(rows),
        "destination": dest["name"],
        "proofs": 0,
        "applied": 0,
        "staged": 0,
        "pushed": 0,
        "errors": [],
    }
    for row in rows:
        company_id = row["company_id"]
        try:
            res = check_results_for_company(company_id, wait_hours=wait_hours)
            if res.get("opened", 0) + res.get("delivered", 0) > 0:
                summary["proofs"] += 1
                # Apply the proven pattern for real; this only writes to lf.db.
                apply_res = apply_proven_pattern(company_id, dry_run=False)
                summary["applied"] += apply_res.get("derived", 0)

            # Stage or push CRM for any ready contacts.
            push_res = dest["func"](
                company_id,
                dry_run=dry_run or not confirm,
                confirm=confirm and not dry_run,
            )
            if push_res.get("contacts_total", 0) > 0:
                summary["staged"] += 1
                if not dry_run and confirm:
                    summary["pushed"] += 1
        except Exception as e:
            summary["errors"].append({"company_id": company_id, "error": str(e)})

    return summary


# ── CLI ─────────────────────────────────────────────────────────────────────

def _parse_plan(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    parser = argparse.ArgumentParser(description="Lead Finder engagement verification bridge")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would be sent")
    parser.add_argument("--stage", action="store_true", help="Stage a batch for approval")
    parser.add_argument("--send", metavar="PLAN", help="Send an approved batch JSON file")
    parser.add_argument("--check", action="store_true", help="Check results from track.db")
    parser.add_argument("--limit", type=int, default=50, help="Max contacts to stage")
    parser.add_argument("--output", "-o", default="engagement_verify_plan.json", help="Output file for staged plan")
    parser.add_argument("--batch-id", help="Batch ID to check / approve / send")
    parser.add_argument("--wait-hours", type=int, default=PATTERN_PROOF_WAIT_HOURS, help="Minimum hours before evaluating results")
    parser.add_argument("--throttle", type=float, default=60.0, help="Seconds between live sends")
    parser.add_argument("--body-template", default="/home/anthonyturgman/ai-stack/templates/email/live-solutions-cornerstone-verify.html", help="HTML body template for sends")
    parser.add_argument("--approve", action="store_true", help="Mark staged batch as approved (no sends yet)")

    # Cornerstone workflow flags
    parser.add_argument("--pick-cornerstone", action="store_true", help="Pick a cornerstone contact for a company")
    parser.add_argument("--stage-cornerstone", action="store_true", help="Stage a cornerstone verification batch")
    parser.add_argument("--all", action="store_true", help="With --pick-cornerstone/--stage-cornerstone, process every unproven company")
    parser.add_argument("--company-id", type=int, help="Company ID for cornerstone / push commands")
    parser.add_argument("--contact-id", type=int, help="Contact ID to use as cornerstone")
    parser.add_argument("--add-contact", action="store_true", help="Add a contact and prepare cornerstone verification")
    parser.add_argument("--first", help="First name for new contact")
    parser.add_argument("--last", help="Last name for new contact")
    parser.add_argument("--title", help="Title for new contact")
    parser.add_argument("--linkedin", help="LinkedIn URL for new contact")
    parser.add_argument("--auto-send", action="store_true", help="Auto-send after staging (requires ENGAGEMENT_VERIFY_AUTOSEND=1)")
    parser.add_argument("--check-company", action="store_true", help="Check results for one company")
    parser.add_argument("--apply-proven", action="store_true", help="Apply proven pattern to sibling contacts")
    parser.add_argument("--push-sf", action="store_true", help="LEGACY: Stage/push company + contacts to Salesforce (requires USE_SALESFORCE=1)")
    parser.add_argument("--push-espo", action="store_true", help="Stage/push company + contacts to EspoCRM")
    parser.add_argument("--push-espocrm", action="store_true", help="Alias for --push-espo; canonical EspoCRM flag")
    parser.add_argument("--confirm", action="store_true", help="Confirm a real CRM push")
    parser.add_argument("--watchdog", action="store_true", help="Run watcher cycle(s)")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--interval", type=int, default=9000, help="Seconds between watcher cycles (default 9000 = 2.5h)")
    parser.add_argument("--status", action="store_true", help="Show engagement pipeline status for a company")
    parser.add_argument("--status-all", action="store_true", help="Show all active engagement runs across all companies")

    args = parser.parse_args()

    init_db()
    _ensure_queue_table()

    if args.dry_run and not any([args.pick_cornerstone, args.stage_cornerstone, args.add_contact, args.push_sf, args.push_espo, args.watchdog]):
        plan = preview_candidates(limit=args.limit)
        print(json.dumps(plan, indent=2, default=str))
        print(f"\n# Preview: {len(plan)} contacts, {sum(len(p['candidates']) for p in plan)} candidate emails")
        return

    if args.pick_cornerstone:
        if args.all:
            unproven = _get_unproven_companies()
            picked = []
            for company in unproven:
                chosen = pick_cornerstone(company["id"], dry_run=False)
                if chosen:
                    picked.append({"company_id": company["id"], "company_name": company["name"], "contact_id": chosen["id"], "full_name": chosen.get("full_name"), "title": chosen.get("title")})
            print(json.dumps({"picked": picked, "count": len(picked)}, indent=2, default=str))
            return
        if not args.company_id:
            print("--pick-cornerstone requires --company-id (or --all)")
            sys.exit(1)
        chosen = pick_cornerstone(args.company_id, dry_run=False)
        if not chosen:
            print(f"No eligible cornerstone for company {args.company_id}")
            sys.exit(2)
        print(json.dumps({"contact_id": chosen["id"], "full_name": chosen.get("full_name"), "title": chosen.get("title")}, indent=2, default=str))
        return

    if args.stage:
        plan = preview_candidates(limit=args.limit)
        batch_id, inserted = stage_batch(plan)
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump({"batch_id": batch_id, "contacts": plan, "inserted": inserted}, f, indent=2, default=str)
        print(f"Staged batch {batch_id}: {inserted} candidate rows")
        print(f"Review {args.output}, then run with --approve --batch-id {batch_id}")
        return

    if args.approve:
        if not args.batch_id:
            print("--approve requires --batch-id")
            sys.exit(1)
        count = approve_batch(args.batch_id)
        print(f"Approved batch {args.batch_id}: {count} rows")
        return

    if args.send:
        # If PLAN is a staged plan JSON, re-stage if needed, else send approved batch
        if Path(args.send).exists():
            with open(args.send, "r", encoding="utf-8") as f:
                plan_doc = json.load(f)
            batch_id = plan_doc.get("batch_id")
        else:
            batch_id = args.send
        if not batch_id:
            print("Could not determine batch_id")
            sys.exit(1)
        approve_batch(batch_id)
        # Cornerstone batches have a dedicated send path that updates the company run.
        if _is_cornerstone_batch(batch_id):
            results = send_cornerstone_batch(batch_id, dry_run=False, throttle_seconds=max(args.throttle, 60.0),
                                             body_template_path=args.body_template)
        else:
            results = send_approved_batch(batch_id, dry_run=False, throttle_seconds=args.throttle,
                                          body_template_path=args.body_template)
        print(json.dumps(results, indent=2, default=str))
        return

    if args.check:
        summary = check_results(batch_id=args.batch_id, wait_hours=args.wait_hours)
        print(json.dumps(summary, indent=2, default=str))
        return

    if args.stage_cornerstone:
        if args.all:
            unproven = _get_unproven_companies()
            staged = []
            for company in unproven:
                try:
                    plan = stage_cornerstone_batch(company["id"], output=None)
                    staged.append(plan)
                except Exception as e:
                    staged.append({"company_id": company["id"], "error": str(e)})
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump({"staged": staged, "count": len(staged)}, f, indent=2, default=str)
            print(json.dumps({"staged": staged, "count": len(staged)}, indent=2, default=str))
            return
        if not args.company_id:
            print("--stage-cornerstone requires --company-id (or --all)")
            sys.exit(1)
        plan = stage_cornerstone_batch(args.company_id, output=args.output)
        print(json.dumps(plan, indent=2, default=str))
        print(f"\nReview {args.output}, then run: --approve --batch-id {plan['batch_id']}")
        print(f"Or send immediately: --send {args.output} --throttle 8.0")
        return

    if args.add_contact:
        result = add_contact_for_verification(
            contact_id=args.contact_id,
            company_id=args.company_id,
            first_name=args.first,
            last_name=args.last,
            title=args.title,
            linkedin_url=args.linkedin,
            auto_send=args.auto_send,
            dry_run=False,
        )
        print(json.dumps(result, indent=2, default=str))
        return

    if args.check_company:
        if not args.company_id:
            print("--check-company requires --company-id")
            sys.exit(1)
        summary = check_results_for_company(args.company_id, wait_hours=args.wait_hours)
        print(json.dumps(summary, indent=2, default=str))
        return

    if args.apply_proven:
        if not args.company_id:
            print("--apply-proven requires --company-id")
            sys.exit(1)
        result = apply_proven_pattern(args.company_id, dry_run=False)
        print(json.dumps(result, indent=2, default=str))
        return

    if args.push_sf:
        print("--push-sf has been retired. Use --push-espo instead.")
        print("Migration log: ~/ai-stack-logs/espo-migration-20260811/PROGRESS.md")
        sys.exit(2)

    if args.push_espo or args.push_espocrm:
        if not args.company_id:
            print("--push-espo/--push-espocrm requires --company-id")
            sys.exit(1)
        result = push_company_to_espocrm(
            args.company_id,
            dry_run=not args.confirm,
            confirm=args.confirm,
        )
        print(json.dumps(result, indent=2, default=str))
        return

    if args.status_all:
        runs = _get_all_active_runs()
        if not runs:
            print(json.dumps({"active_runs": 0, "message": "No active engagement runs"}))
            return
        # Enrich each run with queue row counts
        conn = get_db()
        cur = conn.cursor()
        for run in runs:
            batch_rows = cur.execute(f"""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN result_status='bounced' THEN 1 ELSE 0 END) as bounced,
                    SUM(CASE WHEN result_status='opened' THEN 1 ELSE 0 END) as opened,
                    SUM(CASE WHEN result_status='pending' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN result_status='sent' THEN 1 ELSE 0 END) as sent
                FROM {QUEUE_TABLE}
                WHERE company_id=?
            """, (run["company_id"],)).fetchone()
            run["queue"] = dict(batch_rows) if batch_rows else {}
        conn.close()
        print(json.dumps({"active_runs": len(runs), "runs": runs}, indent=2, default=str))
        return

    if args.status:
        if not args.company_id:
            print("--status requires --company-id")
            sys.exit(1)
        company = get_company(args.company_id)
        run = _get_active_company_run(args.company_id)
        cornerstone = _get_contact(company["cornerstone_contact_id"]) if company and company.get("cornerstone_contact_id") else None
        ready_count = 0
        if company:
            conn = get_db(); cur = conn.cursor()
            ready_count = cur.execute("SELECT COUNT(*) FROM contacts WHERE company_id=? AND email_ready_for_export=1", (args.company_id,)).fetchone()[0]
            conn.close()
        print(json.dumps({
            "company_id": args.company_id,
            "company_name": company.get("name") if company else None,
            "email_pattern": company.get("email_pattern") if company else None,
            "proof_status": company.get("email_pattern_proof_status") if company else None,
            "cornerstone_contact_id": company.get("cornerstone_contact_id") if company else None,
            "cornerstone": {"full_name": cornerstone.get("full_name"), "title": cornerstone.get("title"), "email": cornerstone.get("email")} if cornerstone else None,
            "active_run": run,
            "ready_contacts": ready_count,
        }, indent=2, default=str))
        return

    if args.watchdog:
        if args.once:
            summary = run_watchdog_cycle(
                wait_hours=args.wait_hours,
                dry_run=not args.confirm,
                confirm=args.confirm,
            )
            print(json.dumps(summary, indent=2, default=str))
            return
        # In-process loop: useful for systemd/operator sessions
        print(f"[watchdog] starting in-process loop every {args.interval}s (dry_run={not args.confirm})")
        try:
            while True:
                summary = run_watchdog_cycle(
                    wait_hours=args.wait_hours,
                    dry_run=not args.confirm,
                    confirm=args.confirm,
                )
                print(json.dumps({"ts": _now_iso(), **summary}, indent=2, default=str))
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[watchdog] stopped")
            return

    parser.print_help()


if __name__ == "__main__":
    main()
