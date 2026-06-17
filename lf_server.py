#!/usr/bin/env python3
"""
lf_server.py - Lead Finder Web Server
=====================================
Serves static HTML + REST API on port 8798.
Static files live in ./static/ (no more f-string HTML corruption).
"""

import os, sys, hmac, json, sqlite3, logging, traceback, asyncio
from pathlib import Path
from typing import Optional
from functools import wraps
from datetime import datetime, timezone
import requests  # for PixelRAG proxy endpoint (added 2026-06-14)

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ── Local imports ──────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

# ── Logging (uses BASE_DIR) ────────────────────────────────────────────────────
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / f"lf_server_{datetime.now(timezone.utc).strftime('%Y%m%d')}.log"
logging.basicConfig(
    filename=str(log_file),
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
logger = logging.getLogger("lf_server")
# Also log to stdout for systemd/journal
console = logging.StreamHandler()
console.setLevel(logging.INFO)
console.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logger.addHandler(console)

from lf_config import get
from lf_db import (
    get_db, get_all_sessions, get_session, get_session_companies,
    get_session_contacts, upsert_contact, upsert_company,
    soft_delete_contact, patch_contact, resume_session, delete_session,
    get_companies_for_session, bulk_soft_delete_companies,
    create_company_manual, bulk_soft_delete_contacts,
    init_db,  # added 2026-06-07: ensure schema is up-to-date on startup
)
from lf_search import search_companies as google_search, enrich_company_details
from lf_executives import discover_executives
from lf_export import export_contacts_csv, export_email_patterns_csv, export_company_email_patterns_csv

# ── Config ─────────────────────────────────────────────────────────────────────
API_KEY = get("lf_API_KEY", "lf_key_P9xZq3RvLm7YwT2hJm8FvNu4BcDEs6")
STATIC_DIR = BASE_DIR / "static"

app = FastAPI()

# ── Logging middleware ──────────────────────────────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    try:
        response = await call_next(request)
        if response.status_code >= 400:
            logger.warning(f"{request.method} {request.url.path} -> {response.status_code}")
        return response
    except Exception as e:
        logger.error(f"Unhandled error: {request.method} {request.url.path}: {e}\n{traceback.format_exc()}")
        raise

# Initialize DB schema (runs migrations on startup) — added 2026-06-07
try:
    init_db()
except Exception as e:
    print(f"[init] init_db failed: {e}", flush=True)

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)


