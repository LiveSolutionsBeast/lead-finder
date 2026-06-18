#!/usr/bin/env python3
"""
lf_db.py - Lead Finder Database Init & Helpers
=============================================
Creates the lf.db SQLite schema and provides CRUD helpers.
"""

import sqlite3, json, uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "lf.db"


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _add_column_if_missing(cur, table: str, column: str, col_def: str):
    """Add column to table if it does not already exist."""
    existing = [row[1] for row in cur.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in existing:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")


def init_db():
    """Create all 5 tables if they don't exist."""
    conn = get_db()
    cur = conn.cursor()

    cur.executescript("""
    CREATE TABLE IF NOT EXISTS companies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        place_id TEXT UNIQUE,
        name TEXT NOT NULL,
        street TEXT,
        city TEXT,
        state TEXT,
        postal_code TEXT,
        country TEXT DEFAULT 'USA',
        lat REAL,
        lng REAL,
        business_type TEXT,
        website TEXT,
        phone TEXT,
        rating REAL,
        user_rating_count INTEGER,
        hq_location TEXT,
        is_local_contact INTEGER DEFAULT 0,
        source TEXT DEFAULT 'GOOGLE_PLACES',
        search_query TEXT,
        found_at TEXT,
        confidence_score REAL DEFAULT 1.0,
        data_provenance TEXT,
        email_pattern TEXT
    );

    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        company_id INTEGER REFERENCES companies(id),
        first_name TEXT,
        last_name TEXT,
        full_name TEXT,
        title TEXT,
        linkedin_url TEXT,
        email_pattern_note TEXT,
        is_local INTEGER DEFAULT 1,
        hq_contact INTEGER DEFAULT 0,
        confidence_score REAL DEFAULT 1.0,
        data_provenance TEXT,
        found_at TEXT,
        is_manually_edited INTEGER DEFAULT 0,
        is_deleted INTEGER DEFAULT 0,
        manual_notes TEXT,
        email TEXT,
        is_derived_email INTEGER DEFAULT 0,
        source_primary TEXT,
        source_linkedin_verified INTEGER DEFAULT 0,
        source_linkedin_unverified INTEGER DEFAULT 0,
        title_from_website TEXT,
        title_from_linkedin TEXT,
        linkedin_snippet TEXT
    );

    CREATE TABLE IF NOT EXISTS search_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_key TEXT UNIQUE,
        industry TEXT,
        city TEXT,
        state TEXT,
        region TEXT,
        radius_miles INTEGER DEFAULT 25,
        created_at TEXT,
        last_active_at TEXT,
        status TEXT DEFAULT 'active'
    );

    CREATE TABLE IF NOT EXISTS search_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER REFERENCES search_sessions(id),
        result_type TEXT,
        file_path TEXT,
        created_at TEXT
    );

    CREATE TABLE IF NOT EXISTS geocode_cache (
        city TEXT,
        state TEXT,
        lat REAL,
        lng REAL,
        source TEXT,
        cached_at TEXT,
        PRIMARY KEY (city, state)
    );

    CREATE INDEX IF NOT EXISTS idx_companies_place_id ON companies(place_id);
    CREATE INDEX IF NOT EXISTS idx_companies_session ON companies(search_query);
    CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company_id);
    CREATE INDEX IF NOT EXISTS idx_sessions_key ON search_sessions(session_key);
    """)

    # Migration: add new columns if table existed before
    _add_column_if_missing(cur, "contacts", "is_manually_edited", "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, "contacts", "is_deleted", "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, "contacts", "manual_notes", "TEXT")
    _add_column_if_missing(cur, "contacts", "source_primary", "TEXT")
    _add_column_if_missing(cur, "contacts", "source_linkedin_verified", "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, "contacts", "source_linkedin_unverified", "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, "contacts", "title_from_website", "TEXT")
    _add_column_if_missing(cur, "contacts", "title_from_linkedin", "TEXT")
    _add_column_if_missing(cur, "contacts", "linkedin_snippet", "TEXT")
    _add_column_if_missing(cur, "contacts", "email", "TEXT")
    _add_column_if_missing(cur, "contacts", "is_derived_email", "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, "companies", "email_pattern", "TEXT")
    _add_column_if_missing(cur, "companies", "email_pattern_confidence", "REAL DEFAULT 0.0")
    _add_column_if_missing(cur, "companies", "email_pattern_source", "TEXT")
    # QC-14: soft delete + AI sanity for companies (added 2026-06-07)
    _add_column_if_missing(cur, "companies", "is_deleted", "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, "companies", "is_manually_edited", "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, "companies", "manual_notes", "TEXT")
    _add_column_if_missing(cur, "companies", "canonical_business_type", "TEXT")
    _add_column_if_missing(cur, "companies", "business_type_confidence", "REAL DEFAULT 0.0")
    _add_column_if_missing(cur, "companies", "ai_sanity_status", "TEXT")
    _add_column_if_missing(cur, "companies", "ai_sanity_notes", "TEXT")
    # QC-15: Quality scoring (added 2026-06-09)
    _add_column_if_missing(cur, "companies", "quality_score", "REAL DEFAULT 0.0")
    _add_column_if_missing(cur, "companies", "quality_grade", "TEXT")  # 'A', 'B', 'C', 'D', 'F'
    _add_column_if_missing(cur, "companies", "quality_signals", "TEXT")  # JSON: which signals contributed
    # Email Validation (added 2026-06-09)
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS email_validation_cache (
            email TEXT PRIMARY KEY,
            domain TEXT NOT NULL,
            status TEXT NOT NULL,
            analysis TEXT NOT NULL,
            smtp_probed INTEGER DEFAULT 0,
            smtp_code INTEGER,
            mx_host TEXT,
            catch_all INTEGER,
            validated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_evc_domain ON email_validation_cache(domain);
        CREATE INDEX IF NOT EXISTS idx_evc_validated_at ON email_validation_cache(validated_at);

        CREATE TABLE IF NOT EXISTS email_validation_jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            total INTEGER DEFAULT 0,
            done INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            results TEXT  -- JSON array of ValidationResult dicts
        );
    """)
    _add_column_if_missing(cur, "contacts", "smtp_validation_status", "TEXT")
    _add_column_if_missing(cur, "contacts", "smtp_validated_at", "TEXT")
    _add_column_if_missing(cur, "contacts", "smtp_validation_code", "INTEGER")
    # QC-16: AI-verified contact title (added 2026-06-09)
    _add_column_if_missing(cur, "contacts", "ai_verified_title", "TEXT")
    _add_column_if_missing(cur, "contacts", "ai_title_confidence", "REAL DEFAULT 0.0")
    _add_column_if_missing(cur, "contacts", "ai_title_source", "TEXT")
    # QC-16 extension: phone + location from AI research
    _add_column_if_missing(cur, "contacts", "phone", "TEXT")
    _add_column_if_missing(cur, "contacts", "location", "TEXT")
    _add_column_if_missing(cur, "contacts", "email_ready_for_export", "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, "contacts", "email_rejected_reason", "TEXT")

    # QC-17: Discovery jobs for background processing (added 2026-06-09)
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS discovery_jobs (
            job_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            total INTEGER DEFAULT 0,
            done INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            results TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dj_session ON discovery_jobs(session_key);
        CREATE INDEX IF NOT EXISTS idx_dj_status ON discovery_jobs(status);
    """)
    # Issue #3: stage tracking for enrichment progress (added 2026-06-16)
    _add_column_if_missing(cur, "discovery_jobs", "current_stage", "TEXT")
    _add_column_if_missing(cur, "discovery_jobs", "stage_log", "TEXT")  # JSON array of stage events

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS discovery_job_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL REFERENCES discovery_jobs(job_id),
            company_id INTEGER NOT NULL,
            company_name TEXT NOT NULL,
            place_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dji_job ON discovery_job_items(job_id);
        CREATE INDEX IF NOT EXISTS idx_dji_status ON discovery_job_items(status);
    """)
    conn.commit()
    conn.close()


def seed_geocode_cache():
    """Load ca_city_coords.json and geocode_cache.json into the geocode_cache table."""
    conn = get_db()
    cur = conn.cursor()

    # Load CA city coords
    ca_path = BASE_DIR / "ca_city_coords.json"
    if ca_path.exists():
        with open(ca_path) as f:
            cities = json.load(f)
        now = datetime.now(timezone.utc).isoformat()
        for city, data in cities.items():
            cur.execute("""
                INSERT OR IGNORE INTO geocode_cache (city, state, lat, lng, source, cached_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (city.lower(), "CA", data["lat"], data["lng"], "CITY_DICT_OK", now))

    # Load extended cache
    cache_path = BASE_DIR / "geocode_cache.json"
    if cache_path.exists():
        with open(cache_path) as f:
            entries = json.load(f)
        now = datetime.now(timezone.utc).isoformat()
        for city, data in entries.items():
            cur.execute("""
                INSERT OR IGNORE INTO geocode_cache (city, state, lat, lng, source, cached_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (city.lower(), "CA", data["lat"], data["lng"], data.get("source", "CACHE_OK"), now))

    conn.commit()
    conn.close()


def upsert_company(data: dict) -> int:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO companies (place_id, name, street, city, state, postal_code, country,
            lat, lng, business_type, website, phone, rating, user_rating_count,
            hq_location, is_local_contact, source, search_query, found_at,
            confidence_score, data_provenance, quality_score)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(place_id) DO UPDATE SET
            name=excluded.name, street=excluded.street, city=excluded.city,
            state=excluded.state, postal_code=excluded.postal_code,
            lat=excluded.lat, lng=excluded.lng, business_type=excluded.business_type,
            website=excluded.website, phone=excluded.phone, rating=excluded.rating,
            user_rating_count=excluded.user_rating_count, hq_location=excluded.hq_location,
            is_local_contact=excluded.is_local_contact, confidence_score=excluded.confidence_score,
            quality_score=excluded.quality_score
    """, (
        data.get("place_id"), data.get("name"), data.get("street"),
        data.get("city"), data.get("state"), data.get("postal_code"),
        data.get("country", "USA"), data.get("lat"), data.get("lng"),
        data.get("business_type"), data.get("website"), data.get("phone"),
        data.get("rating"), data.get("user_rating_count"),
        data.get("hq_location"), data.get("is_local_contact", 0),
        data.get("source", "GOOGLE_PLACES"), data.get("search_query"),
        data.get("found_at", datetime.now(timezone.utc).isoformat()),
        data.get("confidence_score", 1.0), data.get("data_provenance", "GOOGLE_PLACES"),
        data.get("quality_score", 0.0)
    ))
    conn.commit()
    company_id = cur.lastrowid or cur.execute("SELECT id FROM companies WHERE place_id=?", (data.get("place_id"),)).fetchone()[0]
    conn.close()
    return company_id


def upsert_contact(company_id: int, data: dict) -> int:
    """
    Upsert a contact. Deduplicates on (company_id, linkedin_url) OR
    (company_id, full_name) if both are present and non-empty.
    Also deduplicates on exact (first_name, last_name) within the company.
    """
    first_name = (data.get("first_name") or "").strip()
    last_name = (data.get("last_name") or "").strip()
    if not first_name or not last_name:
        return -1

    conn = get_db()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()

    linkedin_url = (data.get("linkedin_url") or "").strip()
    full_name = (data.get("full_name") or f"{first_name} {last_name}").strip()

    # Check for existing contact by LinkedIn URL first (strongest signal)
    if linkedin_url:
        existing = cur.execute(
            "SELECT id FROM contacts WHERE company_id=? AND linkedin_url=?",
            (company_id, linkedin_url)
        ).fetchone()
        if existing:
            # Update existing contact with new data
            cur.execute("""
                UPDATE contacts SET
                    first_name=?, last_name=?, full_name=?, title=?, linkedin_url=?,
                    email_pattern_note=?, is_local=?, hq_contact=?,
                    confidence_score=?, data_provenance=?, found_at=?,
                    source_primary=?, source_linkedin_verified=?, source_linkedin_unverified=?,
                    title_from_website=?, title_from_linkedin=?, linkedin_snippet=?,
                    ai_verified_title=?, ai_title_confidence=?, ai_title_source=?
                WHERE id=?
            """, (
                first_name, last_name, full_name,
                data.get("title"), linkedin_url,
                data.get("email_pattern_note"), data.get("is_local", 1),
                data.get("hq_contact", 0), data.get("confidence_score", 1.0),
                data.get("data_provenance"), now,
                data.get("source_primary", ""),
                data.get("source_linkedin_verified", 0),
                data.get("source_linkedin_unverified", 0),
                data.get("title_from_website", ""),
                data.get("title_from_linkedin", ""),
                data.get("linkedin_snippet", ""),
                data.get("ai_verified_title"),
                data.get("ai_title_confidence", 0.0),
                data.get("ai_title_source"),
                existing[0]
            ))
            conn.commit()
            conn.close()
            return existing[0]

    # Check by full_name within company (second signal)
    existing = cur.execute(
        "SELECT id FROM contacts WHERE company_id=? AND full_name=?",
        (company_id, full_name)
    ).fetchone()
    if existing:
        cur.execute("""
            UPDATE contacts SET
                first_name=?, last_name=?, full_name=?, title=?, linkedin_url=?,
                email_pattern_note=?, is_local=?, hq_contact=?,
                confidence_score=?, data_provenance=?, found_at=?,
                source_primary=?, source_linkedin_verified=?, source_linkedin_unverified=?,
                title_from_website=?, title_from_linkedin=?, linkedin_snippet=?,
                ai_verified_title=?, ai_title_confidence=?, ai_title_source=?
            WHERE id=?
        """, (
            first_name, last_name, full_name,
            data.get("title"), linkedin_url,
            data.get("email_pattern_note"), data.get("is_local", 1),
            data.get("hq_contact", 0), data.get("confidence_score", 1.0),
            data.get("data_provenance"), now,
            data.get("source_primary", ""),
            data.get("source_linkedin_verified", 0),
            data.get("source_linkedin_unverified", 0),
            data.get("title_from_website", ""),
            data.get("title_from_linkedin", ""),
            data.get("linkedin_snippet", ""),
            data.get("ai_verified_title"),
            data.get("ai_title_confidence", 0.0),
            data.get("ai_title_source"),
            existing[0]
        ))
        conn.commit()
        conn.close()
        return existing[0]

    # Insert new contact
    cur.execute("""
        INSERT INTO contacts (company_id, first_name, last_name, full_name, title,
            linkedin_url, email_pattern_note, is_local, hq_contact,
            confidence_score, data_provenance, found_at,
            is_manually_edited, manual_notes,
            source_primary, source_linkedin_verified, source_linkedin_unverified,
            title_from_website, title_from_linkedin, linkedin_snippet,
            ai_verified_title, ai_title_confidence, ai_title_source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        company_id, first_name, last_name, full_name,
        data.get("title"), linkedin_url,
        data.get("email_pattern_note"), data.get("is_local", 1),
        data.get("hq_contact", 0), data.get("confidence_score", 1.0),
        data.get("data_provenance"), now,
        data.get("is_manually_edited", 0),
        data.get("manual_notes", ""),
        data.get("source_primary", ""),
        data.get("source_linkedin_verified", 0),
        data.get("source_linkedin_unverified", 0),
        data.get("title_from_website", ""),
        data.get("title_from_linkedin", ""),
        data.get("linkedin_snippet", ""),
        data.get("ai_verified_title"),
        data.get("ai_title_confidence", 0.0),
        data.get("ai_title_source"),
    ))
    conn.commit()
    contact_id = cur.lastrowid
    conn.close()
    return contact_id


def create_session(session_key: str, industry: str, city: str, state: str, region: str, radius_miles: int = 25) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO search_sessions (session_key, industry, city, state, region, radius_miles, created_at, last_active_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active')
    """, (session_key, industry, city, state, region, radius_miles, now, now))
    conn.commit()
    session_id = cur.lastrowid
    conn.close()
    return session_id


def touch_session(session_key: str):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE search_sessions SET last_active_at=? WHERE session_key=?", (now, session_key))
    conn.commit()
    conn.close()


def get_session(session_key: str):
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM search_sessions WHERE session_key=?", (session_key,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_session_companies(session_key: str):
    """Return all companies for a session with contact_count populated.

    Used by GET /api/session/{key}/results. Includes contact_count so the
    frontend can show 'Find Contacts (N found)' before re-running discovery.
    """
    conn = get_db()
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT c.*, COUNT(ct.id) AS contact_count
        FROM companies c
        JOIN search_sessions s ON s.industry = c.search_query
        LEFT JOIN contacts ct
            ON ct.company_id = c.id
        WHERE s.session_key=?
        GROUP BY c.id
        ORDER BY c.id
    """, (session_key,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_session_contacts(session_key: str):
    conn = get_db()
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT ct.* FROM contacts ct
        JOIN companies c ON c.id = ct.company_id
        JOIN search_sessions s ON s.industry = c.search_query
        WHERE s.session_key=?
    """, (session_key,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_sessions():
    conn = get_db()
    cur = conn.cursor()
    rows = cur.execute("SELECT * FROM search_sessions ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_geocode(city: str, state: str = "CA"):
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT lat, lng, source FROM geocode_cache WHERE city=? AND state=?",
        (city.lower(), state.upper())
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def delete_contact(contact_id: int) -> bool:
    """Permanently delete a contact. Returns True if found."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def bulk_delete_contacts(contact_ids: list[int]) -> int:
    """Permanently delete multiple contacts. Returns count of affected rows."""
    if not contact_ids:
        return 0
    conn = get_db()
    cur = conn.cursor()
    placeholders = ",".join("?" * len(contact_ids))
    cur.execute(f"DELETE FROM contacts WHERE id IN ({placeholders})", contact_ids)
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected


def patch_contact(contact_id: int, data: dict) -> bool:
    """
    Update specific fields on a contact.
    Sets is_manually_edited=1 automatically.
    Returns True if contact found and updated.

    Extended 2026-06-09 (QC-16) to also accept:
      - title_from_linkedin, ai_verified_title, ai_title_confidence, ai_title_source
      - is_derived_email, phone
    """
    allowed = {
        "title", "title_from_linkedin", "ai_verified_title",
        "ai_title_confidence", "ai_title_source",
        "manual_notes", "first_name", "last_name", "full_name",
        "linkedin_url", "email", "is_derived_email", "phone",
        "company_id"
    }
    updates = {}
    for key in allowed:
        if key in data:
            updates[key] = data[key]
    if not updates:
        return False
    updates["is_manually_edited"] = 1

    conn = get_db()
    cur = conn.cursor()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [contact_id]
    cur.execute(f"UPDATE contacts SET {set_clause} WHERE id=?", values)
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def patch_company(company_id: int, data: dict) -> bool:
    """
    Update specific fields on a company. Returns True if found and updated.
    Extended 2026-06-07 (QC-14) to also accept:
      - is_manually_edited: 0/1
      - manual_notes: text
      - canonical_business_type: text (AI-categorized)
      - business_type_confidence: float
      - ai_sanity_status: 'ok' | 'issues' | 'pending'
      - ai_sanity_notes: text
    """
    allowed = {
        "email_pattern", "email_pattern_confidence", "email_pattern_source", "website",
        "is_manually_edited", "manual_notes",
        "canonical_business_type", "business_type_confidence",
        "ai_sanity_status", "ai_sanity_notes",
    }
    updates = {}
    for key in allowed:
        if key in data:
            updates[key] = data[key]
    if not updates:
        return False

    conn = get_db()
    cur = conn.cursor()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [company_id]
    cur.execute(f"UPDATE companies SET {set_clause} WHERE id=?", values)
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def delete_company(company_id: int) -> bool:
    """
    Permanently delete a company and all its contacts.
    Also cleans up discovery_job_items for this company.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM contacts WHERE company_id=?", (company_id,))
    cur.execute("DELETE FROM discovery_job_items WHERE company_id=?", (company_id,))
    cur.execute("DELETE FROM companies WHERE id=?", (company_id,))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def bulk_delete_companies(company_ids: list[int]) -> int:
    """Permanently delete multiple companies and their contacts."""
    if not company_ids:
        return 0
    conn = get_db()
    cur = conn.cursor()
    placeholders = ",".join("?" * len(company_ids))
    cur.execute(f"DELETE FROM contacts WHERE company_id IN ({placeholders})", company_ids)
    cur.execute(f"DELETE FROM discovery_job_items WHERE company_id IN ({placeholders})", company_ids)
    cur.execute(f"DELETE FROM companies WHERE id IN ({placeholders})", company_ids)
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected


def create_company_manual(data: dict) -> int:
    """Create a company manually (no place_id required). Returns the new company ID."""
    conn = get_db()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    # Generate a manual place_id so it's unique
    place_id = data.get("place_id") or f"manual-{uuid.uuid4().hex[:12]}"
    cur.execute("""
        INSERT INTO companies (place_id, name, street, city, state, postal_code, country,
            lat, lng, business_type, website, phone, source, search_query, found_at,
            confidence_score, data_provenance)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        place_id, data.get("name", ""),
        data.get("street", ""), data.get("city", ""), data.get("state", ""),
        data.get("postal_code", ""), data.get("country", "USA"),
        data.get("lat"), data.get("lng"),
        data.get("business_type", ""), data.get("website", ""),
        data.get("phone", ""),
        "MANUAL", data.get("search_query", ""), now,
        1.0, "MANUAL_ENTRY"
    ))
    conn.commit()
    company_id = cur.lastrowid
    conn.close()
    return company_id


def get_company(company_id: int) -> Optional[dict]:
    """Get a single company record by id. Returns None if not found."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)


def resume_session(session_key: str) -> bool:
    """Re-activate a session (set status='active', update last_active_at)."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE search_sessions SET status='active', last_active_at=? WHERE session_key=?",
        (now, session_key)
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    touch_session(session_key)
    return affected > 0


def delete_session(session_key: str) -> dict:
    """Permanently delete a session and ALL its related data.

    Hard-deletes: search_sessions, search_results, discovery_jobs,
    and discovery_job_items for this session_key.

    Companies and contacts are NOT deleted — they may belong to other
    sessions (joined via industry = search_query).

    Returns dict with counts of deleted rows.
    """
    conn = get_db()
    cur = conn.cursor()

    # Get session id first
    row = cur.execute(
        "SELECT id FROM search_sessions WHERE session_key=?", (session_key,)
    ).fetchone()
    if not row:
        conn.close()
        return {"deleted": False}

    session_id = row["id"]

    # Delete discovery_job_items for jobs belonging to this session
    cur.execute("""
        DELETE FROM discovery_job_items WHERE job_id IN (
            SELECT job_id FROM discovery_jobs WHERE session_key=?
        )
    """, (session_key,))
    job_items_deleted = cur.rowcount

    # Delete discovery_jobs for this session
    cur.execute("DELETE FROM discovery_jobs WHERE session_key=?", (session_key,))
    jobs_deleted = cur.rowcount

    # Delete search_results referencing this session
    cur.execute("DELETE FROM search_results WHERE session_id=?", (session_id,))
    results_deleted = cur.rowcount

    # Delete the session itself
    cur.execute("DELETE FROM search_sessions WHERE id=?", (session_id,))
    session_deleted = cur.rowcount

    conn.commit()
    conn.close()

    return {
        "deleted": session_deleted > 0,
        "session_rows": session_deleted,
        "result_rows": results_deleted,
        "discovery_jobs": jobs_deleted,
        "discovery_job_items": job_items_deleted,
    }


def get_company_by_id(company_id: int) -> dict | None:
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_companies_for_session(session_key: str) -> list[dict]:
    """Return all companies for a session, with contact counts."""
    conn = get_db()
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT c.*, COUNT(ct.id) as contact_count
        FROM companies c
        JOIN search_sessions s ON s.id = (
            SELECT ss.id FROM search_sessions ss
            WHERE ss.session_key = ?
        )
        LEFT JOIN contacts ct ON ct.company_id = c.id
        WHERE c.search_query = s.industry
        GROUP BY c.id
        ORDER BY c.name
    """, (session_key,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def upsert_geocode(city: str, state: str, lat: float, lng: float, source: str = "GOOGLE"):
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO geocode_cache (city, state, lat, lng, source, cached_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(city, state) DO UPDATE SET lat=excluded.lat, lng=excluded.lng, source=excluded.source, cached_at=excluded.cached_at
    """, (city.lower(), state.upper(), lat, lng, source, now))
    conn.commit()
    conn.close()


# ── Email Validation Cache Helpers ──────────────────────────────────────────

def get_cached_validation(email: str) -> Optional[dict]:
    """Get cached validation result for an email. Returns None if not cached or stale.
    
    Staleness is determined by `cache_ttl_days` from lf_config.json.
    A cached result older than `cache_ttl_days` is treated as not cached.
    """
    from lf_config import get as _get_config
    ttl_days = _get_config("email_validation", {}).get("cache_ttl_days", 7)
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT * FROM email_validation_cache WHERE email=?", (email.strip().lower(),)
    ).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    # Check staleness
    if ttl_days > 0 and result.get("validated_at"):
        from datetime import timedelta
        try:
            validated = datetime.fromisoformat(result["validated_at"])
            if datetime.now(timezone.utc) - validated > timedelta(days=ttl_days):
                return None  # Stale — treat as not cached
        except (ValueError, TypeError):
            pass
    return result


def set_cached_validation(result: dict):
    """Store a validation result in the cache. Upserts on email."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO email_validation_cache
            (email, domain, status, analysis, smtp_probed, smtp_code, mx_host, catch_all, validated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        result.get("email", "").strip().lower(),
        result.get("domain", ""),
        result.get("status", ""),
        result.get("analysis", ""),
        1 if result.get("smtp_probed") else 0,
        result.get("smtp_code"),
        result.get("mx_host"),
        result.get("catch_all"),
        result.get("validated_at", ""),
    ))
    conn.commit()
    conn.close()


def get_cache_stats() -> dict:
    """Return cache statistics: total, by status, unique domains, stale count."""
    from lf_config import get as _get_config
    ttl_days = _get_config("email_validation", {}).get("cache_ttl_days", 7)
    conn = get_db()
    cur = conn.cursor()
    total = cur.execute("SELECT COUNT(*) FROM email_validation_cache").fetchone()[0]
    by_status = {
        r[0]: r[1] for r in cur.execute(
            "SELECT status, COUNT(*) FROM email_validation_cache GROUP BY status"
        ).fetchall()
    }
    unique_domains = cur.execute(
        "SELECT COUNT(DISTINCT domain) FROM email_validation_cache"
    ).fetchone()[0]
    # Count stale entries
    stale = 0
    if ttl_days > 0:
        stale = cur.execute(
            "SELECT COUNT(*) FROM email_validation_cache WHERE validated_at < datetime('now', ?)",
            (f"-{ttl_days} days",)
        ).fetchone()[0]
    conn.close()
    return {
        "total": total, "by_status": by_status, "unique_domains": unique_domains,
        "stale": stale, "ttl_days": ttl_days,
    }


def get_recent_probes(limit: int = 10) -> list[dict]:
    """Return the most recent validation cache entries."""
    conn = get_db()
    cur = conn.cursor()
    rows = cur.execute(
        "SELECT * FROM email_validation_cache ORDER BY validated_at DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def cleanup_stale_cache(dry_run: bool = False) -> dict:
    """
    Remove or count stale cache entries.
    Staleness determined by cache_ttl_days from lf_config.json.
    Returns {deleted, remaining, ttl_days}.
    """
    from lf_config import get as _get_config
    ttl_days = _get_config("email_validation", {}).get("cache_ttl_days", 7)
    if ttl_days <= 0:
        return {"deleted": 0, "remaining": 0, "ttl_days": ttl_days, "reason": "TTL disabled"}
    conn = get_db()
    cur = conn.cursor()
    if dry_run:
        count = cur.execute(
            "SELECT COUNT(*) FROM email_validation_cache WHERE validated_at < datetime('now', ?)",
            (f"-{ttl_days} days",)
        ).fetchone()[0]
        conn.close()
        return {"deleted": 0, "remaining": count, "ttl_days": ttl_days, "dry_run": True}
    cur.execute(
        "DELETE FROM email_validation_cache WHERE validated_at < datetime('now', ?)",
        (f"-{ttl_days} days",)
    )
    deleted = cur.rowcount
    remaining = cur.execute("SELECT COUNT(*) FROM email_validation_cache").fetchone()[0]
    conn.commit()
    conn.close()
    return {"deleted": deleted, "remaining": remaining, "ttl_days": ttl_days, "dry_run": False}


# ── Email Validation Job Helpers ─────────────────────────────────────────────

def create_validation_job(job_id: str, total: int = 0) -> bool:
    """Create a new validation job record. Returns True if created."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO email_validation_jobs (job_id, status, total, done, created_at) VALUES (?, 'pending', ?, 0, ?)",
            (job_id, total, now)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()


def update_validation_job(job_id: str, **kwargs):
    """Update fields on a validation job (status, done, results, finished_at)."""
    allowed = {"status", "done", "results", "finished_at", "started_at"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    conn = get_db()
    cur = conn.cursor()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [job_id]
    cur.execute(f"UPDATE email_validation_jobs SET {set_clause} WHERE job_id=?", values)
    conn.commit()
    conn.close()


def get_validation_job(job_id: str) -> Optional[dict]:
    """Get a validation job record. Returns None if not found."""
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT * FROM email_validation_jobs WHERE job_id=?", (job_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    if result.get("results"):
        try:
            result["results"] = json.loads(result["results"])
        except (json.JSONDecodeError, TypeError):
            pass
    return result


def get_validation_contacts_by_session(session_key: str) -> list[dict]:
    """Return contacts with derived emails for a session, with any cached validation info."""
    conn = get_db()
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT ct.id, ct.full_name, ct.email, ct.is_derived_email,
               c.name as company_name, c.website,
               evc.status as validation_status, evc.analysis as validation_analysis,
               evc.catch_all, evc.smtp_code, evc.validated_at
        FROM contacts ct
        JOIN companies c ON c.id = ct.company_id
        JOIN search_sessions s ON s.industry = c.search_query
        LEFT JOIN email_validation_cache evc ON evc.email = ct.email
        WHERE s.session_key=?
          AND ct.email IS NOT NULL AND ct.email != ''
        ORDER BY ct.full_name
    """, (session_key,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_derived_emails_to_validate(
    revalidate_recent: bool = False,
    max_count: int = 10000,
) -> list[dict]:
    """
    Return all contacts with derived emails that need validation.

    Includes contacts where:
      - email is non-empty
      - email is derived (is_derived_email=1)
      - smtp_validation_status is NULL, 'Unknown', or 'Maybe' (never validated or failed)
      - OR (revalidate_recent=True AND validated > ttl_days ago)

    Excludes contacts already validated as 'Okay to Send' or 'Do Not Send'
    (unless revalidate_recent=True, in which case the TTL applies).

    Returns list of contact dicts: id, full_name, email, company_name, company_id.
    """
    from lf_config import get as _get_config
    ttl_days = _get_config("email_validation", {}).get("cache_ttl_days", 7)
    conn = get_db()
    cur = conn.cursor()

    if revalidate_recent:
        # Re-validate anything older than TTL, regardless of status
        rows = cur.execute("""
            SELECT ct.id, ct.full_name, ct.email, c.name as company_name, c.id as company_id
            FROM contacts ct
            JOIN companies c ON c.id = ct.company_id
            WHERE ct.email IS NOT NULL AND ct.email != ''
              AND ct.is_derived_email = 1
              AND (ct.smtp_validated_at IS NULL
                   OR ct.smtp_validated_at < datetime('now', ?))
            ORDER BY ct.smtp_validated_at ASC, ct.id ASC
            LIMIT ?
        """, (f"-{ttl_days} days", max_count)).fetchall()
    else:
        # Only validate unvalidated or failed/maybe/unknown
        rows = cur.execute("""
            SELECT ct.id, ct.full_name, ct.email, c.name as company_name, c.id as company_id
            FROM contacts ct
            JOIN companies c ON c.id = ct.company_id
            WHERE ct.email IS NOT NULL AND ct.email != ''
              AND ct.is_derived_email = 1
              AND (ct.smtp_validation_status IS NULL
                   OR ct.smtp_validation_status IN ('Unknown', 'Maybe'))
            ORDER BY ct.id ASC
            LIMIT ?
        """, (max_count,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_all_derived_emails() -> dict:
    """
    Return counts of all derived email states.
    Useful for the OWUI tool summary.
    """
    conn = get_db()
    cur = conn.cursor()
    total_derived = cur.execute("""
        SELECT COUNT(*) FROM contacts
        WHERE email IS NOT NULL AND email != ''
          AND is_derived_email = 1
    """).fetchone()[0]
    ready = cur.execute("""
        SELECT COUNT(*) FROM contacts
        WHERE email_ready_for_export = 1
    """).fetchone()[0]
    rejected = cur.execute("""
        SELECT COUNT(*) FROM contacts
        WHERE smtp_validation_status = 'Do Not Send'
    """).fetchone()[0]
    unvalidated = cur.execute("""
        SELECT COUNT(*) FROM contacts
        WHERE email IS NOT NULL AND email != ''
          AND is_derived_email = 1
          AND smtp_validation_status IS NULL
    """).fetchone()[0]
    conn.close()
    return {
        "total_derived": total_derived,
        "ready_for_export": ready,
        "rejected": rejected,
        "unvalidated": unvalidated,
    }


def get_ready_for_export_contacts(limit: int = 500) -> list[dict]:
    """
    Return contacts whose derived email has been validated as 'Okay to Send'.
    These are the ones safe to import to Salesforce / send outreach to.
    """
    conn = get_db()
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT ct.id, ct.full_name, ct.email, ct.title, ct.linkedin_url,
               ct.smtp_validated_at, ct.confidence_score, ct.is_local,
               c.name as company_name, c.id as company_id, c.city, c.state,
               c.website, c.email_pattern, c.email_pattern_confidence,
               s.session_key, s.industry, s.city as search_city
        FROM contacts ct
        JOIN companies c ON c.id = ct.company_id
        JOIN search_sessions s ON s.industry = c.search_query
        WHERE ct.email_ready_for_export = 1
        ORDER BY c.name, ct.full_name
        LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_email_ready_for_export(contact_id: int, ready: bool, reason: str = ""):
    """
    Set the email_ready_for_export flag on a contact.
    Called automatically when validation status changes.
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE contacts SET email_ready_for_export=?, email_rejected_reason=? WHERE id=?",
        (1 if ready else 0, reason if not ready else None, contact_id),
    )
    conn.commit()
    conn.close()


# ── Discovery Job Helpers (QC-17, 2026-06-09) ────────────────────────────────

def create_discovery_job(job_id: str, session_key: str, total: int = 0) -> bool:
    """Create a discovery job record."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO discovery_jobs (job_id, session_key, status, total, done, failed, created_at) VALUES (?, ?, 'pending', ?, 0, 0, ?)",
        (job_id, session_key, total, now),
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def update_discovery_job(job_id: str, **kwargs: dict) -> bool:
    """Update a discovery job's fields."""
    allowed = {"status", "total", "done", "failed", "started_at", "finished_at", "results",
               "current_stage", "stage_log"}  # Issue #3
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    # Serialize stage_log to JSON for storage
    if "stage_log" in updates and updates["stage_log"] is not None and not isinstance(updates["stage_log"], str):
        updates["stage_log"] = json.dumps(updates["stage_log"])
    if not updates:
        return False
    conn = get_db()
    cur = conn.cursor()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [job_id]
    cur.execute(f"UPDATE discovery_jobs SET {set_clause} WHERE job_id=?", values)
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def get_discovery_job(job_id: str) -> Optional[dict]:
    """Get a discovery job record."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM discovery_jobs WHERE job_id=?", (job_id,)).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    if result.get("results"):
        try:
            result["results"] = json.loads(result["results"])
        except (json.JSONDecodeError, TypeError):
            result["results"] = {}
    # Issue #3: deserialize stage_log JSON
    if result.get("stage_log"):
        try:
            result["stage_log"] = json.loads(result["stage_log"])
        except (json.JSONDecodeError, TypeError):
            result["stage_log"] = []
    else:
        result["stage_log"] = []
    return result


def get_active_discovery_jobs(session_key: str) -> list[dict]:
    """Get all active (non-finished) discovery jobs for a session."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM discovery_jobs WHERE session_key=? AND status NOT IN ('completed', 'failed') ORDER BY created_at DESC LIMIT 10",
        (session_key,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_discovery_job_items(job_id: str, companies: list[dict]) -> int:
    """Add companies to a discovery job as items to process."""
    from lf_db import get_db
    conn = get_db()
    cur = conn.cursor()
    count = 0
    for c in companies:
        try:
            cur.execute(
                "INSERT OR IGNORE INTO discovery_job_items (job_id, company_id, company_name, place_id, status) VALUES (?, ?, ?, ?, 'pending')",
                (job_id, c.get("id", 0) or 0, c.get("name", ""), c.get("place_id", ""),),
            )
            count += cur.rowcount if cur.rowcount > 0 else 0
        except Exception:
            pass
    conn.commit()
    conn.close()
    return count


def get_next_discovery_pending(job_id: str) -> Optional[dict]:
    """Get the next pending item from a discovery job."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM discovery_job_items WHERE job_id=? AND status='pending' LIMIT 1",
        (job_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_discovery_job_item(item_id: int, status: str, result: str = "") -> bool:
    """Update a discovery job item's status."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE discovery_job_items SET status=?, result=? WHERE id=?",
        (status, result, item_id),
    )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def get_discovery_job_items(job_id: str) -> list[dict]:
    """Get all items for a discovery job."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM discovery_job_items WHERE job_id=? ORDER BY id",
        (job_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == "__main__":
    print("Initializing lf.db...")
    init_db()
    print("Seeding geocode cache...")
    seed_geocode_cache()
    print("Done.")
