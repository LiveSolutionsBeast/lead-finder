#!/usr/bin/env python3
"""
lf_server.py - Lead Finder Web Server
=====================================
Serves static HTML + REST API on port 8798.
Static files live in ./static/ (no more f-string HTML corruption).
"""

import os, sys, hmac, json, re, sqlite3, logging, traceback, asyncio
from pathlib import Path
from typing import Optional
from functools import wraps
from datetime import datetime, timezone
import requests  # for PixelRAG proxy endpoint (added 2026-06-14)

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Query, Request
from starlette.background import BackgroundTask
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
    delete_contact, patch_contact, resume_session, delete_session,
    get_companies_for_session, bulk_delete_companies,
    create_company_manual, bulk_delete_contacts,
    create_discovery_job, add_discovery_job_items, update_discovery_job,
    get_next_discovery_pending, update_discovery_job_item,
    get_discovery_job, get_discovery_job_items, get_active_discovery_jobs,
    init_db,  # added 2026-06-07: ensure schema is up-to-date on startup
    write_contact_validation,  # Phase 1 / t1.2: single source of truth for validation writes
    # Phase 2 (Session 2): LinkedIn plugin helpers
    get_contact_by_linkedin_slug, get_companies_matching_name,
    find_contacts_by_normalized_name, find_contact_by_validated_email,
    insert_contact_experience, replace_contact_experience,
    insert_pending_push, commit_pending_push, get_pending_pushes,
    create_contact_manual,
    get_company,  # used by the matcher (t2.7)
    get_location_resolution, upsert_location_resolution,
    # Email validation helpers
    get_cached_validation, set_cached_validation, get_cache_stats,
    create_validation_job, update_validation_job, get_validation_job,
    get_recent_probes, get_validation_contacts_by_session,
    cleanup_stale_cache,
    get_all_derived_emails_to_validate, count_all_derived_emails,
    get_ready_for_export_contacts,
    get_send_ready_contacts, insert_email_send_queue,
    remove_from_send_queue, update_email_send_queue,
)
from lf_search import search_companies as google_search, enrich_company_details, text_search, get_place_details_fast
from lf_geocode import get_city_coords, find_nearby_cities
from lf_config import google_maps_api_key, lf_api_key
from lf_matcher import find_matches as find_linkedin_matches  # Phase 2 / t2.7
from lf_agent_verify import create_verify_batch_job, run_verify_batch_job  # Phase 5 verification batch
from lf_export import (  # export endpoints
    export_contacts_csv,
    export_email_patterns_csv,
    export_company_email_patterns_csv,
)
from lf_import import (  # import endpoints
    import_companies,
    normalize_rows,
    parse_companies_csv,
    start_discovery_background,
)
from lf_name_match import normalize_name  # import endpoint name normalization

# ── Config ─────────────────────────────────────────────────────────────────────
API_KEY = lf_api_key()
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

@app.get("/api-key-loader.js")
async def api_key_loader():
    """Serve the shared JS that loads the API key from /api/config at runtime.

    Keeping this as a dedicated route lets all /finder/* pages reference it
    without hard-coding the API key in HTML source.
    """
    return FileResponse(STATIC_DIR / "api-key-loader.js", media_type="application/javascript")


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


# ── Chrome extension distribution (Phase 2 / t2.9) ──────────────────────────
# The extension lives on disk at static/chrome-extension/. The user installs it
# with `chrome://extensions/` -> "Load unpacked" -> this directory. The two
# routes below let the user download the manifest as a sanity check and
# confirm the directory structure from a browser.
EXTENSION_DIR = STATIC_DIR / "chrome-extension"


@app.get("/finder/extension")
async def dashboard_extension():
    """Documentation page for installing the Chrome extension."""
    return FileResponse(STATIC_DIR / "extension.html")


@app.get("/extension/manifest.json")
async def extension_manifest():
    """Serve the extension manifest.json (used by the docs page link)."""
    return FileResponse(EXTENSION_DIR / "manifest.json", media_type="application/json")


@app.get("/extension/filecheck")
async def extension_filecheck():
    """List the files in the chrome-extension/ directory. Useful when debugging
    a broken load-unpacked install (missing icons etc.)."""
    if not EXTENSION_DIR.exists():
        return {"ok": False, "error": "chrome-extension directory missing", "dir": str(EXTENSION_DIR)}
    files = []
    for p in sorted(EXTENSION_DIR.rglob("*")):
        if p.is_file():
            rel = p.relative_to(EXTENSION_DIR)
            files.append({"path": str(rel), "size": p.stat().st_size})
    return {
        "ok": True,
        "dir": str(EXTENSION_DIR),
        "files": files,
        "file_count": len(files),
    }


@app.get("/extension/sync-script")
async def extension_sync_script():
    """Serve the PowerShell sync script as a downloadable file.

    The user runs this on the laptop to pull the latest extension:
        Invoke-WebRequest http://lsb-wsl.tail4f816e.ts.net:8798/extension/sync-script `
            -OutFile "$env:USERPROFILE\\sync-extension.ps1"
        & "$env:USERPROFILE\\sync-extension.ps1"
    """
    script_path = EXTENSION_DIR / ".revisions" / "sync-extension.ps1"
    if not script_path.exists():
        raise HTTPException(status_code=404, detail="sync-extension.ps1 not found")
    return FileResponse(script_path, media_type="text/plain",
                        filename="sync-extension.ps1")


@app.get("/extension/download")
async def extension_download():
    """Download the entire chrome-extension/ directory as a zip.

    Useful for keeping the laptop's extension copy in sync with the canonical
    source on the server. The zip preserves directory structure so the user
    can extract it directly into the Chrome 'Load unpacked' folder.

    Usage from a Windows laptop with PowerShell:
        Invoke-WebRequest -Uri "http://<server>:8798/extension/download" `
            -OutFile "$env:USERPROFILE\\Downloads\\chrome-extension.zip"
        Expand-Archive "$env:USERPROFILE\\Downloads\\chrome-extension.zip" `
            -DestinationPath "<unpacked-extension-folder>" -Force
    """
    import io
    import tempfile
    import zipfile
    from pathlib import Path

    if not EXTENSION_DIR.exists():
        raise HTTPException(status_code=404, detail="chrome-extension directory missing")

    # FileResponse needs a path, not BytesIO. Build the zip in a temp file.
    # Exclude the .revisions/ directory — those are server-side baseline
    # snapshots, not part of the extension that Chrome loads.
    tmp = tempfile.NamedTemporaryFile(prefix="chrome-ext-", suffix=".zip", delete=False)
    tmp.close()
    try:
        with zipfile.ZipFile(tmp.name, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for p in sorted(EXTENSION_DIR.rglob("*")):
                if not p.is_file():
                    continue
                rel = p.relative_to(EXTENSION_DIR)
                # Skip server-side revision tracking
                if rel.parts and rel.parts[0] == ".revisions":
                    continue
                zf.write(p, arcname=str(rel))
        return FileResponse(
            tmp.name,
            media_type="application/zip",
            filename="chrome-extension.zip",
            background=BackgroundTask(lambda: Path(tmp.name).unlink(missing_ok=True)),
        )
    except Exception:
        Path(tmp.name).unlink(missing_ok=True)
        raise



# ── API Endpoints ──────────────────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    db_ok = False
    db_error = None
    db_path = None
    try:
        from lf_db import get_db
        db = get_db()
        db.execute("SELECT 1")
        db.close()
        db_ok = True
    except Exception as e:
        db_error = str(e)
    return {
        "status": "ok" if db_ok else "degraded",
        "service": "lead-finder",
        "version": "2.0",
        "time": datetime.now(timezone.utc).isoformat(),
        "database": {"ok": db_ok, "error": db_error},
    }


@app.get("/api/config")
async def api_config():
    """Return public runtime config needed by the SPA.

    The API key is returned here so it is no longer hard-coded in static HTML.
    This endpoint intentionally omits secrets such as paid-provider API keys.
    """
    return {
        "lf_api_key": API_KEY,
        "api_key_envvar": "LF_API_KEY",
    }


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
    """Permanently delete multiple companies and their contacts."""
    verify_key(_)
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="No company IDs provided")
    # Convert to ints safely
    try:
        int_ids = [int(i) for i in ids]
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid company ID format")
    affected = bulk_delete_companies(int_ids)
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
    session_key: str = Query("", description="Filter by session key (joins via session industry)"),
    limit: int = Query(100, description="Max results to return"),
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
    where_sql = " AND ".join(where_clauses)

    # Session filter joins via companies.search_query = search_sessions.industry.
    # This matches the existing session↔company relationship.
    session_join = ""
    session_where = ""
    if session_key:
        session_join = "JOIN search_sessions s ON s.industry = c.search_query"
        session_where = "AND s.session_key = :session_key"

    query = f"""
        SELECT c.id, c.name, c.city, c.state, c.street, c.postal_code, c.website,
               c.business_type, c.phone, c.rating, c.confidence_score,
               c.hq_location, c.is_local_contact, c.data_provenance,
               c.lat, c.lng, c.place_id, c.email_pattern, c.email_pattern_confidence,
               c.email_pattern_source,
               c.canonical_business_type, c.business_type_confidence,
               c.ai_sanity_status, c.ai_sanity_notes,
               c.quality_score, c.quality_grade,
               1 as is_active,
               COUNT(ct.id) as contact_count
        FROM companies c
        {session_join}
        LEFT JOIN contacts ct ON ct.company_id = c.id
        WHERE {where_sql} {session_where}
        GROUP BY c.id
        ORDER BY c.quality_score DESC, c.name
        LIMIT :limit
    """
    rows = cur.execute(query, {
        "industry": industry, "city": city, "state": state,
        "session_key": session_key,
        "min_quality": min_quality, "quality_grade": quality_grade,
        "limit": min(limit, 500),
    }).fetchall()
    results = [dict(r) for r in rows]
    conn.close()
    return {"companies": results, "total": len(results)}


@app.get("/api/contacts")
async def api_contacts(
    company_name: str = Query("", description="Filter by company name (partial match)"),
    title: str = Query("", description="Filter by job title (partial match)"),
    city: str = Query("", description="Filter by company city (partial match)"),
    session_key: str = Query("", description="Filter by session key (joins via session industry)"),
    source: str = Query("", description="Filter by contact source. '' = all, 'linkedin_plugin' = "
                                       "LI Plugin tool, 'other' = non-plugin sources, or any exact "
                                       "source_primary value (ai, linkedin, website)."),
    min_confidence: float = Query(0.0, description="Minimum confidence score"),
    sort: str = Query("confidence", description="Sort order: 'confidence' (default), 'recent' "
                                                "(found_at DESC), or 'oldest' (found_at ASC)"),
    limit: int = Query(200, description="Max results to return"),
    derive_emails: bool = Query(False, description="Auto-derive missing emails from company patterns"),
    _: str = Header(None, alias="X-LF-Key"),
):
    verify_key(_)
    conn = get_db()
    cur = conn.cursor()
    # Sort order:
    #  - 'recent'  → newest contacts first (found_at DESC)
    #  - 'oldest'  → oldest contacts first (found_at ASC) — by age
    #  - 'confidence' (default) → legacy confidence_score DESC
    if sort == "recent":
        order_clause = "ORDER BY ct.found_at DESC NULLS LAST, ct.confidence_score DESC"
    elif sort == "oldest":
        order_clause = "ORDER BY ct.found_at ASC NULLS LAST, ct.confidence_score DESC"
    else:
        order_clause = "ORDER BY ct.confidence_score DESC"

    session_join = ""
    session_where = ""
    if session_key:
        session_join = "JOIN search_sessions s ON s.industry = c.search_query"
        session_where = "AND s.session_key = :session_key"

    # Source filter: 'other' is a virtual bucket = everything that is NOT
    # 'linkedin_plugin'. Any other value is matched exactly against
    # source_primary (ai, linkedin, website, ...). NULL source_primary is
    # treated as '' for matching so the UI can expose it if needed.
    source_where = ""
    if source == "other":
        source_where = "AND (ct.source_primary IS NULL OR ct.source_primary != 'linkedin_plugin')"
    elif source:
        source_where = "AND ct.source_primary = :source"

    rows = cur.execute(f"""
        SELECT ct.id, ct.company_id, ct.full_name, ct.title, ct.linkedin_url, ct.confidence_score,
               ct.is_local, ct.hq_contact, ct.data_provenance, ct.email, ct.is_derived_email,
               ct.is_manually_edited,
               ct.ai_verified_title, ct.ai_title_confidence, ct.ai_title_source,
               ct.source_linkedin_verified, ct.source_primary,
               ct.pipeline_stage,
               ct.found_at,
               ct.smtp_validation_status, ct.smtp_validated_at, ct.smtp_validation_code,
               ct.email_ready_for_export, ct.email_rejected_reason,
               ct.validation_confidence, ct.validation_method, ct.validation_checked_at,
               ct.validation_mx_host, ct.validation_response, ct.validation_latency_ms,
               c.name as company_name, c.city as company_city, c.state as company_state,
               c.website as company_website, c.email_pattern as company_email_pattern,
               c.email_pattern_confidence as company_email_confidence
        FROM contacts ct
        JOIN companies c ON c.id = ct.company_id
        {session_join}
        WHERE (:company_name = '' OR c.name LIKE '%' || :company_name || '%')
          AND (:title = '' OR ct.title LIKE '%' || :title || '%')
          AND (:city = '' OR c.city LIKE '%' || :city || '%')
          {session_where}
          {source_where}
          AND ct.confidence_score >= :min_confidence
          AND COALESCE(ct.is_test, 0) = 0
        {order_clause}
        LIMIT :limit
    """, {"company_name": company_name, "title": title,
          "city": city, "session_key": session_key,
          "source": source,
          "min_confidence": min_confidence,
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
        "SELECT DISTINCT business_type FROM companies WHERE business_type != '' ORDER BY business_type"
    ).fetchall()]
    cities = [r[0] for r in cur.execute(
        "SELECT DISTINCT city FROM companies WHERE city != '' ORDER BY city"
    ).fetchall()]
    states = [r[0] for r in cur.execute(
        "SELECT DISTINCT state FROM companies WHERE state != '' ORDER BY state"
    ).fetchall()]
    sessions = [dict(zip(["session_key", "industry", "city", "state"], r)) for r in cur.execute(
        "SELECT session_key, industry, city, state FROM search_sessions ORDER BY created_at DESC"
    ).fetchall()]
    conn.close()
    return {"industries": industries, "cities": cities, "states": states, "sessions": sessions}


