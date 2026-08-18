#!/usr/bin/env python3
"""
lf_import.py - Lead Finder company-import logic
================================================
Shared helper used by the OWUI chat tool (lf_import_companies) and the
REST endpoint POST /api/import in lf_server.py.

Responsibilities:
  1. Parse CSV or Excel rows into normalized company dicts.
  2. Create an import-prefixed search session.
  3. Geocode city -> lat/lng (cache + Google fallback).
  4. Deduplicate against existing companies by name+city.
  5. Insert new companies as source='MANUAL_IMPORT', data_provenance='MANUAL_IMPORT'.
  6. Gap-fill via AI (website, business_type, sanity, email_pattern).
  7. Kick off a discovery-batch job for executive discovery.

No OWUI-specific code here — this module is server-side only.
"""

import csv
import io
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from lf_db import (
    create_session,
    create_company_manual,
    upsert_company,
    get_company,
    get_db,
    get_geocode,
    upsert_geocode,
    DB_PATH,
)
from lf_geocode import get_city_coords
from lf_ai_enrich import (
    ai_infer_email_pattern,
    ai_discover_company_website,
    ai_normalize_business_type,
    ai_sanity_check_company,
)
from lf_executives import extract_domain


# ---------------------------------------------------------------------------
# Row parsing / normalization
# ---------------------------------------------------------------------------

NAME_SUFFIX_RE = re.compile(r"\s+(Inc\.?|LLC\.?|Ltd\.?|Limited|Corp\.?|Corporation|Co\.?)$", re.IGNORECASE)

HEADER_ALIASES = {
    "name": [
        "name", "company", "company name", "company_name", "organization",
        "org", "vendor", "contractor", "supplier", "firm", "business",
    ],
    "street": [
        "street", "address", "street address", "street_address", "addr",
    ],
    "city": ["city", "location", "town"],
    "state": ["state", "st", "province", "region"],
    "postal_code": [
        "postal_code", "zip", "zipcode", "zip_code", "postal code", "postcode",
    ],
    "website": ["website", "url", "site", "web", "homepage"],
    "phone": ["phone", "phone_number", "telephone", "tel", "mobile"],
    "business_type": [
        "business_type", "industry", "type", "category", "business type",
        "sector", "vertical",
    ],
}


def _clean(val):
    if val is None:
        return ""
    val = str(val).strip()
    return val if val not in ("-", "n/a", "N/A", "?") else ""


def _norm_state(st: str) -> str:
    st = _clean(st).upper()
    if len(st) > 2:
        # Simple US state name -> abbreviation mapping for common cases
        MAP = {
            "CALIFORNIA": "CA", "TEXAS": "TX", "FLORIDA": "FL", "NEW YORK": "NY",
            "ILLINOIS": "IL", "PENNSYLVANIA": "PA", "OHIO": "OH", "GEORGIA": "GA",
            "NORTH CAROLINA": "NC", "MICHIGAN": "MI", "NEW JERSEY": "NJ",
            "VIRGINIA": "VA", "WASHINGTON": "WA", "ARIZONA": "AZ", "MASSACHUSETTS": "MA",
            "TENNESSEE": "TN", "INDIANA": "IN", "MISSOURI": "MO", "MARYLAND": "MD",
            "WISCONSIN": "WI", "COLORADO": "CO", "MINNESOTA": "MN", "SOUTH CAROLINA": "SC",
            "ALABAMA": "AL", "LOUISIANA": "LA", "KENTUCKY": "KY", "OREGON": "OR",
            "OKLAHOMA": "OK", "CONNECTICUT": "CT", "UTAH": "UT", "IOWA": "IA",
            "NEVADA": "NV", "ARKANSAS": "AR", "MISSISSIPPI": "MS", "KANSAS": "KS",
            "NEW MEXICO": "NM", "NEBRASKA": "NE", "WEST VIRGINIA": "WV", "IDAHO": "ID",
            "HAWAII": "HI", "NEW HAMPSHIRE": "NH", "MAINE": "ME", "MONTANA": "MT",
            "RHODE ISLAND": "RI", "DELAWARE": "DE", "SOUTH DAKOTA": "SD", "NORTH DAKOTA": "ND",
            "ALASKA": "AK", "VERMONT": "VT", "WYOMING": "WY",
        }
        st = MAP.get(st, st[:2])  # fallback to first two chars
    return st


def _norm_website(url: str) -> str:
    url = _clean(url)
    if not url:
        return ""
    url = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    url = re.sub(r"^www\\.", "", url, flags=re.IGNORECASE)
    url = url.rstrip("/")
    return url


def _dedup_name(name: str) -> str:
    return NAME_SUFFIX_RE.sub("", name.strip().lower())


