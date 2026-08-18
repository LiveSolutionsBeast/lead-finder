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
    # timeout=10s lets concurrent writers wait briefly instead of
    # immediately raising "database is locked" (server + batch jobs).
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    conn.row_factory = sqlite3.Row
    # busy_timeout=10000ms so SQLite retries writes for up to 10s when
    # another connection holds the write lock.
    conn.execute("PRAGMA busy_timeout=10000")
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
        manual_notes TEXT,
        email TEXT,
        is_derived_email INTEGER DEFAULT 0,
        source_primary TEXT,
        source_linkedin_verified INTEGER DEFAULT 0,
        source_linkedin_unverified INTEGER DEFAULT 0,
        title_from_website TEXT,
        title_from_linkedin TEXT,
        linkedin_snippet TEXT,
        is_test INTEGER DEFAULT 0
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
    # Phase 2: Pattern proof gate — store the sample email and result that proved the pattern.
    _add_column_if_missing(cur, "companies", "email_pattern_proof_email", "TEXT")
    _add_column_if_missing(cur, "companies", "email_pattern_proof_status", "TEXT")
    _add_column_if_missing(cur, "companies", "email_pattern_proven_at", "TEXT")
    _add_column_if_missing(cur, "companies", "email_pattern_unproved_reason", "TEXT")
    # Phase 1 domain verification: confirmed email domain source-of-truth.
    _add_column_if_missing(cur, "companies", "email_domain_confirmed", "TEXT")
    _add_column_if_missing(cur, "companies", "email_domain_source", "TEXT")
    _add_column_if_missing(cur, "companies", "email_domain_checked_at", "TEXT")
    _add_column_if_missing(cur, "companies", "email_domain_evidence", "TEXT")  # JSON
    _add_column_if_missing(cur, "companies", "email_domain_mismatch", "INTEGER DEFAULT 0")
    # QC-14: AI sanity + manual editing for companies (added 2026-06-07)
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
    # Phase 1 / CONTRACTS.md section 1 — extended validation fields on the cache
    # (added 2026-07-26). Mirrors the Phase 1 columns on `contacts` so a cache hit
    # can re-apply the full validation record instead of dropping the Phase 1
    # metadata (confidence/method/checked_at/mx_host/response/latency).
    _add_column_if_missing(cur, "email_validation_cache", "validation_confidence", "REAL NOT NULL DEFAULT 0.0")
    _add_column_if_missing(cur, "email_validation_cache", "validation_method", "TEXT")  # 'smtp_live' | 'smtp_cached' | 'manual_override'
    _add_column_if_missing(cur, "email_validation_cache", "validation_checked_at", "TEXT")  # ISO 8601
    _add_column_if_missing(cur, "email_validation_cache", "validation_mx_host", "TEXT")
    _add_column_if_missing(cur, "email_validation_cache", "validation_response", "TEXT")  # SMTP response code + text
    _add_column_if_missing(cur, "email_validation_cache", "validation_latency_ms", "INTEGER")
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

    # Phase 1 / CONTRACTS.md section 1 — extended validation fields (added 2026-07-24)
    _add_column_if_missing(cur, "contacts", "validation_confidence", "REAL NOT NULL DEFAULT 0.0")
    _add_column_if_missing(cur, "contacts", "validation_method", "TEXT")  # 'smtp_live' | 'smtp_cached' | 'manual_override'
    _add_column_if_missing(cur, "contacts", "validation_checked_at", "TEXT")  # ISO 8601
    _add_column_if_missing(cur, "contacts", "validation_mx_host", "TEXT")
    _add_column_if_missing(cur, "contacts", "validation_response", "TEXT")  # SMTP response code + text
    _add_column_if_missing(cur, "contacts", "validation_latency_ms", "INTEGER")

    # Phase 1 cross-over for Session 2 — contacts.linkedin_slug UNIQUE (added 2026-07-24)
    # The plugin needs to write to this column. Session 2 owns the migration, but the
    # column itself is part of the shared schema so Session 1 adds it now.
    _add_column_if_missing(cur, "contacts", "linkedin_slug", "TEXT")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_contacts_linkedin_slug ON contacts(linkedin_slug)")

    # Layer 2 / 2026-07-25: preserve raw LinkedIn region strings (e.g. "Los Angeles Metropolitan Area")
    # while storing a resolved real city in contacts.location.
    _add_column_if_missing(cur, "contacts", "linkedin_location_raw", "TEXT")

    # Layer 2 / 2026-07-26: store Google Places enrichment results directly on the contact
    # for Salesforce export. These columns are not shown in the popup UI.
    _add_column_if_missing(cur, "contacts", "linkedin_resolved_city", "TEXT")
    _add_column_if_missing(cur, "contacts", "linkedin_resolved_state", "TEXT")
    _add_column_if_missing(cur, "contacts", "linkedin_resolved_country", "TEXT")
    _add_column_if_missing(cur, "contacts", "linkedin_resolved_lat", "REAL")
    _add_column_if_missing(cur, "contacts", "linkedin_resolved_lng", "REAL")
    _add_column_if_missing(cur, "contacts", "linkedin_resolved_formatted_address", "TEXT")
    _add_column_if_missing(cur, "contacts", "linkedin_resolved_place_id", "TEXT")
    _add_column_if_missing(cur, "contacts", "linkedin_resolved_source", "TEXT")
    _add_column_if_missing(cur, "contacts", "linkedin_resolved_at", "TEXT")

    # H-6 (2026-07-26): is_test flag to isolate test fixtures from production queries.
    # E2E/plugin test runners set is_test=1; production queries filter is_test=0.
    _add_column_if_missing(cur, "contacts", "is_test", "INTEGER DEFAULT 0")

    # Engagement verification / cornerstone contact + Salesforce push tracking
    # (added 2026-08-05). Allows one contact per company to be picked as the
    # pattern-proof cornerstone, then auto-pushes proven contacts to Salesforce.
    _add_column_if_missing(cur, "contacts", "is_cornerstone", "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, "contacts", "cornerstone_picked_at", "TEXT")
    _add_column_if_missing(cur, "contacts", "sf_lead_id", "TEXT")
    _add_column_if_missing(cur, "contacts", "sf_pushed_at", "TEXT")
    _add_column_if_missing(cur, "contacts", "sf_push_status", "TEXT")
    _add_column_if_missing(cur, "contacts", "sf_push_error", "TEXT")
    _add_column_if_missing(cur, "contacts", "sf_push_source", "TEXT")
    # EspoCRM push tracking (added 2026-08-11). EspoCRM is the default push
    # destination for engagement-verify; the sf_* columns above are kept for
    # rollback / audit history.
    _add_column_if_missing(cur, "contacts", "espo_lead_id", "TEXT")
    _add_column_if_missing(cur, "contacts", "espo_pushed_at", "TEXT")
    _add_column_if_missing(cur, "contacts", "espo_push_status", "TEXT")  # created | updated | error | skipped
    _add_column_if_missing(cur, "contacts", "espo_push_error", "TEXT")
    _add_column_if_missing(cur, "contacts", "espo_push_source", "TEXT")
    _add_column_if_missing(cur, "contacts", "espo_lead_url", "TEXT")
    _add_column_if_missing(cur, "companies", "sf_account_id", "TEXT")
    _add_column_if_missing(cur, "companies", "sf_account_pushed_at", "TEXT")
    _add_column_if_missing(cur, "companies", "sf_account_push_status", "TEXT")
    _add_column_if_missing(cur, "companies", "espo_account_id", "TEXT")
    _add_column_if_missing(cur, "companies", "espo_account_pushed_at", "TEXT")
    _add_column_if_missing(cur, "companies", "espo_account_push_status", "TEXT")
    _add_column_if_missing(cur, "companies", "espo_account_url", "TEXT")
    _add_column_if_missing(cur, "companies", "pattern_apply_count", "INTEGER DEFAULT 0")
    _add_column_if_missing(cur, "companies", "pattern_apply_batch_id", "TEXT")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_companies_cornerstone ON companies(cornerstone_contact_id) WHERE cornerstone_contact_id IS NOT NULL")

    # Layer 2 / 2026-07-25: cache Google Places region-to-city resolutions so repeated
    # LinkedIn pushes with the same (company, region) don't hit the API every time.
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS location_resolution_cache (
            raw_location TEXT NOT NULL,
            company_name TEXT NOT NULL,
            resolved_city TEXT,
            resolved_state TEXT,
            resolved_country TEXT,
            place_id TEXT,
            lat REAL,
            lng REAL,
            formatted_address TEXT,
            source TEXT DEFAULT 'google_places',
            cached_at TEXT,
            PRIMARY KEY (raw_location, company_name)
        );
        CREATE INDEX IF NOT EXISTS idx_locres_place_id ON location_resolution_cache(place_id);
        CREATE INDEX IF NOT EXISTS idx_locres_cached_at ON location_resolution_cache(cached_at);
    """)

    # Engagement verification / cornerstone company state machine
    # (added 2026-08-05). Tracks one pattern-proof run per company.
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS engagement_company_runs (
            run_id           TEXT PRIMARY KEY,
            company_id       INTEGER NOT NULL REFERENCES companies(id),
            cornerstone_contact_id INTEGER REFERENCES contacts(id),
            pattern_template TEXT,
            state            TEXT NOT NULL DEFAULT 'queued',
            queued_at        TEXT,
            sent_at          TEXT,
            proven_at        TEXT,
            sf_pushed_at     TEXT,
            apply_count      INTEGER DEFAULT 0,
            sf_push_count    INTEGER DEFAULT 0,
            last_error       TEXT,
            notes            TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ecr_company ON engagement_company_runs(company_id);
        CREATE INDEX IF NOT EXISTS idx_ecr_state   ON engagement_company_runs(state);
    """)

    # SMTP repair 2026-08-17: send queue for human-curated bulk send.
    # Populated by resolve_and_validate_email when a contact is marked
    # "Okay to Send" via the new pattern-proof + SMTP chain on the Contacts page.
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS email_send_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contact_id INTEGER NOT NULL REFERENCES contacts(id),
            company_id INTEGER NOT NULL REFERENCES companies(id),
            queued_email TEXT NOT NULL,
            pattern_template TEXT,
            subject TEXT,
            body_template_path TEXT,
            status TEXT DEFAULT 'queued',
            created_at TEXT NOT NULL,
            sent_at TEXT,
            send_error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_esq_status ON email_send_queue(status);
        CREATE INDEX IF NOT EXISTS idx_esq_contact ON email_send_queue(contact_id);
        CREATE INDEX IF NOT EXISTS idx_esq_company ON email_send_queue(company_id);
    """)

    # QC-17: Discovery jobs for background processing (added 2026-06-09)
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS discovery_jobs (
            job_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            job_type TEXT DEFAULT 'discovery',
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
        CREATE INDEX IF NOT EXISTS idx_dj_type ON discovery_jobs(job_type);
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
            contact_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            result TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_dji_job ON discovery_job_items(job_id);
        CREATE INDEX IF NOT EXISTS idx_dji_status ON discovery_job_items(status);
        CREATE INDEX IF NOT EXISTS idx_dji_contact ON discovery_job_items(contact_id);
    """)
    _add_column_if_missing(cur, "discovery_job_items", "contact_id", "INTEGER")

    # QC-20 / QC-21: per-entity pipeline stage + AI call audit log (2026-07-09)
    _add_column_if_missing(cur, "contacts", "pipeline_stage", "TEXT")
    _add_column_if_missing(cur, "contacts", "ai_edited_at", "TEXT")
    _add_column_if_missing(cur, "companies", "pipeline_stage", "TEXT")
    _add_column_if_missing(cur, "companies", "ai_edited_at", "TEXT")

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS ai_call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            entity_type TEXT,
            entity_id INTEGER,
            model TEXT NOT NULL,
            operation TEXT NOT NULL,
            stage TEXT,
            latency_ms INTEGER DEFAULT 0,
            success INTEGER DEFAULT 0,
            gap_count INTEGER DEFAULT 0,
            tokens INTEGER,
            cost REAL,
            prompt_hash TEXT,
            response_hash TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_acl_entity ON ai_call_log(entity_type, entity_id);
        CREATE INDEX IF NOT EXISTS idx_acl_ts ON ai_call_log(ts);
        CREATE INDEX IF NOT EXISTS idx_acl_operation ON ai_call_log(operation);
        CREATE INDEX IF NOT EXISTS idx_acl_model ON ai_call_log(model);
    """)

    # Backfill existing rows to the first pipeline stage
    cur.execute("UPDATE contacts SET pipeline_stage = 'discovered' WHERE pipeline_stage IS NULL OR pipeline_stage = ''")
    cur.execute("UPDATE companies SET pipeline_stage = 'discovered' WHERE pipeline_stage IS NULL OR pipeline_stage = ''")

    # ── Phase 2: LinkedIn plugin tables (CONTRACTS.md section 3) ──────────
    # Session 2 adds two new tables for the Chrome extension pathway:
    #   contact_experience — full role history per contact
    #   pending_pushes     — audit log for every plugin-injected profile
    # Session 1 already added contacts.linkedin_slug above (cross-over column).
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS contact_experience (
            id INTEGER PRIMARY KEY,
            contact_id INTEGER NOT NULL,
            company_name TEXT,
            title TEXT,
            started_at TEXT,
            ended_at TEXT,
            is_current INTEGER DEFAULT 0,
            linkedin_slug TEXT,
            source TEXT,
            captured_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_contact_experience_contact
            ON contact_experience(contact_id);
        CREATE INDEX IF NOT EXISTS idx_contact_experience_slug
            ON contact_experience(linkedin_slug);

        CREATE TABLE IF NOT EXISTS pending_pushes (
            id INTEGER PRIMARY KEY,
            linkedin_slug TEXT NOT NULL,
            raw_payload TEXT NOT NULL,
            matched_action TEXT,
            matched_contact_id INTEGER,
            matched_company_id INTEGER,
            committed_at TEXT,
            committed_by TEXT,
            created_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_pending_pushes_slug
            ON pending_pushes(linkedin_slug);
        CREATE INDEX IF NOT EXISTS idx_pending_pushes_created
            ON pending_pushes(created_at);
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

    If company_id is changed intentionally by the caller, call
    move_contact_to_company() instead.
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

    # Check for existing contact by LinkedIn URL first (strongest signal).
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
                data.get("hq_contact", 0), data.get("confidence_score", 0.0),
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

    # Check by full_name within company (second signal).
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
            data.get("hq_contact", 0), data.get("confidence_score", 0.0),
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
        data.get("hq_contact", 0), data.get("confidence_score", 0.0),
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
    Extended 2026-07-09 (QC-21) to:
      - track pipeline_stage and ai_edited_at
      - only set is_manually_edited=1 when explicitly requested OR when no
        ai_edited_at marker is present (backward compatibility for legacy callers)
    Returns True if contact found and updated.
    """
    allowed = {
        "title", "title_from_linkedin", "ai_verified_title",
        "ai_title_confidence", "ai_title_source",
        "manual_notes", "first_name", "last_name", "full_name",
        "linkedin_url", "linkedin_slug", "email", "is_derived_email", "phone", "location",
        "source_primary", "source_linkedin_verified", "source_linkedin_unverified",
        "title_from_website", "linkedin_snippet", "linkedin_location_raw",
        "linkedin_resolved_city", "linkedin_resolved_state", "linkedin_resolved_country",
        "linkedin_resolved_lat", "linkedin_resolved_lng",
        "linkedin_resolved_formatted_address", "linkedin_resolved_place_id",
        "linkedin_resolved_source", "linkedin_resolved_at",
        "email_ready_for_export", "email_rejected_reason",
        "company_id", "is_manually_edited", "ai_edited_at", "pipeline_stage",
        "is_local", "hq_contact", "confidence_score",
        "smtp_validation_status", "smtp_validated_at", "smtp_validation_code",
        "is_cornerstone", "cornerstone_picked_at",
        "sf_lead_id", "sf_pushed_at", "sf_push_status", "sf_push_error", "sf_push_source",
        # EspoCRM push tracking (added 2026-08-11). Default destination.
        "espo_lead_id", "espo_pushed_at", "espo_push_status", "espo_push_error",
        "espo_push_source", "espo_lead_url",
    }
    updates = {}
    for key in allowed:
        if key in data:
            updates[key] = data[key]
    if not updates:
        return False

    # Attribution: manual edits must explicitly pass is_manually_edited=1.
    # AI-driven callers pass ai_edited_at and avoid flagging the record manual.
    # Phase 5: removed implicit default to is_manually_edited=1 for back-compat.
    # Every caller must now be explicit about the manual-edit flag.
    if "is_manually_edited" in data:
        updates["is_manually_edited"] = data["is_manually_edited"]

    conn = get_db()
    cur = conn.cursor()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [contact_id]
    cur.execute(f"UPDATE contacts SET {set_clause} WHERE id=?", values)
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def move_contact_to_company(contact_id: int, new_company_id: int, data: dict | None = None) -> bool:
    """
    Move a contact to a different company and optionally update fields.
    Re-splits full_name into first/last in case the company changed.
    Resets email derivation flags because the company (and therefore domain)
    changed.
    """
    data = data or {}
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    if not row:
        conn.close()
        return False
    ct = dict(row)

    full_name = data.get("full_name", ct.get("full_name", "")).strip()
    parts = full_name.split(" ", 1)
    first_name = parts[0] if parts else full_name
    last_name = parts[1] if len(parts) > 1 else ""

    updates = {
        "company_id": new_company_id,
        "first_name": first_name,
        "last_name": last_name,
        "full_name": full_name,
    }
    # Carry over optional updated fields
    for k in ["title", "linkedin_url", "email", "phone", "location", "confidence_score",
              "ai_verified_title", "ai_title_confidence", "ai_title_source",
              "title_from_linkedin", "source_primary", "manual_notes"]:
        if k in data:
            updates[k] = data[k]
    # Domain changed: clear derived email until re-derived/validated
    if "email" not in data or not data.get("email"):
        updates["email"] = None
        updates["is_derived_email"] = 0
        updates["email_ready_for_export"] = 0

    set_clause = ", ".join(f"{k}=?" for k in updates)
    cur.execute(f"UPDATE contacts SET {set_clause} WHERE id=?", list(updates.values()) + [contact_id])
    conn.commit()
    conn.close()
    return True