@app.get("/api/contacts/dropdowns")
async def api_contacts_dropdowns(_: str = Header(None, alias="X-LF-Key")):
    """Return distinct values for contacts filter dropdowns."""
    verify_key(_)
    conn = get_db()
    cur = conn.cursor()
    companies = [r[0] for r in cur.execute(
        "SELECT DISTINCT c.name FROM companies c JOIN contacts ct ON ct.company_id = c.id "
        "WHERE c.name != '' ORDER BY c.name"
    ).fetchall()]
    titles = [r[0] for r in cur.execute(
        "SELECT DISTINCT title FROM contacts WHERE title != '' ORDER BY title"
    ).fetchall()]
    cities = [r[0] for r in cur.execute(
        "SELECT DISTINCT c.city FROM companies c JOIN contacts ct ON ct.company_id = c.id "
        "WHERE c.city != '' ORDER BY c.city"
    ).fetchall()]
    sessions = [dict(zip(["session_key", "industry", "city", "state"], r)) for r in cur.execute(
        "SELECT session_key, industry, city, state FROM search_sessions ORDER BY created_at DESC"
    ).fetchall()]
    # Distinct contact sources (source_primary) with counts, for the Source
    # filter/group dropdown. We always expose 'linkedin_plugin' first (the LI
    # Plugin pathway) even when no rows carry that label yet, so the UI can
    # advertise the pathway. NULL source_primary is bucketed as 'unknown'.
    source_rows = cur.execute(
        "SELECT COALESCE(NULLIF(source_primary, ''), 'unknown') AS src, COUNT(*) AS n "
        "FROM contacts WHERE COALESCE(is_test, 0) = 0 "
        "GROUP BY source_primary ORDER BY COUNT(*) DESC"
    ).fetchall()
    sources = [{"value": r["src"], "label": r["src"], "count": r["n"]} for r in source_rows]
    if not any(s["value"] == "linkedin_plugin" for s in sources):
        sources.insert(0, {"value": "linkedin_plugin", "label": "linkedin_plugin", "count": 0})
    conn.close()
    return {"companies": companies, "titles": titles, "cities": cities,
            "sessions": sessions, "sources": sources}


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
    """Permanently delete a contact."""
    verify_key(_)
    success = delete_contact(contact_id)
    if not success:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"status": "ok", "message": f"Contact {contact_id} permanently deleted"}


@app.patch("/api/contact/{contact_id}")
async def api_patch_contact(
    contact_id: int,
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Update specific fields on a contact (title, manual_notes, name fields, pipeline_stage).
    Legacy manual edits are marked via is_manually_edited=1; AI-driven callers should
    pass ai_edited_at (and optionally pipeline_stage) instead.
    """
    verify_key(_)
    # Phase 5: api_patch_contact is the manual-edit endpoint; default to marking
    # the contact as manually edited if the caller did not explicitly pass the
    # flag or an ai_edited_at timestamp. (patch_contact no longer has a back-compat
    # default for is_manually_edited.)
    if "is_manually_edited" not in body and "ai_edited_at" not in body:
        body = dict(body)
        body["is_manually_edited"] = 1
    success = patch_contact(contact_id, body)
    if not success:
        raise HTTPException(status_code=404, detail="Contact not found")
    return {"status": "ok", "message": f"Contact {contact_id} updated"}


@app.post("/api/contacts/bulk-delete")
async def api_bulk_delete_contacts(
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Permanently delete multiple contacts."""
    verify_key(_)
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="No contact IDs provided")
    try:
        int_ids = [int(i) for i in ids]
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid contact ID format")
    affected = bulk_delete_contacts(int_ids)
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
    """
    DEPRECATED — kept for backward compatibility. The Contacts page now uses
    /api/contacts/queue-smtp-validation which proves the pattern, derives the
    email, then SMTP-validates. This endpoint still validates whatever email
    is already on the contact row.
    """
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
        f"SELECT id, email FROM contacts WHERE id IN ({placeholders}) AND email IS NOT NULL AND email != ''",
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
                try:
                    write_contact_validation(ct["id"], result)
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


@app.post("/api/contacts/queue-smtp-validation")
async def api_queue_smtp_validation(
    body: dict,
    background_tasks: BackgroundTasks,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    SMTP repair entrypoint for the Contacts page.

    For each selected contact:
      1. Confirm the company's real domain via web search if not already confirmed.
      2. Prove / refresh the company's email pattern using the confirmed domain.
      3. Derive the contact's email from the proven pattern.
      4. Run SMTP validation on the derived email.
      5. If Okay to Send, stage the contact in email_send_queue for human send.

    Contacts are processed sequentially with a 15-second delay to avoid spam/
    rate-limit flags. Returns {job_id, total} immediately; poll
    /api/email/validate-job/{job_id} for progress.
    """
    verify_key(_)
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="No contact IDs provided")
    try:
        int_ids = [int(i) for i in ids]
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid contact ID format")

    force_reprove = bool(body.get("force_reprove", False))

    conn = get_db()
    cur = conn.cursor()
    placeholders = ",".join("?" * len(int_ids))
    rows = cur.execute(
        f"SELECT c.id, c.company_id FROM contacts c WHERE c.id IN ({placeholders})",
        int_ids,
    ).fetchall()
    conn.close()

    contacts_to_process = [dict(r) for r in rows]
    if not contacts_to_process:
        return {"status": "no_contacts", "message": "No contacts found in selection"}

    # Dedupe companies so we don't reprove the same pattern repeatedly in one batch.
    seen_companies: set[int] = set()
    deduped_contacts: list[dict] = []
    for ct in contacts_to_process:
        cid = ct.get("company_id")
        if cid and cid not in seen_companies:
            seen_companies.add(cid)
            deduped_contacts.append(ct)
        elif not cid:
            deduped_contacts.append(ct)

    import uuid as _uuid
    job_id = f"val_{_uuid.uuid4().hex[:12]}"
    create_validation_job(job_id, total=len(deduped_contacts))

    async def _run_queue_validation():
        from lf_search import verify_company_domain
        from lf_email_patterns import extract_domain_from_website, _infer_pattern_v2, resolve_and_validate_email
        try:
            update_validation_job(
                job_id,
                status="running",
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            processed = 0
            for ct in deduped_contacts:
                contact_id = ct["id"]
                company_id = ct.get("company_id")
                try:
                    if company_id:
                        # 1. Confirm real domain (idempotent)
                        verify_company_domain(company_id, require_corroboration=True)
                        # 2. Prove pattern if missing/unproven or forced
                        company = get_company(company_id) or {}
                        needs_proof = (
                            force_reprove
                            or not company.get("email_pattern")
                            or company.get("email_pattern_proof_status") not in ("Okay to Send", "Catch-All")
                        )
                        if needs_proof:
                            domain = (company.get("email_domain_confirmed") or "").strip().lower()
                            if not domain:
                                domain = extract_domain_from_website(company.get("website") or "")
                            if domain:
                                _infer_pattern_v2(company_id, domain)
                    # 3. Derive + validate via unified chain, then queue if ready
                    resolve_and_validate_email(
                        contact_id,
                        source="contact_queue",
                        force_revalidate=force_reprove,
                    )
                except Exception as e:
                    logger.error(f"queue-smtp-validation contact {contact_id}: {e}")
                processed += 1
                update_validation_job(job_id, done=processed)
                # 4. Chron spacing: 15s between contacts
                if processed < len(deduped_contacts):
                    await asyncio.sleep(15.0)
            update_validation_job(
                job_id,
                status="completed",
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:
            logger.error(f"queue-smtp-validation job {job_id} failed: {e}")
            update_validation_job(job_id, status="failed")

    background_tasks.add_task(_run_queue_validation)
    return {
        "job_id": job_id,
        "total": len(deduped_contacts),
        "message": f"Queued pattern proof + SMTP validation for {len(deduped_contacts)} contacts",
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
    row = conn.execute("SELECT id, email FROM contacts WHERE id=?", (contact_id,)).fetchone()
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
    try:
        write_contact_validation(contact_id, result)
    except Exception:
        pass
    return {
        "status": "ok",
        "email": email,
        "validation": result["status"],
        "smtp_code": result.get("smtp_code"),
        "validation_confidence": result.get("validation_confidence"),
        "validation_method": result.get("validation_method"),
        "validation_mx_host": result.get("validation_mx_host"),
        "validation_response": result.get("validation_response"),
        "validation_latency_ms": result.get("validation_latency_ms"),
    }


@app.post("/api/contact/{contact_id}/manual-validate")
async def api_manual_validate_contact(
    contact_id: int,
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Phase 1 / t1.4 + t1.8 — Manual override endpoint.

    Body: {"status": "valid" | "invalid", "reason": "..."}

    Sets validation_method = "manual_override" and validation_confidence = 1.0
    regardless of the SMTP state. status="valid" -> smtp_validation_status="Okay to Send",
    status="invalid" -> smtp_validation_status="Do Not Send".

    Use cases:
      - User knows the address works because they sent an email and got a reply.
      - User knows the address doesn't work because they got a hard bounce.
    """
    verify_key(_)
    status = (body.get("status") or "").strip().lower()
    reason = (body.get("reason") or "").strip()
    if status not in ("valid", "invalid"):
        raise HTTPException(status_code=400, detail='status must be "valid" or "invalid"')
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required for audit")

    # Verify the contact exists
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, email FROM contacts WHERE id=?", (contact_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Contact not found")

    smtp_status = "Okay to Send" if status == "valid" else "Do Not Send"
    now = datetime.now(timezone.utc).isoformat()
    result = {
        "email": (row["email"] or "").strip(),
        "status": smtp_status,
        "analysis": f"manual_override: {reason}",
        "smtp_probed": False,
        "smtp_code": None,
        "mx_host": "",
        "catch_all": False,
        "validated_at": now,
        "validation_confidence": 1.0,
        "validation_method": "manual_override",
        "validation_checked_at": now,
        "validation_mx_host": "",
        "validation_response": f"manual_override: {reason}",
        "validation_latency_ms": 0,
    }
    write_contact_validation(contact_id, result)
    return {
        "status": "ok",
        "contact_id": contact_id,
        "validation": smtp_status,
        "validation_method": "manual_override",
        "validation_confidence": 1.0,
        "reason": reason,
    }