def _build_field_map(headers: list[str]) -> dict[str, int]:
    headers = [h.strip().lower().replace(" ", "_") for h in headers]
    field_map = {}
    for field, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            for i, h in enumerate(headers):
                if h == alias.replace(" ", "_"):
                    field_map[field] = i
                    break
            if field in field_map:
                break
    return field_map


def parse_companies_csv(csv_text: str) -> list[dict]:
    """Parse a CSV string into normalized company dicts."""
    csv_text = csv_text.strip()
    if not csv_text:
        return []
    dialect = csv.Sniffer().sniff(csv_text[:2048]) if len(csv_text) > 1 else csv.excel
    reader = csv.reader(io.StringIO(csv_text), dialect)
    headers = next(reader)
    field_map = _build_field_map(headers)
    if "name" not in field_map:
        raise ValueError("CSV must contain a 'name' column")

    rows = []
    for raw in reader:
        if not raw or not any(raw):
            continue
        row = {field: _clean(raw[idx]) if idx < len(raw) else "" for field, idx in field_map.items()}
        rows.append(row)
    return normalize_rows(rows)


def _split_city_state(city_state: str) -> list[tuple[str, str]]:
    """
    Expand a city string that may contain multiple locations.
    Returns list of (city, state) tuples. State may be empty.
    """
    raw = _clean(city_state)
    if not raw:
        return [("", "")]
    parts = [p.strip() for p in raw.split("/")]
    result = []
    for part in parts:
        if "," in part:
            # Try "City, State" pattern if no explicit state is provided
            segments = [s.strip() for s in part.split(",")]
            city_part = segments[0]
            state_part = _norm_state(segments[1]) if len(segments) > 1 else ""
            result.append((city_part, state_part))
        else:
            result.append((part, ""))
    return result


def normalize_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows:
        name = _clean(row.get("name", ""))
        if not name:
            continue
        website = _norm_website(row.get("website", ""))
        base_state = _norm_state(row.get("state", ""))
        base_postal = _clean(row.get("postal_code", ""))
        # Expand multi-location city strings (e.g. "San Diego/Pasadena")
        city_states = _split_city_state(row.get("city", ""))
        for city_part, inferred_state in city_states:
            state = base_state or inferred_state
            out.append({
                "name": name,
                "street": _clean(row.get("street", "")),
                "city": city_part,
                "state": state,
                "postal_code": base_postal,
                "website": website,
                "phone": _clean(row.get("phone", "")),
                "business_type": _clean(row.get("business_type", "")),
            })
    return out


# ---------------------------------------------------------------------------
# Geocoding / duplicate detection
# ---------------------------------------------------------------------------

def geocode_row(row: dict, city_default: str, state_default: str):
    """Resolve lat/lng for a row. Returns (lat, lng, source) or (None, None, error)."""
    city = row.get("city") or city_default
    state = row.get("state") or state_default
    if not city or not state:
        return None, None, "missing_city_or_state"
    coords = get_city_coords(city, state)
    if coords:
        return coords["lat"], coords["lng"], coords.get("source", "CACHE")
    return None, None, "geocode_failed"


