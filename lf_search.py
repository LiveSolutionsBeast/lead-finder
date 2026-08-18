#!/usr/bin/env python3
"""
lf_search.py - Lead Finder Company Search Module
================================================
Google Places Text Search API for discovering companies by industry + location.
Two-pass: pass 1 = companies (immediate), pass 2 = executives (background).
"""

import json
import re
import time, uuid, threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from lf_config import get, google_maps_api_key, rate_limit
from lf_geocode import get_location_bias, proximity_level, haversine
from lf_db import init_db, upsert_company, create_session, touch_session
from lf_db import get_session, get_session_companies, get_session_contacts, get_all_sessions

BASE_DIR = Path(__file__).parent
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

# Default business type queries
DEFAULT_QUERIES = [
    "manufacturing factory",
    "aerospace manufacturer",
    "machinery manufacturer",
    "metal fabrication",
    "food processing company",
    "packaging company",
    "electronics manufacturer",
    # User can also specify custom queries
]

SEARCH_DELAY = 5.0   # seconds between search calls (12/min = 5s)
DETAILS_DELAY = 3.3  # seconds between details calls (18/min)

# Thread-safe rate limiting for concurrent FastAPI requests (Bug LF-9)
_rate_lock = threading.Lock()
_last_search_time = 0.0
_last_details_time = 0.0


def _headers(api_key: str):
    return {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.id,places.displayName,places.shortFormattedAddress,places.formattedAddress,places.primaryType,places.location,places.addressComponents,places.internationalPhoneNumber,places.rating,places.userRatingCount",
        "Content-Type": "application/json",
    }


def text_search(api_key: str, query: str, lat: float, lng: float, radius_meters: int = 40234, max_results: int = 20) -> list:
    """
    Google Places Text Search. Returns list of raw place dicts.
    Thread-safe rate limiting via _rate_lock.
    """
    global _last_search_time
    payload = {
        "textQuery": query,
        "locationBias": {
            "circle": {"center": {"latitude": lat, "longitude": lng}, "radius": radius_meters}
        },
        "maxResultCount": max_results,
    }
    try:
        with _rate_lock:
            elapsed = time.time() - _last_search_time
            if elapsed < SEARCH_DELAY:
                time.sleep(SEARCH_DELAY - elapsed)
            resp = requests.post(
                PLACES_SEARCH_URL,
                json=payload,
                headers=_headers(api_key),
                timeout=15
            )
            _last_search_time = time.time()
        if resp.status_code != 200:
            print(f"[search] text_search HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
        return data.get("places", [])
    except Exception as e:
        print(f"[search] text_search error: {e}")
        return []


def get_place_details(api_key: str, place_id: str) -> Optional[dict]:
    """
    Fetch detailed info for a single place (address components, website, phone, rating).
    Thread-safe rate limiting via _rate_lock.
    """
    global _last_details_time
    url = f"https://places.googleapis.com/v1/places/{place_id}"
    params = {"key": api_key}
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "id,displayName,formattedAddress,addressComponents,websiteUri,internationalPhoneNumber,rating,userRatingCount,location",
    }
    try:
        with _rate_lock:
            elapsed = time.time() - _last_details_time
            if elapsed < DETAILS_DELAY:
                time.sleep(DETAILS_DELAY - elapsed)
            resp = requests.get(url, headers=headers, params=params, timeout=10)
            _last_details_time = time.time()
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        print(f"[search] get_place_details error: {e}")
        return None


def get_place_details_fast(api_key: str, place_id: str) -> Optional[dict]:
    """
    Faster variant of get_place_details that skips the thread-safe
    rate-limiting lock. Use this from a single worker (or in parallel
    via ThreadPoolExecutor with max_workers<=5) when you know the
    Google Places API quota can handle it (default quota is 600/min,
    our 3.3s single-thread delay is intentionally conservative).

    Google Places (New) supports concurrent requests up to your
    account's QPS limit. By default this is much higher than 18/min.
    """
    url = f"https://places.googleapis.com/v1/places/{place_id}"
    params = {"key": api_key}
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "id,displayName,formattedAddress,addressComponents,websiteUri,internationalPhoneNumber,rating,userRatingCount,location",
    }
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as e:
        print(f"[search] get_place_details_fast error: {e}")
        return None