def find_company_by_name(name: str) -> dict | None:
    """Find the best-matching existing company by normalized name."""
    if not name:
        return None
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    # Exact match first
    row = cur.execute("SELECT * FROM companies WHERE name=?", (name,)).fetchone()
    if not row:
        # Case-insensitive substring match either direction
        row = cur.execute(
            "SELECT * FROM companies WHERE LOWER(name) LIKE LOWER(?) OR LOWER(?) LIKE LOWER(name)",
            (f"%{name}%", f"%{name}%"),
        ).fetchone()
    conn.close()
    return dict(row) if row else None


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
        "email_pattern_proof_email", "email_pattern_proof_status",
        "email_pattern_proven_at", "email_pattern_unproved_reason",
        "is_manually_edited", "manual_notes",
        "canonical_business_type", "business_type_confidence",
        "ai_sanity_status", "ai_sanity_notes",
        "pipeline_stage", "ai_edited_at",
        "cornerstone_contact_id", "cornerstone_locked_until",
        "sf_account_id", "sf_account_pushed_at", "sf_account_push_status",
        # EspoCRM Account push tracking (added 2026-08-11). Default destination.
        "espo_account_id", "espo_account_pushed_at", "espo_account_push_status",
        "espo_account_url",
        "pattern_apply_count", "pattern_apply_batch_id",
        # Phase 1 domain verification columns.
        "email_domain_confirmed", "email_domain_source", "email_domain_checked_at",
        "email_domain_evidence", "email_domain_mismatch",
    }
    updates = {}
    for key in allowed:
        if key in data:
            updates[key] = data[key]
    if not updates:
        return False

    # Attribution: manual edits must explicitly pass is_manually_edited=1.
    # AI-driven callers pass ai_edited_at and avoid flagging the record manual.
    if "is_manually_edited" in data:
        updates["is_manually_edited"] = data["is_manually_edited"]
    elif "ai_edited_at" not in updates:
        updates["is_manually_edited"] = 1

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
    # Allow callers to override source/data_provenance for imports vs manual entry
    source = data.get("source", "MANUAL") if data.get("source") else "MANUAL"
    data_provenance = data.get("data_provenance", "MANUAL_ENTRY") if data.get("data_provenance") else "MANUAL_ENTRY"
    confidence_score = data.get("confidence_score")
    if confidence_score is None:
        confidence_score = 1.0
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
        source, data.get("search_query", ""), now,
        confidence_score, data_provenance
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
    """Store a validation result in the cache. Upserts on email.

    Writes BOTH the legacy 9 columns AND the Phase 1 extended columns so a
    cache hit can re-apply the full validation record (matching the columns
    written by `write_contact_validation`). Phase 1 fields default sensibly
    when absent from `result` (e.g. legacy callers).
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT OR REPLACE INTO email_validation_cache
            (email, domain, status, analysis, smtp_probed, smtp_code, mx_host,
             catch_all, validated_at,
             validation_confidence, validation_method, validation_checked_at,
             validation_mx_host, validation_response, validation_latency_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        # Phase 1 extended fields
        float(result.get("validation_confidence") or 0.0),
        result.get("validation_method") or "",
        result.get("validation_checked_at") or "",
        result.get("validation_mx_host") or "",
        result.get("validation_response") or "",
        int(result.get("validation_latency_ms") or 0),
    ))
    conn.commit()
    conn.close()


# ── Contact Validation Writer (Phase 1 / t1.2) ─────────────────────────────

def write_contact_validation(contact_id: int, result: dict) -> None:
    """
    Persist a ValidationResult (as dict) onto a contact row.
    Writes BOTH the legacy 5 columns AND the new Phase 1 columns.
    This is the single source of truth for validation field writes — all 4
    call sites in lf_server.py and lf_email_patterns.py use it.

    Fields written:
      Legacy: smtp_validation_status, smtp_validated_at, smtp_validation_code,
              email_ready_for_export, email_rejected_reason
      Phase 1: validation_confidence, validation_method, validation_checked_at,
                validation_mx_host, validation_response, validation_latency_ms
    """
    status = result.get("status") or ""
    ready = 1 if status == "Okay to Send" else 0
    rejected_reason = result.get("analysis", "") if status == "Do Not Send" else None
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        UPDATE contacts SET
            smtp_validation_status = ?,
            smtp_validated_at = ?,
            smtp_validation_code = ?,
            email_ready_for_export = ?,
            email_rejected_reason = ?,
            validation_confidence = ?,
            validation_method = ?,
            validation_checked_at = ?,
            validation_mx_host = ?,
            validation_response = ?,
            validation_latency_ms = ?
        WHERE id = ?
        """,
        (
            status,
            result.get("validated_at") or result.get("validation_checked_at") or "",
            result.get("smtp_code"),
            ready,
            rejected_reason,
            result.get("validation_confidence", 0.0) or 0.0,
            result.get("validation_method") or "smtp_live",
            result.get("validation_checked_at") or result.get("validated_at") or "",
            result.get("validation_mx_host") or result.get("mx_host") or "",
            result.get("validation_response") or "",
            result.get("validation_latency_ms", 0) or 0,
            contact_id,
        ),
    )
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
    Return all contacts with emails that need validation.

    Includes contacts where:
      - email is non-empty
      - smtp_validation_status is NULL, 'Unknown', or 'Maybe' (never validated or failed)
      - OR (revalidate_recent=True AND validated > ttl_days ago)

    Excludes contacts already validated as 'Okay to Send' or 'Do Not Send'
    (unless revalidate_recent=True, in which case the TTL applies).

    Note: Despite the name, this covers ALL contacts with emails — both derived
    (is_derived_email=1) and non-derived (is_derived_email=0, e.g. popup-captured).
    Per CONTRACTS.md §1, NULL/Maybe status needs validation regardless of
    derivation. The historical is_derived_email=1 filter was dropped because it
    silently excluded non-derived contacts that had never been SMTP-probed.

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
              AND (ct.smtp_validation_status IS NULL
                   OR ct.smtp_validation_status IN ('Unknown', 'Maybe'))
            ORDER BY ct.id ASC
            LIMIT ?
        """, (max_count,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_all_derived_emails() -> dict:
    """
    Return counts of all email-bearing contact validation states.
    Useful for the OWUI tool summary.

    Note: Despite the name, `total_derived` and `unvalidated` count ALL contacts
    with emails — both derived and non-derived. The historical is_derived_email=1
    filter was dropped so non-derived contacts that have never been validated are
    counted (and can be picked up by bulk revalidation).
    """
    conn = get_db()
    cur = conn.cursor()
    total_derived = cur.execute("""
        SELECT COUNT(*) FROM contacts
        WHERE email IS NOT NULL AND email != ''
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