def find_existing_company(name: str, city: str) -> Optional[dict]:
    """Deduplicate by normalized name + city (case-insensitive)."""
    import sqlite3
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, city, state, website, business_type, phone, place_id
        FROM companies
        WHERE LOWER(name) = ? AND LOWER(city) = ?
        ORDER BY id ASC LIMIT 1
    """, (name.lower(), city.lower()))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0], "name": row[1], "city": row[2], "state": row[3],
        "website": row[4], "business_type": row[5], "phone": row[6], "place_id": row[7],
    }


# ---------------------------------------------------------------------------
# AI gap-fill
# ---------------------------------------------------------------------------

def gap_fill_company(company_id: int, company: dict, tasks: list[str] = None, quick_pattern: bool = False):
    """Run AI enrichment on a single imported company to fill gaps.

    Args:
        quick_pattern: if True, use ai_infer_email_pattern_v2 quick mode
                       (skips SearXNG) to avoid blocking import. Default False
                       for deep-research pattern inference.
    """
    tasks = tasks or ["website", "type", "sanity", "pattern"]
    updated = {}

    if "website" in tasks:
        try:
            current = company.get("website", "")
            if not current:
                r = ai_discover_company_website(
                    company_name=company["name"],
                    city=company.get("city", ""),
                    state=company.get("state", ""),
                    current_website="",
                )
                if r and r.get("website") and r.get("confidence", 0) > 0.5:
                    updated["website"] = r["website"]
        except Exception as e:
            updated["website_error"] = str(e)

    if "type" in tasks:
        try:
            r = ai_normalize_business_type(
                raw_type=company.get("business_type", ""),
                company_name=company["name"],
            )
            if r and r.get("canonical_type"):
                updated["canonical_business_type"] = r["canonical_type"]
                updated["business_type_confidence"] = r.get("confidence", 0.0)
        except Exception as e:
            updated["type_error"] = str(e)

    if "sanity" in tasks:
        try:
            r = ai_sanity_check_company(company)
            if r:
                updated["ai_sanity_status"] = "ok" if r.get("is_valid") else "flagged"
                updated["ai_sanity_notes"] = json.dumps({
                    "issues": r.get("issues", []),
                    "corrections": r.get("corrections", {}),
                    "confidence": r.get("confidence", 0.0),
                    "reasoning": r.get("reasoning", ""),
                })
        except Exception as e:
            updated["sanity_error"] = str(e)

    if "pattern" in tasks:
        try:
            website = updated.get("website") or company.get("website", "")
            domain = extract_domain(website) or ""
            if not domain:
                domain = (
                    company["name"].lower().replace(" ", "").replace(",", "").replace(".", "").replace("-", "")
                    + ".com"
                )
            r = ai_infer_email_pattern_v2(
                domain=domain,
                company_name=company["name"],
                industry=company.get("business_type", ""),
                city=company.get("city", ""),
                state=company.get("state", ""),
                quick=quick_pattern,
            )
            if r and r.get("pattern"):
                updated["email_pattern"] = r["pattern"]
                updated["email_pattern_confidence"] = r.get("confidence", 0.0)
                updated["email_pattern_source"] = "ai_inferred_v2"
        except Exception as e:
            updated["pattern_error"] = str(e)

    if updated:
        conn = get_db()
        cur = conn.cursor()
        fields = []
        values = []
        for k, v in updated.items():
            if k.endswith("_error"):
                continue
            fields.append(f"{k}=?")
            values.append(v)
        if fields:
            values.append(company_id)
            cur.execute(f"UPDATE companies SET {', '.join(fields)} WHERE id=?", values)
            conn.commit()
        conn.close()
    return updated


# ---------------------------------------------------------------------------
# Main import flow
# ---------------------------------------------------------------------------

def import_companies(
    rows: list[dict],
    session_name: str,
    industry: str,
    city_default: str = "",
    state_default: str = "CA",
    auto_enrich: bool = True,
) -> dict:
    """
    Import a list of normalized company rows into a new lead-finder session.
    Returns a summary dict with session_key, job_id, and counts.
    """
    if not rows:
        raise ValueError("No companies to import")

    # Session naming: ensure import- prefix and user-derived slug
    raw_session_name = session_name.strip()
    if not raw_session_name:
        raw_session_name = "import"
    if not raw_session_name.lower().startswith("import"):
        session_name = f"import-{raw_session_name}"
    else:
        session_name = raw_session_name

    # Industry label is taken as-is from user
    industry_label = (industry or session_name).strip()

    # Build an identifiable, unique session key: import-{slug}-{short_uuid}
    slug = re.sub(r"[^a-z0-9]+", "-", session_name.lower()).strip("-")[:30]
    short_uuid = uuid.uuid4().hex[:6]
    session_key = f"{slug}-{short_uuid}"

    # Create session
    create_session(
        session_key=session_key,
        industry=industry_label,
        city=city_default,
        state=state_default,
        region="",
        radius_miles=25,
    )

    inserted_ids = []
    reused_ids = []
    failed_rows = []
    geocode_failures = []

    for idx, row in enumerate(rows, start=1):
        try:
            lat, lng, geo_source = geocode_row(row, city_default, state_default)
            if lat is None or lng is None:
                geocode_failures.append({"row": idx, "name": row["name"], "reason": geo_source})

            existing = find_existing_company(row["name"], row["city"])
            if existing:
                reused_ids.append(existing["id"])
                # Merge missing fields into existing record with a targeted UPDATE
                # (do NOT use upsert_company here — partial data would blank fields)
                update_fields = []
                values = []
                if not existing.get("website") and row.get("website"):
                    update_fields.append("website=?")
                    values.append(row["website"])
                if not existing.get("business_type") and row.get("business_type"):
                    update_fields.append("business_type=?")
                    values.append(row["business_type"])
                if not existing.get("phone") and row.get("phone"):
                    update_fields.append("phone=?")
                    values.append(row["phone"])
                if lat is not None and lng is not None:
                    update_fields.extend(["lat=?", "lng=?"])
                    values.extend([lat, lng])
                if update_fields:
                    values.append(existing["id"])
                    conn = sqlite3.connect(str(DB_PATH))
                    cur = conn.cursor()
                    cur.execute(f"UPDATE companies SET {', '.join(update_fields)} WHERE id=?", values)
                    conn.commit()
                    conn.close()
                continue

            # Insert new company as MANUAL_IMPORT
            company_data = {
                "name": row["name"],
                "street": row.get("street", ""),
                "city": row.get("city", ""),
                "state": row.get("state", ""),
                "postal_code": row.get("postal_code", ""),
                "lat": lat,
                "lng": lng,
                "business_type": row.get("business_type", ""),
                "website": row.get("website", ""),
                "phone": row.get("phone", ""),
                "source": "MANUAL_IMPORT",
                "data_provenance": "MANUAL_IMPORT",
                "search_query": industry_label,
                "confidence_score": None,
            }
            company_id = create_company_manual(company_data)
            inserted_ids.append(company_id)

            if auto_enrich:
                gap_fill_company(company_id, company_data, quick_pattern=True)

        except Exception as e:
            failed_rows.append({"row": idx, "name": row.get("name", ""), "error": str(e)})

    job_id = None
    if auto_enrich and (inserted_ids or reused_ids):
        # Build discovery item list from the actual companies touched in this import.
        # Do NOT use get_session_companies() because existing sessions join on
        # search_query=industry and would pull unrelated companies into the job.
        from lf_db import create_discovery_job, add_discovery_job_items, update_discovery_job
        touched_ids = inserted_ids + reused_ids
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        placeholders = ",".join("?" for _ in touched_ids)
        rows = cur.execute(
            f"SELECT id, name, place_id FROM companies WHERE id IN ({placeholders}) ORDER BY id",
            tuple(touched_ids),
        ).fetchall()
        conn.close()
        companies = [dict(r) for r in rows]
        if companies:
            job_id = f"enrich_{uuid.uuid4().hex[:12]}"
            create_discovery_job(job_id, session_key, total=len(companies))
            add_discovery_job_items(job_id, companies)
            update_discovery_job(job_id, stage_log=[{
                "stage": "queued_from_import",
                "at": datetime.now(timezone.utc).isoformat(),
                "tasks": ["website", "pattern_deep_research", "type", "sanity", "derive_emails", "contacts"],
            }])
            # Background worker is started by the caller (lf_server.py BackgroundTasks).

    return {
        "session_key": session_key,
        "session_name": session_name,
        "industry": industry_label,
        "total": len(rows),
        "inserted": len(inserted_ids),
        "inserted_ids": inserted_ids,
        "reused": len(reused_ids),
        "reused_ids": reused_ids,
        "failed": len(failed_rows),
        "failed_rows": failed_rows,
        "geocode_failures": geocode_failures,
        "job_id": job_id,
    }


def start_discovery_background(job_id: str, session_key: str):
    """
    Background worker entry point (mirrors lf_server.py api_discover_batch logic).
    Call this from a BackgroundTasks context.
    """
    from lf_db import get_next_discovery_pending, update_discovery_job_item, update_discovery_job
    from lf_executives import discover_executives_ai_first
    from lf_db import get_db

    try:
        start_ts = datetime.now(timezone.utc).isoformat()
        update_discovery_job(job_id, status="running", started_at=start_ts,
                             current_stage="starting", stage_log=[])
        done = 0
        failed = 0
        while True:
            item = get_next_discovery_pending(job_id)
            if not item:
                break
            try:
                company_id = item["company_id"]
                company_name = item["company_name"]
                conn = get_db()
                cur = conn.cursor()
                row = cur.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
                conn.close()
                if not row:
                    result = {"status": "error", "message": "Company not found"}
                    update_discovery_job_item(item["id"], "failed", json.dumps(result))
                    failed += 1
                else:
                    company = dict(row)
                    update_discovery_job(job_id, current_stage=f"discovering:{company_name}")
                    contacts = discover_executives_ai_first(
                        company_name=company["name"],
                        company_id=company["id"],
                        website=company.get("website", ""),
                        industry=company.get("search_query") or company.get("business_type", ""),
                        search_city=company.get("city", ""),
                        search_state=company.get("state", "CA"),
                        search_lat=float(company.get("lat", 0.0) or 0.0),
                        search_lng=float(company.get("lng", 0.0) or 0.0),
                        enable_pixelrag_scrape=True,
                    )
                    result = {"status": "ok", "contacts_found": len(contacts)}
                    update_discovery_job_item(item["id"], "completed", json.dumps(result))
                    done += 1
            except Exception as e:
                result = {"status": "error", "message": str(e)}
                update_discovery_job_item(item["id"], "failed", json.dumps(result))
                failed += 1
            update_discovery_job(job_id, done=done, failed=failed)
        finish_ts = datetime.now(timezone.utc).isoformat()
        update_discovery_job(
            job_id, status="completed" if failed == 0 else "completed_with_errors",
            done=done, failed=failed, finished_at=finish_ts,
            current_stage="complete",
        )
    except Exception as e:
        update_discovery_job(job_id, status="failed", current_stage="failed")
        raise