# ── Domain Verification (Phase 1) ───────────────────────────────────────────

def _normalize_domain(url_or_domain: str) -> Optional[str]:
    """Extract bare email domain from a URL or domain string."""
    if not url_or_domain:
        return None
    s = url_or_domain.strip().lower()
    if not s:
        return None
    if "://" not in s:
        s = "http://" + s
    try:
        from urllib.parse import urlparse
        host = urlparse(s).hostname
    except Exception:
        host = None
    if not host:
        m = re.match(r"^(?:[^@/]+@)?([a-z0-9.-]+\.[a-z]{2,})$", url_or_domain.strip().lower())
        host = m.group(1) if m else None
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host


def _is_aggregator_domain(domain: str) -> bool:
    """Return True if domain is a known aggregator / 3rd-party directory."""
    if not domain:
        return True
    d = domain.lower().strip()
    return any(d == agg or d.endswith("." + agg) for agg in AGGREGATOR_DOMAINS)


def _mx_record_exists(domain: str, timeout: float = 5.0) -> bool:
    """Return True if the domain has one or more MX records."""
    if not domain:
        return False
    try:
        import dns.resolver
        answers = dns.resolver.resolve(domain, "MX", lifetime=timeout)
        return len(answers) > 0
    except Exception:
        return False


def _homepage_has_domain_emails(domain: str, timeout: float = 10.0) -> tuple[bool, list[str]]:
    """
    Fetch homepage and search for published email addresses at @domain.
    Returns (found_any, list_of_emails).
    """
    if not domain:
        return False, []
    emails: list[str] = []
    urls = [f"https://{domain}", f"http://{domain}"]
    regex = re.compile(r'\b([a-zA-Z0-9._-]+@' + re.escape(domain.lower()) + r')\b')
    for url in urls:
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0 (LeadFinder domain verifier)"})
            if resp.status_code == 200:
                text = resp.text
                found = regex.findall(text)
                for e in found:
                    e = e.lower()
                    if e not in emails:
                        emails.append(e)
                if emails:
                    return True, emails
        except Exception:
            continue
    return False, emails


def _search_published_emails(
    domain: str,
    company_name: str,
    timeout: int = 8,
    city: str = "",
    state: str = "",
    industry: str = "",
) -> tuple[bool, list[str]]:
    """Search the web for published @domain emails for this company."""
    if not domain:
        return False, []
    from lf_search_providers import search
    emails: list[str] = []
    queries = [
        f'"@{domain}" "{company_name}" email',
        f'"@{domain}" contact email',
        f'site:{domain} "@" email',
    ]
    # Add company-specific context queries when location is available.
    if company_name and city and state:
        queries.append(f'"{company_name}" "{city}, {state}" email')
        if industry:
            queries.append(f'"{company_name}" {industry} "{city}, {state}" email')
            queries.append(f'"{company_name}" manufacturer "{city}, {state}" email')
    regex = re.compile(r'\b([a-zA-Z0-9._-]+@' + re.escape(domain.lower()) + r')\b')
    for q in queries:
        try:
            results, _ = search(q, timeout=timeout, ai_extract=False)
            for r in results:
                text = " ".join(filter(None, [r.get("title", ""), r.get("snippet", ""), r.get("url", "")]))
                for e in regex.findall(text):
                    e = e.lower()
                    if e not in emails:
                        emails.append(e)
            if emails:
                return True, emails
        except Exception:
            continue
    return bool(emails), emails


