#!/usr/bin/env python3
"""
lf_openwebui_tools.py - Open WebUI Tools Registration (HTTP-to-REST Refactor)
==============================================================================

Registers 16 consolidated lead-finder Tools in Open WebUI's webui.db database.
Every tool now calls the lead-finder REST API on port 8798 (X-LF-Key auth)
instead of reading lf.db directly or hardcoding API keys / service URLs.

Pipeline stages reflected in tool descriptions:
  1 Discovery -> 2 Enrichment -> 3 Contacts -> 3.5 Verify ->
  4 Email Patterns -> 5 Validation -> 6 Export

Usage:
  python3 lf_openwebui_tools.py register    # Atomic unregister + register all tools
  python3 lf_openwebui_tools.py list        # Show registered lf_ tools
  python3 lf_openwebui_tools.py unregister  # Remove all lf_ tools
"""

import json
import subprocess
import sys
import base64

# ============================================================================
# TOOL DEFINITIONS (16 consolidated tools + optional lf_ai_usage)
# ============================================================================

TOOLS = [
    # --- Stage 1: Discovery ---
    {
        "id": "lf_search_companies",
        "name": "lf_search_companies",
        "description": "Stage 1 Discovery: search for companies by industry and location. Calls POST /api/search on the lead-finder server. Returns companies with place_id, name, address, website, rating.",
        "parameters": {
            "type": "object",
            "properties": {
                "industry": {"type": "string", "description": "Industry or business type keyword"},
                "city": {"type": "string", "description": "City name"},
                "state": {"type": "string", "description": "2-letter state code, default CA"},
                "radius_miles": {"type": "integer", "description": "Search radius in miles, default 25"},
                "max_results": {"type": "integer", "description": "Maximum companies to return, default 20"},
            },
            "required": ["industry", "city"],
        },
    },
    {
        "id": "lf_import_companies",
        "name": "lf_import_companies",
        "description": "Stage 1 Discovery: import a pasted CSV list of companies into lead-finder. Calls POST /api/import/csv, geocodes, gap-fills, deduplicates, and kicks off executive discovery. Returns session_key and discovery job_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "companies": {"type": "string", "description": "CSV text with header row, or a file path ending in .csv/.xlsx readable inside the OWUI container. Required column: name."},
                "session_name": {"type": "string", "description": "User-derived session name, prefixed with 'import-'"},
                "industry": {"type": "string", "description": "Classification label taken as-is"},
                "city_default": {"type": "string", "description": "Default city for rows that omit it"},
                "state_default": {"type": "string", "description": "Default state for rows that omit it (2-letter)"},
                "auto_enrich": {"type": "boolean", "description": "If true (default), gap-fill and kick off executive discovery"},
            },
            "required": ["companies"],
        },
    },
    {
        "id": "lf_geocode_location",
        "name": "lf_geocode_location",
        "description": "Stage 1 Discovery: convert city+state to lat/lng via GET /api/geocode. Uses server cache first, then Google Geocoding API.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "City name"},
                "state": {"type": "string", "description": "2-letter state code, default CA"},
            },
            "required": ["city"],
        },
    },
    {
        "id": "lf_list_sessions",
        "name": "lf_list_sessions",
        "description": "Stage 1 Discovery + Overview: list search sessions and dashboard stats. Calls GET /api/sessions and GET /api/stats and merges summary counts into each session.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "id": "lf_get_session",
        "name": "lf_get_session",
        "description": "Stage 1 Discovery: get full session data by session_key. Calls GET /api/session/{key}/results and returns session details, all companies, and all contacts.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_key": {"type": "string", "description": "Session key from lf_list_sessions"},
            },
            "required": ["session_key"],
        },
    },
    # --- Stage 2: Enrichment ---
    {
        "id": "lf_get_company",
        "name": "lf_get_company",
        "description": "Stage 2 Enrichment: get a single company record by numeric id. Calls GET /api/company-by-id/{id}. Returns full company fields including AI sanity status, quality score, email pattern, and pipeline_stage.",
        "parameters": {
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "Numeric company id (not Google place_id)"},
            },
            "required": ["company_id"],
        },
    },
    {
        "id": "lf_enrich_company",
        "name": "lf_enrich_company",
        "description": "Stage 2 Enrichment: AI-enrich a company (website, business type, email pattern, sanity check). Calls POST /api/company/{id}/ai-discover. Returns enrichment result and updated fields.",
        "parameters": {
            "type": "object",
            "properties": {
                "company_id": {"type": "integer", "description": "Numeric company id"},
            },
            "required": ["company_id"],
        },
    },
    # --- Stage 3: Contacts ---
    {
        "id": "lf_find_executives",
        "name": "lf_find_executives",
        "description": "Stage 3 Contacts: discover executive contacts for a company. Calls POST /api/discover/{place_id}/executives. Returns contacts with names, titles, LinkedIn URLs, and confidence scores.",
        "parameters": {
            "type": "object",
            "properties": {
                "place_id": {"type": "string", "description": "Google Places place_id"},
                "company_name": {"type": "string", "description": "Full company name"},
            },
            "required": ["place_id", "company_name"],
        },
    },
    {
        "id": "lf_visual_rag_search",
        "name": "lf_visual_rag_search",
        "description": "Stage 3 Contacts: visual RAG search via PixelRAG. Discovers a candidate company page (About/Team/Leadership) using SearXNG, then calls POST /api/pixelrag/extract to retrieve visually-matched screenshot tiles and text. Primary for leadership/executive discovery from rendered pages.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-form query, e.g. 'Varda Space Industries leadership'"},
                "n_docs": {"type": "integer", "description": "Number of top visual matches, default 10"},
                "include_images": {"type": "boolean", "description": "Include base64 tile PNGs, default false"},
            },
            "required": ["query"],
        },
    },
    # --- Stage 3.5: Verify ---
    {
        "id": "lf_verify_contact",
        "name": "lf_verify_contact",
        "description": "Stage 3.5 Verify: AI-verify a single contact (title, LinkedIn URL, email, phone, location). Calls POST /api/contact/{id}/verify-title synchronously (up to 180s). Use for one-off verification.",
        "parameters": {
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer", "description": "Numeric contact id"},
            },
            "required": ["contact_id"],
        },
    },
    {
        "id": "lf_verify_contacts_batch",
        "name": "lf_verify_contacts_batch",
        "description": "Stage 3.5 Verify: start an agentic batch verification job for many contacts. Calls POST /api/contacts/verify-batch and returns a job_id. Poll lf_get_discovery_job for progress. The server runs an AI+PixelRAG+SearXNG loop for each contact.",
        "parameters": {
            "type": "object",
            "properties": {
                "contact_ids": {"type": "array", "items": {"type": "integer"}, "description": "List of numeric contact ids to verify"},
            },
            "required": ["contact_ids"],
        },
    },
    # --- Cross-read / Cross-edit ---
    {
        "id": "lf_list_records",
        "name": "lf_list_records",
        "description": "Cross-stage read: list companies and contacts in one call. Calls GET /api/companies and GET /api/contacts and merges contacts under their company. Filter by industry, city, state, company_name, title, min_confidence, and limit.",
        "parameters": {
            "type": "object",
            "properties": {
                "industry": {"type": "string", "description": "Filter companies by business type (partial match)"},
                "city": {"type": "string", "description": "Filter by city (partial match)"},
                "state": {"type": "string", "description": "Filter by state (exact match, e.g. CA)"},
                "company_name": {"type": "string", "description": "Filter contacts by company name (partial match)"},
                "title": {"type": "string", "description": "Filter contacts by title (partial match)"},
                "min_confidence": {"type": "number", "description": "Minimum contact confidence 0.0-1.0, default 0.0"},
                "limit": {"type": "integer", "description": "Max records, default 100"},
            },
            "required": [],
        },
    },
    {
        "id": "lf_manage_record",
        "name": "lf_manage_record",
        "description": "Cross-stage edit: update or delete a contact or company. Calls PATCH/DELETE /api/contact/{id} or /api/company/{id}. For updates, pass the fields to change in updates. For deletes, action='delete'.",
        "parameters": {
            "type": "object",
            "properties": {
                "record_type": {"type": "string", "enum": ["contact", "company"], "description": "Which entity to edit"},
                "record_id": {"type": "integer", "description": "Numeric id"},
                "action": {"type": "string", "enum": ["update", "delete"], "description": "update or delete"},
                "updates": {"type": "object", "description": "Fields to update when action=update"},
            },
            "required": ["record_type", "record_id", "action"],
        },
    },
    # --- Stage 4+5: Email ---
    {
        "id": "lf_email_pipeline",
        "name": "lf_email_pipeline",
        "description": "Stage 4 Email Patterns + Stage 5 Validation: discover email pattern for a company, derive emails, validate a contact email, or view cache stats. Calls POST /api/company/{id}/discover-email-pattern, POST /api/company/{id}/derive-emails, POST /api/contact/{id}/validate-email, or GET /api/email/cache-stats.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["discover", "derive", "validate", "cache_stats"], "description": "Which email step to run"},
                "company_id": {"type": "integer", "description": "Required for discover/derive"},
                "contact_id": {"type": "integer", "description": "Required for validate"},
            },
            "required": ["action"],
        },
    },
    # --- Stage 6: Export ---
    {
        "id": "lf_export_session",
        "name": "lf_export_session",
        "description": "Stage 6 Export: export contacts CSV for a session and return ready-for-export stats. Calls GET /api/export/contacts/{key} and GET /api/email/ready-for-export. Returns CSV filename/content and counts of export-ready contacts.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_key": {"type": "string", "description": "Session key to export"},
            },
            "required": ["session_key"],
        },
    },
    # --- Cross: job polling ---
    {
        "id": "lf_get_discovery_job",
        "name": "lf_get_discovery_job",
        "description": "Cross-stage job polling: get one discovery/verify job by id, or list recent jobs. Calls GET /api/discover-job/{id} or GET /api/discover-jobs. Use to poll lf_import_companies, lf_find_executives, lf_enrich_company, and lf_verify_contacts_batch progress.",
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Job id to look up. If omitted, lists recent jobs."},
            },
            "required": [],
        },
    },
    # --- Optional observability (not counted in the 16) ---
    {
        "id": "lf_ai_usage",
        "name": "lf_ai_usage",
        "description": "Observability: return AI model usage stats. Calls GET /api/ai/usage and shows enabled models, model chain, and current-month usage by model/operation.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

# ============================================================================
# TOOL IMPLEMENTATION CODE
# (each value is a Python function body that runs inside the open-webui container)
# ============================================================================

# Inline shared helper pattern used by every tool below.
# It discovers the host gateway, loads the X-LF-Key from lf_config.json,
# and calls the lead-finder REST API. Returns parsed JSON or a structured error.

TOOL_CODE = {
    # --- Stage 1: Discovery ---
    "lf_search_companies": '''def lf_search_companies(industry, city, state="CA", radius_miles=25, max_results=20):
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 headers = {"X-LF-Key": api_key, "Content-Type": "application/json"}
 payload = {"industry": industry, "city": city, "state": state, "radius_miles": radius_miles, "max_results": max_results}
 try:
  r = requests.post(base + "/api/search", headers=headers, json=payload, timeout=30)
  if r.status_code >= 400:
   return {"error": "HTTP " + str(r.status_code) + ": " + r.text[:200]}
  return r.json()
 except Exception as e:
  return {"error": str(e)}
''',

    "lf_import_companies": '''def lf_import_companies(companies, session_name="import", industry="", city_default="", state_default="CA", auto_enrich=True):
 import json, os, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 headers = {"X-LF-Key": api_key, "Content-Type": "application/json"}
 csv_text = companies
 if isinstance(companies, str):
  path = companies.strip()
  lp = path.lower()
  if lp.endswith(".csv") and os.path.exists(path):
   with open(path) as f:
    csv_text = f.read()
  elif lp.endswith(".xlsx") and os.path.exists(path):
   try:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    rows = [[str(cell) if cell is not None else "" for cell in row] for row in ws.iter_rows(values_only=True)]
    if not rows:
     return {"error": "Excel file appears empty"}
    import csv as _csv, io as _io
    out = _io.StringIO()
    writer = _csv.writer(out)
    writer.writerows(rows)
    csv_text = out.getvalue()
   except ImportError:
    return {"error": "openpyxl is not available in this container; convert .xlsx to .csv"}
   except Exception as e:
    return {"error": "Failed to read Excel file: " + str(e)}
 payload = {"session_name": session_name, "industry": industry or session_name, "city_default": city_default, "state_default": state_default, "auto_enrich": bool(auto_enrich), "csv": csv_text}
 try:
  r = requests.post(base + "/api/import/csv", headers=headers, json=payload, timeout=120)
  if r.status_code >= 400:
   return {"error": "Import failed: HTTP " + str(r.status_code), "details": r.text[:500]}
  data = r.json()
 except Exception as e:
  return {"error": "Import request failed: " + str(e)}
 msg = "Imported " + str(data.get("total", 0)) + " companies into session '" + data.get("session_name", "") + "' (key: " + data.get("session_key", "") + "). " + str(data.get("inserted", 0)) + " new, " + str(data.get("reused", 0)) + " reused, " + str(data.get("failed", 0)) + " failed. "
 if data.get("geocode_failures"):
  msg += "Geocode failures: " + str(len(data["geocode_failures"])) + ". "
 if data.get("job_id"):
  msg += "Discovery job started: " + data["job_id"] + ". Poll lf_get_discovery_job for progress."
 else:
  msg += "No discovery job started."
 return {"summary": msg, "session_key": data.get("session_key"), "session_name": data.get("session_name"), "industry": data.get("industry"), "job_id": data.get("job_id"), "total": data.get("total"), "inserted": data.get("inserted"), "reused": data.get("reused"), "failed": data.get("failed"), "geocode_failures": data.get("geocode_failures", [])}
''',

    "lf_geocode_location": '''def lf_geocode_location(city, state="CA"):
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 try:
  r = requests.get(base + "/api/geocode", headers={"X-LF-Key": api_key}, params={"city": city, "state": state}, timeout=15)
  if r.status_code >= 400:
   return {"error": "HTTP " + str(r.status_code) + ": " + r.text[:200]}
  return r.json()
 except Exception as e:
  return {"error": str(e)}
''',

    "lf_list_sessions": '''def lf_list_sessions():
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 headers = {"X-LF-Key": api_key}
 try:
  sessions_raw = requests.get(base + "/api/sessions", headers=headers, timeout=30).json()
 except Exception as e:
  return {"error": "sessions request failed: " + str(e)}
 if isinstance(sessions_raw, list):
  sessions = sessions_raw
  total = len(sessions)
 else:
  sessions = sessions_raw.get("sessions", []) if isinstance(sessions_raw, dict) else []
  total = sessions_raw.get("total", len(sessions)) if isinstance(sessions_raw, dict) else len(sessions)
 try:
  stats = requests.get(base + "/api/stats", headers=headers, timeout=15).json()
 except Exception:
  stats = {}
 result = {"sessions": sessions, "total": total, "stats": stats}
 return result
''',

    "lf_get_session": '''def lf_get_session(session_key):
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 try:
  r = requests.get(base + "/api/session/" + session_key + "/results", headers={"X-LF-Key": api_key}, timeout=30)
  if r.status_code >= 400:
   return {"error": "HTTP " + str(r.status_code) + ": " + r.text[:200]}
  return r.json()
 except Exception as e:
  return {"error": str(e)}
''',

    # --- Stage 2: Enrichment ---
    "lf_get_company": '''def lf_get_company(company_id):
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 try:
  r = requests.get(base + "/api/company-by-id/" + str(company_id), headers={"X-LF-Key": api_key}, timeout=30)
  if r.status_code >= 400:
   return {"error": "HTTP " + str(r.status_code) + ": " + r.text[:200]}
  return r.json()
 except Exception as e:
  return {"error": str(e)}
''',

    "lf_enrich_company": '''def lf_enrich_company(company_id):
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 try:
  r = requests.post(base + "/api/company/" + str(company_id) + "/ai-discover", headers={"X-LF-Key": api_key, "Content-Type": "application/json"}, json={}, timeout=120)
  if r.status_code >= 400:
   return {"error": "HTTP " + str(r.status_code) + ": " + r.text[:200]}
  return r.json()
 except Exception as e:
  return {"error": str(e)}
''',

    # --- Stage 3: Contacts ---
    "lf_find_executives": '''def lf_find_executives(place_id, company_name):
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 try:
  r = requests.post(base + "/api/discover/" + place_id + "/executives", headers={"X-LF-Key": api_key, "Content-Type": "application/json"}, json={"company_name": company_name}, timeout=120)
  if r.status_code >= 400:
   return {"error": "HTTP " + str(r.status_code) + ": " + r.text[:200]}
  return r.json()
 except Exception as e:
  return {"error": str(e)}
''',

    "lf_visual_rag_search": '''def lf_visual_rag_search(query, n_docs=10, include_images=False):
 import json, re, socket, urllib.parse, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 headers = {"X-LF-Key": api_key}
 # Step 1: find candidate page via SearXNG
 candidate_url = ""
 search_queries = [query + " about leadership team", query + " team", query + " about", query]
 try:
  for sq in search_queries:
   r = requests.get("http://searxng:8080/search?q=" + urllib.parse.quote_plus(sq) + "&format=json", timeout=15)
   if r.status_code == 200:
    for item in r.json().get("results", [])[:10]:
     u = item.get("url", "")
     if any(k in u.lower() for k in ["/about", "/team", "/leadership", "/people", "/company"]):
      candidate_url = u
      break
   if candidate_url:
    break
 except Exception:
  pass
 # Note: if SearXNG container hostname is not reachable, this tool will fail.
 # In that environment use lf_search_companies / lf_find_executives instead.
 if not candidate_url:
  return {"error": "No candidate company page found for visual search"}
 # Step 2: call PixelRAG /extract proxy
 try:
  payload = {"url": candidate_url, "query": query, "top_k": min(n_docs, 10), "tile_height": 1568, "viewport_width": 1280, "quality": 85, "wait_seconds": 1.0}
  r = requests.post(base + "/api/pixelrag/extract", headers={"X-LF-Key": api_key, "Content-Type": "application/json"}, json=payload, timeout=180)
  if r.status_code >= 400:
   return {"error": "PixelRAG /extract HTTP " + str(r.status_code) + ": " + r.text[:200]}
  data = r.json()
  matches = data.get("matches", [])
  if not include_images:
   for m in matches:
    m.pop("image_b64", None)
  return {"url": candidate_url, "query": query, "match_count": len(matches), "matches": matches}
 except Exception as e:
  return {"error": "PixelRAG /extract unreachable: " + str(e)}
''',

    # --- Stage 3.5: Verify ---
    "lf_verify_contact": '''def lf_verify_contact(contact_id):
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 try:
  r = requests.post(base + "/api/contact/" + str(contact_id) + "/verify-title", headers={"X-LF-Key": api_key}, timeout=180)
  if r.status_code >= 400:
   return {"error": "HTTP " + str(r.status_code) + ": " + r.text[:200]}
  return r.json()
 except Exception as e:
  return {"error": str(e)}
''',

    "lf_verify_contacts_batch": '''def lf_verify_contacts_batch(contact_ids):
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 try:
  r = requests.post(base + "/api/contacts/verify-batch", headers={"X-LF-Key": api_key, "Content-Type": "application/json"}, json={"contact_ids": contact_ids}, timeout=30)
  if r.status_code >= 400:
   return {"error": "HTTP " + str(r.status_code) + ": " + r.text[:200]}
  return r.json()
 except Exception as e:
  return {"error": str(e)}
''',

    # --- Cross-read / Cross-edit ---
    "lf_list_records": '''def lf_list_records(industry="", city="", state="", company_name="", title="", min_confidence=0.0, limit=100):
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 headers = {"X-LF-Key": api_key}
 try:
  companies = requests.get(base + "/api/companies", headers=headers, params={"industry": industry, "city": city, "state": state, "limit": limit}, timeout=30).json().get("companies", [])
 except Exception as e:
  return {"error": "companies request failed: " + str(e)}
 try:
  contacts = requests.get(base + "/api/contacts", headers=headers, params={"company_name": company_name, "title": title, "city": city, "min_confidence": min_confidence, "limit": limit}, timeout=30).json().get("contacts", [])
 except Exception as e:
  return {"error": "contacts request failed: " + str(e)}
 company_map = {c.get("id"): c for c in companies}
 for c in contacts:
  cid = c.get("company_id")
  if cid in company_map:
   company_map[cid].setdefault("contacts", []).append(c)
 result = [v for v in company_map.values() if v.get("contacts") or not (company_name or title)]
 return {"records": result, "company_count": len(companies), "contact_count": len(contacts)}
''',

    "lf_manage_record": '''def lf_manage_record(record_type, record_id, action, updates=None):
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 path = "/api/" + record_type + "/" + str(record_id)
 headers = {"X-LF-Key": api_key}
 try:
  if action == "delete":
   r = requests.delete(base + path, headers=headers, timeout=30)
  else:
   r = requests.patch(base + path, headers={"X-LF-Key": api_key, "Content-Type": "application/json"}, json=updates or {}, timeout=30)
  if r.status_code >= 400:
   return {"error": "HTTP " + str(r.status_code) + ": " + r.text[:200]}
  try:
   return r.json()
  except Exception:
   return {"status": "ok", "detail": r.text[:200]}
 except Exception as e:
  return {"error": str(e)}
''',

    # --- Stage 4+5: Email ---
    "lf_email_pipeline": '''def lf_email_pipeline(action, company_id=None, contact_id=None):
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 headers = {"X-LF-Key": api_key}
 try:
  if action == "discover":
   if not company_id:
    return {"error": "company_id required for discover"}
   r = requests.post(base + "/api/company/" + str(company_id) + "/discover-email-pattern", headers={"X-LF-Key": api_key, "Content-Type": "application/json"}, json={}, timeout=120)
  elif action == "derive":
   if not company_id:
    return {"error": "company_id required for derive"}
   r = requests.post(base + "/api/company/" + str(company_id) + "/derive-emails", headers={"X-LF-Key": api_key}, timeout=120)
  elif action == "validate":
   if not contact_id:
    return {"error": "contact_id required for validate"}
   r = requests.post(base + "/api/contact/" + str(contact_id) + "/validate-email", headers={"X-LF-Key": api_key}, timeout=120)
  elif action == "cache_stats":
   r = requests.get(base + "/api/email/cache-stats", headers=headers, timeout=30)
  else:
   return {"error": "Unknown action: " + action}
  if r.status_code >= 400:
   return {"error": "HTTP " + str(r.status_code) + ": " + r.text[:200]}
  return r.json()
 except Exception as e:
  return {"error": str(e)}
''',

    # --- Stage 6: Export ---
    "lf_export_session": '''def lf_export_session(session_key):
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 headers = {"X-LF-Key": api_key}
 try:
  r = requests.get(base + "/api/export/contacts/" + session_key, headers=headers, timeout=30)
  csv_content = ""
  if r.status_code == 200:
   csv_content = r.text
  try:
   ready = requests.get(base + "/api/email/ready-for-export", headers=headers, timeout=30).json()
  except Exception:
   ready = {}
  return {"filename": "lf_contacts_" + session_key + ".csv", "csv_content": csv_content, "ready_for_export": ready}
 except Exception as e:
  return {"error": str(e)}
''',

    # --- Cross: job polling ---
    "lf_get_discovery_job": '''def lf_get_discovery_job(job_id=""):
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 try:
  if job_id:
   r = requests.get(base + "/api/discover-job/" + job_id, headers={"X-LF-Key": api_key}, timeout=15)
  else:
   r = requests.get(base + "/api/discover-jobs", headers={"X-LF-Key": api_key}, timeout=15)
  if r.status_code >= 400:
   return {"error": "HTTP " + str(r.status_code) + ": " + r.text[:200]}
  return r.json()
 except Exception as e:
  return {"error": str(e)}
''',

    # --- Optional observability ---
    "lf_ai_usage": '''def lf_ai_usage():
 import json, socket, requests
 api_key = ""
 try:
  with open("/lead-finder/lf_config.json") as f:
   cfg = json.load(f)
   api_key = cfg.get("lf_api_key", "")
 except Exception:
  pass
 candidates = []
 try:
  with open("/proc/net/route") as f:
   for line in f:
    parts = line.split()
    if len(parts) > 2 and parts[1] == "00000000":
     gw = socket.inet_ntoa(bytes.fromhex(parts[2])[::-1])
     candidates.append("http://" + gw + ":8798")
 except Exception:
  pass
 candidates.extend(["http://172.18.0.1:8798", "http://host.docker.internal:8798"])
 base = candidates[0] if candidates else "http://172.18.0.1:8798"
 for url in candidates:
  try:
   if requests.get(url + "/api/health", timeout=3).status_code == 200:
    base = url
    break
  except Exception:
   pass
 try:
  r = requests.get(base + "/api/ai/usage", headers={"X-LF-Key": api_key}, timeout=15)
  if r.status_code >= 400:
   return {"error": "HTTP " + str(r.status_code) + ": " + r.text[:200]}
  return r.json()
 except Exception as e:
  return {"error": str(e)}
''',
}


# ============================================================================
# REGISTRATION / LISTING / UNREGISTERING
# ============================================================================

def _run_base64_script(script: str, timeout: int = 30) -> tuple[str, int]:
    """Write script via base64 to /tmp in container, execute it, return (stdout, returncode)."""
    encoded = base64.b64encode(script.encode()).decode()
    write_cmd = [
        "docker", "exec", "-i", "open-webui",
        "python3", "-c",
        f"import sys,base64; open('/tmp/lf_script.py','wb').write(base64.b64decode('{encoded}'))"
    ]
    subprocess.run(write_cmd, capture_output=True, timeout=10)
    result = subprocess.run(
        ["docker", "exec", "open-webui", "python3", "/tmp/lf_script.py"],
        capture_output=True, text=True, timeout=timeout
    )
    subprocess.run(["docker", "exec", "open-webui", "rm", "-f", "/tmp/lf_script.py"],
                   capture_output=True, timeout=5)
    return result.stdout.strip(), result.returncode


def _register_one(tool_id: str, code: str, meta: str) -> None:
    code_json = json.dumps(code)
    meta_json = meta
    script = (
        "import sqlite3, json, time, uuid\n"
        "DB = \"/app/backend/data/webui.db\"\n"
        "conn = sqlite3.connect(DB)\n"
        "cur = conn.cursor()\n"
        "tool_id = \"" + tool_id + "\"\n"
        "meta_json = " + repr(meta_json) + "\n"
        "row = cur.execute(\"SELECT id FROM function WHERE name=?\", (tool_id,)).fetchone()\n"
        "if row:\n"
        "    cur.execute(\"UPDATE function SET content=?, meta=?, updated_at=?, is_active=1, is_global=1 WHERE name=?\",\n"
        "        (" + repr(code_json) + ", meta_json, int(time.time()), tool_id))\n"
        "    print(\"UPDATED:\" + tool_id)\n"
        "else:\n"
        "    now = int(time.time())\n"
        "    cur.execute(\"INSERT INTO function (id, user_id, name, type, content, meta, created_at, updated_at, valves, is_active, is_global) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\",\n"
        "        (str(uuid.uuid4()), \"1\", tool_id, \"tool\", " + repr(code_json) + ", meta_json, now, now, \"{}\", 1, 1))\n"
        "    print(\"OK:\" + tool_id)\n"
        "conn.commit()\n"
        "conn.close()\n"
    )
    stdout, rc = _run_base64_script(script)
    if rc != 0 or "ERROR" in stdout:
        print(f"  [FAIL] {tool_id}: {stdout}")
    elif "UPDATED" in stdout:
        print(f"  [UPDATED] {tool_id}")
    else:
        print(f"  [OK]   {tool_id} registered")


def register_tools():
    """Atomic unregister + register so existing tools are replaced cleanly."""
    print("Unregistering old lead-finder tools...")
    unregister_tools()
    print("Registering consolidated lead-finder Open WebUI tools...")
    for tool in TOOLS:
        tool_id = tool["id"]
        code = TOOL_CODE.get(tool_id, "")
        meta = json.dumps({
            "description": tool["description"],
            "parameters": tool["parameters"]
        })
        _register_one(tool_id, code, meta)


def list_tools():
    script = """
import sqlite3
DB = "/app/backend/data/webui.db"
conn = sqlite3.connect(DB)
cur = conn.cursor()
rows = cur.execute("SELECT id, name, type, is_active, is_global FROM function WHERE name LIKE 'lf_%' ORDER BY name").fetchall()
if not rows:
    print("No lf_ tools found.")
for r in rows:
    print("  id=%s, name=%s, type=%s, active=%s, global=%s" % (r[0], r[1], r[2], r[3], r[4]))
conn.close()
"""
    stdout, rc = _run_base64_script(script)
    if stdout:
        print(stdout)


def unregister_tools():
    script = """
import sqlite3
DB = "/app/backend/data/webui.db"
conn = sqlite3.connect(DB)
cur = conn.cursor()
rows = cur.execute("SELECT id, name FROM function WHERE name LIKE 'lf_%'").fetchall()
count = 0
for r in rows:
    print("  Deleting %s (id=%s)" % (r[1], r[0]))
    cur.execute("DELETE FROM function WHERE id=?", (r[0],))
    count += 1
conn.commit()
conn.close()
print("Deleted %s tool(s)." % count)
"""
    stdout, rc = _run_base64_script(script)
    if stdout:
        print(stdout)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "register":
        register_tools()
    elif cmd == "list":
        print("Registered lead-finder tools:")
        list_tools()
    elif cmd == "unregister":
        print("Removing lead-finder tools...")
        unregister_tools()
    else:
        print(f"Usage: python3 {sys.argv[0]} [register|list|unregister]")