@app.post("/api/contact/{contact_id}/verify")
async def api_verify_contact(
    contact_id: int,
    _: str = Header(None, alias="X-LF-Key"),
):
    """AI-verify and update ALL fields of a contact. (QC-16 + QC-22)

    AI researches title, LinkedIn URL, email, phone, location, and
    current employer. If the AI finds the person now works at a different
    company, the contact is moved to that company (created if missing).
    """
    verify_key(_)
    try:
        from lf_ai_enrich import ai_research_contact
        from lf_db import patch_contact, move_contact_to_company, find_company_by_name, create_company_manual
        conn = get_db()
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT ct.id, ct.full_name, ct.title, ct.linkedin_url, ct.email,
                   ct.phone, ct.location,
                   c.id as company_id, c.name as company_name, c.website as company_website,
                   c.email_pattern as company_email_pattern
            FROM contacts ct
            JOIN companies c ON c.id = ct.company_id
            WHERE ct.id=?
        """, (contact_id,)).fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Contact not found")
        ct = dict(row)

        # AI PRIMARY: research current identity
        ai_result = ai_research_contact(
            full_name=ct["full_name"],
            company_name=ct["company_name"],
            linkedin_url=ct.get("linkedin_url", ""),
            current_title=ct.get("title", ""),
            entity_type="contact",
            entity_id=contact_id,
            stage="verify",
        )

        if not ai_result:
            return {"status": "failed", "reason": "AI unavailable"}

        if not ai_result.get("title"):
            return {
                "status": "failed",
                "reason": ai_result.get("reasoning", "AI could not verify any fields for this contact"),
                "ai_result": ai_result,
            }

        # Decide if the contact changed companies
        ai_company = (ai_result.get("canonical_company_name") or "").strip()
        original_company_id = ct["company_id"]
        current_company_id = original_company_id
        current_company_name = ct["company_name"]
        company_changed = False

        if ai_company and ai_company.lower() != ct["company_name"].lower():
            # Try to find an existing company; otherwise create a placeholder
            existing = find_company_by_name(ai_company)
            if existing:
                current_company_id = existing["id"]
                current_company_name = existing["name"]
            else:
                current_company_id = create_company_manual({
                    "name": ai_company,
                    "source": "AI_VERIFIED_REASSIGNMENT",
                    "data_provenance": "AI_VERIFIED_REASSIGNMENT",
                })
                current_company_name = ai_company
            company_changed = current_company_id != original_company_id

        # Build update dict
        now = datetime.now(timezone.utc).isoformat()
        update = {
            "ai_title_confidence": ai_result.get("confidence", 0),
            "ai_title_source": ai_result.get("_source_method") or ai_result.get("source"),
            "ai_edited_at": now,
            "pipeline_stage": "verified",
        }
        if ai_result.get("title"):
            update["title"] = ai_result["title"]
            update["title_from_linkedin"] = ai_result["title"]
            update["ai_verified_title"] = ai_result["title"]
        if ai_result.get("linkedin_url"):
            update["linkedin_url"] = ai_result["linkedin_url"]

        # Email policy: only overwrite if AI returns something. Preserve validated emails if AI is uncertain.
        existing_email_confidence = ct.get("email") and 1.0 or 0.0  # placeholder; real validation status lives elsewhere
        ai_email = ai_result.get("email")
        if ai_email:
            # Accept AI email only if it looks reasonable and confidence is decent
            ai_conf = ai_result.get("confidence", 0)
            if ai_conf >= 0.6 and "@" in ai_email:
                update["email"] = ai_email
                update["is_derived_email"] = 1

        if ai_result.get("phone"):
            update["phone"] = ai_result["phone"]
        if ai_result.get("location"):
            update["location"] = ai_result["location"]

        if company_changed:
            move_contact_to_company(contact_id, current_company_id, update)
        else:
            patch_contact(contact_id, update)

        logger.info(f"verify: contact {contact_id} {ct['full_name']}: title {ct.get('title')} -> {ai_result.get('title')} company {'changed' if company_changed else 'same'}")
        return {
            "status": "ok",
            "title": ai_result.get("title"),
            "previous_title": ct.get("title"),
            "company_name": current_company_name,
            "previous_company_name": ct["company_name"],
            "company_changed": company_changed,
            "linkedin_url": ai_result.get("linkedin_url"),
            "email": update.get("email") or ct.get("email"),
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
        logger.error(f"verify error: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/contacts/verify-batch")
async def api_verify_contacts_batch(
    body: dict,
    background_tasks: BackgroundTasks,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Start a HARDENED escalator batch verification job for a list of contact IDs.
    Each contact is re-researched; if AI finds they moved companies, they are
    reassigned. Only independently confirmed data is written. Returns immediately
    with a job_id. Poll /api/discover-job/{job_id} for live progress.
    """
    verify_key(_)
    contact_ids = body.get("contact_ids")
    if not isinstance(contact_ids, list) or not contact_ids:
        raise HTTPException(status_code=400, detail="contact_ids must be a non-empty list")
    try:
        parsed_ids = [int(cid) for cid in contact_ids]
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="contact_ids must be integers")

    job_id = create_verify_batch_job(parsed_ids)
    if not job_id:
        raise HTTPException(status_code=404, detail="No contacts found for the provided IDs")

    background_tasks.add_task(run_verify_batch_job, job_id)
    return {
        "status": "ok",
        "job_id": job_id,
        "total": len(parsed_ids),
        "message": f"Hardened verification job started. Poll /api/discover-job/{job_id} for live progress.",
    }