def verify_company_domain(
    company_id: int,
    *,
    require_corroboration: bool = True,
    timeout: float = 30.0,
    places_api_key: Optional[str] = None,
) -> dict:
    """
    Phase 1: confirm a company's real email domain via Google Places + corroboration.

    Steps:
      1. Load company (name, city, state, stored website).
      2. Google Places Text Search for '"<name>" <city> <state>' (or with industry/business_type).
      3. Pick the result whose name best matches and has a non-aggregator websiteUri.
      4. Normalize websiteUri -> confirmed_domain.
      5. Cross-check: MX lookup OR published @domain emails on homepage OR web search.
      6. Compare with stored website domain; set email_domain_mismatch if they differ.
      7. Persist evidence on companies.{email_domain_confirmed, email_domain_source,
         email_domain_checked_at, email_domain_evidence, email_domain_mismatch}.

    Returns:
      {
        "company_id": int,
        "stored_domain": str | None,
        "confirmed_domain": str | None,
        "source": "google_places" | "stored" | "none",
        "mismatch": bool,
        "corroborated": bool,
        "evidence": dict,
        "updated": bool,
      }
    """
    from lf_db import get_db, get_company, patch_company

    company = get_company(company_id) or {}
    if not company:
        return {"company_id": company_id, "error": "Company not found"}

    name = (company.get("name") or "").strip()
    city = (company.get("city") or "").strip()
    state = (company.get("state") or "").strip()
    industry = (company.get("business_type") or "").strip()
    stored_website = (company.get("website") or "").strip()
    stored_domain = _normalize_domain(stored_website)

    api_key = places_api_key or google_maps_api_key()
    confirmed_domain: Optional[str] = None
    source = "none"
    evidence: dict = {
        "stored_domain": stored_domain,
        "google_places_candidates": [],
        "mx_ok": False,
        "homepage_emails": [],
        "search_emails": [],
        "corroboration_count": 0,
        "errors": [],
    }

    if api_key and name and city and state:
        # Build queries; prefer exact name + location, add industry disambiguation.
        queries = [f'"{name}" {city} {state}']
        if industry:
            queries.append(f'"{name}" {city} {state} {industry}')
        # Some company names include city; still try a clean variant.
        clean_name = re.sub(r"\s*,?\s*(Inc\.?|LLC|Corp\.?|Ltd\.?|Co\.)", "", name, flags=re.I).strip()
        if clean_name and clean_name != name:
            queries.append(f'"{clean_name}" {city} {state}')

        loc = get_location_bias(city, state, 25)
        lat, lng = (loc["lat"], loc["lng"]) if loc else (0.0, 0.0)
        radius_meters = int(25 * 1609.34)

        for q in queries:
            try:
                places = text_search(api_key, q, lat, lng, radius_meters, max_results=10)
                for p in places:
                    website_uri = (p.get("websiteUri") or "").strip()
                    if not website_uri:
                        continue
                    cand_domain = _normalize_domain(website_uri)
                    if not cand_domain or _is_aggregator_domain(cand_domain):
                        continue
                    evidence["google_places_candidates"].append({
                        "place_id": p.get("id"),
                        "name": p.get("displayName", {}).get("text") if isinstance(p.get("displayName"), dict) else p.get("displayName"),
                        "domain": cand_domain,
                        "website_uri": website_uri,
                    })
                    if not confirmed_domain:
                        confirmed_domain = cand_domain
                        source = "google_places"
                if confirmed_domain:
                    break
            except Exception as e:
                evidence["errors"].append(f"Places query failed: {q} -> {e}")

    # If no Places result, fall back to the stored domain if it looks real.
    if not confirmed_domain and stored_domain and not _is_aggregator_domain(stored_domain):
        confirmed_domain = stored_domain
        source = "stored"

    # Cross-check the candidate confirmed domain; also consider the stored domain
    # as an alternative when it has stronger corroboration (e.g. arrowheadproducts.com
    # vs .net). Pick the domain with the highest corroboration count; tie-break
    # toward Google Places result, then stored domain.
    def _score_domain(domain: Optional[str]) -> tuple[int, bool, list[str], bool, list[str]]:
        if not domain:
            return -1, False, [], False, []
        mx_ok = _mx_record_exists(domain)
        has_homepage, homepage_emails = _homepage_has_domain_emails(domain)
        has_search, search_emails = _search_published_emails(
            domain, name, city=city, state=state, industry=industry
        )
        score = sum([mx_ok, has_homepage, has_search])
        return score, mx_ok, homepage_emails, has_search, search_emails

    candidate_domains: list[tuple[str, str]] = []
    if confirmed_domain:
        candidate_domains.append((confirmed_domain, source))
    if stored_domain and stored_domain != confirmed_domain and not _is_aggregator_domain(stored_domain):
        candidate_domains.append((stored_domain, "stored_alternative"))

    best_domain: Optional[str] = None
    best_source = "none"
    best_score = -1
    best_mx_ok = False
    best_has_homepage = False
    best_homepage_emails: list[str] = []
    best_has_search = False
    best_search_emails: list[str] = []
    for cand, cand_source in candidate_domains:
        score, mx_ok, homepage_emails, has_search, search_emails = _score_domain(cand)
        if score > best_score:
            best_score = score
            best_domain = cand
            best_source = cand_source
            best_mx_ok = mx_ok
            best_has_homepage = bool(homepage_emails)
            best_homepage_emails = homepage_emails
            best_has_search = has_search
            best_search_emails = search_emails

    # Tie-break: prefer Google Places over stored alternative; prefer
    # stored alternative only when it strictly outscores.
    if best_domain == confirmed_domain and best_source != source:
        best_source = source

    confirmed_domain = best_domain
    source = best_source
    evidence["homepage_emails"] = best_homepage_emails
    evidence["search_emails"] = best_search_emails
    evidence["mx_ok"] = best_mx_ok
    evidence["homepage_emails_found"] = best_has_homepage
    evidence["search_emails_found"] = best_has_search
    evidence["corroboration_count"] = best_score

    corroborated = False
    if confirmed_domain:
        corroborated = not require_corroboration or best_score >= 1
        mismatch = bool(stored_domain and stored_domain != confirmed_domain)
        evidence["mismatch"] = mismatch

        now = datetime.now(timezone.utc).isoformat()
        evidence_json = json.dumps(evidence, default=str)
        update = {
            "email_domain_confirmed": confirmed_domain,
            "email_domain_source": source,
            "email_domain_checked_at": now,
            "email_domain_evidence": evidence_json,
            "email_domain_mismatch": 1 if mismatch else 0,
        }
        # Also update website to the confirmed domain when mismatched or when
        # the stored domain was empty.
        if mismatch or not stored_domain:
            update["website"] = f"https://{confirmed_domain}"
            old_notes = (company.get("manual_notes") or "").strip()
            note = f"[domain-verify] website changed from {stored_website} ({stored_domain}) to {confirmed_domain} at {now}"
            update["manual_notes"] = f"{old_notes}\n{note}".strip() if old_notes else note
        patch_company(company_id, update)
        return {
            "company_id": company_id,
            "stored_domain": stored_domain,
            "confirmed_domain": confirmed_domain,
            "source": source,
            "mismatch": mismatch,
            "corroborated": corroborated,
            "evidence": evidence,
            "updated": True,
        }

    # No confirmed domain at all.
    now = datetime.now(timezone.utc).isoformat()
    evidence_json = json.dumps(evidence, default=str)
    patch_company(company_id, {
        "email_domain_confirmed": None,
        "email_domain_source": None,
        "email_domain_checked_at": now,
        "email_domain_evidence": evidence_json,
        "email_domain_mismatch": 0,
    })
    return {
        "company_id": company_id,
        "stored_domain": stored_domain,
        "confirmed_domain": None,
        "source": source,
        "mismatch": False,
        "corroborated": False,
        "evidence": evidence,
        "updated": True,
        "error": "Could not confirm a real email domain",
    }