# ── Auth ────────────────────────────────────────────────────────────────────────
def verify_key(x_lf_key: str = Header(None)):
    if not x_lf_key or not hmac.compare_digest(x_lf_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing X-LF-Key")


# ── Pydantic models ────────────────────────────────────────────────────────────
class SearchRequest(BaseModel):
    industry: str
    city: str
    state: str = "CA"
    radius_miles: int = 25
    max_results: int = 20
    extra_queries: list[str] = []
    session_key: Optional[str] = None
    # QC-15 quality filters (added 2026-06-09)
    min_rating: float = 3.0
    min_reviews: int = 0
    require_website_or_phone: bool = False
    min_quality_score: float = 0.0

class DiscoverExecutivesRequest(BaseModel):
    company_name: str = ""
    website: str = ""
    linkedin_url: str = ""
    search_city: str = ""
    search_state: str = "CA"
    search_radius_miles: int = 25


# PixelRAG ad-hoc visual API request models (updated 2026-06-15).
# The old /search endpoint required a pre-built FAISS index. The new API
# renders any URL on demand and optionally runs visual search against it.
class PixelRAGScreenshotRequest(BaseModel):
    url: str
    tile_height: int = 1568
    viewport_width: int = 1280
    quality: int = 85
    wait_seconds: float = 1.0


class PixelRAGExtractRequest(BaseModel):
    url: str
    query: str
    top_k: int = 5
    tile_height: int = 1568
    viewport_width: int = 1280
    quality: int = 85
    wait_seconds: float = 1.0


# ── Static file serving ────────────────────────────────────────────────────────
@app.get("/")
async def root():
    return RedirectResponse(url="/finder/", status_code=302)

@app.get("/finder/")
async def dashboard():
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/finder/sessions")
async def dashboard_sessions():
    return FileResponse(STATIC_DIR / "sessions.html")

@app.get("/finder/companies")
async def dashboard_companies():
    return FileResponse(STATIC_DIR / "companies.html")

@app.get("/finder/contacts")
async def dashboard_contacts():
    return FileResponse(STATIC_DIR / "contacts.html")

@app.get("/finder/audit")
async def dashboard_audit():
    return FileResponse(STATIC_DIR / "audit.html")

@app.get("/finder/emails")
async def dashboard_emails():
    """Email Validation dashboard page (added 2026-06-09)."""
    return FileResponse(STATIC_DIR / "emails.html")


# ── API Endpoints ──────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "lead-finder", "version": "2.0"}


@app.post("/api/search")
async def api_search(
    body: SearchRequest,
    _: str = Header(None, alias="X-LF-Key"),
):
    verify_key(_)
    result = google_search(
        industry=body.industry,
        city=body.city,
        state=body.state,
        radius_miles=body.radius_miles,
        max_results_per_query=body.max_results,
        extra_queries=body.extra_queries,
        min_rating=body.min_rating,
        min_reviews=body.min_reviews,
        require_website_or_phone=body.require_website_or_phone,
        min_quality_score=body.min_quality_score,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ── PixelRAG ad-hoc visual API (updated 2026-06-15) ─────────────────────────────
# The new PixelRAG container does NOT need a pre-built FAISS index.
# It renders any URL on demand via /screenshot and performs visual search
# via /extract using Qwen3-VL-Embedding-2B. We expose both endpoints here
# so callers can either just capture a page or capture + query it.
@app.post("/api/pixelrag/screenshot")
async def api_pixelrag_screenshot(
    body: PixelRAGScreenshotRequest,
    _: str = Header(None, alias="X-LF-Key"),
):
    verify_key(_)
    from lf_search_providers import _pixelrag_screenshot
    try:
        result = _pixelrag_screenshot(body.url, timeout=90)
        if not result:
            raise HTTPException(status_code=502, detail="PixelRAG /screenshot returned no result")
        return {
            "url": body.url,
            "provider": "pixelrag",
            "tile_count": result.get("tile_count", 0),
            "result": result,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PixelRAG /screenshot error: {e}")


@app.post("/api/pixelrag/extract")
async def api_pixelrag_extract(
    body: PixelRAGExtractRequest,
    _: str = Header(None, alias="X-LF-Key"),
):
    verify_key(_)
    from lf_search_providers import _pixelrag_extract
    try:
        matches = _pixelrag_extract(
            url=body.url,
            query=body.query,
            top_k=body.top_k,
            timeout=180,
        )
        return {
            "url": body.url,
            "query": body.query,
            "provider": "pixelrag",
            "match_count": len(matches),
            "matches": matches,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PixelRAG /extract error: {e}")


@app.get("/api/pixelrag/status")
async def api_pixelrag_status(
    _: str = Header(None, alias="X-LF-Key"),
):
    """Proxy to PixelRAG /status endpoint for dashboard health display."""
    verify_key(_)
    from lf_config import pixelrag_url
    try:
        resp = requests.get(f"{pixelrag_url()}/status", timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"PixelRAG unreachable: {e}")


@app.get("/api/company/{place_id}")
async def api_company_details(
    place_id: str,
    _: str = Header(None, alias="X-LF-Key"),
):
    verify_key(_)
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute("SELECT * FROM companies WHERE place_id=?", (place_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Company not found")
    company = dict(row)
    conn2 = get_db()
    contacts = [dict(r) for r in conn2.cursor().execute(
        "SELECT * FROM contacts WHERE company_id=?", (company["id"],)
    ).fetchall()]
    conn2.close()
    return {"company": company, "contacts": contacts}


@app.post("/api/company/{place_id}/enrich")
async def api_enrich_company(
    place_id: str,
    _: str = Header(None, alias="X-LF-Key"),
):
    verify_key(_)
    # Enrich company details via Google Places
    details = _enrich_company_details(place_id)
    if not details:
        raise HTTPException(status_code=500, detail="Failed to enrich company")
    website = details.get("websiteUri", "") or details.get("website", "")
    return {"status": "ok", "website": website, "place_id": place_id}


@app.post("/api/discover/{place_id}/executives")
async def api_discover_executives(
    place_id: str,
    body: Optional[dict] = None,
    _: str = Header(None, alias="X-LF-Key"),
):
    verify_key(_)
    try:
        conn = get_db()
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM companies WHERE place_id=?", (place_id,)).fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Company not found")
        company = dict(row)
        # Accept both Pydantic model body and raw dict (for sessions.html empty-body calls)
        if body is None: body = {}
        search_lat = company.get("lat", 0.0) or 0.0
        search_lng = company.get("lng", 0.0) or 0.0

        # Run AI-first discover_executives in a thread with a 60s deadline.
        # The new chain is AI-first (primary) with SearXNG + website scrape
        # as fallbacks. PixelRAG is opt-in for visual scraping of LinkedIn
        # profiles when AI data is stale.
        import concurrent.futures
        from lf_executives import discover_executives_ai_first
        deadline_s = 120
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(
                discover_executives_ai_first,
                company_name=body.get("company_name") or company.get("name", ""),
                company_id=company["id"],
                website=body.get("website") or company.get("website", ""),
                linkedin_url=body.get("linkedin_url") or "",
                industry=company.get("search_query") or company.get("business_type", ""),
                search_city=body.get("search_city") or company.get("city", ""),
                search_state=body.get("search_state", "CA"),
                search_lat=float(search_lat),
                search_lng=float(search_lng),
                radius_miles=body.get("search_radius_miles", 25),
                enable_pixelrag_scrape=True,
            )
            try:
                contacts = future.result(timeout=deadline_s)
            except concurrent.futures.TimeoutError:
                contacts = []
                logger.warning(f"discover_executives_ai_first for {company.get('name')} hit {deadline_s}s deadline; returning empty")
                return {
                    "status": "timeout",
                    "contacts": [],
                    "count": 0,
                    "message": f"Discovery exceeded {deadline_s}s deadline; partial results unavailable",
                }
        return {"status": "completed", "contacts": contacts, "count": len(contacts)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"discover_executives error for place_id={place_id}: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Discovery error: {str(e)}")


@app.get("/api/sessions")
async def api_sessions(_: str = Header(None, alias="X-LF-Key")):
    verify_key(_)
    return get_all_sessions()


@app.get("/api/session/{session_key}/results")
async def api_session_results(
    session_key: str,
    _: str = Header(None, alias="X-LF-Key"),
):
    verify_key(_)
    session = get_session(session_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    companies = get_session_companies(session_key)
    contacts = get_session_contacts(session_key)
    return {"session": session, "companies": companies, "contacts": contacts}


@app.post("/api/session/{session_key}/resume")
async def api_session_resume(
    session_key: str,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Re-activate a session (set status='active') and return its companies.

    Returns a `redirect_url` field the frontend can use to navigate to the
    search page with the session loaded, fixing the 'Resume search doesn't
    work' issue (Issue #4).
    """
    verify_key(_)
    session = get_session(session_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    success = resume_session(session_key)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to resume session")

    companies = get_companies_for_session(session_key)
    return {
        "status": "ok",
        "message": f"Session {session_key} resumed",
        "session": session,
        "companies": companies,
        "company_count": len(companies),
        # Frontend navigates to this URL after resume to load the session.
        # Must use /finder/ path — the root / redirects to /finder/ which
        # strips query parameters (fixes View/Resume buttons Issue #4).
        "redirect_url": f"/finder/?session={session_key}",
    }


@app.delete("/api/session/{session_key}")
async def api_session_delete(
    session_key: str,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Permanently delete a session and its search_results.

    Companies and contacts are NOT deleted — they may belong to other sessions.
    """
    verify_key(_)
    session = get_session(session_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    result = delete_session(session_key)
    return {
        "status": "ok",
        "message": f"Session {session_key} deleted",
        **result,
    }


@app.post("/api/companies")
async def api_create_company(
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Create a company manually. Auto-discovers executives after creation."""
    verify_key(_)
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Company name is required")
    company_id = create_company_manual(body)
    return {
        "status": "ok",
        "message": f"Company '{name}' created (id={company_id})",
        "company_id": company_id,
    }


@app.post("/api/companies/bulk-delete")
async def api_bulk_delete_companies(
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Soft-delete multiple companies by ID list."""
    verify_key(_)
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="No company IDs provided")
    # Convert to ints safely
    try:
        int_ids = [int(i) for i in ids]
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid company ID format")
    affected = bulk_soft_delete_companies(int_ids)
    return {
        "status": "ok",
        "message": f"Deleted {affected} companies",
        "deleted_count": affected,
    }


@app.post("/api/companies/bulk-discover")
async def api_bulk_discover_executives(
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Run executive discovery for multiple companies concurrently."""
    verify_key(_)
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="No company IDs provided")
    try:
        int_ids = [int(i) for i in ids]
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid company ID format")

    import concurrent.futures
    from lf_executives import discover_executives_ai_first

    # Fetch all companies
    conn = get_db()
    cur = conn.cursor()
    placeholders = ",".join("?" * len(int_ids))
    rows = cur.execute(f"SELECT * FROM companies WHERE id IN ({placeholders})", int_ids).fetchall()
    conn.close()

    results = {"found": 0, "errors": 0, "details": []}

    def _discover_one(company):
        try:
            search_lat = float(company.get("lat", 0.0) or 0.0)
            search_lng = float(company.get("lng", 0.0) or 0.0)
            discover_executives_ai_first(
                company_name=company.get("name", ""),
                company_id=company["id"],
                website=company.get("website", ""),
                linkedin_url="",
                industry=company.get("search_query") or company.get("business_type", ""),
                search_city=company.get("city", ""),
                search_state=company.get("state", "CA"),
                search_lat=search_lat,
                search_lng=search_lng,
                radius_miles=25,
            )
            return (company["id"], company.get("name", ""), True, 0)
        except Exception as e:
            return (company["id"], company.get("name", ""), False, str(e))

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(_discover_one, dict(r)): r for r in rows}
        for f in concurrent.futures.as_completed(futures):
            cid, name, ok, err = f.result()
            if ok:
                results["found"] += 1
            else:
                results["errors"] += 1
                results["details"].append({"id": cid, "name": name, "error": err})

    return {
        "status": "ok",
        "message": f"Discovered executives for {results['found']}/{len(int_ids)} companies ({results['errors']} errors)",
        **results,
    }


@app.get("/api/companies")
async def api_companies(
    industry: str = Query("", description="Filter by business type (partial match)"),
    city: str = Query("", description="Filter by city (partial match)"),
    state: str = Query("", description="Filter by state (exact match)"),
    limit: int = Query(100, description="Max results to return"),
    include_deleted: bool = Query(False, description="Include soft-deleted companies (default: False)"),
    min_quality: float = Query(0.0, description="Minimum quality_score (0-1)"),
    quality_grade: str = Query("", description="Filter by quality grade (A/B/C/D/F)"),
    _: str = Header(None, alias="X-LF-Key"),
):
    verify_key(_)
    conn = get_db()
    cur = conn.cursor()
    where_clauses = [
        "(:industry = '' OR c.business_type LIKE '%' || :industry || '%')",
        "(:city = '' OR c.city LIKE '%' || :city || '%')",
        "(:state = '' OR c.state = :state)",
        "(:min_quality = 0 OR c.quality_score >= :min_quality)",
        "(:quality_grade = '' OR c.quality_grade = :quality_grade)",
    ]
    if not include_deleted:
        where_clauses.append("(c.is_deleted IS NULL OR c.is_deleted = 0)")
    where_sql = " AND ".join(where_clauses)
    query = f"""
        SELECT c.id, c.name, c.city, c.state, c.street, c.postal_code, c.website,
               c.business_type, c.phone, c.rating, c.confidence_score,
               c.hq_location, c.is_local_contact, c.data_provenance,
               c.lat, c.lng, c.place_id, c.email_pattern, c.email_pattern_confidence,
               c.email_pattern_source,
               c.canonical_business_type, c.business_type_confidence,
               c.ai_sanity_status, c.ai_sanity_notes,
               c.quality_score, c.quality_grade,
               (c.is_deleted IS NULL OR c.is_deleted = 0) as is_active,
               COUNT(ct.id) as contact_count
        FROM companies c
        LEFT JOIN contacts ct ON ct.company_id = c.id AND (ct.is_deleted IS NULL OR ct.is_deleted = 0)
        WHERE {where_sql}
        GROUP BY c.id
        ORDER BY c.quality_score DESC, c.name
        LIMIT :limit
    """
    rows = cur.execute(query, {
        "industry": industry, "city": city, "state": state,
        "min_quality": min_quality, "quality_grade": quality_grade,
        "limit": min(limit, 500),
    }).fetchall()
    results = [dict(r) for r in rows]
    conn.close()
    return {"companies": results, "total": len(results), "include_deleted": include_deleted}


@app.get("/api/contacts")
async def api_contacts(
    company_name: str = Query("", description="Filter by company name (partial match)"),
    title: str = Query("", description="Filter by job title (partial match)"),
    city: str = Query("", description="Filter by company city (partial match)"),
    min_confidence: float = Query(0.0, description="Minimum confidence score"),
    sort: str = Query("confidence", description="Sort order: 'confidence' (default) or 'recent'"),
    limit: int = Query(200, description="Max results to return"),
    derive_emails: bool = Query(False, description="Auto-derive missing emails from company patterns"),
    _: str = Header(None, alias="X-LF-Key"),
):
    verify_key(_)
    conn = get_db()
    cur = conn.cursor()
    # Issue #6: 'recent' sort orders by found_at DESC; 'confidence' (default)
    # is the legacy ordering.
    if sort == "recent":
        order_clause = "ORDER BY ct.found_at DESC NULLS LAST, ct.confidence_score DESC"
    else:
        order_clause = "ORDER BY ct.confidence_score DESC"
    rows = cur.execute(f"""
        SELECT ct.id, ct.company_id, ct.full_name, ct.title, ct.linkedin_url, ct.confidence_score,
               ct.is_local, ct.hq_contact, ct.data_provenance, ct.email, ct.is_derived_email,
               ct.is_manually_edited, ct.is_deleted,
               ct.ai_verified_title, ct.ai_title_confidence, ct.ai_title_source,
               ct.found_at,
               c.name as company_name, c.city as company_city, c.state as company_state,
               c.website as company_website, c.email_pattern as company_email_pattern,
               c.email_pattern_confidence as company_email_confidence
        FROM contacts ct
        JOIN companies c ON c.id = ct.company_id
        WHERE (:company_name = '' OR c.name LIKE '%' || :company_name || '%')
          AND (:title = '' OR ct.title LIKE '%' || :title || '%')
          AND (:city = '' OR c.city LIKE '%' || :city || '%')
          AND ct.confidence_score >= :min_confidence
          AND (ct.is_deleted IS NULL OR ct.is_deleted = 0)
        {order_clause}
        LIMIT :limit
    """, {"company_name": company_name, "title": title,
          "city": city, "min_confidence": min_confidence,
          "limit": min(limit, 500)}).fetchall()
    results = [dict(r) for r in rows]
    conn.close()

    # Auto-derive missing emails from company patterns if requested (added 2026-06-16)
    derived_count = 0
    if derive_emails:
        from lf_email_patterns import derive_email
        # Group missing emails by company pattern
        missing_by_company: dict[int, list[tuple[int, str, str]]] = {}
        for ct in results:
            if ct.get("email"):
                continue
            company_id = ct.get("company_id")
            pattern = ct.get("company_email_pattern")
            if not company_id or not pattern:
                continue
            if not ct.get("first_name") and not ct.get("last_name"):
                # Try to parse full_name if first/last are missing
                full = (ct.get("full_name") or "").strip()
                if full:
                    parts = full.split()
                    if len(parts) >= 2:
                        ct["first_name"] = parts[0]
                        ct["last_name"] = " ".join(parts[1:])
            first = ct.get("first_name") or ""
            last = ct.get("last_name") or ""
            if not first or not last:
                continue
            missing_by_company.setdefault(company_id, []).append((ct["id"], first, last))

        if missing_by_company:
            conn = get_db()
            cur = conn.cursor()
            for company_id, contacts in missing_by_company.items():
                row = cur.execute("SELECT email_pattern FROM companies WHERE id=?", (company_id,)).fetchone()
                if not row or not row[0]:
                    continue
                pattern = row[0]
                for contact_id, first, last in contacts:
                    email = derive_email(first, last, pattern)
                    if email:
                        cur.execute("UPDATE contacts SET email=?, is_derived_email=1 WHERE id=?",
                                    (email, contact_id))
                        derived_count += 1
            conn.commit()
            conn.close()
            # Refresh results to include newly derived emails
            conn = get_db()
            cur = conn.cursor()
            ids = [str(ct["id"]) for ct in results]
            if ids:
                email_map = {}
                id_list = ",".join(ids)
                for r in cur.execute(f"SELECT id, email, is_derived_email FROM contacts WHERE id IN ({id_list})"):
                    email_map[r["id"]] = (r["email"], r["is_derived_email"])
                for ct in results:
                    em, is_der = email_map.get(ct["id"], (None, 0))
                    if em:
                        ct["email"] = em
                        ct["is_derived_email"] = is_der
            conn.close()

    return {"contacts": results, "total": len(results), "derived_count": derived_count}


@app.get("/api/companies/dropdowns")
async def api_companies_dropdowns(_: str = Header(None, alias="X-LF-Key")):
    """Return distinct values for companies filter dropdowns."""
    verify_key(_)
    conn = get_db()
    cur = conn.cursor()
    industries = [r[0] for r in cur.execute(
        "SELECT DISTINCT business_type FROM companies WHERE business_type != '' AND (is_deleted IS NULL OR is_deleted = 0) ORDER BY business_type"
    ).fetchall()]
    cities = [r[0] for r in cur.execute(
        "SELECT DISTINCT city FROM companies WHERE city != '' AND (is_deleted IS NULL OR is_deleted = 0) ORDER BY city"
    ).fetchall()]
    states = [r[0] for r in cur.execute(
        "SELECT DISTINCT state FROM companies WHERE state != '' AND (is_deleted IS NULL OR is_deleted = 0) ORDER BY state"
    ).fetchall()]
    conn.close()
    return {"industries": industries, "cities": cities, "states": states}


@app.get("/api/contacts/dropdowns")
async def api_contacts_dropdowns(_: str = Header(None, alias="X-LF-Key")):
    """Return distinct values for contacts filter dropdowns."""
    verify_key(_)
    conn = get_db()
    cur = conn.cursor()
    companies = [r[0] for r in cur.execute(
        "SELECT DISTINCT c.name FROM companies c JOIN contacts ct ON ct.company_id = c.id "
        "WHERE (ct.is_deleted IS NULL OR ct.is_deleted = 0) AND c.name != '' ORDER BY c.name"
    ).fetchall()]
    titles = [r[0] for r in cur.execute(
        "SELECT DISTINCT title FROM contacts WHERE title != '' AND (is_deleted IS NULL OR is_deleted = 0) ORDER BY title"
    ).fetchall()]
    cities = [r[0] for r in cur.execute(
        "SELECT DISTINCT c.city FROM companies c JOIN contacts ct ON ct.company_id = c.id "
        "WHERE (ct.is_deleted IS NULL OR ct.is_deleted = 0) AND c.city != '' ORDER BY c.city"
    ).fetchall()]
    conn.close()
    return {"companies": companies, "titles": titles, "cities": cities}


@app.post("/api/contact/{contact_id}/derive-email")
async def api_derive_contact_email(
    contact_id: int,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Derive email for a single contact using its company's email pattern."""
    verify_key(_)
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    row = cur.execute("""
        SELECT ct.id, ct.full_name, ct.email,
               c.email_pattern, c.website
        FROM contacts ct
        JOIN companies c ON c.id = ct.company_id
        WHERE ct.id=?
    """, (contact_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Contact not found")
    ct = dict(row)
    if not ct.get("email_pattern"):
        conn.close()
        try:
            from lf_ai_enrich import ai_infer_email_pattern
            r = ai_infer_email_pattern(
                company_name="",
                domain="",
                contact_first=ct.get("full_name", ""),
                contact_last="",
                industry="",
            )
            pattern = r.get("pattern") if r else None
            if not pattern:
                return {"status": "failed", "reason": "No pattern available - try discovering one first"}
        except Exception as e:
            return {"status": "failed", "reason": f"AI error: {str(e)}"}
    else:
        pattern = ct["email_pattern"]

    # Split full_name into first/last
    full_name = ct.get("full_name", "")
    name_parts = full_name.strip().split(None, 1)
    first = name_parts[0].lower().strip(".,!?;:") if name_parts else ""
    last = name_parts[1].lower().strip(".,!?;:") if len(name_parts) > 1 else ""
    domain = ""
    # Try extracting domain from website first
    if ct.get("website"):
        from lf_executives import extract_domain
        domain = extract_domain(ct["website"])
    # Fallback: extract domain from pattern string (e.g. "{first}.{last}@boeing.com" → "boeing.com")
    if not domain and pattern and "@" in pattern:
        domain = pattern.split("@")[-1].strip()
        # Remove any pattern tokens (e.g. "{first}", "{last}") from domain
        import re
        domain = re.sub(r'\{[^}]+\}', '', domain).strip()
        domain = re.sub(r'[^a-zA-Z0-9.-]', '', domain).strip()
    if not domain:
        return {"status": "failed", "reason": "No domain available for this contact's company"}

    try:
        from lf_email_patterns import derive_email
        email = derive_email(first, last, pattern)
        if not email:
            conn.close()
            return {"status": "failed", "reason": f"Could not render pattern '{pattern}' with name '{first} {last}'"}
        full_email = f"{email}@{domain}"
        cur.execute("UPDATE contacts SET email=?, is_derived_email=1 WHERE id=?", (full_email, contact_id))
        conn.commit()
        conn.close()
        return {"status": "ok", "email": full_email, "pattern": pattern, "domain": domain}
    except Exception as e:
        conn.close()
        return {"status": "failed", "reason": f"Render error: {str(e)}"}


@app.delete("/api/contact/{contact_id}")
async def api_delete_contact(
    contact_id: int,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Soft-delete a contact (marks is_deleted=1)."""
    verify_key(_)
    success = soft_delete_contact(contact_id)
    if not success:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"status": "ok", "message": f"Contact {contact_id} soft-deleted"}


@app.patch("/api/contact/{contact_id}")
async def api_patch_contact(
    contact_id: int,
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Update specific fields on a contact (title, manual_notes, name fields).
    Sets is_manually_edited=1 automatically.
    """
    verify_key(_)
    success = patch_contact(contact_id, body)
    if not success:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"status": "ok", "message": f"Contact {contact_id} updated"}


@app.post("/api/contacts/bulk-delete")
async def api_bulk_delete_contacts(
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Soft-delete multiple contacts by ID list."""
    verify_key(_)
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="No contact IDs provided")
    try:
        int_ids = [int(i) for i in ids]
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid contact ID format")
    affected = bulk_soft_delete_contacts(int_ids)
    return {
        "status": "ok",
        "message": f"Deleted {affected} contacts",
        "deleted_count": affected,
    }


@app.post("/api/contacts/bulk-validate-emails")
async def api_bulk_validate_contacts_emails(
    body: dict,
    background_tasks: BackgroundTasks,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Bulk-validate emails for specific contact IDs. Runs in background."""
    verify_key(_)
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="No contact IDs provided")
    try:
        int_ids = [int(i) for i in ids]
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid contact ID format")

    # Fetch contacts with emails
    conn = get_db()
    cur = conn.cursor()
    placeholders = ",".join("?" * len(int_ids))
    rows = cur.execute(
        f"SELECT id, email FROM contacts WHERE id IN ({placeholders}) AND email IS NOT NULL AND email != '' AND (is_deleted IS NULL OR is_deleted=0)",
        int_ids,
    ).fetchall()
    conn.close()

    contacts_to_validate = [dict(r) for r in rows]
    if not contacts_to_validate:
        return {"status": "no_emails", "message": "No contacts with emails found in selection"}

    import uuid as _uuid
    job_id = f"val_{_uuid.uuid4().hex[:12]}"
    create_validation_job(job_id, total=len(contacts_to_validate))

    async def _run_validation():
        from lf_email_validator import check_email
        from lf_db import get_db as _get_db
        try:
            update_validation_job(job_id, status="running",
                                  started_at=datetime.now(timezone.utc).isoformat())
            for i, ct in enumerate(contacts_to_validate):
                email = ct["email"].strip()
                result = check_email(email).to_dict()
                try:
                    set_cached_validation(result)
                except Exception:
                    pass
                ready = 1 if result["status"] == "Okay to Send" else 0
                rejected_reason = result.get("analysis", "") if result["status"] == "Do Not Send" else None
                try:
                    _conn = _get_db()
                    _cur = _conn.cursor()
                    _cur.execute(
                        "UPDATE contacts SET smtp_validation_status=?, smtp_validated_at=?, "
                        "smtp_validation_code=?, email_ready_for_export=?, email_rejected_reason=? "
                        "WHERE id=?",
                        (result["status"], result["validated_at"], result["smtp_code"],
                         ready, rejected_reason, ct["id"]),
                    )
                    _conn.commit()
                    _conn.close()
                except Exception:
                    pass
                update_validation_job(job_id, done=i + 1)
            update_validation_job(job_id, status="completed",
                                  finished_at=datetime.now(timezone.utc).isoformat())
        except Exception as e:
            logger.error(f"Contact validation job {job_id} failed: {e}")
            update_validation_job(job_id, status="failed")

    background_tasks.add_task(_run_validation)
    return {
        "job_id": job_id,
        "total": len(contacts_to_validate),
        "message": f"Validating {len(contacts_to_validate)} emails in background",
    }


@app.post("/api/contact/{contact_id}/validate-email")
async def api_validate_single_contact_email(
    contact_id: int,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Validate a single contact's email via SMTP probe."""
    verify_key(_)
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, email FROM contacts WHERE id=? AND (is_deleted IS NULL OR is_deleted=0)", (contact_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Contact not found")
    email = (row["email"] or "").strip()
    if not email:
        return {"status": "no_email", "message": "Contact has no email"}

    from lf_email_validator import check_email
    result = check_email(email).to_dict()
    try:
        set_cached_validation(result)
    except Exception:
        pass
    ready = 1 if result["status"] == "Okay to Send" else 0
    rejected_reason = result.get("analysis", "") if result["status"] == "Do Not Send" else None
    try:
        _conn = get_db()
        _cur = _conn.cursor()
        _cur.execute(
            "UPDATE contacts SET smtp_validation_status=?, smtp_validated_at=?, "
            "smtp_validation_code=?, email_ready_for_export=?, email_rejected_reason=? "
            "WHERE id=?",
            (result["status"], result["validated_at"], result["smtp_code"],
             ready, rejected_reason, contact_id),
        )
        _conn.commit()
        _conn.close()
    except Exception:
        pass
    return {
        "status": "ok",
        "email": email,
        "validation": result["status"],
        "smtp_code": result.get("smtp_code"),
    }


@app.post("/api/contact/{contact_id}/verify-title")
async def api_verify_contact_title(
    contact_id: int,
    _: str = Header(None, alias="X-LF-Key"),
):
    """AI-verify and update ALL fields of a contact at their company. (QC-16)

    AI researches title, LinkedIn URL, email, phone, location, and
    employment status. SearXNG is only used as a fallback for gaps.
    """
    verify_key(_)
    try:
        from lf_ai_enrich import ai_research_contact
        from lf_db import patch_contact
        conn = get_db()
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT ct.id, ct.full_name, ct.title, ct.linkedin_url, ct.email,
                   c.name as company_name, c.website as company_website,
                   c.email_pattern as company_email_pattern
            FROM contacts ct
            JOIN companies c ON c.id = ct.company_id
            WHERE ct.id=?
        """, (contact_id,)).fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Contact not found")
        ct = dict(row)

        # AI PRIMARY: research everything
        ai_result = ai_research_contact(
            full_name=ct["full_name"],
            company_name=ct["company_name"],
            linkedin_url=ct.get("linkedin_url", ""),
            current_title=ct.get("title", ""),
        )

        if not ai_result:
            return {"status": "failed", "reason": "AI unavailable"}

        # Build update dict — only update fields where AI has new/better info
        update = {
            "is_manually_edited": 1,
            "ai_title_confidence": ai_result.get("confidence", 0),
            "ai_title_source": ai_result.get("_source_method") or ai_result.get("source"),
        }
        if ai_result.get("title"):
            update["title"] = ai_result["title"]
            update["title_from_linkedin"] = ai_result["title"]
            update["ai_verified_title"] = ai_result["title"]
        if ai_result.get("linkedin_url"):
            update["linkedin_url"] = ai_result["linkedin_url"]
        if ai_result.get("email"):
            update["email"] = ai_result["email"]
            update["is_derived_email"] = 1
        if ai_result.get("phone"):
            update["phone"] = ai_result["phone"]

        if not ai_result.get("title"):
            return {
                "status": "failed",
                "reason": ai_result.get("reasoning", "AI could not verify any fields for this contact"),
                "ai_result": ai_result,
            }

        patch_contact(contact_id, update)
        logger.info(f"verify-title: contact {contact_id} {ct['full_name']}: title {ct.get('title')} -> {ai_result.get('title')}")
        return {
            "status": "ok",
            "title": ai_result.get("title"),
            "previous_title": ct.get("title"),
            "linkedin_url": ai_result.get("linkedin_url"),
            "email": ai_result.get("email"),
            "phone": ai_result.get("phone"),
            "location": ai_result.get("location"),
            "is_current_employee": ai_result.get("is_current_employee"),
            "confidence": ai_result.get("confidence"),
            "reasoning": ai_result.get("reasoning", ""),
            "source": ai_result.get("source", ""),
            "is_verified": ai_result.get("is_verified"),
            "updated_fields": list(update.keys()),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"verify-title error: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/company/{company_id}/contact")
async def api_create_company_contact(
    company_id: int,
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Manually create a contact for a company. (QC-16 gap-fill)"""
    verify_key(_)
    full_name = (body.get("full_name") or "").strip()
    title = (body.get("title") or "").strip()
    linkedin_url = (body.get("linkedin_url") or "").strip()

    if not full_name:
        raise HTTPException(status_code=400, detail="full_name is required")
    if not title:
        raise HTTPException(status_code=400, detail="title is required")

    from lf_db import upsert_contact
    name_parts = full_name.split(" ", 1)
    contact = {
        "first_name": name_parts[0] if name_parts else full_name,
        "last_name": name_parts[1] if len(name_parts) > 1 else "",
        "full_name": full_name,
        "title": title,
        "linkedin_url": linkedin_url,
        "is_local": 1,
        "hq_contact": 0,
        "confidence_score": 0.5,
        "data_provenance": "MANUAL_EDIT",
        "source_primary": "manual",
        "is_manually_edited": 1,
        "manual_notes": body.get("notes", f"Manually added via UI on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"),
    }
    cid = upsert_contact(company_id, contact)
    if cid <= 0:
        raise HTTPException(status_code=409, detail="Contact could not be created (possible duplicate)")
    logger.info(f"create-contact: company {company_id}, contact {full_name} ({title})")
    return {"status": "ok", "contact_id": cid, "full_name": full_name, "title": title}


# ── Discovery Job Endpoints (QC-17, 2026-06-09) ─────────────────────────────

@app.post("/api/discover-batch")
async def api_discover_batch(
    body: dict,
    background_tasks: BackgroundTasks,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Start a background discovery job for a session.
    Creates a job record and starts processing companies in background.
    The job continues even if the page is closed/refreshed.
    Returns {job_id} immediately. Poll GET /api/discover-job/{job_id}.
    """
    verify_key(_)
    session_key = (body.get("session_key") or "").strip()
    if not session_key:
        raise HTTPException(status_code=400, detail="session_key is required")

    from lf_db import (
        get_session, get_session_companies,
        create_discovery_job, add_discovery_job_items, update_discovery_job,
        get_next_discovery_pending, update_discovery_job_item,
    )

    session = get_session(session_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    companies = get_session_companies(session_key)
    if not companies:
        raise HTTPException(status_code=400, detail="No companies in this session")

    import uuid
    job_id = f"disc_{uuid.uuid4().hex[:12]}"
    create_discovery_job(job_id, session_key, total=len(companies))
    add_discovery_job_items(job_id, companies)

    async def _run_discovery():
        """Background worker — processes items one by one.
        Issue #2/3: now uses the AI-first enrichment hierarchy and writes
        stage progress to discovery_jobs.current_stage / stage_log so
        the frontend can show what's happening.
        """
        from lf_executives import discover_executives_ai_first
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
                    # Fetch company record
                    conn = get_db()
                    cur = conn.cursor()
                    row = cur.execute(
                        "SELECT * FROM companies WHERE id=?", (company_id,)
                    ).fetchone()
                    conn.close()
                    if not row:
                        result = {"status": "error", "message": "Company not found"}
                        update_discovery_job_item(item["id"], "failed", json.dumps(result))
                        failed += 1
                    else:
                        company = dict(row)
                        update_discovery_job(job_id, current_stage=f"discovering:{company_name}")
                        # AI-first chain: AI searches for executives, AI
                        # researches each, PixelRAG if AI data is stale,
                        # SearXNG + website scrape as fallbacks.
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
                    logger.error(f"discovery-batch item error: {e}")
                update_discovery_job(job_id, done=done, failed=failed)
            finish_ts = datetime.now(timezone.utc).isoformat()
            update_discovery_job(
                job_id, status="completed" if failed == 0 else "completed_with_errors",
                done=done, failed=failed, finished_at=finish_ts,
                current_stage="complete",
            )
            logger.info(f"discovery-batch {job_id}: {done} done, {failed} failed")
        except Exception as e:
            logger.error(f"discovery-batch run error: {e}")
            update_discovery_job(job_id, status="failed", done=done, failed=failed,
                                 current_stage="failed")

    background_tasks.add_task(_run_discovery)
    logger.info(f"discovery-batch {job_id}: {len(companies)} items, session={session_key}")
    return {"job_id": job_id, "total": len(companies), "session_key": session_key}


@app.get("/api/discover-job/{job_id}")
async def api_get_discovery_job(
    job_id: str,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Get the current status of a discovery job."""
    verify_key(_)
    from lf_db import get_discovery_job, get_discovery_job_items
    job = get_discovery_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    items = get_discovery_job_items(job_id)
    job["items"] = items
    return job


@app.get("/api/discover-jobs")
async def api_list_discovery_jobs(
    session_key: str = Query("", description="Filter by session key"),
    _: str = Header(None, alias="X-LF-Key"),
):
    """List recent discovery jobs."""
    verify_key(_)
    from lf_db import get_active_discovery_jobs
    if not session_key:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM discovery_jobs ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        conn.close()
        return {"jobs": [dict(r) for r in rows]}
    return {"jobs": get_active_discovery_jobs(session_key)}


@app.get("/api/stats")
async def api_stats(_: str = Header(None, alias="X-LF-Key")):
    verify_key(_)
    try:
        conn = get_db()
        cur = conn.cursor()
        companies = cur.execute("SELECT COUNT(*) FROM companies WHERE is_deleted IS NULL OR is_deleted = 0").fetchone()[0]
        contacts = cur.execute("SELECT COUNT(*) FROM contacts WHERE is_deleted IS NULL OR is_deleted = 0").fetchone()[0]
        sessions = cur.execute("SELECT COUNT(*) FROM search_sessions").fetchone()[0]
        recent = [dict(r) for r in cur.execute(
            "SELECT session_key, industry, city, state, created_at FROM search_sessions ORDER BY created_at DESC LIMIT 10"
        ).fetchall()]
        top_companies = [dict(r) for r in cur.execute(
            "SELECT c.name, c.city, c.state, COUNT(ct.id) as contact_count "
            "FROM companies c LEFT JOIN contacts ct ON ct.company_id = c.id AND (ct.is_deleted IS NULL OR ct.is_deleted = 0) "
            "GROUP BY c.id ORDER BY contact_count DESC LIMIT 10"
        ).fetchall()]
        conn.close()
        return {
            "companies": companies, "contacts": contacts, "sessions": sessions,
            "recent_sessions": recent, "top_companies": top_companies,
        }
    except Exception as e:
        logger.error(f"api_stats error: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail="Stats error")


@app.post("/api/backfill-quality-scores")
async def api_backfill_quality_scores(
    limit: int = Query(500, description="Max companies to score"),
    hide_below: float = Query(0.0, description="Soft-delete companies with score below this threshold (0=don't hide)"),
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Backfill quality scores for existing companies (QC-15).
    Computes quality_score, quality_grade (A-F), and quality_signals (JSON).
    Optionally soft-deletes companies below a threshold.
    """
    verify_key(_)
    try:
        from lf_search import compute_quality_score
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = [dict(r) for r in cur.execute(
            "SELECT id, name, rating, user_rating_count, website, phone, business_type "
            "FROM companies WHERE (is_deleted IS NULL OR is_deleted = 0) "
            "LIMIT ?", (limit,)
        ).fetchall()]
        scored = 0
        hidden = 0
        grade_counts = {"A": 0, "B": 0, "C": 0, "D": 0, "F": 0}
        for c in rows:
            qs = compute_quality_score(
                rating=c.get("rating"),
                user_rating_count=c.get("user_rating_count"),
                website=c.get("website", ""),
                phone=c.get("phone", ""),
                business_type=c.get("business_type", ""),
            )
            # Grade: A=0.8+, B=0.6+, C=0.4+, D=0.2+, F=<0.2
            if qs >= 0.8: grade = "A"
            elif qs >= 0.6: grade = "B"
            elif qs >= 0.4: grade = "C"
            elif qs >= 0.2: grade = "D"
            else: grade = "F"
            grade_counts[grade] += 1
            signals = json.dumps({
                "rating": c.get("rating"),
                "reviews": c.get("user_rating_count"),
                "has_website": bool(c.get("website")),
                "has_phone": bool(c.get("phone")),
                "business_type": c.get("business_type"),
            })
            # Optionally soft-delete below threshold
            if hide_below > 0 and qs < hide_below:
                cur.execute("UPDATE companies SET is_deleted=1 WHERE id=?", (c["id"],))
                hidden += 1
            cur.execute(
                "UPDATE companies SET quality_score=?, quality_grade=?, quality_signals=? WHERE id=?",
                (qs, grade, signals, c["id"]),
            )
            scored += 1
        conn.commit()
        conn.close()
        return {
            "scored": scored,
            "hidden": hidden,
            "grade_counts": grade_counts,
            "limit": limit,
            "hide_below": hide_below,
        }
    except Exception as e:
        logger.error(f"backfill-quality-scores error: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ai/usage")
async def api_ai_usage(_: str = Header(None, alias="X-LF-Key")):
    """Return AI enrichment usage statistics (added 2026-06-06)."""
    verify_key(_)
    try:
        from lf_ai_enrich import get_ai_usage_summary, cloud_model_chain, ai_enabled, ai_sanity_check_enabled
        return {
            "enabled": ai_enabled(),
            "sanity_check_enabled": ai_sanity_check_enabled(),
            "model_chain": cloud_model_chain(),
            "deep_research_model": "deepseek-v4-pro:cloud",
            "usage": get_ai_usage_summary(),
        }
    except Exception as e:
        return {"enabled": False, "error": str(e), "usage": {}}


@app.post("/api/company/{company_id}/ai-discover")
async def api_company_ai_discover(
    company_id: int,
    body: dict | None = None,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Run all 4 AI discovery tasks on a single company. (added 2026-06-07, QC-13)

    Tasks (use `?tasks=pattern,sanity` to run subset):
      - pattern: ai_infer_email_pattern (uses deepseek-v4-pro for deep research)
      - website: ai_discover_company_website (detect aggregators, find real site)
      - type: ai_normalize_business_type (Google Places raw → canonical)
      - sanity: ai_sanity_check_company (record integrity)

    Default: run all 4 in sequence. Use `?dry_run=1` to preview without writing to DB.
    """
    verify_key(_)
    from lf_ai_enrich import (
        ai_infer_email_pattern, ai_discover_company_website,
        ai_normalize_business_type, ai_sanity_check_company,
    )
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id, name, website, business_type, city, state, email_pattern FROM companies WHERE id=?",
        (company_id,),
    ).fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Company not found")
    company = dict(row)
    conn.close()

    body = body or {}
    requested = body.get("tasks") or ["pattern", "website", "type", "sanity"]
    if isinstance(requested, str):
        requested = [t.strip() for t in requested.split(",")]
    dry_run = bool(body.get("dry_run", False))

    results = {}
    warnings = []

    # Extract domain for pattern inference
    domain = ""
    if company.get("website"):
        from lf_executives import extract_domain
        domain = extract_domain(company["website"]) or ""

    # Task 1: pattern
    if "pattern" in requested:
        try:
            r = ai_infer_email_pattern(
                domain=domain or f"{company['name'].lower().replace(' ', '')}.com",
                company_name=company["name"],
                industry=company.get("business_type", ""),
            )
            results["pattern"] = r
            if r and r.get("pattern") and not dry_run and not company.get("email_pattern"):
                # Auto-store if no pattern exists
                conn = get_db()
                cur = conn.cursor()
                cur.execute(
                    "UPDATE companies SET email_pattern=?, email_pattern_confidence=?, email_pattern_source=? WHERE id=?",
                    (r["pattern"], r.get("confidence", 0.0), "ai_inferred", company_id),
                )
                conn.commit()
                conn.close()
        except Exception as e:
            results["pattern"] = {"error": str(e)}

    # Task 2: website
    if "website" in requested:
        try:
            r = ai_discover_company_website(
                company_name=company["name"],
                city=company.get("city", ""),
                state=company.get("state", ""),
                current_website=company.get("website", ""),
            )
            results["website"] = r
            if r and r.get("is_aggregator") and not dry_run:
                warnings.append(f"Current website is an aggregator ({r.get('reasoning', '')})")
            if r and r.get("website") and r.get("confidence", 0) > 0.7 and not dry_run:
                # Update if AI found a better one (with high confidence)
                if r["website"] != company.get("website"):
                    conn = get_db()
                    cur = conn.cursor()
                    cur.execute(
                        "UPDATE companies SET website=? WHERE id=?",
                        (r["website"], company_id),
                    )
                    conn.commit()
                    conn.close()
                    results["website"]["updated"] = True
        except Exception as e:
            results["website"] = {"error": str(e)}

    # Task 3: type
    if "type" in requested:
        try:
            r = ai_normalize_business_type(
                raw_type=company.get("business_type", ""),
                company_name=company.get("name", ""),
            )
            results["type"] = r
        except Exception as e:
            results["type"] = {"error": str(e)}

    # Task 4: sanity
    if "sanity" in requested:
        try:
            r = ai_sanity_check_company(company)
            results["sanity"] = r
        except Exception as e:
            results["sanity"] = {"error": str(e)}

    return {
        "company_id": company_id,
        "company_name": company["name"],
        "domain_used": domain,
        "results": results,
        "warnings": warnings,
        "dry_run": dry_run,
    }


@app.post("/api/ai/backfill")
async def api_ai_backfill(
    body: dict | None = None,
    background_tasks: BackgroundTasks = BackgroundTasks(),
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Backfill companies with AI discovery — runs as a background job.

    Returns {job_id, total} immediately. Poll GET /api/ai/backfill-job/{job_id}.

    Args (body):
      tasks: list of tasks to run (default: ["website"])
      only_missing: if True, only run on companies missing data
      limit: max companies to process (default: 100)
    """
    verify_key(_)

    body = body or {}
    tasks = body.get("tasks") or ["website"]
    only_missing = bool(body.get("only_missing", True))
    limit = int(body.get("limit", 100))

    conn = get_db()
    cur = conn.cursor()
    if only_missing:
        cur.execute("""
            SELECT id, name, website, business_type, city, state, email_pattern
            FROM companies
            WHERE (email_pattern IS NULL OR email_pattern = ''
                   OR website IS NULL OR website = ''
                   OR business_type IN ('point_of_interest', 'establishment', 'place_of_worship'))
            LIMIT ?
        """, (limit,))
    else:
        cur.execute("SELECT id, name, website, business_type, city, state, email_pattern FROM companies LIMIT ?", (limit,))
    companies = [dict(r) for r in cur.fetchall()]
    conn.close()

    if not companies:
        return {"status": "no_work", "message": "No companies need backfill"}

    import uuid as _uuid
    job_id = f"backfill_{_uuid.uuid4().hex[:12]}"
    create_validation_job(job_id, total=len(companies))

    async def _run_backfill():
        from lf_ai_enrich import ai_infer_email_pattern, ai_discover_company_website
        from lf_db import get_db as _get_db
        try:
            update_validation_job(job_id, status="running",
                                  started_at=datetime.now(timezone.utc).isoformat())
            summary = {"patterns_found": 0, "websites_updated": 0, "errors": 0}
            for i, c in enumerate(companies):
                try:
                    # Website discovery
                    if "website" in tasks:
                        ai_r = ai_discover_company_website(
                            company_name=c["name"],
                            city=c.get("city", ""), state=c.get("state", ""),
                            current_website=c.get("website", ""),
                        )
                        if ai_r and ai_r.get("website") and ai_r.get("confidence", 0) > 0.7:
                            if ai_r["website"] != c.get("website"):
                                _conn = _get_db()
                                _cur = _conn.cursor()
                                _cur.execute("UPDATE companies SET website=? WHERE id=?", (ai_r["website"], c["id"]))
                                _conn.commit()
                                _conn.close()
                                summary["websites_updated"] += 1

                    # Pattern inference
                    if "pattern" in tasks and not c.get("email_pattern"):
                        domain = ""
                        if c.get("website"):
                            from lf_executives import extract_domain
                            domain = extract_domain(c["website"]) or ""
                        if not domain:
                            domain = c["name"].lower().replace(" ", "").replace(",", "").replace(".", "").replace("-", "") + ".com"
                        ai_r = ai_infer_email_pattern(
                            domain=domain, company_name=c["name"],
                            industry=c.get("business_type", ""),
                        )
                        if ai_r and ai_r.get("pattern"):
                            _conn = _get_db()
                            _cur = _conn.cursor()
                            _cur.execute(
                                "UPDATE companies SET email_pattern=?, email_pattern_confidence=?, email_pattern_source=? WHERE id=?",
                                (ai_r["pattern"], ai_r.get("confidence", 0.0), "ai_backfill", c["id"]),
                            )
                            _conn.commit()
                            _conn.close()
                            summary["patterns_found"] += 1

                    # Small delay between companies to avoid rate limits
                    await asyncio.sleep(1.5)
                except Exception as e:
                    summary["errors"] += 1
                    logger.warning(f"backfill {job_id}: company {c['id']} ({c['name']}) error: {e}")

                update_validation_job(job_id, done=i + 1)

            update_validation_job(job_id, status="completed",
                                  finished_at=datetime.now(timezone.utc).isoformat(),
                                  results=json.dumps(summary))
        except Exception as e:
            logger.error(f"backfill job {job_id} failed: {e}")
            update_validation_job(job_id, status="failed")

    background_tasks.add_task(_run_backfill)
    return {
        "job_id": job_id,
        "total": len(companies),
        "message": f"Backfill started for {len(companies)} companies. Poll job endpoint for progress.",
    }


@app.get("/api/export/contacts/{session_key}")
async def api_export_contacts(
    session_key: str,
    _: str = Header(None, alias="X-LF-Key"),
):
    verify_key(_)
    session = get_session(session_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    companies = get_session_companies(session_key)
    contacts = get_session_contacts(session_key)
    enriched = []
    for ct in contacts:
        company_name, website, street, city, state, postal_code = "", "", "", "", "", ""
        for comp in companies:
            if comp.get("id") == ct.get("company_id"):
                company_name = comp.get("name", "")
                website = comp.get("website", "")
                street = comp.get("street", "")
                city = comp.get("city", "")
                state = comp.get("state", "")
                postal_code = comp.get("postal_code", "")
                break
        enriched.append({
            "first_name": ct.get("first_name", ""),
            "last_name": ct.get("last_name", ""),
            "title": ct.get("title", ""),
            "company_name": company_name,
            "street": street,
            "city": city,
            "state": state,
            "postal_code": postal_code,
            "country": "USA",
            "website": website,
            "description": "",
            "industry": session.get("industry", ""),
            "linkedin_url": ct.get("linkedin_url", ""),
        })
    path = export_contacts_csv(enriched, session_key)
    return FileResponse(path, media_type="text/csv", filename=f"lf_contacts_{session_key}.csv")


@app.get("/api/export/email-patterns/{session_key}")
async def api_export_email_patterns(
    session_key: str,
    _: str = Header(None, alias="X-LF-Key"),
):
    verify_key(_)
    session = get_session(session_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    companies = get_session_companies(session_key)
    contacts = get_session_contacts(session_key)
    enriched = []
    for ct in contacts:
        company_name, website = "", ""
        for comp in companies:
            if comp.get("id") == ct.get("company_id"):
                company_name = comp.get("name", "")
                website = comp.get("website", "")
                break
        enriched.append({
            "first_name": ct.get("first_name", ""),
            "last_name": ct.get("last_name", ""),
            "company_name": company_name,
            "website": website,
        })
    path = export_email_patterns_csv(enriched, session_key)
    return FileResponse(path, media_type="text/csv", filename=f"lf_email_patterns_{session_key}.csv")


@app.get("/api/geocode")
async def api_geocode(
    city: str = Query(...),
    state: str = Query("CA"),
    _: str = Header(None, alias="X-LF-Key"),
):
    verify_key(_)
    from lf_geocode import get_city_coords
    coords = get_city_coords(city, state)
    if not coords:
        raise HTTPException(status_code=404, detail=f"Could not geocode {city}, {state}")
    return coords


# ── Email Pattern Discovery ───────────────────────────────────────────────────

from lf_email_patterns import (
    discover_and_store_pattern,
    derive_emails_for_company,
    extract_domain_from_website,
)


@app.post("/api/company/{company_id}/discover-email-pattern")
async def api_discover_email_pattern(
    company_id: int,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Discover email pattern for a company using AI v2 (3-step inner loop)
    and derive emails for contacts. The website domain is the source of truth.

    - Fail if no website on the company
    - If pattern already exists, just derive emails
    - Otherwise call ai_infer_email_pattern_v2 (single AI call, 3-step loop)
    - Auto-derive emails on any confidence > 0
    """
    verify_key(_)
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT id, name, website, business_type as industry, city, state, email_pattern, email_pattern_confidence "
        "FROM companies WHERE id=?", (company_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Company not found")
    company = dict(row)

    # If pattern already exists, just derive emails
    if company.get("email_pattern"):
        count = derive_emails_for_company(company_id)
        return {
            "status": "derived",
            "pattern": company["email_pattern"],
            "confidence": company.get("email_pattern_confidence", 0.0),
            "emails_derived": count,
            "source": "existing",
        }

    # Extract domain from website — fail if no website
    website = (company.get("website") or "").strip()
    if not website:
        return {
            "status": "no_website",
            "pattern": None,
            "confidence": 0.0,
            "emails_derived": 0,
            "message": "Company has no website — cannot infer email pattern",
        }

    domain = extract_domain_from_website(website)
    if not domain:
        return {
            "status": "invalid_website",
            "pattern": None,
            "confidence": 0.0,
            "emails_derived": 0,
            "message": f"Could not extract valid domain from '{website}'",
        }

    # Call AI v2 (single call, 3-step inner loop)
    from lf_ai_enrich import ai_infer_email_pattern_v2
    result = ai_infer_email_pattern_v2(
        domain=domain,
        company_name=company.get("name", ""),
        industry=company.get("industry", ""),
        city=company.get("city", ""),
        state=company.get("state", ""),
    )
    if not result or not result.get("pattern"):
        return {
            "status": "not_found",
            "pattern": None,
            "confidence": 0.0,
            "emails_derived": 0,
            "domain": domain,
            "message": "AI could not determine a reliable email pattern",
        }

    pattern = result["pattern"]
    confidence = result.get("confidence", 0.0)
    reasoning = result.get("reasoning", "")
    source = result.get("source", "ai_v2")

    # Store the pattern in the companies table
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE companies SET email_pattern=?, email_pattern_confidence=? WHERE id=?",
        (pattern, confidence, company_id),
    )
    conn.commit()
    conn.close()

    # Auto-derive emails if confidence is reasonable (>= 0.35)
    emails_derived = 0
    if confidence >= 0.35:
        emails_derived = derive_emails_for_company(company_id)

    return {
        "status": "discovered",
        "pattern": pattern,
        "confidence": confidence,
        "reasoning": reasoning,
        "domain": domain,
        "emails_derived": emails_derived,
        "source": source,
    }


@app.post("/api/email/discover-pattern")
async def api_email_discover_pattern(
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Discover email pattern for a company (body-based endpoint for batch use).

    Body: {"company_id": <int>}

    Uses AI v2 (single call, 3-step inner loop) with the company website
    domain as source of truth for the email tail. Fails if no website.
    """
    verify_key(_)
    company_id = body.get("company_id")
    if not company_id:
        raise HTTPException(status_code=400, detail="company_id required")

    # Delegate to the URL-based endpoint logic
    return await api_discover_email_pattern(company_id, _)


@app.patch("/api/company/{company_id}")
async def api_patch_company(
    company_id: int,
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Update company fields. Extended 2026-06-07 (QC-14) to accept soft-delete + AI fields."""
    verify_key(_)
    from lf_db import patch_company
    success = patch_company(company_id, body)
    if not success:
        raise HTTPException(status_code=404, detail="Company not found or no valid fields to update")
    return {"status": "ok", "message": f"Company {company_id} updated"}


@app.delete("/api/company/{company_id}")
async def api_delete_company(
    company_id: int,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Soft-delete a company (is_deleted=1). Reversible. Hides from WebUI lists."""
    verify_key(_)
    from lf_db import soft_delete_company
    success = soft_delete_company(company_id)
    if not success:
        raise HTTPException(status_code=404, detail="Company not found")
    return {"status": "ok", "message": f"Company {company_id} soft-deleted"}


@app.post("/api/company/{company_id}/undelete")
async def api_undelete_company(
    company_id: int,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Re-activate a soft-deleted company (is_deleted=0)."""
    verify_key(_)
    from lf_db import undelete_company
    success = undelete_company(company_id)
    if not success:
        raise HTTPException(status_code=404, detail="Company not found")
    return {"status": "ok", "message": f"Company {company_id} re-activated"}


@app.get("/api/company-by-id/{company_id}")
async def api_get_company_by_id(
    company_id: int,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Get a single company record by numeric id. Added 2026-06-07."""
    verify_key(_)
    from lf_db import get_company
    company = get_company(company_id)
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


@app.post("/api/company/{company_id}/derive-emails")
async def api_derive_emails(
    company_id: int,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Derive emails for all contacts at a company using stored pattern."""
    verify_key(_)
    conn = get_db()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT email_pattern, email_pattern_confidence FROM companies WHERE id=?", (company_id,)
    ).fetchone()
    conn.close()
    if not row or not row[0]:
        raise HTTPException(status_code=404, detail="No email pattern found for this company")
    count = derive_emails_for_company(company_id)
    return {"status": "derived", "pattern": row[0], "confidence": row[1] or 0.0, "emails_derived": count}


@app.get("/api/company/{company_id}/email-patterns")
async def api_export_company_email_patterns(
    company_id: int,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Export email patterns CSV for a single company."""
    verify_key(_)
    conn = get_db()
    cur = conn.cursor()
    company = cur.execute(
        "SELECT id, name, website, email_pattern FROM companies WHERE id=?", (company_id,)
    ).fetchone()
    if not company:
        conn.close()
        raise HTTPException(status_code=404, detail="Company not found")
    company = dict(company)

    contacts = cur.execute(
        "SELECT first_name, last_name, full_name, title, linkedin_url FROM contacts WHERE company_id=? AND (is_deleted IS NULL OR is_deleted = 0)",
        (company_id,)
    ).fetchall()
    contacts = [dict(r) for r in contacts]
    conn.close()

    path = export_company_email_patterns_csv(
        company_id=company_id,
        company_name=company["name"],
        website=company.get("website", ""),
        email_pattern=company.get("email_pattern", ""),
        contacts=contacts,
    )

    from fastapi.responses import FileResponse
    return FileResponse(
        path,
        media_type="text/csv",
        filename=f"lf_email_patterns_{company_id}.csv",
    )


# ── Email Validation Endpoints (added 2026-06-09) ──────────────────────────────
from lf_db import (
    get_cached_validation, set_cached_validation, get_cache_stats,
    create_validation_job, update_validation_job, get_validation_job,
    get_recent_probes, get_validation_contacts_by_session,
    cleanup_stale_cache,
    get_all_derived_emails_to_validate, count_all_derived_emails,
    get_ready_for_export_contacts,
)


@app.post("/api/email/validate")
async def api_validate_email(
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Validate a single email. Gated by email_validation.enabled config flag.
    Returns full ValidationResult.
    """
    verify_key(_)
    email = (body.get("email") or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="email is required")

    from lf_email_validator import check_email

    try:
        result = check_email(email)
        result_dict = result.to_dict()

        # Cache to DB
        try:
            set_cached_validation(result_dict)
        except Exception:
            pass  # non-blocking

        return result_dict
    except Exception as e:
        logger.error(f"Email validation error for {email}: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/email/validate-batch")
async def api_validate_email_batch(
    body: dict,
    background_tasks: BackgroundTasks,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Submit a batch of emails for validation as a background job.
    Returns {job_id} immediately. Poll GET /api/email/validate-job/{job_id} for results.
    """
    verify_key(_)
    emails = body.get("emails", [])
    if not emails or not isinstance(emails, list):
        raise HTTPException(status_code=400, detail="emails list is required")

    max_batch = get("email_validation", {}).get("max_per_batch", 200)
    if len(emails) > max_batch:
        raise HTTPException(status_code=400, detail=f"Max {max_batch} emails per batch")

    import uuid
    job_id = f"val_{uuid.uuid4().hex[:12]}"
    create_validation_job(job_id, total=len(emails))

    async def _run_background():
        from lf_email_validator import check_email
        try:
            start_ts = datetime.now(timezone.utc).isoformat()
            update_validation_job(job_id, status="running", started_at=start_ts)
            results = []
            for i, email in enumerate(emails):
                result = check_email(email).to_dict()
                try:
                    set_cached_validation(result)
                except Exception:
                    pass
                results.append(result)
                update_validation_job(job_id, done=i + 1)
            import json as _json
            update_validation_job(
                job_id, status="completed", results=_json.dumps(results),
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:
            logger.error(f"Batch validation job {job_id} failed: {e}")
            update_validation_job(job_id, status="failed")

    background_tasks.add_task(_run_background)
    return {"job_id": job_id, "total": len(emails)}


@app.get("/api/email/validate-job/{job_id}")
async def api_validate_job_status(
    job_id: str,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Poll validation job status. Returns {status, total, done, results?}."""
    verify_key(_)
    job = get_validation_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/contacts/validate-emails")
async def api_validate_contacts_emails(
    body: dict,
    background_tasks: BackgroundTasks,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Bulk-validate all derived emails in a session.
    Updates smtp_validation_status on contacts rows when done.
    """
    verify_key(_)
    session_key = (body.get("session_key") or "").strip()
    if not session_key:
        raise HTTPException(status_code=400, detail="session_key is required")

    contacts_to_validate = get_validation_contacts_by_session(session_key)
    if not contacts_to_validate:
        return {"status": "no_emails", "message": "No derived emails found for this session"}

    import uuid as _uuid
    job_id = f"val_{_uuid.uuid4().hex[:12]}"
    create_validation_job(job_id, total=len(contacts_to_validate))

    async def _run_contact_validation():
        from lf_email_validator import check_email
        from lf_db import get_db as _get_db
        try:
            update_validation_job(job_id, status="running",
                                  started_at=datetime.now(timezone.utc).isoformat())
            for i, ct in enumerate(contacts_to_validate):
                email = (ct.get("email") or "").strip()
                if not email:
                    update_validation_job(job_id, done=i + 1)
                    continue
                result = check_email(email).to_dict()
                try:
                    set_cached_validation(result)
                except Exception:
                    pass
                # Update contact row with validation status + auto-set ready-for-export flag
                try:
                    ready = 1 if result["status"] == "Okay to Send" else 0
                    rejected_reason = (
                        result.get("analysis", "") if result["status"] == "Do Not Send" else None
                    )
                    _conn = _get_db()
                    _cur = _conn.cursor()
                    _cur.execute(
                        "UPDATE contacts SET smtp_validation_status=?, smtp_validated_at=?, "
                        "smtp_validation_code=?, email_ready_for_export=?, email_rejected_reason=? "
                        "WHERE id=?",
                        (result["status"], result["validated_at"], result["smtp_code"],
                         ready, rejected_reason, ct["id"]),
                    )
                    _conn.commit()
                    _conn.close()
                except Exception:
                    pass
                update_validation_job(job_id, done=i + 1)
            update_validation_job(job_id, status="completed",
                                  finished_at=datetime.now(timezone.utc).isoformat())
        except Exception as e:
            logger.error(f"Contact validation job {job_id} failed: {e}")
            update_validation_job(job_id, status="failed")

    background_tasks.add_task(_run_contact_validation)
    return {
        "job_id": job_id,
        "total": len(contacts_to_validate),
        "message": "Validation started. Poll job endpoint for results.",
    }


@app.post("/api/email/validate-all-derived")
async def api_validate_all_derived(
    body: dict,
    background_tasks: BackgroundTasks,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Validate ALL derived emails globally (no session_key required).
    Skips already-validated ('Okay to Send' / 'Do Not Send') unless revalidate_recent=True.
    Returns {job_id, total, message}.
    """
    verify_key(_)
    revalidate_recent = bool(body.get("revalidate_recent", False))
    max_count = int(body.get("max_count", 200))
    max_cfg = get("email_validation", {}).get("max_per_batch", 200)
    if max_count > max_cfg:
        max_count = max_cfg

    contacts_to_validate = get_all_derived_emails_to_validate(
        revalidate_recent=revalidate_recent, max_count=max_count,
    )
    if not contacts_to_validate:
        counts = count_all_derived_emails()
        return {
            "status": "no_emails",
            "message": "No unvalidated derived emails found.",
            "counts": counts,
        }

    import uuid as _uuid
    job_id = f"val_{_uuid.uuid4().hex[:12]}"
    create_validation_job(job_id, total=len(contacts_to_validate))

    async def _run_global_validation():
        from lf_email_validator import check_email
        from lf_db import get_db as _get_db
        try:
            update_validation_job(job_id, status="running",
                                  started_at=datetime.now(timezone.utc).isoformat())
            for i, ct in enumerate(contacts_to_validate):
                email = (ct.get("email") or "").strip()
                if not email:
                    update_validation_job(job_id, done=i + 1)
                    continue
                result = check_email(email).to_dict()
                try:
                    set_cached_validation(result)
                except Exception:
                    pass
                # Update contact row with validation status + auto-set ready-for-export flag
                try:
                    ready = 1 if result["status"] == "Okay to Send" else 0
                    rejected_reason = (
                        result.get("analysis", "") if result["status"] == "Do Not Send" else None
                    )
                    _conn = _get_db()
                    _cur = _conn.cursor()
                    _cur.execute(
                        "UPDATE contacts SET smtp_validation_status=?, smtp_validated_at=?, "
                        "smtp_validation_code=?, email_ready_for_export=?, email_rejected_reason=? "
                        "WHERE id=?",
                        (result["status"], result["validated_at"], result["smtp_code"],
                         ready, rejected_reason, ct["id"]),
                    )
                    _conn.commit()
                    _conn.close()
                except Exception:
                    pass
                update_validation_job(job_id, done=i + 1)
            update_validation_job(job_id, status="completed",
                                  finished_at=datetime.now(timezone.utc).isoformat())
        except Exception as e:
            logger.error(f"Global validation job {job_id} failed: {e}")
            update_validation_job(job_id, status="failed")

    background_tasks.add_task(_run_global_validation)
    counts = count_all_derived_emails()
    return {
        "job_id": job_id,
        "total": len(contacts_to_validate),
        "counts": counts,
        "message": "Global validation started. Poll job endpoint for results.",
    }


@app.get("/api/email/ready-for-export")
async def api_ready_for_export(
    limit: int = 500,
    download: int = 0,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Return contacts whose email has been validated as 'Okay to Send'.
    Set download=1 to get a CSV file instead of JSON.
    """
    verify_key(_)
    contacts = get_ready_for_export_contacts(limit=limit)
    if download:
        from lf_export import export_ready_for_export_csv
        path = export_ready_for_export_csv(contacts)
        return FileResponse(
            path, media_type="text/csv",
            filename=f"lf_ready_for_export_{datetime.now().strftime('%Y%m%d')}.csv",
        )
    return {
        "total": len(contacts),
        "contacts": contacts,
    }


@app.get("/api/email/cache-stats")
async def api_email_cache_stats(
    _: str = Header(None, alias="X-LF-Key"),
):
    """Return email validation cache statistics and recent probes."""
    verify_key(_)
    stats = get_cache_stats()
    recent = get_recent_probes(10)
    smtp_enabled = get("email_validation", {}).get("enabled", False)
    return {
        "smtp_probing_enabled": smtp_enabled,
        "cache": stats,
        "recent_probes": recent,
    }


@app.post("/api/email/cache-cleanup")
async def api_email_cache_cleanup(
    body: dict | None = None,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Purge stale entries from the validation cache.
    Staleness determined by cache_ttl_days in lf_config.json (default 7 days).
    
    Args (body):
      dry_run: if True, counts stale entries without deleting (default: False)
    
    Returns {deleted, remaining, ttl_days}.
    """
    verify_key(_)
    body = body or {}
    dry_run = bool(body.get("dry_run", False))
    result = cleanup_stale_cache(dry_run=dry_run)
    return result


# ── Helpers ────────────────────────────────────────────────────────────────────
def _enrich_company_details(place_id: str) -> Optional[dict]:
    """Fetch extended details for a Google Places place_id."""
    return enrich_company_details(place_id)


# ── Startup ────────────────────────────────────────────────────────────────────
def main():
    port = get("lf_PORT", 8798)
    host = get("lf_HOST", "0.0.0.0")
    print(f"Starting Lead Finder on {host}:{port}...")
    print(f"Dashboard: http://localhost:{port}/finder/")
    print(f"Static files: {STATIC_DIR}")
    uvicorn.run(app, host=host, port=port, log_level="info")

if __name__ == "__main__":
    main()