@app.post("/api/contact/{contact_id}/escalate")
async def api_escalate_one_contact(
    contact_id: int,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Run the hardened escalator on a single contact. Synchronous (waits for
    completion) so the UI can show the result inline. Use /api/contacts/verify-batch
    for bulk runs.
    """
    verify_key(_)
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        row = conn.execute("""
            SELECT ct.id AS contact_id, ct.full_name, ct.title AS current_title,
                   ct.linkedin_url AS current_linkedin_url, ct.email AS current_email,
                   ct.phone AS current_phone, ct.location AS current_location,
                   ct.is_local AS current_is_local, ct.hq_contact AS current_hq_contact,
                   c.id AS company_id, c.name AS company_name, c.website AS company_website,
                   c.email_pattern AS company_email_pattern,
                   c.city AS company_city, c.state AS company_state,
                   c.lat AS company_lat, c.lng AS company_lng
            FROM contacts ct JOIN companies c ON c.id = ct.company_id
            WHERE ct.id = ?
        """, (contact_id,)).fetchone()
        conn.close()
        if not row:
            raise HTTPException(status_code=404, detail="Contact not found")
        item = dict(row)
        item["item_id"] = None  # single-shot, no DB item tracking
        from lf_agent_verify import _escalate_one_contact
        result = _escalate_one_contact(item, job_id=None)
        return {"status": "ok", **result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"escalate error: {e}\n{traceback.format_exc()}")
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
    """Get the current status of a discovery job including live stage events."""
    verify_key(_)
    from lf_db import get_discovery_job, get_discovery_job_items
    job = get_discovery_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    items = get_discovery_job_items(job_id)
    # Per-item event log: extract events from result JSON
    for it in items:
        if it.get("result"):
            try:
                env = json.loads(it["result"]) if isinstance(it["result"], str) else it["result"]
                if isinstance(env, dict) and "events" in env:
                    it["events"] = env["events"]
            except (json.JSONDecodeError, TypeError):
                pass
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
        companies = cur.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
        contacts = cur.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
        sessions = cur.execute("SELECT COUNT(*) FROM search_sessions").fetchone()[0]
        recent = [dict(r) for r in cur.execute(
            "SELECT session_key, industry, city, state, created_at FROM search_sessions ORDER BY created_at DESC LIMIT 10"
        ).fetchall()]
        top_companies = [dict(r) for r in cur.execute(
            "SELECT c.name, c.city, c.state, COUNT(ct.id) as contact_count "
            "FROM companies c LEFT JOIN contacts ct ON ct.company_id = c.id "
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
    hide_below: float = Query(0.0, description="Hard-delete companies with score below this threshold (0=don't hide)"),
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Backfill quality scores for existing companies (QC-15).
    Computes quality_score, quality_grade (A-F), and quality_signals (JSON).
    Optionally hard-deletes companies below a threshold.
    """
    verify_key(_)
    try:
        from lf_search import compute_quality_score
        conn = get_db()
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        rows = [dict(r) for r in cur.execute(
            "SELECT id, name, rating, user_rating_count, website, phone, business_type "
            "FROM companies "
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
            # Optionally hard-delete below threshold
            if hide_below > 0 and qs < hide_below:
                cur.execute("DELETE FROM contacts WHERE company_id=?", (c["id"],))
                cur.execute("DELETE FROM discovery_job_items WHERE company_id=?", (c["id"],))
                cur.execute("DELETE FROM companies WHERE id=?", (c["id"],))
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
    """Return AI enrichment usage statistics (added 2026-06-06; extended 2026-07-10 with ai_call_log)."""
    verify_key(_)
    try:
        from lf_ai_enrich import get_ai_usage_summary, cloud_model_chain, ai_enabled, ai_sanity_check_enabled
        from lf_db import get_ai_call_log_summary
        return {
            "enabled": ai_enabled(),
            "sanity_check_enabled": ai_sanity_check_enabled(),
            "model_chain": cloud_model_chain(),
            "deep_research_model": "deepseek-v4-pro:cloud",
            "usage": get_ai_usage_summary(),
            "call_log": get_ai_call_log_summary(days=30),
        }
    except Exception as e:
        return {"enabled": False, "error": str(e), "usage": {}, "call_log": {}}


@app.post("/api/import")
async def api_import_companies(
    body: dict,
    background_tasks: BackgroundTasks,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Import a list of companies into a new lead-finder session.
    Expects:
      session_name, industry, city_default, state_default, auto_enrich, companies[]
    Each company is a dict with optional fields: name, street, city, state,
    postal_code, website, phone, business_type.

    Creates an import-prefixed session, geocodes, deduplicates, inserts
    MANUAL_IMPORT records, gap-fills via AI, and kicks off discovery-batch.
    """
    verify_key(_)

    session_name = (body.get("session_name") or "import").strip()
    industry = (body.get("industry") or session_name).strip()
    city_default = (body.get("city_default") or "").strip()
    state_default = (body.get("state_default") or "CA").strip()
    auto_enrich = bool(body.get("auto_enrich", True))

    companies = body.get("companies", [])
    if not companies:
        raise HTTPException(status_code=400, detail="No companies provided")
    if isinstance(companies, str):
        # Convenience: if a CSV string is passed, parse it
        try:
            companies = parse_companies_csv(companies)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"CSV parse error: {e}")
    else:
        companies = normalize_rows(companies)

    try:
        result = import_companies(
            rows=companies,
            session_name=session_name,
            industry=industry,
            city_default=city_default,
            state_default=state_default,
            auto_enrich=auto_enrich,
        )
    except Exception as e:
        logger.error(f"/api/import error: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"Import failed: {e}")

    # Start background discovery if a job was created
    if result.get("job_id") and result.get("session_key"):
        background_tasks.add_task(
            start_discovery_background,
            result["job_id"],
            result["session_key"],
        )

    return {
        "status": "ok",
        "session_key": result["session_key"],
        "session_name": result["session_name"],
        "industry": result["industry"],
        "total": result["total"],
        "inserted": result["inserted"],
        "reused": result["reused"],
        "failed": result["failed"],
        "geocode_failures": result["geocode_failures"],
        "job_id": result["job_id"],
    }


@app.post("/api/import/csv")
async def api_import_csv(
    body: dict,
    background_tasks: BackgroundTasks,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    CSV-text import convenience endpoint. Body must contain 'csv' string plus
    session_name, industry, city_default, state_default, auto_enrich.
    """
    verify_key(_)
    csv_text = body.get("csv", "")
    if not csv_text:
        raise HTTPException(status_code=400, detail="csv field is required")
    try:
        companies = parse_companies_csv(csv_text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"CSV parse error: {e}")

    body["companies"] = companies
    return await api_import_companies(body, background_tasks, _)


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
    Comprehensive AI enrichment — runs as a background discovery job.

    Returns {job_id, total} immediately. Poll GET /api/ai/backfill-job/{job_id}.

    Body args:
      session_key (optional): scope to companies in this session
      company_ids (optional list): restrict to these company ids
      tasks: list of tasks (default: ["website","pattern","type","sanity","derive_emails","contacts"])
      only_missing: if True, only enqueue companies missing ALL requested fields
      limit: max companies to enqueue (default 100)
    """
    verify_key(_)

    body = body or {}
    session_key = body.get("session_key") or ""
    company_ids = body.get("company_ids") or []
    tasks = body.get("tasks") or [
        "website", "pattern", "type", "sanity", "derive_emails", "contacts"
    ]
    only_missing = bool(body.get("only_missing", True))
    limit = int(body.get("limit", 100))

    if isinstance(tasks, str):
        tasks = [tasks]
    requested = [t for t in tasks if isinstance(t, str)]

    conn = get_db()
    cur = conn.cursor()

    params: list = []
    where_parts: list = []

    if session_key:
        where_parts.append(
            "c.search_query = (SELECT s.industry FROM search_sessions s WHERE s.session_key=? LIMIT 1)"
        )
        params.append(session_key)

    if company_ids:
        ids = [int(cid) for cid in company_ids if isinstance(cid, (int, str)) and str(cid).strip().lstrip('-').isdigit()]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            where_parts.append(f"c.id IN ({placeholders})")
            params.extend(ids)

    if only_missing and requested:
        missing_clauses = []
        if "website" in requested:
            missing_clauses.append("(c.website IS NULL OR c.website = '')")
        if "pattern" in requested:
            missing_clauses.append("(c.email_pattern IS NULL OR c.email_pattern = '')")
        if "type" in requested:
            missing_clauses.append(
                "(c.canonical_business_type IS NULL OR c.canonical_business_type = '' "
                "OR c.business_type IN ('point_of_interest','establishment','place_of_interest'))"
            )
        if "sanity" in requested:
            missing_clauses.append("(c.ai_sanity_status IS NULL OR c.ai_sanity_status = '')")
        if missing_clauses:
            where_parts.append("(" + " AND ".join(missing_clauses) + ")")

    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

    cur.execute(
        f"""
        SELECT c.id, c.name, c.website, c.business_type, c.city, c.state,
               c.email_pattern, c.canonical_business_type, c.search_query,
               c.lat, c.lng, c.ai_sanity_status
        FROM companies c
        {where_sql}
        ORDER BY c.id
        LIMIT ?
        """,
        tuple(params) + (limit,),
    )
    companies = [dict(r) for r in cur.fetchall()]
    conn.close()

    if not companies:
        return {"status": "no_work", "message": "No companies need backfill"}

    import uuid as _uuid
    job_label = session_key if session_key else "backfill"
    job_id = f"enrich_{_uuid.uuid4().hex[:12]}"
    create_discovery_job(job_id, session_key=job_label, total=len(companies))
    add_discovery_job_items(job_id, companies)

    init_stage_log = [{
        "stage": "queued",
        "at": datetime.now(timezone.utc).isoformat(),
        "tasks": requested,
        "only_missing": only_missing,
    }]
    update_discovery_job(job_id, stage_log=init_stage_log)

    async def _run_backfill():
        from lf_ai_enrich import (
            ai_discover_company_website,
            ai_infer_email_pattern_v2,
            ai_normalize_business_type,
            ai_sanity_check_company,
        )
        from lf_email_patterns import derive_emails_for_company
        from lf_executives import discover_executives_ai_first
        from lf_db import get_db as _get_db
        from lf_executives import extract_domain

        logger.info(f"backfill {job_id}: STARTED with {len(companies)} companies, tasks={requested}")

        # Per-company hard deadline. The system can take its time on smaller
        # batches (this fixes the prior 30-50s/company average by giving
        # up to 600s for 10-candidate verification) but can never hang.
        per_company_deadline_s = int(body.get("per_company_deadline_s") or 600) if isinstance(body, dict) else 600
        per_candidate_timeout_s = int(body.get("per_candidate_timeout_s") or 30) if isinstance(body, dict) else 30

        summary = {
            "total": len(companies),
            "done": 0,
            "failed": 0,
            "websites_found": 0,
            "patterns_found": 0,
            "types_normalized": 0,
            "sanity_checked": 0,
            "emails_derived": 0,
            "contacts_found": 0,
            "contacts_saved": 0,
            "searxng_finds": 0,
            "errors": 0,
        }

        skip_domains = {
            'yelp.com','facebook.com','linkedin.com','google.com','mapquest.com',
            'yellowpages.com','bbb.org','chamberofcommerce.com','manta.com',
            'buzzfile.com','indeed.com','glassdoor.com','crunchbase.com',
            'wesocal.com','usbusiness.com','dnb.com','trustpilot.com',
            'en.wikipedia.org','reddit.com','twitter.com','instagram.com',
        }

        try:
            update_discovery_job(
                job_id,
                status="running",
                started_at=datetime.now(timezone.utc).isoformat(),
                current_stage="starting",
            )

            while True:
                item = get_next_discovery_pending(job_id)
                if not item:
                    break

                item_id = item["id"]
                company_id = item["company_id"]
                company_name = item.get("company_name", "")

                row = get_db().execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
                if not row:
                    update_discovery_job_item(item_id, "skipped", json.dumps({"error": "company missing"}))
                    summary["failed"] += 1
                    update_discovery_job(job_id, done=summary["done"], failed=summary["failed"])
                    continue
                company = dict(row)

                stage_log = []
                errors = []
                result = {
                    "company_id": company_id,
                    "company_name": company.get("name", company_name),
                    "tasks_requested": requested,
                }
                contacts_found = 0
                contacts_saved = 0
                emails_derived = 0
                update_fields: dict = {}

                def _stage(stage: str):
                    update_discovery_job(job_id, current_stage=stage)
                    stage_log.append({
                        "stage": stage,
                        "at": datetime.now(timezone.utc).isoformat(),
                    })

                try:
                    _stage(f"normalizing:{company.get('name','')}")

                    if "website" in requested:
                        _stage(f"website:{company.get('name','')}")
                        try:
                            ai_r = ai_discover_company_website(
                                company_name=company.get("name", ""),
                                city=company.get("city", ""),
                                state=company.get("state", ""),
                                current_website=company.get("website", ""),
                            )
                            website_found = None
                            website_conf = 0.0
                            if ai_r and ai_r.get("website") and ai_r.get("confidence", 0) >= 0.5:
                                website_found = ai_r["website"]
                                website_conf = ai_r.get("confidence", 0.0)

                            if not website_found:
                                try:
                                    query = (
                                        f"{company.get('name','')} "
                                        f"{company.get('city','')} {company.get('state','')} official website"
                                    )
                                    results, _ = web_search(query, timeout=15)
                                    for sr in results:
                                        url = (sr.get("url", "") or "")
                                        url_lower = url.lower()
                                        if not url.startswith('http'):
                                            continue
                                        if any(agg in url_lower for agg in skip_domains):
                                            continue
                                        if '/listing/' in url_lower or '/directory/' in url_lower:
                                            continue
                                        website_found = url
                                        break
                                    if website_found:
                                        summary["searxng_finds"] += 1
                                        logger.info(f"backfill {job_id}: SearXNG found {website_found} for {company.get('name','')}")
                                except Exception as search_e:
                                    logger.warning(f"backfill {job_id}: SearXNG fallback failed for {company.get('name','')}: {search_e}")

                            if website_found and website_found != company.get("website"):
                                update_fields["website"] = website_found
                                summary["websites_found"] += 1

                            result["website"] = {
                                "found": website_found,
                                "ai_confidence": website_conf,
                                "updated": bool(website_found and website_found != company.get("website")),
                            }
                        except Exception as e:
                            errors.append(f"website: {e}")
                            result["website"] = {"error": str(e)}
                            logger.warning(f"backfill {job_id}: website error for {company_id}: {e}")

                    if "pattern" in requested:
                        _stage(f"pattern_deep_research:{company.get('name','')}")
                        try:
                            domain = ""
                            ws = update_fields.get("website") or company.get("website") or ""
                            if ws:
                                domain = extract_domain(ws) or ""
                            if not domain:
                                domain = (
                                    company.get("name", "").lower()
                                    .replace(" ", "").replace(",", "").replace(".", "").replace("-", "")
                                    + ".com"
                                )
                            ai_r = ai_infer_email_pattern_v2(
                                domain=domain,
                                company_name=company.get("name", ""),
                                industry=company.get("business_type", ""),
                                city=company.get("city", ""),
                                state=company.get("state", ""),
                                quick=False,
                            )
                            if ai_r and ai_r.get("pattern"):
                                update_fields["email_pattern"] = ai_r["pattern"]
                                update_fields["email_pattern_confidence"] = ai_r.get("confidence", 0.0)
                                update_fields["email_pattern_source"] = "ai_backfill"
                                summary["patterns_found"] += 1
                            result["pattern"] = ai_r
                        except Exception as e:
                            errors.append(f"pattern: {e}")
                            result["pattern"] = {"error": str(e)}
                            logger.warning(f"backfill {job_id}: pattern error for {company_id}: {e}")

                    if "type" in requested:
                        _stage(f"type:{company.get('name','')}")
                        try:
                            r = ai_normalize_business_type(
                                raw_type=company.get("business_type", ""),
                                company_name=company.get("name", ""),
                            )
                            if r and r.get("canonical_type"):
                                update_fields["canonical_business_type"] = r["canonical_type"]
                                update_fields["business_type_confidence"] = r.get("confidence", 0.0)
                                summary["types_normalized"] += 1
                            result["type"] = r
                        except Exception as e:
                            errors.append(f"type: {e}")
                            result["type"] = {"error": str(e)}
                            logger.warning(f"backfill {job_id}: type error for {company_id}: {e}")

                    if "sanity" in requested:
                        _stage(f"sanity:{company.get('name','')}")
                        try:
                            sanity_company = dict(company)
                            sanity_company.update(update_fields)
                            r = ai_sanity_check_company(sanity_company)
                            if r:
                                update_fields["ai_sanity_status"] = "ok" if r.get("is_valid") else "flagged"
                                update_fields["ai_sanity_notes"] = json.dumps({
                                    "issues": r.get("issues", []),
                                    "corrections": r.get("corrections", {}),
                                    "confidence": r.get("confidence", 0.0),
                                    "reasoning": r.get("reasoning", ""),
                                })
                                summary["sanity_checked"] += 1
                            result["sanity"] = r
                        except Exception as e:
                            errors.append(f"sanity: {e}")
                            result["sanity"] = {"error": str(e)}
                            logger.warning(f"backfill {job_id}: sanity error for {company_id}: {e}")

                    if "derive_emails" in requested:
                        _stage(f"derive_emails:{company.get('name','')}")
                        try:
                            emails_derived = derive_emails_for_company(company_id)
                            summary["emails_derived"] += emails_derived
                            result["derive_emails"] = {"emails_derived": emails_derived}
                        except Exception as e:
                            errors.append(f"derive_emails: {e}")
                            result["derive_emails"] = {"error": str(e)}
                            logger.warning(f"backfill {job_id}: derive_emails error for {company_id}: {e}")

                    if "contacts" in requested:
                        _stage(f"contacts:{company.get('name','')}")
                        try:
                            # Run the discovery + verification in a worker thread
                            # so we can enforce a hard per-company deadline.
                            import concurrent.futures as _cf
                            with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
                                _future = _pool.submit(
                                    discover_executives_ai_first,
                                    company_name=company.get("name", ""),
                                    company_id=company_id,
                                    website=update_fields.get("website") or company.get("website", ""),
                                    linkedin_url="",
                                    industry=company.get("business_type", ""),
                                    search_city=company.get("city", ""),
                                    search_state=company.get("state", "CA"),
                                    search_lat=company.get("lat", 0.0) or 0.0,
                                    search_lng=company.get("lng", 0.0) or 0.0,
                                    radius_miles=25,
                                    enable_pixelrag_scrape=False,
                                    ai_timeout_s=60,
                                    per_candidate_timeout_s=per_candidate_timeout_s,
                                )
                                try:
                                    saved_contacts = _future.result(timeout=per_company_deadline_s)
                                except _cf.TimeoutError:
                                    errors.append(
                                        f"contacts: company exceeded {per_company_deadline_s}s deadline"
                                    )
                                    result["contacts"] = {"error": f"timeout after {per_company_deadline_s}s"}
                                    saved_contacts = []
                                    logger.warning(
                                        f"backfill {job_id}: {company.get('name','')} hit {per_company_deadline_s}s deadline"
                                    )
                            contacts_found = len(saved_contacts) if saved_contacts else 0
                            contacts_saved = contacts_found
                            summary["contacts_found"] += contacts_found
                            summary["contacts_saved"] += contacts_saved
                            result["contacts"] = {
                                "contacts_found": contacts_found,
                                "contacts_saved": contacts_saved,
                            }
                        except Exception as e:
                            errors.append(f"contacts: {e}")
                            result["contacts"] = {"error": str(e)}
                            logger.warning(f"backfill {job_id}: contacts error for {company_id}: {e}")

                    if update_fields:
                        try:
                            _conn = get_db()
                            _cur = _conn.cursor()
                            set_clause = ", ".join(f"{k}=?" for k in update_fields)
                            _cur.execute(
                                f"UPDATE companies SET {set_clause} WHERE id=?",
                                tuple(update_fields.values()) + (company_id,),
                            )
                            _conn.commit()
                            _conn.close()
                        except Exception as e:
                            errors.append(f"update_companies: {e}")
                            logger.error(f"backfill {job_id}: UPDATE failed for {company_id}: {e}")

                    result["contacts_found"] = contacts_found
                    result["contacts_saved"] = contacts_saved
                    result["emails_derived"] = emails_derived
                    result["errors"] = errors
                    result["stage_log"] = stage_log

                    item_status = "completed" if not errors else "completed_with_errors"
                    update_discovery_job_item(item_id, item_status, json.dumps(result))

                    if errors:
                        summary["errors"] += 1
                    summary["done"] += 1

                    try:
                        job = get_discovery_job(job_id)
                        existing_log = job.get("stage_log", []) if job else []
                        if isinstance(existing_log, str):
                            existing_log = json.loads(existing_log)
                        existing_log.extend(stage_log)
                        update_discovery_job(job_id, stage_log=existing_log)
                    except Exception:
                        pass

                    update_discovery_job(job_id, done=summary["done"], failed=summary["errors"])

                    await asyncio.sleep(1.5)

                except Exception as e:
                    summary["failed"] += 1
                    summary["errors"] += 1
                    logger.error(f"backfill {job_id}: company {company_id} ({company.get('name','')}) error: {e}")
                    update_discovery_job_item(
                        item_id, "failed",
                        json.dumps({"company_id": company_id, "error": str(e)}),
                    )
                    update_discovery_job(job_id, done=summary["done"], failed=summary["errors"])

            update_discovery_job(
                job_id,
                status="completed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                results=json.dumps(summary),
                current_stage="completed",
            )
            logger.info(f"backfill {job_id}: COMPLETED {summary}")
        except Exception as e:
            logger.error(f"backfill job {job_id} failed: {e}")
            update_discovery_job(
                job_id,
                status="failed",
                finished_at=datetime.now(timezone.utc).isoformat(),
                results=json.dumps(summary),
            )

    background_tasks.add_task(_run_backfill)
    return {
        "job_id": job_id,
        "total": len(companies),
        "message": f"Backfill started for {len(companies)} companies. Poll /api/ai/backfill-job/{job_id} for progress.",
    }


@app.get("/api/ai/backfill-job/{job_id}")
async def api_backfill_job_status(
    job_id: str,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Return a backfill (discovery) job plus its items."""
    verify_key(_)
    job = get_discovery_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    items = get_discovery_job_items(job_id)
    for it in items:
        if it.get("result"):
            try:
                it["result"] = json.loads(it["result"])
            except (json.JSONDecodeError, TypeError):
                pass
    return {"job": job, "items": items}


@app.get("/api/ai/backfill-jobs")
async def api_backfill_jobs(
    session_key: str = Query(None),
    _: str = Header(None, alias="X-LF-Key"),
):
    """List recent backfill (discovery) jobs for a session."""
    verify_key(_)
    if session_key:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM discovery_jobs WHERE session_key=? ORDER BY created_at DESC LIMIT 20",
            (session_key,),
        ).fetchall()
        conn.close()
        jobs = [dict(r) for r in rows]
    else:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM discovery_jobs ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        conn.close()
        jobs = [dict(r) for r in rows]
    for j in jobs:
        if isinstance(j.get("results"), str):
            try:
                j["results"] = json.loads(j["results"])
            except (json.JSONDecodeError, TypeError):
                pass
        if isinstance(j.get("stage_log"), str):
            try:
                j["stage_log"] = json.loads(j["stage_log"])
            except (json.JSONDecodeError, TypeError):
                j["stage_log"] = []
        else:
            j["stage_log"] = j.get("stage_log") or []
    return {"jobs": jobs}


@app.get("/api/export/contacts/{session_key}")
async def api_export_contacts(
    session_key: str,
    include_unvalidated: int = Query(0, description="Set to 1 to bypass the validation filter (CONTRACTS.md §1)"),
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Export session contacts as a Salesforce-ready CSV.

    By default, only contacts with `smtp_validation_status='Okay to Send'`
    AND `email_ready_for_export=1` are included, per CONTRACTS.md §1.
    Pass `?include_unvalidated=1` to bypass the filter and export all
    session contacts (useful for debugging; not for production outreach).
    """
    verify_key(_)
    session = get_session(session_key)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    companies = get_session_companies(session_key)
    all_contacts = get_session_contacts(session_key)

    # Session 3 / t4.3: filter to only validated+ready contacts by default
    # (CONTRACTS.md §1). Log how many were excluded for audit.
    if not include_unvalidated:
        contacts = [
            ct for ct in all_contacts
            if (ct.get("smtp_validation_status") == "Okay to Send"
                and ct.get("email_ready_for_export") == 1)
        ]
        excluded = len(all_contacts) - len(contacts)
        if excluded:
            logger.info(
                f"export filter: session={session_key} total={len(all_contacts)} "
                f"included={len(contacts)} excluded={excluded}"
            )
    else:
        contacts = all_contacts

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

    from lf_email_patterns import extract_domain_from_website, derive_emails_for_company
    from lf_ai_enrich import ai_infer_email_pattern_v2

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


@app.post("/api/companies/bulk-discover-patterns")
async def api_bulk_discover_patterns(
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Bulk discover email patterns for multiple companies as a background job.

    Body: {"ids": [int, ...], "quick": true}

    Uses ai_infer_email_pattern_v2 (quick mode skips SearXNG for speed).
    Returns {job_id, total} immediately. Poll GET /api/email/validate-job/{job_id}.
    """
    verify_key(_)
    ids = body.get("ids", [])
    if not ids:
        raise HTTPException(status_code=400, detail="No company IDs provided")
    try:
        int_ids = [int(i) for i in ids]
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid company ID format")
    quick = bool(body.get("quick", True))

    # Fetch all companies
    conn = get_db()
    cur = conn.cursor()
    placeholders = ",".join("?" * len(int_ids))
    rows = cur.execute(
        f"SELECT id, name, website, business_type, city, state, email_pattern "
        f"FROM companies WHERE id IN ({placeholders})", int_ids
    ).fetchall()
    conn.close()

    # Filter to only companies actually missing patterns
    need_pattern = [dict(r) for r in rows if not dict(r).get("email_pattern")]
    if not need_pattern:
        return {"status": "ok", "message": "All selected companies already have patterns", "total": 0}

    import uuid as _uuid
    job_id = f"patdisc_{_uuid.uuid4().hex[:12]}"
    create_validation_job(job_id, total=len(need_pattern))

    async def _run():
        from lf_ai_enrich import ai_infer_email_pattern_v2
        from lf_db import get_db as _get_db
        from lf_email_patterns import extract_domain_from_website, derive_emails_for_company
        logger.info(f"bulk-discover-patterns {job_id}: STARTED with {len(need_pattern)} companies, quick={quick}")
        try:
            update_validation_job(job_id, status="running",
                                  started_at=datetime.now(timezone.utc).isoformat())
            summary = {"patterns_found": 0, "no_website": 0, "not_found": 0, "errors": 0}
            sem = asyncio.Semaphore(3)  # max 3 concurrent AI calls

            async def _discover_one(c):
                async with sem:
                    website = (c.get("website") or "").strip()
                    if not website:
                        summary["no_website"] += 1
                        logger.info(f"bulk-discover-patterns {job_id}: {c['name']} — no website, skipping")
                        return
                    domain = extract_domain_from_website(website)
                    if not domain:
                        summary["no_website"] += 1
                        logger.info(f"bulk-discover-patterns {job_id}: {c['name']} — invalid website {website}, skipping")
                        return
                    try:
                        result = ai_infer_email_pattern_v2(
                            domain=domain,
                            company_name=c.get("name", ""),
                            industry=c.get("business_type", ""),
                            city=c.get("city", ""),
                            state=c.get("state", ""),
                            quick=quick,
                        )
                        if result and result.get("pattern"):
                            pattern = result["pattern"]
                            confidence = result.get("confidence", 0.0)
                            _conn = _get_db()
                            _cur = _conn.cursor()
                            _cur.execute(
                                "UPDATE companies SET email_pattern=?, email_pattern_confidence=?, email_pattern_source=? WHERE id=?",
                                (pattern, confidence, "ai_v2_bulk", c["id"]),
                            )
                            _conn.commit()
                            _conn.close()
                            summary["patterns_found"] += 1
                            logger.info(f"bulk-discover-patterns {job_id}: {c['name']} → {pattern} (conf={confidence:.2f})")
                            # Auto-derive emails if confidence >= 0.35
                            if confidence >= 0.35:
                                try:
                                    derive_emails_for_company(c["id"])
                                except Exception:
                                    pass
                        else:
                            summary["not_found"] += 1
                            logger.info(f"bulk-discover-patterns {job_id}: {c['name']} — AI returned no pattern")
                    except Exception as e:
                        summary["errors"] += 1
                        logger.warning(f"bulk-discover-patterns {job_id}: {c['name']} — error: {e}")

            # Run companies sequentially with small delay to respect AI rate limits
            for i, c in enumerate(need_pattern):
                await _discover_one(c)
                update_validation_job(job_id, done=i + 1)
                # Small delay between AI calls to avoid rate limits
                if i < len(need_pattern) - 1:
                    await asyncio.sleep(1.5)

            logger.info(f"bulk-discover-patterns {job_id}: DONE — {summary}")
            update_validation_job(job_id, status="completed",
                                  finished_at=datetime.now(timezone.utc).isoformat(),
                                  results=json.dumps(summary))
        except Exception as e:
            logger.error(f"bulk-discover-patterns {job_id}: FAILED — {e}")
            update_validation_job(job_id, status="failed",
                                  finished_at=datetime.now(timezone.utc).isoformat(),
                                  results=json.dumps({"error": str(e)}))

    asyncio.create_task(_run())
    return {"job_id": job_id, "total": len(need_pattern), "message": f"Discovering patterns for {len(need_pattern)} companies. Poll job endpoint for progress."}


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
    """Update company fields. Extended 2026-06-07 (QC-14) to accept AI fields."""
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
    """Permanently delete a company and all its contacts."""
    verify_key(_)
    from lf_db import delete_company
    success = delete_company(company_id)
    if not success:
        raise HTTPException(status_code=404, detail="Company not found")
    return {"status": "ok", "message": f"Company {company_id} permanently deleted"}


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
    from lf_email_patterns import derive_emails_for_company
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
        "SELECT first_name, last_name, full_name, title, linkedin_url FROM contacts WHERE company_id=?",
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
                email = (email or "").strip().lower()
                if not email:
                    continue
                # Cache pre-check (dedup)
                cached = get_cached_validation(email)
                if cached:
                    result = cached
                else:
                    result = check_email(email).to_dict()
                    try:
                        set_cached_validation(result)
                    except Exception:
                        pass
                # Look up matching contact(s) and write validation state to row(s)
                try:
                    conn = get_db()
                    conn.row_factory = sqlite3.Row
                    rows = conn.execute(
                        "SELECT id FROM contacts WHERE LOWER(email)=?", (email,)
                    ).fetchall()
                    conn.close()
                    for row in rows:
                        try:
                            write_contact_validation(row["id"], result)
                        except Exception:
                            pass
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
                # Phase 1: write_contact_validation handles all 11 fields
                try:
                    write_contact_validation(ct["id"], result)
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
                # Phase 1: write_contact_validation handles all 11 fields
                try:
                    write_contact_validation(ct["id"], result)
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


@app.get("/api/email/send-ready")
async def api_send_ready(
    status: Optional[str] = Query(None, description="Filter by queue status: queued | sent | failed | skipped"),
    company_id: Optional[int] = Query(None, description="Filter by company id"),
    session_key: Optional[str] = Query(None, description="Filter by session key"),
    limit: int = Query(500, description="Max rows to return"),
    _: str = Header(None, alias="X-LF-Key"),
):
    """Return the flat human send queue: contacts with pattern-derived emails."""
    verify_key(_)
    rows = get_send_ready_contacts(
        status=status,
        company_id=company_id,
        session_key=session_key,
        limit=limit,
    )
    return {"total": len(rows), "contacts": rows}


@app.get("/api/email/templates")
async def api_email_templates(
    _: str = Header(None, alias="X-LF-Key"),
):
    """List available email body templates from LS_TEMPLATE_DIR."""
    verify_key(_)
    import os
    tmpl_dir = Path(os.environ.get("LS_TEMPLATE_DIR", "/home/anthonyturgman/ai-stack/templates/email"))
    if not tmpl_dir.exists():
        return {"templates": []}
    templates = [
        {"name": p.name, "path": str(p)}
        for p in sorted(tmpl_dir.glob("*.html"))
    ]
    return {"templates": templates}


@app.post("/api/email/send-ready/remove")
async def api_send_ready_remove(
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """Mark selected email_send_queue rows as skipped (default) or hard-delete."""
    verify_key(_)
    queue_ids = body.get("queue_ids", [])
    if not queue_ids:
        raise HTTPException(status_code=400, detail="queue_ids required")
    try:
        int_ids = [int(i) for i in queue_ids]
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid queue id format")
    affected = remove_from_send_queue(int_ids, hard_delete=bool(body.get("hard_delete", False)))
    return {"status": "ok", "removed_count": affected}


@app.post("/api/email/send-batch")
async def api_send_batch(
    body: dict,
    background_tasks: BackgroundTasks,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Send (or dry-run) a batch of email_send_queue rows.

    Body: {"queue_ids": [...], "body_template_path": "...", "dry_run": true}

    Sends are spaced 15 seconds apart to avoid spam flags.
    """
    verify_key(_)
    queue_ids = body.get("queue_ids", [])
    if not queue_ids:
        raise HTTPException(status_code=400, detail="queue_ids required")
    try:
        int_ids = [int(i) for i in queue_ids]
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid queue id format")

    body_template_path = body.get("body_template_path")
    dry_run = bool(body.get("dry_run", True))

    import uuid as _uuid
    job_id = f"send_{_uuid.uuid4().hex[:12]}"
    create_validation_job(job_id, total=len(int_ids))

    async def _run_send_batch():
        try:
            update_validation_job(
                job_id,
                status="running",
                started_at=datetime.now(timezone.utc).isoformat(),
            )
            # Build an ad-hoc approval batch from queue rows.
            conn = get_db()
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            placeholders = ",".join("?" for _ in int_ids)
            rows = cur.execute(
                f"SELECT * FROM email_send_queue WHERE id IN ({placeholders}) AND status='queued'",
                int_ids,
            ).fetchall()
            conn.close()

            if not rows:
                update_validation_job(job_id, status="completed", done=0)
                return

            # Reuse engagement_verify helper for actual rendering/sending.
            from scripts import engagement_verify
            engagement_verify._ensure_queue_table()
            batch_id = f"send-batch-{job_id}"

            # Mirror queue rows into engagement_verify_queue so send_approved_batch
            # can render and send them. The staging token is derived from the queue id.
            conn = get_db()
            cur = conn.cursor()
            for r in rows:
                token = f"esq-{r['id']}-{job_id}"
                cur.execute(f"""
                    INSERT OR REPLACE INTO {engagement_verify.QUEUE_TABLE}
                    (token, contact_id, company_id, candidate_email, pattern_name, pattern_template,
                     subject, dry_run, approved, approval_batch, result_status, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'pending', ?)
                """, (
                    token,
                    r["contact_id"],
                    r["company_id"],
                    r["queued_email"],
                    "derived",
                    r["pattern_template"],
                    r["subject"] or engagement_verify._default_subject({"company_name": ""}),
                    1 if dry_run else 0,
                    batch_id,
                    json.dumps({"queue_id": r["id"], "staged_at": datetime.now(timezone.utc).isoformat()}),
                ))
                cur.execute(
                    "UPDATE email_send_queue SET body_template_path=?, status='approved' WHERE id=?",
                    (body_template_path, r["id"]),
                )
            conn.commit()
            conn.close()

            results = engagement_verify.send_approved_batch(
                batch_id=batch_id,
                dry_run=dry_run,
                throttle_seconds=15.0,
                body_template_path=body_template_path,
            )

            # Reflect final statuses back to email_send_queue.
            conn = get_db()
            cur = conn.cursor()
            for r in rows:
                token = f"esq-{r['id']}-{job_id}"
                qrow = cur.execute(
                    f"SELECT result_status, notes FROM {engagement_verify.QUEUE_TABLE} WHERE token=?",
                    (token,),
                ).fetchone()
                final_status = qrow["result_status"] if qrow else None
                error = (qrow["notes"] if qrow else "") or ""
                if final_status == "error":
                    new_status = "failed"
                elif not dry_run:
                    new_status = "sent"
                else:
                    new_status = "queued"
                cur.execute(
                    "UPDATE email_send_queue SET status=?, sent_at=?, send_error=? WHERE id=?",
                    (new_status, datetime.now(timezone.utc).isoformat(), error if new_status == "failed" else None, r["id"]),
                )
            conn.commit()
            conn.close()

            update_validation_job(
                job_id,
                status="completed",
                done=len(rows),
                finished_at=datetime.now(timezone.utc).isoformat(),
                results=json.dumps(results, default=str),
            )
        except Exception as e:
            logger.error(f"send-batch job {job_id} failed: {e}")
            update_validation_job(job_id, status="failed")

    background_tasks.add_task(_run_send_batch)
    return {
        "job_id": job_id,
        "total": len(int_ids),
        "dry_run": dry_run,
        "message": f"Send batch started ({'dry-run' if dry_run else 'real'}). Poll /api/email/validate-job/{job_id}.",
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


# ── Layer 2: LinkedIn location enrichment via Google Places ──────────────────
# Every LinkedIn profile push runs a Google Places lookup using the company name
# (and the LinkedIn location string as a bias when available). The full formatted
# address from the top result becomes contacts.location; the raw LinkedIn string
# is preserved in contacts.linkedin_location_raw. City/state/lat/lng are also
# extracted for newly created companies. Results are cached per (raw, company).
_REGION_RE = re.compile(r"\b(Greater|Metropolitan|Bay|Area|Region)\b", re.IGNORECASE)
_CORE_CITY_SUFFIXES = [
    r"\s+Metropolitan\s+Area",
    r"\s+Greater\s+Area",
    r"\s+Bay\s+Area",
    r"\s+Region",
    r"\s+Area",
]


def _extract_core_city(raw_location: str) -> str:
    """Strip region suffixes to get a city anchor for the Places query bias."""
    city = raw_location.strip()
    for suffix in _CORE_CITY_SUFFIXES:
        city = re.sub(suffix, "", city, flags=re.IGNORECASE).strip()
    # Remove work-mode bullets that might leak in (Hybrid/Remote/On-site)
    city = re.sub(r"\s*[·•-]\s*(On-site|Remote|Hybrid)\s*$", "", city, flags=re.IGNORECASE).strip()
    return city


def _resolve_state_from_components(components: list[dict]) -> str | None:
    """Pull the admin area (state) from Google Places addressComponents."""
    for comp in components:
        types = comp.get("types", [])
        if "administrative_area_level_1" in types:
            return comp.get("shortText") or comp.get("text") or None
    return None


def _resolve_locality_from_components(components: list[dict]) -> str | None:
    """Pull the locality (city) from Google Places addressComponents.

    Prefer sublocality (neighborhood / suburb) over broad locality
    (metropolitan area) so we capture places like Los Alamitos instead of
    Los Angeles.
    """
    locality = None
    sublocality = None
    for comp in components:
        types = comp.get("types", [])
        if "sublocality" in types or "sublocality_level_1" in types:
            sublocality = comp.get("text") or sublocality
        if "locality" in types:
            locality = comp.get("text") or locality
    return sublocality or locality


def _resolve_city_from_formatted_address(formatted_address: str | None) -> str | None:
    """Extract the city from a US-style formatted address.

    Examples:
      "10842 Noel St Unit 102, Los Alamitos, CA 90720, USA" -> "Los Alamitos"
      "123 Main St, Hawthorne, California 90250, USA" -> "Hawthorne"
    """
    if not formatted_address:
        return None
    parts = [p.strip() for p in formatted_address.split(",")]
    if len(parts) < 3:
        return None
    # Last part is usually country, second-to-last is "CA 90720" or "California 90250".
    # The part before that is the city.
    return parts[-3] if parts[-3] else None


def _resolve_location(current_company: str, raw_location: str, state_hint: str | None = None) -> dict | None:
    """
    Enrich a LinkedIn location by looking up the company in Google Places.
    Returns dict with resolved_location, city, state, lat, lng, place_id,
    formatted_address, source, or None if enrichment failed.

    The resolved_location is the full formatted address from Google Places when
    available (e.g. "10842 Noel St Unit 102, Los Alamitos, CA 90720, USA"),
    otherwise a canonical city/state/country string.
    """
    company_name = (current_company or "").strip()
    cache_key_location = (raw_location or "").strip() or "__none__"
    cached = get_location_resolution(cache_key_location, company_name)
    if cached and cached.get("resolved_city"):
        formatted_address = cached.get("formatted_address") or ""
        cleaned_formatted_address = re.sub(r",\s*USA\s*$", "", formatted_address).strip()
        return {
            "resolved_location": cleaned_formatted_address or _format_location(
                cached["resolved_city"], cached["resolved_state"], cached["resolved_country"]
            ),
            "city": cached["resolved_city"],
            "state": cached["resolved_state"],
            "country": cached["resolved_country"] or "USA",
            "lat": cached["lat"],
            "lng": cached["lng"],
            "place_id": cached["place_id"],
            "formatted_address": cleaned_formatted_address or formatted_address,
            "source": f"cached:{cached.get('source', 'google_places')}",
        }

    api_key = google_maps_api_key()
    if not api_key:
        logger.warning("[lf_server] No google_maps_api_key; cannot enrich location")
        return None

    # Use the LinkedIn location to build a query + bias, but the company name is
    # always the primary search anchor.
    core_city = _extract_core_city(raw_location) if raw_location else ""
    bias_state = state_hint or "CA"  # default to CA if no hint; California-first pipeline
    lat: float | None = None
    lng: float | None = None
    if core_city:
        bias_coords = get_city_coords(core_city, bias_state) or get_city_coords(core_city, "")
        if bias_coords:
            lat = bias_coords.get("lat")
            lng = bias_coords.get("lng")

    query = (f"{company_name} {core_city}".strip() if core_city else company_name).strip()
    if not query:
        return None

    radius = 40234  # 25 miles
    max_results = 5

    try:
        places = text_search(api_key, query, lat or 0.0, lng or 0.0, radius, max_results)
    except Exception as e:
        logger.warning(f"[lf_server] text_search failed for location enrichment: {e}")
        places = []

    chosen = None
    for place in places:
        components = place.get("addressComponents", [])
        if _resolve_city_from_formatted_address(place.get("formattedAddress")):
            chosen = place
            break
        if _resolve_locality_from_components(components):
            chosen = place
            break
    if not chosen and places:
        chosen = places[0]  # best-effort fallback

    if not chosen:
        return None

    components = chosen.get("addressComponents", [])
    place_id = chosen.get("id") or chosen.get("placeId")
    formatted_address = chosen.get("formattedAddress") or chosen.get("vicinity")
    place_lat = chosen.get("location", {}).get("latitude")
    place_lng = chosen.get("location", {}).get("longitude")

    resolved_city = (
        _resolve_city_from_formatted_address(formatted_address)
        or _resolve_locality_from_components(components)
    )
    resolved_state = _resolve_state_from_components(components) or state_hint or ""

    if not resolved_city:
        if place_lat is not None and place_lng is not None and core_city:
            from lf_geocode import find_nearby_cities
            nearby = find_nearby_cities(
                center_city=core_city,
                state=resolved_state or bias_state,
                radius_miles=25,
            )
            if nearby:
                resolved_city = nearby[0][0]
                resolved_state = nearby[0][1] or resolved_state
        if not resolved_city:
            resolved_city = core_city  # last resort

    if not resolved_state:
        resolved_state = bias_state

    # Resolve lat/lng for the resolved city via geocode cache.
    coords = get_city_coords(resolved_city, resolved_state)
    lat = coords.get("lat") if coords else place_lat
    lng = coords.get("lng") if coords else place_lng

    resolved_country = "USA"
    # Strip the trailing country from Google's formatted address so the stored
    # location is "Street, City, State ZIP" rather than "..., USA".
    cleaned_formatted_address = re.sub(r",\s*USA\s*$", "", formatted_address or "").strip() if formatted_address else ""
    resolved_location = cleaned_formatted_address or _format_location(resolved_city, resolved_state, resolved_country)

    upsert_location_resolution(
        raw_location=cache_key_location,
        company_name=company_name,
        resolved_city=resolved_city,
        resolved_state=resolved_state,
        resolved_country=resolved_country,
        place_id=place_id,
        lat=lat,
        lng=lng,
        formatted_address=formatted_address,
        source="google_places",
    )

    return {
        "resolved_location": resolved_location,
        "city": resolved_city,
        "state": resolved_state,
        "country": resolved_country,
        "lat": lat,
        "lng": lng,
        "place_id": place_id,
        "formatted_address": cleaned_formatted_address or formatted_address,
        "source": "google_places",
    }


def _format_location(city: str | None, state: str | None, country: str | None) -> str:
    """Format a canonical location string like 'Hawthorne, California'.

    We intentionally omit the country from the display string; it is still
    stored separately in linkedin_resolved_country for export/audit.
    """
    if city:
        city = " ".join(w.capitalize() for w in city.split())
    if state:
        state = state.upper() if len(state) == 2 else " ".join(w.capitalize() for w in state.split())
    parts = [p for p in [city, state] if p]
    return ", ".join(parts) if parts else ""


def _enrich_company_from_resolution(company_id: int, resolved: dict | None) -> None:
    """Write resolved city/state/lat/lng into a company row if it is missing them."""
    if not resolved or not company_id:
        return
    city = resolved.get("city")
    state = resolved.get("state")
    lat = resolved.get("lat")
    lng = resolved.get("lng")
    if not city:
        return
    conn = get_db()
    cur = conn.cursor()
    # Only fill empty fields so we don't overwrite richer Google Places data.
    row = cur.execute(
        "SELECT city, state, lat, lng FROM companies WHERE id=?", (company_id,)
    ).fetchone()
    if not row:
        conn.close()
        return
    updates = {}
    if city and not row[0]:
        updates["city"] = city
    if state and not row[1]:
        updates["state"] = state
    if lat is not None and row[2] is None:
        updates["lat"] = lat
    if lng is not None and row[3] is None:
        updates["lng"] = lng
    if updates:
        set_clause = ", ".join(f"{k}=?" for k in updates)
        cur.execute(f"UPDATE companies SET {set_clause} WHERE id=?", list(updates.values()) + [company_id])
        conn.commit()
    conn.close()


# ── Phase 2 / t2.4 + t2.7: LinkedIn plugin inject endpoint ────────────────────
# The Chrome extension POSTs the profile data here. Per CONTRACTS.md section 2
# we run the 4-case matching engine, write a pending_pushes audit row, then
# commit the contact. The popup ALWAYS shows first (t2.3), so the user has
# already confirmed the action by the time this fires.
@app.post("/api/inject/linkedin-profile")
async def api_inject_linkedin_profile(
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Phase 2 / t2.4 — Inject a LinkedIn profile into lead-finder.

    Request body (CONTRACTS.md section 4):
      linkedin_url, linkedin_slug, full_name, first_name, last_name,
      current_title, current_company, location, email (optional), phone (optional),
      experience: [...], match_action, matched_contact_id, matched_company_id

    match_action is one of:
      - update_existing                (matched_contact_id required)
      - new_contact_existing_company   (matched_company_id required)
      - new_company_new_contact        (creates a new company from current_company)
      - discard                        (just record the audit row, no DB write)

    If the popup didn't include match_action, the server runs the matcher
    (Phase 2 / t2.7) and chooses the strongest candidate. The response
    always echoes the action that was actually taken.
    """
    verify_key(_)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    linkedin_slug = (body.get("linkedin_slug") or "").strip()
    linkedin_url = (body.get("linkedin_url") or "").strip()
    full_name = (body.get("full_name") or "").strip()
    first_name = (body.get("first_name") or "").strip()
    last_name = (body.get("last_name") or "").strip()
    current_title = (body.get("current_title") or "").strip()
    current_company = (body.get("current_company") or "").strip()
    raw_location = (body.get("location") or "").strip()
    email = (body.get("email") or "").strip() or None
    phone = (body.get("phone") or "").strip() or None
    experience = body.get("experience") or []
    explicit_action = (body.get("match_action") or "").strip() or None
    explicit_contact_id = body.get("matched_contact_id")
    explicit_company_id = body.get("matched_company_id")
    # t3.4: explicit confirmation that the user knows they're overwriting a
    # is_manually_edited contact. The popup collects this; the plugin sends it.
    confirm_manual = bool(body.get("confirm_overwrite_manual", False))
    # H-6 (2026-07-26): optional is_test flag so plugin-pushed test fixtures
    # can be isolated from production queries. Default 0 (production).
    is_test_flag = 1 if body.get("is_test") else 0

    if not linkedin_slug and linkedin_url:
        linkedin_slug = _extract_linkedin_slug(linkedin_url)
    if not full_name or not linkedin_slug:
        raise HTTPException(
            status_code=400,
            detail="full_name and linkedin_slug (or linkedin_url) are required",
        )

    # ── Step 0: enrich location via Google Places for every push.
    # If Places returns a formatted address, use it as contacts.location.
    # The raw LinkedIn string is preserved in linkedin_location_raw for audit.
    # Resolved components are stored in linkedin_resolved_* columns for export.
    location = raw_location
    resolved = None
    resolved_fields = {}
    if current_company:
        resolved = _resolve_location(current_company, raw_location, state_hint=None)
        if resolved and resolved.get("resolved_location"):
            location = resolved["resolved_location"]
            resolved_fields = {
                "linkedin_resolved_city": resolved.get("city"),
                "linkedin_resolved_state": resolved.get("state"),
                "linkedin_resolved_country": resolved.get("country"),
                "linkedin_resolved_lat": resolved.get("lat"),
                "linkedin_resolved_lng": resolved.get("lng"),
                "linkedin_resolved_formatted_address": resolved.get("formatted_address") or resolved.get("resolved_location"),
                "linkedin_resolved_place_id": resolved.get("place_id"),
                "linkedin_resolved_source": resolved.get("source"),
                "linkedin_resolved_at": datetime.now(timezone.utc).isoformat(),
            }

    # ── Step 1: write the pending_pushes row first (audit-before-commit) ──
    push_id = insert_pending_push(
        linkedin_slug=linkedin_slug,
        raw_payload=body,
        matched_action=explicit_action,
        matched_contact_id=explicit_contact_id,
        matched_company_id=explicit_company_id,
    )

    # ── Step 2: determine the action. Explicit user choice wins. ──
    action = explicit_action
    chosen_contact_id: int | None = None
    chosen_company_id: int | None = None

    if explicit_action in ("update_existing", "new_contact_existing_company"):
        chosen_contact_id = int(explicit_contact_id) if explicit_contact_id else None
        chosen_company_id = int(explicit_company_id) if explicit_company_id else None

    if not action:
        # Run the matcher (t2.7).
        match_result = find_linkedin_matches(
            linkedin_slug=linkedin_slug,
            full_name=full_name,
            current_company=current_company,
            email=email or "",
            get_contact_by_slug=get_contact_by_linkedin_slug,
            find_contacts_by_name=find_contacts_by_normalized_name,
            get_companies_matching=get_companies_matching_name,
            find_contact_by_email=find_contact_by_validated_email,
            get_company=get_company,
        )
        if match_result.best_contact and match_result.best_contact.strength <= 3:
            # Strong enough to default to update_existing.
            action = "update_existing"
            chosen_contact_id = match_result.best_contact.contact_id
            chosen_company_id = match_result.best_contact.company_id
        elif match_result.best_company:
            action = "new_contact_existing_company"
            chosen_company_id = match_result.best_company.company_id
        else:
            action = "new_company_new_contact"

    # Phase 5 hardening: if the matcher chose new_company_new_contact or
    # new_contact_existing_company but the slug is already bound to an
    # existing contact, treat the push as an update_existing instead. This
    # prevents accidental double-fires from hitting the UNIQUE index and
    # returning HTTP 500.
    from lf_email_patterns import resolve_and_validate_email
    if action in ("new_contact_existing_company", "new_company_new_contact"):
        existing = get_contact_by_linkedin_slug(linkedin_slug)
        if existing:
            action = "update_existing"
            chosen_contact_id = existing["id"]
            chosen_company_id = existing.get("company_id")

    if action not in ("update_existing", "new_contact_existing_company",
                      "new_company_new_contact", "discard"):
        commit_pending_push(push_id, "discard", None, None)
        raise HTTPException(status_code=400, detail=f"invalid match_action: {action!r}")

    # ── Step 3: discard is a no-op. Just record it. ──
    if action == "discard":
        commit_pending_push(push_id, "discard", None, None)
        return {
            "ok": True,
            "contact_id": None,
            "company_id": None,
            "matched_action": "discard",
            "smtp_validation_status": None,
        }

    # ── Step 4: resolve the contact / company we need ──
    smtp_validation_status: str | None = None

    try:
        if action == "update_existing":
            if not chosen_contact_id:
                raise HTTPException(
                    status_code=400,
                    detail="update_existing requires matched_contact_id",
                )
            row = get_contact_by_linkedin_slug(linkedin_slug)
            if row and row["id"] != chosen_contact_id:
                # Slug now points to a different contact. Reject — the user
                # should pick the new one explicitly.
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"linkedin_slug {linkedin_slug!r} is now bound to "
                        f"contact #{row['id']} but you sent matched_contact_id="
                        f"{chosen_contact_id}. Pick one."
                    ),
                )
            # Apply the update.
            contact = get_db()
            contact.row_factory = sqlite3.Row
            current = contact.execute(
                "SELECT * FROM contacts WHERE id=?", (chosen_contact_id,)
            ).fetchone()
            contact.close()
            if not current:
                raise HTTPException(status_code=404, detail="matched contact not found")

            # t3.4: is_manually_edited guard
            if current["is_manually_edited"] and not confirm_manual:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"contact #{chosen_contact_id} is is_manually_edited=1. "
                        "The popup should have collected explicit confirmation "
                        "before sending; pass confirm_overwrite_manual=true to "
                        "acknowledge overwrite."
                    ),
                )

            # Build the patch: only update fields the user can see and edit.
            # We do NOT silently overwrite is_manually_edited (the user
            # would have to set it themselves in the popup).
            # Session 3 finding: patch_contact() defaults to
            # is_manually_edited=1 for back-compat, which would mark every
            # plugin update as a manual edit — making the t3.4 guard trip
            # on the next push. Plugin-pushed edits are NOT manual edits.
            # Pass 0 explicitly to clear the flag.
            patch = {
                "first_name": first_name or current["first_name"],
                "last_name": last_name or current["last_name"],
                "full_name": full_name or current["full_name"],
                "title": current_title or current["title"],
                "linkedin_url": linkedin_url or current["linkedin_url"],
                "linkedin_slug": linkedin_slug,
                "phone": phone if phone is not None else current["phone"],
                "location": location or current["location"],
                "title_from_linkedin": current_title or current["title_from_linkedin"],
                "source_primary": "linkedin_plugin",
                "source_linkedin_verified": 1,
                "is_manually_edited": 0,
            }
            if raw_location:
                patch["linkedin_location_raw"] = raw_location
            patch.update(resolved_fields)
            # If the popup also sent a different company, move the contact.
            if chosen_company_id and chosen_company_id != current["company_id"]:
                patch["company_id"] = chosen_company_id
            patch_contact(chosen_contact_id, patch)
            chosen_company_id = chosen_company_id or current["company_id"]
            smtp_validation_status = current["smtp_validation_status"]
            # If user attached an email, set validation to NULL so Session 1
            # re-validates. This is the one carve-out from the
            # write_contact_validation() pattern: we don't have a full
            # ValidationResult here, just a deliberate "please re-validate"
            # signal. CONTRACTS.md §2 rule 7.
            if email:
                conn = get_db()
                conn.execute(
                    "UPDATE contacts SET email=?, smtp_validation_status=NULL, "
                    "smtp_validated_at=NULL, email_rejected_reason=NULL, "
                    "validation_method=NULL, validation_checked_at=NULL, "
                    "validation_mx_host=NULL, validation_response=NULL, "
                    "validation_confidence=0.0, validation_latency_ms=NULL "
                    "WHERE id=?",
                    (email, chosen_contact_id),
                )
                conn.commit()
                conn.close()
                smtp_validation_status = None

            # Phase 5 — run the unified email chain on the updated contact.
            try:
                resolution = resolve_and_validate_email(
                    chosen_contact_id,
                    source="linkedin_plugin",
                    popup_email=email,
                )
                smtp_validation_status = resolution.smtp_validation_status
            except Exception as e:
                logger.error(f"Email chain failed for linkedin_plugin contact {chosen_contact_id}: {e}")
                # Leave smtp_validation_status as None; don't fail the inject.

        elif action == "new_contact_existing_company":
            if not chosen_company_id:
                raise HTTPException(
                    status_code=400,
                    detail="new_contact_existing_company requires matched_company_id",
                )
            chosen_contact_id = create_contact_manual({
                "company_id": chosen_company_id,
                "first_name": first_name,
                "last_name": last_name,
                "full_name": full_name,
                "title": current_title,
                "linkedin_url": linkedin_url,
                "linkedin_slug": linkedin_slug,
                "email": email,
                "phone": phone,
                "location": location,
                "linkedin_location_raw": raw_location,
                **resolved_fields,
                "source_primary": "linkedin_plugin",
                "is_test": is_test_flag,
            })
            if chosen_contact_id <= 0:
                raise HTTPException(status_code=500, detail="failed to create contact")
            # Enrich the matched company with resolved location data too.
            _enrich_company_from_resolution(chosen_company_id, resolved)
            smtp_validation_status = None

            # Phase 5 — run the unified email chain on the new contact.
            try:
                resolution = resolve_and_validate_email(
                    chosen_contact_id,
                    source="linkedin_plugin",
                    popup_email=email,
                )
                smtp_validation_status = resolution.smtp_validation_status
            except Exception as e:
                logger.error(f"Email chain failed for linkedin_plugin contact {chosen_contact_id}: {e}")

        elif action == "new_company_new_contact":
            # Create a brand-new company record from the LinkedIn profile.
            # If we resolved a location, use the real city/state/lat/lng
            # so the company record is geocoded from the start.
            company_kwargs = {
                "name": current_company or full_name,
                "data_provenance": "plugin_pushed",
                "source": "LINKEDIN_PLUGIN",
                "country": "USA",
            }
            if resolved:
                company_kwargs.update({
                    "city": resolved.get("city"),
                    "state": resolved.get("state"),
                    "lat": resolved.get("lat"),
                    "lng": resolved.get("lng"),
                })
            chosen_company_id = create_company_manual(company_kwargs)
            chosen_contact_id = create_contact_manual({
                "company_id": chosen_company_id,
                "first_name": first_name,
                "last_name": last_name,
                "full_name": full_name,
                "title": current_title,
                "linkedin_url": linkedin_url,
                "linkedin_slug": linkedin_slug,
                "email": email,
                "phone": phone,
                "location": location,
                "linkedin_location_raw": raw_location,
                **resolved_fields,
                "source_primary": "linkedin_plugin",
                "is_test": is_test_flag,
            })
            if chosen_contact_id <= 0:
                raise HTTPException(status_code=500, detail="failed to create contact")
            smtp_validation_status = None

            # Phase 5 — run the unified email chain on the new contact.
            try:
                resolution = resolve_and_validate_email(
                    chosen_contact_id,
                    source="linkedin_plugin",
                    popup_email=email,
                )
                smtp_validation_status = resolution.smtp_validation_status
            except Exception as e:
                logger.error(f"Email chain failed for linkedin_plugin contact {chosen_contact_id}: {e}")

        # Enrich company on update_existing too, in case the existing company is missing geocode.
        if action == "update_existing" and chosen_company_id and resolved:
            _enrich_company_from_resolution(chosen_company_id, resolved)

        # ── Step 5: write experience rows ──
        if chosen_contact_id:
            replace_contact_experience(chosen_contact_id, experience, linkedin_slug)

        # ── Step 6: mark the audit row as committed ──
        commit_pending_push(push_id, action, chosen_contact_id, chosen_company_id)

        return {
            "ok": True,
            "contact_id": chosen_contact_id,
            "company_id": chosen_company_id,
            "matched_action": action,
            "smtp_validation_status": smtp_validation_status,
            "pending_push_id": push_id,
        }

    except HTTPException:
        # Audit row stays as-is so we can investigate; just re-raise.
        raise
    except Exception as e:
        logger.error(f"inject_linkedin_profile failed: {e}\n{traceback.format_exc()}")
        raise HTTPException(status_code=500, detail=f"inject failed: {e}")


# ── Phase 2 / t2.7: matcher probe endpoint ────────────────────────────────────
# The popup calls this BEFORE the user picks an action, to populate the
# 4-case radio buttons. The popup is responsible for showing the result.
@app.post("/api/inject/linkedin-profile/match")
async def api_inject_linkedin_profile_match(
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Phase 2 / t2.7 — Run the 4-case matching engine without committing.

    Body: {linkedin_slug, full_name, current_company, email}
    Returns: {best_contact, contact_ties, company_candidates, best_company,
              normalized_name, notes}
    """
    verify_key(_)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    linkedin_slug = (body.get("linkedin_slug") or "").strip()
    if not linkedin_slug and body.get("linkedin_url"):
        linkedin_slug = _extract_linkedin_slug(body.get("linkedin_url") or "")

    result = find_linkedin_matches(
        linkedin_slug=linkedin_slug,
        full_name=body.get("full_name") or "",
        current_company=body.get("current_company") or "",
        email=body.get("email") or "",
        get_contact_by_slug=get_contact_by_linkedin_slug,
        find_contacts_by_name=find_contacts_by_normalized_name,
        get_companies_matching=get_companies_matching_name,
        find_contact_by_email=find_contact_by_validated_email,
        get_company=get_company,
    )
    return result.to_dict()


# ── Phase 3 / t3.2: audit log query ──────────────────────────────────────────
@app.get("/api/audit/plugin-pushes")
async def api_audit_plugin_pushes(
    limit: int = Query(100, ge=1, le=1000),
    from_ts: str | None = Query(None, description="ISO 8601 lower bound on created_at"),
    to_ts: str | None = Query(None, description="ISO 8601 upper bound on created_at"),
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Phase 3 / t3.2 — Recent LinkedIn plugin pushes, newest first.

    Returns {pushes: [...], count: N}. Each push row contains the raw payload
    (so an audit can replay it) and the resolved matched action / ids.
    """
    verify_key(_)
    rows = get_pending_pushes(limit=limit, from_ts=from_ts, to_ts=to_ts)
    # raw_payload is a JSON string; parse it for the UI.
    for r in rows:
        try:
            r["raw_payload"] = json.loads(r.get("raw_payload") or "{}")
        except Exception:
            r["raw_payload"] = {}
    return {"pushes": rows, "count": len(rows)}


# ── Debug endpoint: capture LinkedIn profile DOM structure from the plugin ─────
@app.post("/api/debug/profile-structure")
async def api_debug_profile_structure(
    body: dict,
    _: str = Header(None, alias="X-LF-Key"),
):
    """
    Non-critical debug endpoint. The extension POSTs a snapshot of the
    Experience section DOM shape so we can inspect real LinkedIn renderings
    without asking the user to copy/paste console output.
    Writes one JSON file per slug under debug/ and returns the file path.
    """
    verify_key(_)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    linkedin_slug = (body.get("linkedin_slug") or "").strip()
    if not linkedin_slug:
        linkedin_slug = _extract_linkedin_slug(body.get("linkedin_url") or "")
    if not linkedin_slug:
        raise HTTPException(status_code=400, detail="linkedin_slug or linkedin_url required")

    debug_dir = BASE_DIR / "debug"
    debug_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    filename = f"profile-{linkedin_slug}-{timestamp}.json"
    filepath = debug_dir / filename

    snapshot = {
        "linkedin_slug": linkedin_slug,
        "linkedin_url": body.get("linkedin_url") or "",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "shape": body.get("shape") or {},
        "raw_text": body.get("raw_text") or "",
        "container_html": body.get("container_html") or "",
        "parsed_result": body.get("parsed_result") or {},
        "safe_mode": bool(body.get("safe_mode")),
    }
    filepath.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    return {"ok": True, "path": str(filepath)}


# ── Helpers ────────────────────────────────────────────────────────────────────
def _enrich_company_details(place_id: str) -> Optional[dict]:
    """Fetch extended details for a Google Places place_id."""
    return enrich_company_details(place_id)


# Phase 2 helper: pull the /in/<slug> portion out of a LinkedIn URL.
_LINKEDIN_SLUG_RE = re.compile(r"linkedin\.com/in/([^/?#]+)", re.IGNORECASE)


def _extract_linkedin_slug(url: str) -> str:
    """Return the /in/<slug> portion of a LinkedIn profile URL, or '' if not found."""
    if not url:
        return ""
    m = _LINKEDIN_SLUG_RE.search(url)
    return m.group(1) if m else ""


# ── Engagement Verification (cornerstone workflow) ────────────────────────────
# Wraps scripts/engagement_verify.py so the OWUI lf_email_pipeline tool (and
# any HTTP client) can drive the cornerstone proof-by-engagement loop without
# shelling out. The server runs on the host with write access to lf.db.
_ENG_IMPORT_ERROR: Optional[str] = None
try:
    import importlib.util
    _ev_path = BASE_DIR / "scripts" / "engagement_verify.py"
    _ev_spec = importlib.util.spec_from_file_location("engagement_verify", _ev_path)
    _ev_mod = importlib.util.module_from_spec(_ev_spec)
    _ev_spec.loader.exec_module(_ev_mod)
except Exception as _e:  # pragma: no cover
    _ev_mod = None
    _ENG_IMPORT_ERROR = f"engagement_verify import failed: {_e}"


def _ev_or_error():
    """Return the engagement_verify module or raise an HTTPException."""
    if _ev_mod is None:
        raise HTTPException(
            status_code=500,
            detail=f"engagement_verify module not loaded: {_ENG_IMPORT_ERROR}",
        )
    return _ev_mod


class EVStageBody(BaseModel):
    max_candidates: int = 3
    output: Optional[str] = None


class EVSendBody(BaseModel):
    batch_id: str
    dry_run: bool = False
    throttle_seconds: float = 15.0
    body_template_path: Optional[str] = None


class EVAddContactBody(BaseModel):
    contact_id: Optional[int] = None
    company_id: Optional[int] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    title: Optional[str] = None
    linkedin_url: Optional[str] = None
    auto_send: bool = False
    dry_run: bool = False


class EVPushSFBody(BaseModel):
    dry_run: bool = True
    confirm: bool = False


class EVPushEspoBody(BaseModel):
    dry_run: bool = True
    confirm: bool = False


class EVWatchdogBody(BaseModel):
    wait_hours: int = 24
    dry_run: bool = True
    confirm: bool = False
    confirm_sf: bool = False  # deprecated alias for confirm


@app.get("/api/engagement/status-all")
async def ev_status_all(_: str = Header(None, alias="X-LF-Key")):
    """Show all active engagement runs across all companies."""
    verify_key(_)
    ev = _ev_or_error()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM engagement_company_runs ORDER BY queued_at DESC"
        ).fetchall()
        runs = []
        for r in rows:
            run = dict(r)
            co = conn.execute(
                "SELECT name, website FROM companies WHERE id=?", (run["company_id"],)
            ).fetchone()
            cs = None
            if run.get("cornerstone_contact_id"):
                cs = conn.execute(
                    "SELECT full_name, title, email FROM contacts WHERE id=?",
                    (run["cornerstone_contact_id"],),
                ).fetchone()
            qcounts = {"total": 0, "bounced": 0, "opened": 0, "pending": 0, "sent": 0}
            qrows = conn.execute(
                "SELECT result_status, COUNT(*) as n FROM engagement_verify_queue "
                "WHERE approval_batch=? GROUP BY result_status",
                ((run.get("notes") or "").split("batch_id=")[-1].strip(),),
            ).fetchall() if run.get("notes") else []
            for qr in qrows:
                st = qr["result_status"] or "pending"
                qcounts["total"] += qr["n"]
                if st in ("bounced", "do_not_send"):
                    qcounts["bounced"] += qr["n"]
                elif st in ("opened", "delivered", "okay_to_send"):
                    qcounts["opened"] += qr["n"]
                elif st == "sent":
                    qcounts["sent"] += qr["n"]
                else:
                    qcounts["pending"] += qr["n"]
            run["company_name"] = co["name"] if co else None
            run["website"] = co["website"] if co else None
            run["cornerstone_name"] = cs["full_name"] if cs else None
            run["cornerstone_title"] = cs["title"] if cs else None
            run["cornerstone_email"] = cs["email"] if cs else None
            run["queue"] = qcounts
            runs.append(run)
        return {"active_runs": len(runs), "runs": runs}
    finally:
        conn.close()


@app.get("/api/engagement/status/{company_id}")
async def ev_status(company_id: int, _: str = Header(None, alias="X-LF-Key")):
    """Show engagement pipeline status for one company."""
    verify_key(_)
    ev = _ev_or_error()
    conn = get_db()
    try:
        co = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
        if not co:
            raise HTTPException(status_code=404, detail="Company not found")
        co = dict(co)
        cs = None
        if co.get("cornerstone_contact_id"):
            cs = conn.execute(
                "SELECT full_name, title, email FROM contacts WHERE id=?",
                (co["cornerstone_contact_id"],),
            ).fetchone()
        run = conn.execute(
            "SELECT * FROM engagement_company_runs WHERE company_id=? "
            "ORDER BY queued_at DESC LIMIT 1",
            (company_id,),
        ).fetchone()
        ready = conn.execute(
            "SELECT COUNT(*) as n FROM contacts WHERE company_id=? AND email_ready_for_export=1",
            (company_id,),
        ).fetchone()["n"]
        return {
            "company_id": company_id,
            "company_name": co["name"],
            "email_pattern": co.get("email_pattern"),
            "proof_status": co.get("email_pattern_proof_status"),
            "cornerstone_contact_id": co.get("cornerstone_contact_id"),
            "cornerstone": {"full_name": cs["full_name"], "title": cs["title"], "email": cs["email"]} if cs else None,
            "active_run": dict(run) if run else None,
            "ready_contacts": ready,
        }
    finally:
        conn.close()


@app.post("/api/engagement/pick-cornerstone/{company_id}")
async def ev_pick(company_id: int, _: str = Header(None, alias="X-LF-Key")):
    """Pick a cornerstone contact for a company (dry-run safe; no writes to queue)."""
    verify_key(_)
    ev = _ev_or_error()
    result = ev.pick_cornerstone(company_id, dry_run=False)
    if not result:
        return {"picked": False, "message": "No eligible cornerstone contact found"}
    return {"picked": True, "contact": result}


@app.post("/api/engagement/stage-cornerstone/{company_id}")
async def ev_stage(company_id: int, body: EVStageBody, _: str = Header(None, alias="X-LF-Key")):
    """Stage a cornerstone verification batch (writes to engagement_verify_queue)."""
    verify_key(_)
    ev = _ev_or_error()
    result = ev.stage_cornerstone_batch(
        company_id, output=body.output, max_candidates=body.max_candidates
    )
    return result


@app.post("/api/engagement/send")
async def ev_send(body: EVSendBody, _: str = Header(None, alias="X-LF-Key")):
    """Send an approved cornerstone batch via MS Graph. Real sends — use carefully."""
    verify_key(_)
    ev = _ev_or_error()
    result = ev.send_cornerstone_batch(
        body.batch_id,
        dry_run=body.dry_run,
        throttle_seconds=body.throttle_seconds,
        body_template_path=body.body_template_path,
    )
    return result


@app.post("/api/engagement/check/{company_id}")
async def ev_check(company_id: int, wait_hours: int = 24, _: str = Header(None, alias="X-LF-Key")):
    """Check track.db results for one company and update lf.db."""
    verify_key(_)
    ev = _ev_or_error()
    result = ev.check_results_for_company(company_id, wait_hours=wait_hours)
    return result


@app.post("/api/engagement/apply-proven/{company_id}")
async def ev_apply(company_id: int, dry_run: bool = False, force: bool = False, _: str = Header(None, alias="X-LF-Key")):
    """Apply the proven pattern to sibling contacts (writes lf.db only)."""
    verify_key(_)
    ev = _ev_or_error()
    result = ev.apply_proven_pattern(company_id, dry_run=dry_run, force=force)
    return result


@app.post("/api/engagement/push-sf/{company_id}")
async def ev_push_sf(company_id: int, body: EVPushSFBody, _: str = Header(None, alias="X-LF-Key")):
    """LEGACY: Push company + validated contacts to Salesforce. dry_run=True by default."""
    verify_key(_)
    ev = _ev_or_error()
    result = ev.push_company_to_salesforce(
        company_id, dry_run=body.dry_run, confirm=body.confirm
    )
    return result


@app.post("/api/engagement/push-espo/{company_id}")
async def ev_push_espo(company_id: int, body: EVPushEspoBody, _: str = Header(None, alias="X-LF-Key")):
    """Push company + validated contacts to EspoCRM. dry_run=True by default."""
    verify_key(_)
    ev = _ev_or_error()
    result = ev.push_company_to_espocrm(
        company_id, dry_run=body.dry_run, confirm=body.confirm
    )
    return result


@app.post("/api/engagement/push-espocrm/{company_id}")
async def ev_push_espocrm(company_id: int, body: EVPushEspoBody, _: str = Header(None, alias="X-LF-Key")):
    """Alias for /api/engagement/push-espo/{company_id}. Canonical EspoCRM URL form."""
    verify_key(_)
    ev = _ev_or_error()
    result = ev.push_company_to_espocrm(
        company_id, dry_run=body.dry_run, confirm=body.confirm
    )
    return result


@app.post("/api/engagement/watchdog")
async def ev_watchdog(body: EVWatchdogBody, _: str = Header(None, alias="X-LF-Key")):
    """Run one watcher cycle (polls track.db, applies patterns, stages CRM push)."""
    verify_key(_)
    ev = _ev_or_error()
    confirm = body.confirm or body.confirm_sf
    result = ev.run_watchdog_cycle(
        wait_hours=body.wait_hours, dry_run=body.dry_run, confirm=confirm
    )
    return result


@app.post("/api/engagement/add-contact")
async def ev_add_contact(body: EVAddContactBody, _: str = Header(None, alias="X-LF-Key")):
    """Add a contact and prepare cornerstone verification for its company."""
    verify_key(_)
    ev = _ev_or_error()
    result = ev.add_contact_for_verification(
        contact_id=body.contact_id,
        company_id=body.company_id,
        first_name=body.first_name,
        last_name=body.last_name,
        title=body.title,
        linkedin_url=body.linkedin_url,
        auto_send=body.auto_send,
        dry_run=body.dry_run,
    )
    return result


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