# ── Quality Scoring & Filtering (added 2026-06-09, QC-15) ────────────────────
# Generic/low-quality business types from Google Places
GENERIC_BIZ_TYPES = {
    "point_of_interest", "establishment", "store", "food", "lodging",
    "local_business", "place_of_worship", "church", "school", "university",
    "hospital", "pharmacy", "bank", "atm", "gas_station", "parking",
    "real_estate_agency", "travel_agency", "insurance_agency", "lawyer",
    "beauty_salon", "hair_care", "spa", "gym", "museum", "zoo",
    "aquarium", "amusement_park", "campground", "rv_park", "cemetery",
    "funeral_home", "moving_company", "laundry", "car_wash", "car_rental",
    "car_repair", "car_dealer", "locksmith", "plumber", "electrician",
    "roofing_contractor", "painter", "pest_control_service",
}

# Aggregator/3rd-party domains (not real company websites)
AGGREGATOR_DOMAINS = {
    "yelp.com", "facebook.com", "linkedin.com", "google.com", "googleadservices.com",
    "mapquest.com", "yellowpages.com", "bbb.org", "manta.com", "superpages.com",
    "merchantcircle.com", "citysearch.com", "tripadvisor.com", "foursquare.com",
    "angi.com", "thumbtack.com", "homeadvisor.com", "houzz.com", "alignable.com",
    "chamberofcommerce.com", "bizapedia.com", "buzzfile.com", "inc.com",
    "crunchbase.com", "wikipedia.org", "dnb.com", "dandb.com",
}