def get_send_ready_contacts(
    status: Optional[str] = None,
    company_id: Optional[int] = None,
    session_key: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    """
    Return contacts that are queued for human send via email_send_queue.

    Joins queue rows with contacts and companies. Optionally filters by:
      - status: 'queued' | 'sent' | 'failed' | 'skipped' | None for all
      - company_id: limit to a single company
      - session_key: limit to companies in a session (via search_sessions.industry)
    """
    params: list = []
    where_parts: list[str] = []
    if status:
        where_parts.append("q.status = ?")
        params.append(status)
    if company_id:
        where_parts.append("q.company_id = ?")
        params.append(company_id)
    if session_key:
        where_parts.append("c.search_query = (SELECT s.industry FROM search_sessions s WHERE s.session_key = ? LIMIT 1)")
        params.append(session_key)

    where_sql = " WHERE " + " AND ".join(where_parts) if where_parts else ""

    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(f"""
        SELECT q.id as queue_id, q.status as queue_status, q.queued_email,
               q.pattern_template, q.subject, q.body_template_path,
               q.created_at, q.sent_at, q.send_error,
               ct.id as contact_id, ct.full_name, ct.first_name, ct.last_name,
               ct.title, ct.linkedin_url, ct.email as contact_email,
               co.id as company_id, co.name as company_name, co.website,
               co.city, co.state, co.email_pattern as company_pattern
        FROM email_send_queue q
        JOIN contacts ct ON ct.id = q.contact_id
        JOIN companies co ON co.id = q.company_id
        {where_sql}
        ORDER BY q.created_at DESC
        LIMIT ?
    """, tuple(params) + (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def insert_email_send_queue(
    contact_id: int,
    company_id: int,
    queued_email: str,
    pattern_template: Optional[str] = None,
    subject: Optional[str] = None,
    body_template_path: Optional[str] = None,
) -> int:
    """Insert a contact into the human send queue. Returns the new queue row id."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO email_send_queue
            (contact_id, company_id, queued_email, pattern_template, subject,
             body_template_path, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
    """, (contact_id, company_id, queued_email, pattern_template, subject, body_template_path, now))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id or 0


def update_email_send_queue(queue_id: int, **kwargs) -> bool:
    """Update fields on an email_send_queue row."""
    allowed = {"status", "sent_at", "send_error", "subject", "body_template_path"}
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    if not updates:
        return False
    conn = get_db()
    cur = conn.cursor()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [queue_id]
    cur.execute(f"UPDATE email_send_queue SET {set_clause} WHERE id=?", values)
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def remove_from_send_queue(queue_ids: list[int], *, hard_delete: bool = False) -> int:
    """
    Remove queue rows. By default marks them 'skipped' so they remain auditable.
    Pass hard_delete=True to actually delete rows.
    """
    if not queue_ids:
        return 0
    conn = get_db()
    cur = conn.cursor()
    placeholders = ",".join("?" for _ in queue_ids)
    if hard_delete:
        cur.execute(f"DELETE FROM email_send_queue WHERE id IN ({placeholders})", queue_ids)
    else:
        now = datetime.now(timezone.utc).isoformat()
        cur.execute(
            f"UPDATE email_send_queue SET status='skipped', sent_at=? WHERE id IN ({placeholders})",
            (now,) + tuple(queue_ids),
        )
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected


# ── Pipeline Stage Helpers (QC-20, 2026-07-09) ───────────────────────────────

def get_entity_stage(entity_type: str, entity_id: int) -> Optional[str]:
    """Return the current pipeline_stage for a contact or company."""
    if entity_type not in ("contact", "company") or not entity_id:
        return None
    table = "contacts" if entity_type == "contact" else "companies"
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute(f"SELECT pipeline_stage FROM {table} WHERE id=?", (entity_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def set_entity_stage(entity_type: str, entity_id: int, stage: str) -> bool:
    """Set pipeline_stage for a contact or company. Returns True if row updated.
    Validates the stage against the canonical stage lists in lf_stages."""
    if entity_type not in ("contact", "company") or not entity_id or not stage:
        return False
    # Lazy import to avoid a module-level cycle (lf_stages may be used by callers)
    from lf_stages import is_valid_contact_stage, is_valid_company_stage
    if entity_type == "contact" and not is_valid_contact_stage(stage):
        return False
    if entity_type == "company" and not is_valid_company_stage(stage):
        return False
    table = "contacts" if entity_type == "contact" else "companies"
    conn = get_db()
    cur = conn.cursor()
    cur.execute(f"UPDATE {table} SET pipeline_stage=? WHERE id=?", (stage, entity_id))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


# ── AI Call Log (QC-21, 2026-07-09) ─────────────────────────────────────────

def log_ai_call(
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    model: str = "",
    operation: str = "",
    stage: Optional[str] = None,
    latency_ms: int = 0,
    success: bool = False,
    gap_count: int = 0,
    tokens: Optional[int] = None,
    cost: Optional[float] = None,
    prompt_hash: Optional[str] = None,
    response_hash: Optional[str] = None,
) -> None:
    """
    Write a single AI model call to the ai_call_log table.
    This augments the file-based provider_usage.json with per-entity,
    per-call, timestamped records.
    """
    if not model or not operation:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO ai_call_log (
            ts, entity_type, entity_id, model, operation, stage,
            latency_ms, success, gap_count, tokens, cost,
            prompt_hash, response_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        now, entity_type, entity_id, model, operation, stage,
        latency_ms, 1 if success else 0, gap_count, tokens, cost,
        prompt_hash, response_hash,
    ))
    conn.commit()
    conn.close()


def get_ai_call_log_summary(days: int = 30) -> dict:
    """
    Aggregate ai_call_log into per-model, per-operation, per-stage, and
    per-entity breakdowns for the last N days.
    """
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_db()
    cur = conn.cursor()

    total_row = cur.execute(
        "SELECT COUNT(*), SUM(latency_ms), SUM(success) FROM ai_call_log WHERE ts >= ?",
        (cutoff,)
    ).fetchone()
    total_calls = total_row[0] or 0
    total_latency = total_row[1] or 0
    total_success = total_row[2] or 0

    by_model = {}
    for r in cur.execute(
        "SELECT model, COUNT(*), SUM(latency_ms), SUM(success) FROM ai_call_log WHERE ts >= ? GROUP BY model",
        (cutoff,)
    ).fetchall():
        by_model[r[0]] = {"calls": r[1], "total_latency_ms": r[2] or 0, "success": r[3] or 0}

    by_operation = {}
    for r in cur.execute(
        "SELECT operation, COUNT(*), SUM(latency_ms), SUM(success) FROM ai_call_log WHERE ts >= ? GROUP BY operation",
        (cutoff,)
    ).fetchall():
        by_operation[r[0]] = {"calls": r[1], "total_latency_ms": r[2] or 0, "success": r[3] or 0}

    by_stage = {}
    for r in cur.execute(
        "SELECT stage, COUNT(*), SUM(latency_ms), SUM(success) FROM ai_call_log WHERE ts >= ? GROUP BY stage",
        (cutoff,)
    ).fetchall():
        by_stage[r[0] or "unknown"] = {"calls": r[1], "total_latency_ms": r[2] or 0, "success": r[3] or 0}

    recent = []
    for r in cur.execute(
        """SELECT ts, entity_type, entity_id, model, operation, stage, latency_ms, success
           FROM ai_call_log WHERE ts >= ? ORDER BY ts DESC LIMIT 50""",
        (cutoff,)
    ).fetchall():
        recent.append({
            "ts": r[0], "entity_type": r[1], "entity_id": r[2],
            "model": r[3], "operation": r[4], "stage": r[5],
            "latency_ms": r[6], "success": bool(r[7]),
        })

    conn.close()
    return {
        "days": days,
        "total_calls": total_calls,
        "total_success": total_success,
        "avg_latency_ms": round(total_latency / total_calls, 1) if total_calls else 0,
        "by_model": by_model,
        "by_operation": by_operation,
        "by_stage": by_stage,
        "recent_calls": recent,
    }


# ── Discovery Job Helpers (QC-17, 2026-06-09) ────────────────────────────────

def create_discovery_job(job_id: str, session_key: str, total: int = 0,
                         job_type: str = "discovery") -> bool:
    """Create a discovery job record."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO discovery_jobs (job_id, session_key, job_type, status, total, done, failed, created_at) VALUES (?, ?, ?, 'pending', ?, 0, 0, ?)",
        (job_id, session_key, job_type, total, now),
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


def append_discovery_job_event(
    job_id: str,
    event: dict,
    max_events: int = 200,
) -> bool:
    """Append an event to a job's stage_log and update current_stage.

    Used for live progress streaming: the frontend polls
    /api/discover-job/{job_id} and shows the latest current_stage and the
    rolling event buffer.
    """
    if not job_id or not isinstance(event, dict):
        return False
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    row = cur.execute("SELECT stage_log, current_stage FROM discovery_jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        conn.close()
        return False
    log_raw = row["stage_log"]
    if log_raw:
        try:
            log = json.loads(log_raw) if isinstance(log_raw, str) else log_raw
        except (json.JSONDecodeError, TypeError):
            log = []
    else:
        log = []
    if not isinstance(log, list):
        log = []
    log.append(event)
    if len(log) > max_events:
        log = log[-max_events:]
    new_stage = event.get("stage") or row["current_stage"]
    cur.execute(
        "UPDATE discovery_jobs SET stage_log=?, current_stage=? WHERE job_id=?",
        (json.dumps(log), new_stage, job_id),
    )
    conn.commit()
    conn.close()
    return True


def append_discovery_job_item_event(
    item_id: int,
    event: dict,
    max_events: int = 100,
) -> bool:
    """Append a per-item event to discovery_job_items.result event_log.

    Keeps the existing result envelope if present and adds a `events` key.
    """
    if not item_id or not isinstance(event, dict):
        return False
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    row = cur.execute("SELECT result FROM discovery_job_items WHERE id=?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return False
    raw = row["result"]
    if raw:
        try:
            env = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            env = {}
    else:
        env = {}
    if not isinstance(env, dict):
        env = {}
    events = env.get("events") or []
    if not isinstance(events, list):
        events = []
    events.append(event)
    if len(events) > max_events:
        events = events[-max_events:]
    env["events"] = events
    cur.execute(
        "UPDATE discovery_job_items SET result=? WHERE id=?",
        (json.dumps(env, default=str), item_id),
    )
    conn.commit()
    conn.close()
    return True


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


def add_verify_job_items(job_id: str, contacts: list[dict]) -> int:
    """Add contacts to a verify-batch job as items to process."""
    from lf_db import get_db
    conn = get_db()
    cur = conn.cursor()
    count = 0
    for c in contacts:
        try:
            cur.execute(
                "INSERT OR IGNORE INTO discovery_job_items (job_id, company_id, company_name, place_id, contact_id, status) VALUES (?, ?, ?, ?, ?, 'pending')",
                (
                    job_id,
                    c.get("company_id", 0) or 0,
                    c.get("company_name", ""),
                    c.get("place_id", ""),
                    c.get("id", 0) or 0,
                ),
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


# ── Phase 2: LinkedIn plugin helpers (CONTRACTS.md section 3) ─────────────────
# These helpers back the Chrome extension inject pathway. They are intentionally
# separate from upsert_contact/patch_contact so the plugin never silently merges
# into the bulk import path. All writes are gated behind an explicit
# `matched_action` chosen by the user in the popup.


def get_contact_by_linkedin_slug(slug: str) -> Optional[dict]:
    """Look up a contact by its /in/<slug>. Returns the contact row or None."""
    if not slug:
        return None
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM contacts WHERE linkedin_slug=? LIMIT 1",
        (slug,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_companies_matching_name(name: str) -> list[dict]:
    """Return existing companies whose name matches the given string.

    Used as a first pass for fuzzy company matching. Caller still runs
    lf_name_match.fuzzy_company_match() to apply the Levenshtein<=3 filter.
    """
    if not name:
        return []
    conn = get_db()
    conn.row_factory = sqlite3.Row
    # Pull a candidate set: exact, case-insensitive, and substring matches.
    # Then fuzzy_company_match will narrow this down to within edit distance.
    like = f"%{name}%"
    rows = conn.execute(
        """
        SELECT * FROM companies
        WHERE name = ?
           OR LOWER(name) = LOWER(?)
           OR LOWER(name) LIKE LOWER(?)
        ORDER BY name
        LIMIT 200
        """,
        (name, name, like),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_contacts_by_normalized_name(normalized_name: str) -> list[dict]:
    """Return contacts whose full_name normalizes to the given value.

    The name is matched after applying normalize_name() so that
    'Katherine J. Smith, Jr.' matches 'katherine j smith'.
    """
    if not normalized_name:
        return []
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, company_id, full_name, first_name, last_name, title, "
        "linkedin_slug, linkedin_url, smtp_validation_status "
        "FROM contacts"
    ).fetchall()
    conn.close()
    from lf_name_match import normalize_name as _norm  # local import to avoid cycle
    out = []
    for r in rows:
        d = dict(r)
        if _norm(d.get("full_name") or "") == normalized_name:
            out.append(d)
    return out


def find_contact_by_validated_email(email: str) -> Optional[dict]:
    """Match priority 4 (CONTRACTS.md section 5): email match on a
    validated contact. Returns the contact row or None."""
    if not email:
        return None
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT * FROM contacts
        WHERE LOWER(email) = LOWER(?)
          AND smtp_validation_status = 'Okay to Send'
        LIMIT 1
        """,
        (email,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def insert_contact_experience(contact_id: int, entries: list[dict], linkedin_slug: str = "") -> int:
    """
    Insert one row per experience entry. Returns the number of rows written.
    Each entry: {company_name, title, started_at, ended_at, is_current}
    `is_current` should be truthy for the current role.
    """
    if not contact_id or not entries:
        return 0
    conn = get_db()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for e in entries:
        cur.execute("""
            INSERT INTO contact_experience
                (contact_id, company_name, title, started_at, ended_at,
                 is_current, linkedin_slug, source, captured_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'plugin', ?)
        """, (
            contact_id,
            (e.get("company_name") or e.get("company") or "")[:200],
            (e.get("title") or "")[:200],
            e.get("started_at") or "",
            e.get("ended_at") or "",
            1 if e.get("is_current") else 0,
            linkedin_slug or "",
            now,
        ))
        count += 1
    conn.commit()
    conn.close()
    return count


def replace_contact_experience(contact_id: int, entries: list[dict], linkedin_slug: str = "") -> int:
    """Delete then re-insert experience rows. Used on update_existing so the
    snapshot reflects the latest profile push. Returns rows written."""
    if not contact_id:
        return 0
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM contact_experience WHERE contact_id=?", (contact_id,))
    conn.commit()
    conn.close()
    return insert_contact_experience(contact_id, entries, linkedin_slug)


def insert_pending_push(
    linkedin_slug: str,
    raw_payload: dict,
    matched_action: str | None = None,
    matched_contact_id: int | None = None,
    matched_company_id: int | None = None,
    committed_by: str = "plugin",
) -> int:
    """Write an audit row to pending_pushes. Returns the new row id."""
    conn = get_db()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.execute("""
        INSERT INTO pending_pushes
            (linkedin_slug, raw_payload, matched_action,
             matched_contact_id, matched_company_id,
             committed_at, committed_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        linkedin_slug or "",
        json.dumps(raw_payload, ensure_ascii=False, default=str),
        matched_action,
        matched_contact_id,
        matched_company_id,
        now,  # written before commit so audit row is always present
        committed_by,
        now,
    ))
    conn.commit()
    push_id = cur.lastrowid
    conn.close()
    return push_id


def commit_pending_push(push_id: int, action: str, contact_id: int | None, company_id: int | None) -> bool:
    """Mark a pending_push row as fully committed (action + ids finalized)."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE pending_pushes
        SET matched_action=?, matched_contact_id=?, matched_company_id=?,
            committed_at=?
        WHERE id=?
    """, (
        action,
        contact_id,
        company_id,
        datetime.now(timezone.utc).isoformat(),
        push_id,
    ))
    conn.commit()
    affected = cur.rowcount
    conn.close()
    return affected > 0


def get_pending_pushes(limit: int = 100, from_ts: str | None = None, to_ts: str | None = None) -> list[dict]:
    """Audit query: recent plugin pushes, newest first."""
    conn = get_db()
    conn.row_factory = sqlite3.Row
    where = []
    params: list = []
    if from_ts:
        where.append("created_at >= ?")
        params.append(from_ts)
    if to_ts:
        where.append("created_at <= ?")
        params.append(to_ts)
    sql = "SELECT * FROM pending_pushes"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_location_resolution(raw_location: str, company_name: str) -> dict | None:
    """
    Fetch a cached region-to-city resolution for a (raw_location, company_name)
    pair. Returns None if not cached.
    """
    if not raw_location:
        return None
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM location_resolution_cache WHERE raw_location=? AND company_name=?",
        (raw_location, company_name or "")
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def upsert_location_resolution(
    raw_location: str,
    company_name: str,
    resolved_city: str | None,
    resolved_state: str | None,
    resolved_country: str | None,
    place_id: str | None,
    lat: float | None,
    lng: float | None,
    formatted_address: str | None,
    source: str = "google_places",
) -> bool:
    """Cache a region-to-city resolution. Overwrites any previous entry."""
    if not raw_location:
        return False
    conn = get_db()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.execute("""
        INSERT INTO location_resolution_cache
            (raw_location, company_name, resolved_city, resolved_state, resolved_country,
             place_id, lat, lng, formatted_address, source, cached_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(raw_location, company_name) DO UPDATE SET
            resolved_city=excluded.resolved_city,
            resolved_state=excluded.resolved_state,
            resolved_country=excluded.resolved_country,
            place_id=excluded.place_id,
            lat=excluded.lat,
            lng=excluded.lng,
            formatted_address=excluded.formatted_address,
            source=excluded.source,
            cached_at=excluded.cached_at
    """, (
        raw_location, company_name or "", resolved_city, resolved_state, resolved_country,
        place_id, lat, lng, formatted_address, source, now,
    ))
    conn.commit()
    conn.close()
    return True


def create_contact_manual(data: dict) -> int:
    """
    Create a contact without going through upsert_contact. Used by the plugin
    when the user picks 'new_company_new_contact' or 'new_contact_existing_company'
    and the user has full control over every field.

    Required: first_name, last_name (or full_name), company_id. Everything else
    is optional. Returns the new contact id, or -1 on validation failure.
    """
    first_name = (data.get("first_name") or "").strip()
    last_name = (data.get("last_name") or "").strip()
    full_name = (data.get("full_name") or f"{first_name} {last_name}").strip()
    if not full_name:
        return -1
    # If first/last were missing, derive from full_name.
    if not first_name or not last_name:
        parts = full_name.split(" ", 1)
        first_name = parts[0]
        last_name = parts[1] if len(parts) > 1 else ""

    company_id = data.get("company_id")
    if not company_id:
        return -1

    conn = get_db()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.execute("""
        INSERT INTO contacts (
            company_id, first_name, last_name, full_name,
            title, linkedin_url, linkedin_slug,
            email, phone, location, linkedin_location_raw,
            linkedin_resolved_city, linkedin_resolved_state, linkedin_resolved_country,
            linkedin_resolved_lat, linkedin_resolved_lng,
            linkedin_resolved_formatted_address, linkedin_resolved_place_id,
            linkedin_resolved_source, linkedin_resolved_at,
            is_local, hq_contact,
            confidence_score, data_provenance, found_at,
            source_primary, source_linkedin_verified, source_linkedin_unverified,
            title_from_linkedin, linkedin_snippet,
            pipeline_stage, smtp_validation_status,
            is_test
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        company_id,
        first_name,
        last_name,
        full_name,
        data.get("title") or data.get("current_title") or "",
        data.get("linkedin_url") or "",
        data.get("linkedin_slug") or "",
        data.get("email") or None,
        data.get("phone") or None,
        data.get("location") or None,
        data.get("linkedin_location_raw") or None,
        data.get("linkedin_resolved_city") or None,
        data.get("linkedin_resolved_state") or None,
        data.get("linkedin_resolved_country") or None,
        data.get("linkedin_resolved_lat") or None,
        data.get("linkedin_resolved_lng") or None,
        data.get("linkedin_resolved_formatted_address") or None,
        data.get("linkedin_resolved_place_id") or None,
        data.get("linkedin_resolved_source") or None,
        data.get("linkedin_resolved_at") or None,
        data.get("is_local", 1),
        data.get("hq_contact", 0),
        float(data.get("confidence_score", 1.0)),
        data.get("data_provenance", "plugin_pushed"),
        now,
        data.get("source_primary", "linkedin_plugin"),
        1,  # source_linkedin_verified (we are reading the live profile)
        0,
        data.get("title") or data.get("current_title") or "",
        "",
        "discovered",
        None,  # NULL = not_validated; Session 1 picks this up
        data.get("is_test", 0),
    ))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id