def _domain_quality_penalty(domain: str) -> float:
    """Return a penalty (0.0-0.15) for low-quality domains. Lower = better."""
    if not domain:
        return 0.15  # no website = biggest penalty
    d = domain.lower().strip()
    for agg in AGGREGATOR_DOMAINS:
        if agg in d:
            return 0.12  # aggregator
    # Free website builders (low credibility)
    free_builders = ["wix.com", "weebly.com", "wordpress.com", "blogspot.com",
                     "squarespace.com", "webs.com", "jimdo.com", "godaddysites.com",
                     "sites.google.com", "carrd.co"]
    for fb in free_builders:
        if fb in d:
            return 0.05  # free builder, lower signal
    return 0.0  # looks like a real company domain


def compute_quality_score(
    rating: Optional[float] = None,
    user_rating_count: Optional[int] = None,
    website: str = "",
    phone: str = "",
    business_type: str = "",
) -> float:
    """
    Compute company quality score 0.0-1.0 from public signals.

    Components (max 1.0):
      - Rating: 0.0-0.4 (scaled from 0-5 stars, 5 stars = 0.4)
      - Reviews: 0.0-0.3 (log-scaled: 5=0.15, 25=0.22, 100+=0.30)
      - Domain quality: 0.0-0.15 (real domain = 0.15, aggregator = 0.03, none = 0.0)
      - Phone presence: 0.0 or 0.1
      - Business type specificity: 0.0 or 0.05
    """
    score = 0.0

    # Rating component (0-5 stars → 0.0-0.4)
    if rating is not None and rating > 0:
        score += min(0.4, (rating / 5.0) * 0.4)

    # Review count component (log-scaled)
    if user_rating_count is not None and user_rating_count > 0:
        import math
        # log10(1) = 0, log10(10) = 1, log10(100) = 2, log10(1000) = 3
        log_reviews = math.log10(max(1, user_rating_count))
        # 0 reviews = 0, 5 reviews = 0.15, 25 reviews = 0.22, 100+ reviews = 0.30
        score += min(0.3, log_reviews * 0.15)

    # Domain quality (0.0-0.15)
    if website:
        domain = ""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(website if "://" in website else f"http://{website}")
            domain = parsed.hostname or ""
            if domain.startswith("www."):
                domain = domain[4:]
        except Exception:
            domain = website
        penalty = _domain_quality_penalty(domain)
        score += max(0.0, 0.15 - penalty)
    else:
        # No website gets a flat 0.02 (not zero — some legit companies only have phone)
        score += 0.02

    # Phone presence (0.0 or 0.1)
    if phone and phone.strip():
        score += 0.1

    # Business type specificity (0.0 or 0.05)
    if business_type and business_type.lower() not in GENERIC_BIZ_TYPES:
        score += 0.05

    return round(min(1.0, score), 3)


def passes_quality_filter(
    p: dict,
    min_rating: float = 3.0,
    min_reviews: int = 0,
    require_website_or_phone: bool = False,
) -> bool:
    """
    Hard pre-filter: should we even consider this place?
    Returns True if the place passes the minimum quality bar.
    """
    rating = p.get("rating")
    if rating is not None and rating < min_rating:
        return False

    reviews = p.get("userRatingCount") or 0
    if reviews < min_reviews:
        return False

    if require_website_or_phone:
        has_website = bool(p.get("websiteUri") or p.get("website"))
        has_phone = bool(p.get("internationalPhoneNumber") or p.get("phone"))
        if not (has_website or has_phone):
            return False

    return True


def _verify_company_by_proximity(
    suggestions: list[dict],
    industry: str,
    city: str,
    state: str = "CA",
    radius_miles: int = 25,
    max_results_per_query: int = 20,
    extra_queries: list = None,
) -> list[dict]:
    """
    Google Maps proximity verifier. Issue #2 — chain stage 2.

    For each AI-suggested company name, look it up via Google Places Text
    Search and filter by haversine distance from the search center.
    Also runs the plain industry+city query to catch companies the AI
    didn't know about. Returns a list of verified place dicts (place_id,
    name, lat, lng, address, rating, etc.) within the radius.

    Inputs:
      suggestions: list of {"name": str, ...} from ai_research_companies
      industry, city, state, radius_miles: search parameters
      max_results_per_query, extra_queries: standard Google Places tuning

    Returns:
      list of Google Places place dicts (raw) within the radius.
    """
    api_key = google_maps_api_key()
    if not api_key:
        return []

    loc = get_location_bias(city, state, radius_miles)
    if not loc:
        return []
    lat, lng = loc["lat"], loc["lng"]
    radius_meters = int(radius_miles * 1609.34)

    # Build query list: explicit industry query + extras + top AI suggestions
    # (capped to 5 to keep the search fast — each query costs ~5s due to
    # Google Places rate limiting; 5 suggestions + 1 industry query = ~30s
    # max in the verify stage.)
    queries = [industry]
    if extra_queries:
        queries.extend(extra_queries)
    # Sort AI suggestions by confidence desc, take top 5
    top_suggestions = sorted(
        (s for s in (suggestions or []) if s.get("name") and len(s.get("name", "")) >= 3),
        key=lambda s: s.get("confidence", 0),
        reverse=True,
    )[:5]
    for s in top_suggestions:
        queries.append(f"{s['name']} {city}")
    # Dedupe while preserving order
    queries = list(dict.fromkeys(queries))

    all_places = []
    seen_place_ids = set()

    for q in queries:
        places = text_search(api_key, q, lat, lng, radius_meters, max_results_per_query)
        for p in places:
            pid = p.get("id", "")
            if pid and pid not in seen_place_ids:
                # Filter by proximity (haversine)
                loc2 = p.get("location", {})
                plat = loc2.get("latitude") if loc2 else None
                plng = loc2.get("longitude") if loc2 else None
                if plat is None or plng is None:
                    continue
                dist = haversine(lat, lng, float(plat), float(plng))
                if dist > radius_miles:
                    continue
                # Filter by name: prefer exact name matches (so AI suggestions
                # are verified precisely), but allow fuzzy matches
                seen_place_ids.add(pid)
                p["_distance_miles"] = round(dist, 2)
                all_places.append(p)
        time.sleep(SEARCH_DELAY)

    return all_places


def search_companies(
    industry: str,
    city: str,
    state: str = "CA",
    radius_miles: int = 25,
    max_results_per_query: int = 20,
    extra_queries: list = None,
    min_rating: float = 3.0,
    min_reviews: int = 0,
    require_website_or_phone: bool = False,
    min_quality_score: float = 0.0,
) -> dict:
    """
    Main company search entry point.
    Returns {'session_key', 'companies': [list], 'location': dict, 'pipeline_log': [list]}.
    Two-pass: pass 1 returns companies immediately.

    Issue #2: This now runs the explicit COMPANY_SEARCH chain from
    lf_pipeline.py:
      1. AI research (PRIMARY)
      2. Google Maps verify by proximity (PRIMARY)
      3. Google Maps details (PRIMARY)
      4. AI website validation (fallback for stage 3)
      5. AI business type normalize (fallback for stage 3)

    PixelRAG is NOT in this chain (per user feedback — poor for company
    search). It remains available as an opt-in visual enrichment tool.

    Quality filters (added 2026-06-09, QC-15):
      - min_rating: hard filter, default 3.0
      - min_reviews: hard filter, default 0 (no minimum)
      - require_website_or_phone: hard filter, default False
      - min_quality_score: soft filter, default 0.0 (no minimum)
    """
    from lf_config import get as cfg_get
    use_chain = cfg_get("search_use_pipeline", True)
    api_key = google_maps_api_key()
    if not api_key:
        return {"error": "No Google Maps API key configured"}

    # Get location bias
    loc = get_location_bias(city, state, radius_miles)
    if not loc:
        return {"error": f"Could not geocode {city}, {state}"}

    lat, lng = loc["lat"], loc["lng"]
    radius_meters = int(radius_miles * 1609.34)

    session_key = str(uuid.uuid4())[:8]
    create_session(session_key, industry, city, state, "", radius_miles)

    # Run the explicit search chain (or fall back to the legacy
    # Google-only path if config disables it).
    if use_chain:
        try:
            from lf_pipeline import search_companies_chain
            pipeline_result, ctx = search_companies_chain(
                industry=industry, city=city, state=state,
                radius_miles=radius_miles,
                max_results_per_query=max_results_per_query,
                extra_queries=extra_queries,
            )
            all_places = ctx.get("verified_places", [])
            pipeline_log = [s.to_dict() for s in pipeline_result.stages]
            # If the chain failed entirely (no verified places), fall
            # back to the legacy Google-only path so the user still gets
            # results.
            if not all_places:
                print("[search] chain returned no places, falling back to legacy Google-only path")
                all_places = _legacy_google_search(
                    industry=industry, city=city, state=state,
                    radius_meters=radius_meters, max_results=max_results_per_query,
                    extra_queries=extra_queries, lat=lat, lng=lng, api_key=api_key,
                )
        except Exception as e:
            print(f"[search] chain error: {e}; falling back to legacy path")
            all_places = _legacy_google_search(
                industry=industry, city=city, state=state,
                radius_meters=radius_meters, max_results=max_results_per_query,
                extra_queries=extra_queries, lat=lat, lng=lng, api_key=api_key,
            )
            pipeline_log = [{"stage": "chain_error", "status": "failed", "error": str(e)}]
    else:
        all_places = _legacy_google_search(
            industry=industry, city=city, state=state,
            radius_meters=radius_meters, max_results=max_results_per_query,
            extra_queries=extra_queries, lat=lat, lng=lng, api_key=api_key,
        )
        pipeline_log = [{"stage": "legacy_path", "status": "complete", "detail": "search_use_pipeline=false"}]

    # Apply hard quality filters (rating / reviews / website-or-phone)
    filtered_count = 0
    quality_filtered = 0
    filtered_places = []
    for p in all_places:
        if not passes_quality_filter(p, min_rating, min_reviews, require_website_or_phone):
            filtered_count += 1
            continue
        filtered_places.append(p)

    # Save to DB
    saved_companies = []
    for p in filtered_places:
        name = p.get("displayName", {}).get("text", p.get("name", "Unknown"))
        pid = p.get("id", "") or p.get("place_id", "")
        addr = p.get("formattedAddress", "") or p.get("shortFormattedAddress", "")
        btype = p.get("primaryType", "") or p.get("business_type", "") or p.get("canonical_business_type", "")
        website = p.get("website") or p.get("websiteUri", "") or ""
        phone = p.get("internationalPhoneNumber", "") or p.get("phone", "")
        rating = p.get("rating")
        user_count = p.get("userRatingCount") or p.get("user_rating_count")
        loc2 = p.get("location", {})
        plat = loc2.get("latitude", lat) if loc2 else lat
        plng = loc2.get("longitude", lng) if loc2 else lng

        # Estimate distance from search center
        dist = p.get("_distance_miles")
        if dist is None:
            dist = haversine(lat, lng, float(plat), float(plng))
        tier = proximity_level(dist)

        # Parse address components
        # Address format: "225 S Aviation Blvd, El Segundo, CA 90245, USA"
        street = city_s = state_s = postal = ""
        if addr:
            parts = [pp.strip() for pp in addr.split(",")]
            street = parts[0] if parts else ""
            if len(parts) >= 2:
                second_last = parts[-2] if len(parts) > 2 else ""
                if " " in second_last:
                    state_candidate, postal_candidate = second_last.rsplit(" ", 1)
                    if len(state_candidate) == 2 and state_candidate.isalpha():
                        state_s = state_candidate
                        postal = postal_candidate
                        if len(parts) >= 3:
                            city_s = parts[-3].strip() if len(parts) >= 3 else ""
                    else:
                        city_s = second_last
                        postal = parts[-1].strip()
                        state_s = parts[-2].strip() if len(parts) >= 2 else ""
                elif len(parts) == 3:
                    city_s = parts[-2].strip()
                    postal = parts[-1].strip()
                    state_s = ""
                elif len(parts) == 2:
                    city_s = parts[-1].strip()
                    postal = ""
                    state_s = ""

        data = {
            "place_id": pid,
            "name": name,
            "street": street,
            "city": city_s or state,
            "state": state_s or state,
            "postal_code": postal,
            "country": "USA",
            "lat": plat,
            "lng": plng,
            "business_type": btype,
            "website": website,
            "phone": phone,
            "rating": rating,
            "user_rating_count": user_count,
            "hq_location": "",
            "is_local_contact": 1 if tier <= 5 else 0,
            "source": "PIPELINE_AI_MAPS" if use_chain else "GOOGLE_PLACES",
            "search_query": industry,
            "found_at": "",
            "confidence_score": 1.0,
            "data_provenance": "PIPELINE_CHAIN" if use_chain else "GOOGLE_PLACES_TEXT_SEARCH",
        }
        # Compute initial quality score
        qs = compute_quality_score(
            rating=rating,
            user_rating_count=user_count,
            website=website,
            phone=phone,
            business_type=btype,
        )
        data["quality_score"] = qs
        if qs < min_quality_score:
            quality_filtered += 1
            continue
        cid = upsert_company(data)
        data["id"] = cid
        saved_companies.append(data)

    touch_session(session_key)

    return {
        "session_key": session_key,
        "industry": industry,
        "city": city,
        "state": state,
        "radius_miles": radius_miles,
        "location": loc,
        "companies": saved_companies,
        "total_found": len(saved_companies),
        "filtered_by_quality": filtered_count,
        "filtered_by_score": quality_filtered,
        "websites_enriched": len([p for p in saved_companies if p.get("website")]),
        "pipeline_log": pipeline_log,
    }


def _legacy_google_search(
    industry: str,
    city: str,
    state: str,
    radius_meters: int,
    max_results: int,
    extra_queries: list,
    lat: float,
    lng: float,
    api_key: str,
) -> list[dict]:
    """
    Legacy company search path (used when pipeline is disabled or fails).
    Same as the pre-pipeline behavior — just Google Places Text Search.
    """
    queries = [industry]
    if extra_queries:
        queries.extend(extra_queries)
    queries = list(dict.fromkeys(queries))

    all_places = []
    seen_place_ids = set()
    for q in queries:
        print(f"[search] (legacy) Query: '{q}' near {city}, {state}")
        places = text_search(api_key, q, lat, lng, radius_meters, max_results)
        for p in places:
            pid = p.get("id", "")
            if pid and pid not in seen_place_ids:
                seen_place_ids.add(pid)
                all_places.append(p)
        time.sleep(SEARCH_DELAY)
    return all_places


def enrich_company_details(place_id: str) -> Optional[dict]:
    """
    Fetch and update a single company with full details.
    """
    api_key = google_maps_api_key()
    if not api_key:
        return None

    details = get_place_details(api_key, place_id)
    if not details:
        return None

    # Extract address components
    addr_comps = {}
    for comp in details.get("addressComponents", []):
        for t in comp.get("types", []):
            addr_comps[t] = comp.get("longText", "")

    from lf_db import get_db
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE companies SET
            street=?, city=?, state=?, postal_code=?,
            lat=?, lng=?, website=?, phone=?, rating=?, user_rating_count=?
        WHERE place_id=?
    """, (
        addr_comps.get("street_address", ""),
        addr_comps.get("locality", ""),
        addr_comps.get("administrative_area_level_1", ""),
        addr_comps.get("postal_code", ""),
        details.get("location", {}).get("latitude"),
        details.get("location", {}).get("longitude"),
        details.get("websiteUri", "") or details.get("website", ""),
        details.get("internationalPhoneNumber", ""),
        details.get("rating"),
        details.get("userRatingCount"),
        place_id,
    ))
    conn.commit()
    conn.close()

    time.sleep(DETAILS_DELAY)
    return details


def enrich_companies_batch(place_ids: list[str]) -> dict:
    """
    Batch-enrich multiple companies with full details.
    Calls get_place_details() for each company with rate limiting.
    Returns {'enriched': int, 'failed': int, 'results': [{place_id, website, ...}]}
    """
    api_key = google_maps_api_key()
    if not api_key:
        return {"enriched": 0, "failed": len(place_ids), "results": []}

    results = []
    enriched = 0
    failed = 0

    for pid in place_ids:
        details = enrich_company_details(pid)
        if details:
            enriched += 1
            results.append({
                "place_id": pid,
                "website": details.get("websiteUri", "") or details.get("website", ""),
                "phone": details.get("internationalPhoneNumber", ""),
                "rating": details.get("rating"),
                "status": "enriched",
            })
        else:
            failed += 1
            results.append({
                "place_id": pid,
                "website": "",
                "phone": "",
                "rating": None,
                "status": "failed",
            })

    return {"enriched": enriched, "failed": failed, "results": results}
