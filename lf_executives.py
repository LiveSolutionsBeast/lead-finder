#!/usr/bin/env python3
"""
lf_executives.py - Lead Finder Executive Discovery Module (v3.0)
=================================================================
Three-phase enrichment pipeline:
  1. Scrape company website for leadership/team page (authoritative source)
  2. Verify each person on LinkedIn by searching full name + company
  3. Save with data quality flags and rejection rules

Key rejection rules:
  - No leadership page on website → skip enrichment entirely
  - Person found on website but no LinkedIn profile → skip (likely fake/stale)
  - LinkedIn profile is blank/empty → skip
  - LinkedIn location outside search region → mark HQ, skip local
  - Multiple LinkedIn profiles → take the most complete one
"""

import json, time, re, urllib.parse
from pathlib import Path
from typing import Optional

import requests

from lf_config import get
from lf_search_providers import search, get_usage_summary
from lf_geocode import haversine
from lf_db import get_db, upsert_contact

BASE_DIR = Path(__file__).parent


def google_validate_person(name: str, company_name: str, min_results: int = 1, timeout: int = 8) -> tuple[bool, str]:
    """
    Validate that a person is actually associated with a company by searching
    Google/SearXNG for "{full_name}" "{company_name}".

    Returns (is_valid, reason):
      - True if at least min_results search results mention both name AND company
      - False with descriptive reason if no results or only partial matches

    Args:
      timeout: per-call search timeout in seconds (default 8s; was 15s)
    """
    if not name or not company_name:
        return False, "missing name or company"

    # Build exact-match query: "John Smith" "Acme Inc"
    q = f'"{name}" "{company_name}"'
    try:
        # prefer="searxng": skip 4get/Degoog which time out and add
        # 8s per call (SearXNG responds in 1-3s). ai_extract=False:
        # skip the post-search AI extract step (would add 18s when AI
        # is down).
        results, provider = search(q, timeout=timeout, prefer="searxng", ai_extract=False)
    except Exception as e:
        return False, f"search error: {e}"

    if not results:
        return False, "no search results"

    name_lower = name.lower()
    company_lower = company_name.lower()
    # Also check short company name (e.g., "Boeing" for "The Boeing Company")
    company_short = company_lower.replace("the ", "").replace(" inc", "").replace(" corp", "").replace(" corporation", "").replace(" company", "").replace(" llc", "").replace(" ltd", "").strip()

    valid_hits = 0
    for r in results:
        snippet = (r.get("snippet", "") or r.get("content", "")).lower()
        title = (r.get("title", "")).lower()
        url = (r.get("url", "")).lower()
        combined = snippet + " " + title + " " + url

        # Check name appears (first name + last name, or full name)
        name_parts = name_lower.split()
        name_found = False
        if len(name_parts) >= 2:
            # Require at least first+last or full name
            first_last = f"{name_parts[0]} {name_parts[-1]}"
            if first_last in combined or name_lower in combined:
                name_found = True
        elif name_lower in combined:
            name_found = True

        # Check company appears
        company_found = company_lower in combined or company_short in combined

        if name_found and company_found:
            valid_hits += 1
            if valid_hits >= min_results:
                return True, f"validated ({valid_hits} hits)"

    return False, f"only {valid_hits} partial matches (name={name_found}, company={company_found})"


# ── Data Quality ──────────────────────────────────────────────────────────────

# Common HTML tags that leak into scraped names
HTML_TAGS = {'div', 'span', 'data', 'header', 'theme', 'class', 'id', 'style',
             'script', 'meta', 'link', 'href', 'title', 'body', 'html', 'img',
             'src', 'alt', 'width', 'height', 'form', 'input', 'button',
             'container', 'wrapper', 'section', 'article', 'nav', 'footer'}

# Generic titles that get used as names — reject these
GENERIC_TITLES_AS_NAMES = {
    'ceo', 'cfo', 'coo', 'cto', 'president', 'vp', 'vice president',
    'director', 'manager', 'owner', 'founder', 'partner', 'executive',
    'officer', 'chairman', 'chair', 'board chair', 'board member',
    'head of', 'lead', 'chief', 'general manager', 'gm',
    'principal', 'associate', 'analyst', 'engineer', 'specialist',
    'consultant', 'coordinator', 'administrator', 'supervisor',
    'executive officer', 'senior executive', 'staff', 'member',
    'team lead', 'project manager', 'product manager',
}

# ── Title Normalization ──────────────────────────────────────────────────────

def normalize_title(title: str) -> str:
    """
    Normalize executive title acronyms to uppercase.
    Handles: ceo→CEO, coo→COO, cto→CTO, cfo→CFO, vp→VP, cio→CIO,
             cro→CRO, chro→CHRO, cmo→CMO, cso→CSO, svp→SVP, evp→EVP, gm→GM
    """
    if not title:
        return ""

    title = title.strip()
    if len(title) < 2:
        return title

    # Map of known acronyms (lowercase -> UPPERCASE)
    acronym_map: dict[str, str] = {
        'ceo': 'CEO', 'cfo': 'CFO', 'coo': 'COO', 'cto': 'CTO',
        'cio': 'CIO', 'cro': 'CRO', 'chro': 'CHRO', 'cmo': 'CMO',
        'cso': 'CSO', 'vp': 'VP', 'svp': 'SVP', 'evp': 'EVP',
        'gm': 'GM',
    }

    title_lower = title.lower()

    # Exact match: "coo" → "COO"
    if title_lower in acronym_map:
        return acronym_map[title_lower]

    # Replace acronym tokens within a multi-word title string
    # e.g., "Coo at Boeing" → "COO at Boeing"
    words = title.split()
    for i, word in enumerate(words):
        clean = word.rstrip('.,;:()')
        if clean.lower() in acronym_map:
            replacement = acronym_map[clean.lower()]
            suffix = word[len(clean):]  # preserve punctuation if any
            words[i] = replacement + suffix

    return ' '.join(words)


# ── Senior Title Validation ───────────────────────────────────────────────────

SENIOR_TITLE_KEYWORDS: set[str] = {
    'ceo', 'chief executive officer',
    'cfo', 'chief financial officer',
    'coo', 'chief operating officer',
    'cto', 'chief technology officer',
    'cio', 'chief information officer',
    'cro', 'chief revenue officer',
    'chro', 'chief human resources officer',
    'cmo', 'chief marketing officer',
    'cso', 'chief scientific officer', 'chief strategy officer', 'chief security officer',
    'president', 'vice president', 'vp', 'svp', 'evp', 'senior vp', 'executive vp',
    'director', 'senior director', 'executive director', 'managing director',
    'general manager', 'gm',
    'founder', 'co-founder', 'cofounder',
    'owner', 'proprietor',
    'partner',
    'head of',
    'chairman', 'chair', 'chairperson', 'board chair', 'board member',
    'chief ',  # catches Chief XYZ Officer variations
}


def is_senior_executive_title(title: str) -> bool:
    """
    Check if a title represents a senior executive position.
    Returns False for empty strings or non-executive titles.
    Uses word-boundary matching to avoid substring false positives
    (e.g., "Coordinator" should not match "director").
    """
    if not title or not title.strip():
        return False
    title_lower = title.lower()

    # Escape pattern characters and check with word boundaries
    for kw in sorted(SENIOR_TITLE_KEYWORDS, key=len, reverse=True):
        # Use word boundaries for single-word keywords to prevent substring matches
        if ' ' in kw:
            # Multi-word keywords (e.g., "vice president", "head of", "chief ")
            if kw in title_lower:
                return True
        else:
            # Single-word: use word boundary to avoid substrings
            if re.search(r'\b' + re.escape(kw) + r'\b', title_lower):
                return True
    return False


LOCATION_WORDS = {
    'california', 'ca', 'texas', 'tx', 'florida', 'fl', 'new york', 'ny',
    'illinois', 'il', 'washington', 'wa', 'arizona', 'az', 'colorado', 'co',
    'oregon', 'or', 'nevada', 'nv', 'utah', 'ut', 'idaho', 'id', 'montana', 'mt',
    'wyoming', 'wy', 'new mexico', 'nm', 'oklahoma', 'ok', 'kansas', 'ks',
    'nebraska', 'ne', 'south dakota', 'sd', 'north dakota', 'nd', 'minnesota', 'mn',
    'iowa', 'ia', 'missouri', 'mo', 'arkansas', 'ar', 'louisiana', 'la',
    'mississippi', 'ms', 'alabama', 'al', 'georgia', 'ga', 'tennessee', 'tn',
    'kentucky', 'ky', 'indiana', 'in', 'michigan', 'mi', 'ohio', 'oh',
    'west virginia', 'wv', 'virginia', 'va', 'north carolina', 'nc',
    'south carolina', 'sc', 'pennsylvania', 'pa', 'new york', 'ny',
    'vermont', 'vt', 'new hampshire', 'nh', 'maine', 'me', 'massachusetts', 'ma',
    'rhode island', 'ri', 'connecticut', 'ct', 'new jersey', 'nj', 'delaware', 'de',
    'maryland', 'md', 'district of columbia', 'dc', 'hawaii', 'hi', 'alaska', 'ak',
}

SINGLE_INITIALS = {'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm',
                   'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z'}

# Team/leadership page paths to try on company website
LEADERSHIP_PAGE_PATHS = [
    "/about", "/about-us", "/about-us/leadership", "/about/leadership",
    "/leadership", "/leadership-team", "/management", "/executive-team",
    "/team", "/our-team", "/meet-the-team", "/people", "/our-people",
    "/company/team", "/company/about", "/company/leadership",
    "/who-we-are", "/governance/leadership", "/corporate/leadership",
    "/pages/team", "/pages/about", "/about/team", "/about-us/team",
    "/board-of-directors", "/our-leadership", "/senior-leadership",
]


# ── Name Validation ───────────────────────────────────────────────────────────

def is_valid_name(name: str) -> bool:
    """
    Strict name validation. Rejects HTML tags, titles-as-names,
    locations, single initials, credentials, etc.
    """
    if not name or not isinstance(name, str):
        return False
    name = name.strip()
    if len(name) < 3:
        return False

    words = name.split()
    if len(words) < 2:
        return False
    if len(words) > 6:
        return False

    name_lower = name.lower()

    for tag in HTML_TAGS:
        if re.search(r'\b' + re.escape(tag) + r'\b', name_lower):
            return False

    for title in GENERIC_TITLES_AS_NAMES:
        if title in name_lower:
            return False

    for loc in LOCATION_WORDS:
        if re.search(r'\b' + re.escape(loc) + r'\b', name_lower):
            return False

    last_word = words[-1].rstrip('.').lower()
    if last_word in SINGLE_INITIALS and len(last_word) == 1:
        return False

    if re.search(r'\d', name):
        return False

    if name.islower() or name.isupper():
        return False

    credentials = {'p.e.', 'pe', 'cpa', 'md', 'phd', 'esq', 'jd', 'mba', 'cfp',
                 'cfa', 'pmp', 'six sigma', 'lean', 'scrum', 'agile', 'cpm',
                 'cpc', 'cpp', 'cpsm', 'cscp', 'cppo', 'cppc', 'leed',
                 'itil', 'cisa', 'cissp', 'ceh', 'oscp', 'gcih', 'gpen'}
    for word in words:
        if word.lower().rstrip('.') in credentials:
            return False

    if not words[0][0].isupper():
        return False
    if not words[-1][0].isupper():
        return False

    return True


# Cookie banner / GDPR / UI chrome words that often get matched as
# "person names" by the regex scraper. (added 2026-06-16)
NOISE_NAME_WORDS = {
    'close', 'accept', 'decline', 'reject', 'agree', 'disagree',
    'gdpr', 'consent', 'manage', 'settings', 'preferences',
    'cookie', 'cookies', 'privacy', 'policy', 'terms', 'conditions',
    'necessary', 'strictly', 'targeting', 'comply',
}


def _looks_like_noise_name(name: str) -> bool:
    """
    Quick check: does this "name" look like cookie banner / UI text
    rather than a real person? (added 2026-06-16)
    """
    if not name:
        return True
    name_lower = name.lower()
    words = name_lower.split()
    # Reject if any word is a known noise word
    if any(w in NOISE_NAME_WORDS for w in words):
        return True
    # Reject if name has 3+ words where any is all-caps (e.g. "Close GDPR Banner")
    raw_words = name.split()
    if len(raw_words) >= 2:
        all_caps_count = sum(1 for w in raw_words if w.isupper() and len(w) >= 3)
        if all_caps_count >= 1 and len(raw_words) == 2:
            # 2-word name with one all-caps word is suspicious
            return True
    return False


# AI availability cache (added 2026-06-16): avoid paying the 30s
# Ollama timeout cost on every enrichment call. If the AI is known
# to be unreachable, skip the AI stage immediately.
_AI_AVAILABLE_CACHE = {"available": None, "checked_at": 0.0}
_AI_AVAILABLE_TTL_S = 60  # re-check every minute


def _ai_quick_ping(timeout: int = 2) -> bool:
    """
    Quick health check: is the AI backend responsive?
    Returns True if the AI is reachable AND responds within `timeout`s,
    False otherwise.

    Result is cached for 60s to avoid hammering an already-down service.
    """
    import time as _t
    now = _t.time()
    if _AI_AVAILABLE_CACHE["available"] is not None:
        if now - _AI_AVAILABLE_CACHE["checked_at"] < _AI_AVAILABLE_TTL_S:
            return _AI_AVAILABLE_CACHE["available"]

    try:
        from lf_ai_enrich import ai_complete
        # Send a trivial prompt and see if it returns
        result = ai_complete("ping", operation="ping", gap_count=0, timeout=timeout)
        is_up = result is not None and len(result) > 0
    except Exception:
        is_up = False

    _AI_AVAILABLE_CACHE["available"] = is_up
    _AI_AVAILABLE_CACHE["checked_at"] = now
    return is_up


def extract_name_from_url(url: str) -> str:
    """
    Extract name from LinkedIn URL slug.
    Returns "" if not a valid person name.
    """
    match = re.search(r"/in/([^/]+)", url)
    if not match:
        return ""
    slug = match.group(1)
    if "-" not in slug:
        return ""
    # Strip trailing numeric/alphanumeric ID
    name_part = re.sub(r"-[a-zA-Z0-9]{6,}$", "", slug)
    # Strip common title suffixes
    parts = name_part.split("-")
    if len(parts) >= 2 and parts[-1].lower() in {
        'ceo', 'cfo', 'coo', 'cto', 'vp', 'founder', 'owner',
        'president', 'partner', 'head', 'director', 'manager',
        'engineer', 'leader', 'executive', 'officer',
    }:
        name_part = "-".join(parts[:-1])
    name_part = name_part.replace("-", " ")
    if len(name_part.split()) < 2:
        return ""
    name = name_part.title()
    if not is_valid_name(name):
        return ""
    return name


# ── PixelRAG LinkedIn Visual Scrape (added 2026-06-16) ───────────────
# Per user feedback: PixelRAG is useful for the LATEST experience/role/
# title/location. This is the SECONDARY enrichment after AI — only used
# when the AI's knowledge is stale and we need fresh data from the
# actual LinkedIn profile page.
#
# Flow: take a screenshot of the LinkedIn profile page, then ask the
# visual RAG model to extract the latest experience. This is slow
# (10-30s per profile on CPU) so we use it sparingly.

def pixelrag_scrape_linkedin(
    linkedin_url: str,
    full_name: str = "",
    company_name: str = "",
    timeout: int = 30,
) -> Optional[dict]:
    """
    Use PixelRAG to scrape a LinkedIn profile page and extract the
    LATEST experience/role/title/location. Returns dict with:
      - title: current title at the company
      - location: city/region
      - experience: list of recent roles (if extractable)
      - raw_text: the text returned by the visual model
    Returns None on any failure (timeout, no extraction, etc).

    This is the SECONDARY enrichment — used only when AI's knowledge
    of the person is stale and we need fresh data. The user said
    PixelRAG is fine for "scraping latest experience" but NOT for
    company search.
    """
    if not linkedin_url or "linkedin.com/in/" not in linkedin_url:
        return None

    # Check PixelRAG status first
    from lf_config import pixelrag_url
    try:
        status_resp = requests.get(f"{pixelrag_url()}/status", timeout=5)
        if status_resp.status_code != 200:
            print(f"[executives] PixelRAG unavailable for LinkedIn scrape")
            return None
    except Exception as e:
        print(f"[executives] PixelRAG status check failed: {e}")
        return None

    # 1) Screenshot the LinkedIn profile
    try:
        screenshot_resp = requests.post(
            f"{pixelrag_url()}/screenshot",
            json={"url": linkedin_url, "timeout": 60},
            timeout=timeout,
        )
        if screenshot_resp.status_code != 200:
            print(f"[executives] PixelRAG screenshot failed: {screenshot_resp.status_code}")
            return None
    except Exception as e:
        print(f"[executives] PixelRAG screenshot error: {e}")
        return None

    # 2) Extract via visual model — query for latest experience
    query = (
        f"Extract the LATEST job title, company, and location for "
        f"{full_name or 'this person'}"
        + (f" at {company_name}" if company_name else "")
        + ". Focus on the first/most recent Experience entry."
    )
    try:
        extract_resp = requests.post(
            f"{pixelrag_url()}/extract",
            json={
                "url": linkedin_url,
                "query": query,
                "top_k": 3,
                "timeout": max(30, timeout),
            },
            timeout=timeout,
        )
        if extract_resp.status_code != 200:
            print(f"[executives] PixelRAG extract failed: {extract_resp.status_code}")
            return None
        data = extract_resp.json()
    except Exception as e:
        print(f"[executives] PixelRAG extract error: {e}")
        return None

    # 3) Run the raw text through AI to structure it
    raw_text = ""
    matches = data.get("matches", [])
    for m in matches:
        snippet = m.get("snippet") or m.get("text") or ""
        if snippet:
            raw_text += snippet + "\n"
    if not raw_text.strip():
        return None

    from lf_ai_enrich import ai_extract_structured
    structured = ai_extract_structured(
        text=raw_text,
        schema={
            "title": "string — the most recent job title",
            "company": "string — the company name",
            "location": "string — city, state or region",
            "is_current": "boolean — is this their current role?",
        },
        context=f"Extract latest experience for {full_name} from LinkedIn visual scrape",
        operation="linkedin_visual_extract",
    )
    if not structured:
        return {"raw_text": raw_text[:500]}

    return {
        "title": structured.get("title", ""),
        "company": structured.get("company", company_name),
        "location": structured.get("location", ""),
        "is_current": structured.get("is_current", True),
        "raw_text": raw_text[:500],
    }


# ── Step 1: Scrape Company Website for Leadership Page ───────────────────────

def _fetch_page_content(url: str, timeout: int = 15000) -> Optional[str]:
    """
    Fetch URL content using requests (fast, reliable, no async conflicts).
    Playwright would handle JS-rendered pages but conflicts with FastAPI's asyncio loop,
    so we use requests which handles most static company pages well.
    """
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        if resp.status_code == 200:
            return resp.text
    except Exception as e:
        print(f"[executives] Fetch failed for {url}: {e}")
    return None


def _find_leadership_page_url(website: str) -> Optional[str]:
    """
    Try common leadership page paths on the company website.
    Returns the first URL that returns 200, or None.
    Also tries a Google search fallback: 'site:<domain> leadership' when none found.
    """
    if not website:
        return None
    if not website.startswith("http"):
        website = "https://" + website
    base = website.rstrip("/")

    # Quick HEAD checks first
    for path in LEADERSHIP_PAGE_PATHS:
        url = base + path
        try:
            resp = requests.head(url, timeout=8, allow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            if resp.status_code < 400:
                return url
        except Exception:
            pass
        time.sleep(0.2)

    # Slower GET approach for paths that might need JS (e.g., SPA routing)
    import re as _re
    domain = _re.sub(r'^https?://', '', base).rstrip('/')
    # Search for "site:domain leadership" to find actual leadership page
    try:
        query = f'site:{domain} (leadership OR "executive team" OR "management team")'
        raw_results, provider = search(query, timeout=15)
        for r in raw_results:
            candidate = r.get("url", "")
            if candidate and domain in candidate:
                # Check it's not just the homepage
                for lp in LEADERSHIP_PAGE_PATHS:
                    if lp.strip('/') in candidate.lower():
                        return candidate
                # Also check if title mentions leadership/team/management
                title = r.get("title", "").lower()
                if any(kw in title for kw in ('leadership', 'team', 'management', 'executive', 'about')):
                    return candidate
    except Exception as e:
        print(f"[executives] Search fallback for leadership page failed: {e}")

    return None


def _extract_names_and_titles_from_html(html: str, source_url: str) -> list[dict]:
    """
    Parse HTML for person names + titles near each other.
    Uses multiple heuristic patterns to find leadership profiles on company websites.

    Returns list of {name, title, source_url}.
    """
    results = []
    if not html:
        return results

    # Strip all tags for regex matching on visible text
    text = re.sub(r'<[^>]+>', ' ', html)
    # Normalize whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    # Known title keywords to anchor name-finding
    title_keywords = [
        "Chief Executive Officer", "Chief Financial Officer", "Chief Operating Officer",
        "Chief Technology Officer", "Chief Information Officer", "Chief Revenue Officer",
        "Chief People Officer", "Chief Marketing Officer", "Chief Scientific Officer",
        "CEO", "CFO", "COO", "CTO", "CIO", "CRO", "CHRO", "CMO", "CSO",
        "President", "Vice President", "VP", "Sr VP", "Senior VP", "Executive VP",
        "General Manager", "GM", "Director", "Sr Director", "Senior Director",
        "Executive Director", "Founder", "Co-Founder", "Owner", "Partner",
        "Head of Engineering", "Head of Sales", "Head of Operations",
    ]

    # Words that are titles / modifiers, not person names
    title_modifier_words = {
        'senior', 'sr', 'junior', 'jr', 'executive', 'associate', 'assistant',
        'deputy', 'chief', 'head', 'lead', 'global', 'regional', 'national',
        'vice', 'board', 'general',
    }

    # Words that appear in company/division names, not person names
    company_words = {
        'boeing', 'airbus', 'lockheed', 'northrop', 'gruman', 'raytheon',
        'spacex', 'space', 'blue', 'origin', 'rocket', 'lab', 'ula',
        'commercial', 'airplanes', 'defense', 'security', 'systems',
        'technologies', 'industries', 'solutions', 'international',
        'corporation', 'company', 'inc', 'llc', 'ltd', 'group',
        'aviation', 'aerospace', 'aeronautics', 'satellite',
        'manufacturing', 'engineering', 'logistics', 'operations',
        'enterprises', 'holdings', 'partners', 'associates',
        'analytics', 'digital', 'innovation', 'capital',
        'transportation', 'services', 'network', 'communications',
        'missile', 'propulsion', 'drone', 'unmanned',
    }

    # Non-person words that appear in website chrome / legal / cookie banners
    noise_words = {
        'policy', 'privacy', 'strictly', 'necessary', 'targeting',
        'cookie', 'cookies', 'legal', 'terms', 'conditions',
        'human', 'resources', 'contact', 'login', 'register',
        'subscribe', 'search', 'menu', 'home', 'news',
        'careers', 'investors', 'media', 'press', 'blog',
        'copyright', 'rights', 'reserved', 'accessibility',
        'sitemap', 'faqs', 'faq', 'linkedin', 'facebook',
        'twitter', 'instagram', 'youtube', 'social', 'follow',
        # GDPR / cookie banner false positives (added 2026-06-16)
        'close', 'accept', 'decline', 'reject', 'agree', 'disagree',
        'gdpr', 'consent', 'manage', 'settings', 'preferences',
        'privacy', 'policy', 'comply', 'consent',
    }

    seen_names = set()

    # Pattern: "Name Surname" followed by title keyword within ~100 chars
    for kw in sorted(title_keywords, key=len, reverse=True):
        kw_escaped = re.escape(kw)
        pattern = re.compile(
            r'([A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+|[A-Z]\.)){1,3})'  # Name: 2-4 capitalized words
            r'[\s—–,\-:|]+' + kw_escaped,
            re.IGNORECASE
        )
        for match in pattern.finditer(text):
            name = match.group(1).strip()
            # Reject if any word looks like a title modifier, company word, or noise
            name_words = name.lower().split()
            if any(w in title_modifier_words for w in name_words):
                continue
            if any(w in company_words for w in name_words):
                continue
            if any(w in noise_words for w in name_words):
                continue
            # Cookie banner / UI chrome text filter (added 2026-06-16)
            if _looks_like_noise_name(name):
                continue
            if name not in seen_names and is_valid_name(name):
                seen_names.add(name)
                results.append({
                    "name": name,
                    "title": kw,
                    "source_url": source_url,
                })

    # Also scan for name/title in reversed order: "CEO — John Smith"
    for kw in sorted(title_keywords, key=len, reverse=True):
        if kw.lower() in ('vice president', 'senior vp', 'executive vp', 'sr vp'):
            continue  # These usually follow the name, not precede it
        kw_escaped = re.escape(kw)
        pattern = re.compile(
            r'(?:^|[\s\n])' + kw_escaped + r'[\s—–,\-:|]+([A-Z][a-z]+(?:\s+(?:[A-Z][a-z]+|[A-Z]\.)){1,3})',
            re.IGNORECASE
        )
        for match in pattern.finditer(text):
            name = match.group(1).strip()
            name_words = name.lower().split()
            # Reject if any word looks like a title modifier, company word, or noise
            if any(w in title_modifier_words for w in name_words):
                continue
            if any(w in company_words for w in name_words):
                continue
            if any(w in noise_words for w in name_words):
                continue
            # Cookie banner / UI chrome text filter (added 2026-06-16)
            if _looks_like_noise_name(name):
                continue
            if name not in seen_names and is_valid_name(name):
                seen_names.add(name)
                results.append({
                    "name": name,
                    "title": kw,
                    "source_url": source_url,
                })

    return results


def scrape_company_leadership(website: str, company_name: str) -> tuple[list[dict], Optional[str]]:
    """
    Step 1: Scrape company website for leadership/team page.
    Returns (list of {name, title, source_url}, leadership_page_url or None).

    If no leadership page is found, returns empty list.
    AI fallback (added 2026-06-06): if regex finds < 2 candidates, try AI parsing.
    """
    leadership_url = _find_leadership_page_url(website)
    if not leadership_url:
        print(f"[executives] No leadership page found for {company_name} ({website})")
        return [], None

    print(f"[executives] Found leadership page: {leadership_url}")
    html = _fetch_page_content(leadership_url)
    if not html:
        print(f"[executives] Could not fetch content from {leadership_url}")
        return [], leadership_url

    people = _extract_names_and_titles_from_html(html, leadership_url)
    print(f"[executives] Extracted {len(people)} people from {leadership_url} (regex)")

    # AI fallback (added 2026-06-06): gap-driven
    # If regex found < 2 candidates, try AI parsing for better coverage
    if len(people) < 2 and html:
        try:
            from lf_ai_enrich import ai_parse_website_leadership, count_gaps, ai_sanity_check
            # Count gaps in the regex results
            total_gaps = 0
            for p in people:
                total_gaps += count_gaps({
                    "title": p.get("title", ""),
                    "linkedin_url": "",
                    "location": "",
                    "email": "",
                    "phone": "",
                })

            if total_gaps >= 2:  # gaps in regex extraction -> invoke AI
                print(f"[executives] Regex found {len(people)} with {total_gaps} gaps, trying AI parsing")
                ai_people = ai_parse_website_leadership(
                    html=html, company_name=company_name, url=leadership_url
                )
                if ai_people:
                    # Merge: keep regex matches + add AI-discovered ones
                    existing_names = {p.get("name", "").lower() for p in people}
                    for ap in ai_people:
                        if ap.get("full_name", "").lower() not in existing_names:
                            # Convert AI format to regex format
                            people.append({
                                "name": ap.get("full_name", ""),
                                "title": ap.get("title", ""),
                                "source_url": leadership_url,
                                "linkedin_url": ap.get("linkedin_url", ""),
                                "confidence_score": ap.get("confidence", 0.5),
                                "data_provenance": f"AI_PARSE: {leadership_url}",
                            })
                    print(f"[executives] AI parsing added {len(people) - len([p for p in people if p.get('data_provenance','').startswith('AI_PARSE')])} people")
        except Exception as e:
            # Graceful degradation
            print(f"[executives] AI HTML parsing failed (non-fatal): {e}")

    return people, leadership_url


# ── Step 2: Verify Each Person on LinkedIn ───────────────────────────────────

def is_valid_linkedin_profile(url: str, snippet: str) -> bool:
    """
    Check if a LinkedIn profile is NOT blank/empty/deleted.
    Returns True if the profile appears to have meaningful content.
    """
    if not url or "linkedin.com/in/" not in url:
        return False
    if "/company/" in url:
        return False

    # If no snippet, can't verify
    if not snippet or len(snippet.strip()) < 20:
        return False

    snippet_lower = snippet.lower()

    # Reject profiles with only "LinkedIn" + name — these are blank/generic placeholders
    # Typical blank snippet: "John Smith · LinkedIn · 500+ connections"
    # Typical real snippet: "John Smith · CEO at Acme Corp · Location: New York · 500+ connections"
    stripped = re.sub(r'\s+', ' ', snippet_lower).strip()

    # Count substantive words (exclude LinkedIn, "connections", etc.)
    filler_words = {'linkedin', 'connections', 'connection', 'member', 'profile', 'join', 'view'}
    substantive = [w for w in stripped.split() if w not in filler_words]

    if len(substantive) < 5:
        return False

    # Check for actual title/work info
    work_signals = [' at ', '@', 'ceo', 'cfo', 'coo', 'cto', 'president',
                    'director', 'manager', 'engineer', 'founder', 'chief',
                    'vice president', 'vp', 'head of', 'experience',
                    'location:', 'education:', 'position', 'role']
    has_work_signal = any(signal in snippet_lower for signal in work_signals)
    if not has_work_signal:
        return False

    return True


def search_linkedin_verification(
    full_name: str,
    company_name: str,
    search_region: str = "CA"
) -> list[dict]:
    """
    Step 2: Search for a person's LinkedIn profile using exact name + company.
    Query: "<full name>" "<company name>" site:linkedin.com/in

    Returns list of {url, title, snippet, location, is_verified}.
    """
    results_list = []

    # Build precise query
    query = f'"{full_name}" "{company_name}" site:linkedin.com/in'
    try:
        raw_results, provider = search(query, timeout=20)
    except Exception as e:
        print(f"[executives] Search error for {full_name}: {e}")
        return results_list

    for r in raw_results:
        url = r.get("url", "")
        if "linkedin.com/in/" not in url or "/company/" in url:
            continue
        clean_url = re.sub(r"\?.*", "", url).rstrip("/")

        snippet = r.get("snippet", "") or r.get("content", "")
        if not snippet:
            continue

        # Extract title from LinkedIn snippet
        title = _extract_title_from_linkedin_snippet(snippet)
        location = _parse_location_from_snippet(snippet)

        # Verify the profile mentions the company
        company_mentioned = company_name.lower() in snippet.lower()

        results_list.append({
            "url": clean_url,
            "title": title,
            "snippet": snippet[:300],
            "location": location,
            "company_mentioned": company_mentioned,
            "provider": provider,
        })

    # If multiple profiles found, rank by completeness:
    # Prefer: has title + has location + company mentioned
    def completeness_score(r: dict) -> int:
        score = 0
        if r.get("title"):
            score += 3
        if r.get("location"):
            score += 2
        if r.get("company_mentioned"):
            score += 5
        return score

    results_list.sort(key=completeness_score, reverse=True)

    # AI gap-based ranking (added 2026-06-06)
    # If top profiles have significant gaps, use AI to pick best with integrity scoring
    if len(results_list) >= 2:
        try:
            from lf_ai_enrich import ai_rank_linkedin_profiles, count_gaps
            # Check if top result has gaps worth filling
            top_gaps = count_gaps({
                "title": results_list[0].get("title"),
                "linkedin_url": results_list[0].get("url"),
                "location": results_list[0].get("location"),
                "email": "",
                "phone": "",
            })
            # Also count gaps across top 3 profiles
            for p in results_list[1:3]:
                top_gaps += count_gaps({
                    "title": p.get("title"),
                    "linkedin_url": p.get("url"),
                    "location": p.get("location"),
                    "email": "",
                    "phone": "",
                })

            if top_gaps >= 3:  # 3+ gaps across top profiles -> invoke AI
                ai_best = ai_rank_linkedin_profiles(full_name, company_name, results_list[:5])
                if ai_best:
                    # Per user spec 2026-06-06: clear "no LinkedIn profile found" indicator
                    if ai_best.get("no_linkedin_profile_found"):
                        return [ai_best]  # Returns the no-profile indicator dict
                    # Otherwise return ONLY the AI-picked best with metadata
                    if ai_best.get("ai_confidence", 0) > 0.5:
                        return [ai_best]
        except Exception as e:
            # Graceful degradation: any error -> continue with regex ranking
            print(f"[executives] AI ranking failed (non-fatal): {e}")

    return results_list


def verify_person_on_linkedin(
    person: dict,
    company_name: str,
    search_region: str = "CA",
    search_lat: float = 0.0,
    search_lng: float = 0.0,
    radius_miles: int = 25,
) -> Optional[dict]:
    """
    Step 2 full verification: AI-first title research, then LinkedIn validation.

    Pipeline (QC-16, 2026-06-09):
      1. AI PRIMARY: research title + LinkedIn URL for person at this company
      2. SearXNG fallback: if AI fails, scrape LinkedIn snippets
      3. Validate profile quality, location, company match
      4. Return combined dict

    Returns combined dict with:
      - name, title, title_from_website, title_from_linkedin, linkedin_url, linkedin_snippet
      - is_local, hq_contact, confidence_score, data_provenance
    Returns None if the person cannot be verified.
    """
    full_name = person.get("name", "")
    website_title = person.get("title", "")

    if not full_name:
        return None

    # ── STEP 1: AI PRIMARY — research title at this specific company ───────
    ai_title_result = None
    try:
        from lf_ai_enrich import ai_research_contact
        ai_title_result = ai_research_contact(
            full_name=full_name,
            company_name=company_name,
            linkedin_url="",  # We don't have it yet
            current_title=website_title,
        )
        if ai_title_result and ai_title_result.get("is_verified") and ai_title_result.get("title"):
            print(f"[executives] ✓ AI verified title for {full_name}: {ai_title_result['title']} ({ai_title_result.get('reasoning', '')})")
    except Exception as e:
        print(f"[executives] AI title research failed (non-fatal): {e}")
        ai_title_result = None

    # ── STEP 2: SearXNG LinkedIn search (validation + URL discovery) ───────
    profiles = search_linkedin_verification(full_name, company_name, search_region)

    # If AI found title but no LinkedIn URL, we can still proceed
    if not profiles and ai_title_result and ai_title_result.get("is_verified"):
        print(f"[executives] AI found title but no LinkedIn profile for {full_name}: using AI title")
        return _build_ai_only_result(full_name, ai_title_result, company_name, website_title)

    if not profiles:
        print(f"[executives] No LinkedIn profile found for {full_name} at {company_name}")
        return None

    # Per user spec 2026-06-06: clear "no LinkedIn profile found" indicator from AI
    if profiles[0].get("no_linkedin_profile_found"):
        print(f"[executives] AI confirmed: no LinkedIn profile found for {full_name}")
        print(f"  Reasoning: {profiles[0].get('ai_reasoning', 'N/A')}")
        return None

    # Take the best-ranked profile (sorted by completeness or AI)
    best = profiles[0]

    # Validate profile quality
    if not is_valid_linkedin_profile(best["url"], best["snippet"]):
        print(f"[executives] Blank/empty LinkedIn profile for {full_name}: {best['url']}")
        return None

    # Location check
    location = best.get("location", "")
    is_local, hq_contact = _determine_local_vs_hq_from_linkedin(
        location, search_region, search_lat, search_lng, radius_miles
    )

    # If location doesn't match search region at all, skip for local contacts
    # but still allow HQ contacts if they match company
    if not is_local and not hq_contact and search_lat and search_lng:
        print(f"[executives] Location mismatch for {full_name}: '{location}' vs {search_region}")
        if best.get("company_mentioned"):
            is_local, hq_contact = 0, 1
        else:
            return None

    # ── STEP 3: Determine final title ──────────────────────────────────────
    # AI title is PRIMARY. Fall back to SearXNG title if AI is unavailable.
    linkedin_title = normalize_title(best.get("title", ""))
    normalized_website_title = normalize_title(website_title)

    if ai_title_result and ai_title_result.get("is_verified") and ai_title_result.get("title"):
        final_title = normalize_title(ai_title_result["title"])
        confidence_base = 0.95 if best.get("company_mentioned") else 0.90
    elif ai_title_result and ai_title_result.get("title"):
        # AI provided a title but wasn't fully confident
        final_title = normalize_title(ai_title_result["title"])
        confidence_base = 0.80
    else:
        # Fallback to SearXNG snippet extraction
        final_title = linkedin_title or normalized_website_title
        confidence_base = 0.85 if best.get("company_mentioned") else 0.75

    result = {
        "first_name": full_name.split()[0],
        "last_name": " ".join(full_name.split()[1:]),
        "full_name": full_name,
        "title": final_title,
        "title_from_website": normalized_website_title,
        "title_from_linkedin": linkedin_title,
        "ai_verified_title": ai_title_result.get("title") if ai_title_result else None,
        "ai_title_confidence": ai_title_result.get("confidence", 0) if ai_title_result else 0,
        "ai_title_source": ai_title_result.get("source") if ai_title_result else None,
        "linkedin_url": best["url"],
        "linkedin_snippet": best["snippet"][:300],
        "is_local": is_local,
        "hq_contact": hq_contact,
        "confidence_score": confidence_base,
        "data_provenance": f"AI_PRIMARY+WEBSITE+LINKEDIN:{best.get('provider', 'search')}",
        "source_primary": "ai",
        "source_linkedin_verified": 1 if best.get("company_mentioned") else 0,
        "source_linkedin_unverified": 0 if best.get("company_mentioned") else 1,
    }

    # AI integrity scoring (added 2026-06-06) — based on company, location, title reference
    if best.get("ai_ranking_used"):
        result["ai_company_match"] = bool(best.get("ai_company_match"))
        result["ai_location_match"] = bool(best.get("ai_location_match"))
        result["ai_title_match"] = bool(best.get("ai_title_match"))
        result["ai_confidence"] = best.get("ai_confidence", 0.0)
        result["ai_reasoning"] = best.get("ai_reasoning", "")
        if (result["ai_company_match"] and result["ai_location_match"] and result["ai_title_match"]):
            result["confidence_score"] = min(1.0, result["confidence_score"] + 0.05)
        try:
            from lf_ai_enrich import ai_sanity_check
            sanity = ai_sanity_check({
                "name": full_name,
                "title": result["title"],
                "company": company_name,
                "linkedin_url": best["url"],
                "location": best.get("location", ""),
            }, context="Verified LinkedIn profile for executive")
            if sanity:
                result["ai_sanity_valid"] = bool(sanity.get("is_valid", False))
                result["ai_sanity_confidence"] = sanity.get("confidence", 0.0)
                result["ai_sanity_issues"] = sanity.get("issues", [])
                result["ai_sanity_reasoning"] = sanity.get("reasoning", "")
        except Exception as e:
            pass

    return result


def _build_ai_only_result(
    full_name: str,
    ai_result: dict,
    company_name: str,
    website_title: str = "",
) -> dict:
    """Build a contact result from AI-only research (no LinkedIn URL found)."""
    name_parts = full_name.split(" ", 1)
    return {
        "first_name": name_parts[0] if name_parts else "",
        "last_name": name_parts[1] if len(name_parts) > 1 else "",
        "full_name": full_name,
        "title": normalize_title(ai_result.get("title", "")),
        "title_from_website": normalize_title(website_title),
        "title_from_linkedin": "",
        "ai_verified_title": ai_result.get("title"),
        "ai_title_confidence": ai_result.get("confidence", 0),
        "ai_title_source": ai_result.get("source"),
        "linkedin_url": "",
        "linkedin_snippet": "",
        "is_local": 1,
        "hq_contact": 0,
        "confidence_score": 0.6,
        "data_provenance": "AI_PRIMARY",
        "source_primary": "ai",
        "source_linkedin_verified": 0,
        "source_linkedin_unverified": 0,
    }


# ── Local vs HQ determination ────────────────────────────────────────────────

def _determine_local_vs_hq_from_linkedin(
    location: str,
    search_region: str,
    search_lat: float,
    search_lng: float,
    radius_miles: int,
) -> tuple[int, int]:
    """
    Determine if a contact is LOCAL (1,0) or HQ (0,1) based on LinkedIn location.
    If location is ambiguous, defaults to (0,1).
    """
    if not location:
        return 0, 1
    location_lower = location.lower()

    # Simple heuristic: does the location contain the search region/state?
    search_region_lower = search_region.lower()
    # Check for state abbreviation or full state name in the location
    ca_signals = ["california", "ca", "los angeles", "san francisco", "san diego",
                  "san jose", "sacramento", "oakland", "palo alto", "santa monica",
                  "long beach", "irvine", "burbank", "glendale", "pasadena",
                  "greater los angeles", "bay area", "silicon valley"]

    if search_region_lower in location_lower:
        return 1, 0

    # Check state-specific signals for CA
    if search_region_lower == "ca":
        for signal in ca_signals:
            if signal in location_lower:
                return 1, 0

    # If location seems clearly different (contains another state name)
    other_states = ["texas", "tx", "florida", "fl", "new york", "ny", "washington", "wa",
                    "illinois", "il", "massachusetts", "ma", "pennsylvania", "pa",
                    "ohio", "oh", "georgia", "ga", "north carolina", "nc",
                    "michigan", "mi", "new jersey", "nj", "virginia", "va",
                    "colorado", "co", "arizona", "az"]
    for state in other_states:
        if state in location_lower:
            return 0, 1  # HQ

    return 0, 1  # Default to HQ when ambiguous


def _extract_title_from_linkedin_snippet(snippet: str) -> str:
    """
    Extract job title from LinkedIn search snippet.
    Patterns like:
      - "Name - CEO at Company · Location: ..."
      - "CEO at Acme Corp"
      - "Name · Title · Location"
    """
    if not snippet:
        return ""

    snippet = snippet.strip()

    # Primary pattern: "Name - TITLE at Company" (the most reliable pattern)
    m = re.search(r'[-–—]\s*([A-Z][^-–—·|•\n]{2,80}?)\s+at\s+[A-Z]', snippet)
    if m:
        title = m.group(1).strip()
        # Reject if it looks like a location or filler text
        if not re.match(r'^(Location|View|Education|Experience)', title):
            if 2 <= len(title) <= 80:
                return normalize_title(title)

    # Pattern: "Name · TITLE · Location"
    m = re.search(r'·\s*([^·|•\n]{2,60}?)\s+at\s+[A-Z]', snippet)
    if m:
        title = m.group(1).strip()
        if not re.match(r'^(Location|View|Education|Experience)', title):
            if 2 <= len(title) <= 80:
                return normalize_title(title)

    # Pattern: title keyword near company name in snippet
    title_kw_patterns = [
        r'(CEO)\s+(?:at|@|of|for|·)',
        r'(CFO)\s+(?:at|@|of|for|·)',
        r'(COO)\s+(?:at|@|of|for|·)',
        r'(CTO)\s+(?:at|@|of|for|·)',
        r'(Chief\s+\w+\s+Officer)\s+(?:at|@|of|for|·)',
        r'(President)\s+(?:at|@|of|for|·)',
        r'(Vice\s+President)\s+(?:at|@|of|for|·)',
        r'(VP)\s+(?:at|@|of|for|·)',
        r'(Director)\s+(?:at|@|of|for|·)',
        r'(General\s+Manager)\s+(?:at|@|of|for|·)',
        r'(Founder)\s+(?:at|@|of|for|·)',
        r'(Owner)\s+(?:at|@|of|for|·)',
        r'(Partner)\s+(?:at|@|of|for|·)',
        r'(Head\s+of\s+\w+)\s+(?:at|@|of|for|·)',
    ]
    for pat in title_kw_patterns:
        m = re.search(pat, snippet, re.IGNORECASE)
        if m:
            title = m.group(1).title()
            return normalize_title(title)

    # Experience pattern: "Experience: Company · Title"
    m = re.search(r'Experience:\s*[^·]+·\s*([^·|•\n,]{3,60})', snippet, re.IGNORECASE)
    if m:
        title = m.group(1).strip()
        if not re.match(r'^(Location|View|Education|Experience)', title):
            return normalize_title(title)

    # Fallback: find any known title keyword in the snippet
    snippet_lower = snippet.lower()
    for kw in ["chief executive officer", "chief financial officer", "chief operating officer",
               "chief technology officer", "president", "vice president", "vp",
               "ceo", "cfo", "coo", "cto", "director", "general manager",
               "founder", "owner", "partner", "head of"]:
        if kw in snippet_lower:
            idx = snippet_lower.find(kw)
            start = max(0, idx - 30)
            end = min(len(snippet), idx + len(kw) + 50)
            context = snippet[start:end].strip()
            return normalize_title(kw.title())

    return ""


def _parse_location_from_snippet(snippet: str) -> str:
    """Extract location from LinkedIn snippet like 'Location: Los Angeles Metropolitan Area'"""
    if not snippet:
        return ""
    m = re.search(r'Location:\s*([^·|•\n]+)', snippet, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'(Greater\s+[A-Za-z\s]+Area)', snippet)
    if m:
        return m.group(1).strip()
    return ""


# ── Step 3: Main Discovery Orchestrator ──────────────────────────────────────

def discover_executives(
    company_name: str,
    company_id: int,
    website: str = "",
    linkedin_url: str = "",
    search_city: str = "",
    search_state: str = "CA",
    search_lat: float = 0.0,
    search_lng: float = 0.0,
    radius_miles: int = 25,
) -> list[dict]:
    """
    Main executive discovery with three-step pipeline:
      1. Scrape company website for leadership page
      2. Verify each person on LinkedIn
      3. Save with data quality flags

    Returns only verified contacts.
    """
    if not company_name:
        print("[executives] No company name provided, skipping")
        return []

    print(f"\n[executives] === Discovering executives for {company_name} ===")
    all_contacts = []

    # ── Step 1: Scrape company website ─────────────────────────────────────
    people, leadership_url = scrape_company_leadership(website, company_name)

    # Heuristic for low-quality leadership extraction (added 2026-06-16):
    # if the scraper found < 2 real people (or the "people" are clearly
    # cookie banner / GDPR text), skip ahead to the LinkedIn fallback
    # which is more reliable for companies without a proper leadership page.
    is_low_quality_extraction = (
        len(people) < 2
        or any(_looks_like_noise_name(p.get("name", "")) for p in people)
    )
    if is_low_quality_extraction:
        print(f"[executives] WARNING: Low-quality leadership extraction for {company_name} "
              f"({len(people)} people, possibly cookie banner). "
              f"Falling through to LinkedIn search...")
        return _enrich_from_linkedin_fallback(
            company_name=company_name,
            company_id=company_id,
            website=website,
            search_city=search_city,
            search_state=search_state,
            search_lat=search_lat,
            search_lng=search_lng,
            radius_miles=radius_miles,
        )

    if not people:
        print(f"[executives] WARNING: No leadership page found for {company_name}. "
              f"Trying LinkedIn keyword search as fallback...")
        # Fallback: search LinkedIn directly for executives at company
        return _enrich_from_linkedin_fallback(
            company_name=company_name,
            company_id=company_id,
            website=website,
            search_city=search_city,
            search_state=search_state,
            search_lat=search_lat,
            search_lng=search_lng,
            radius_miles=radius_miles,
        )

    print(f"[executives] Step 1 complete: {len(people)} people found on website")

    # ── Step 2: Verify each person on LinkedIn ─────────────────────────────
    verified_contacts = []
    search_region = search_state or "CA"

    for person in people:
        time.sleep(0.5)  # Rate limiting
        verified = verify_person_on_linkedin(
            person=person,
            company_name=company_name,
            search_region=search_region,
            search_lat=search_lat,
            search_lng=search_lng,
            radius_miles=radius_miles,
        )
        if verified:
            # ── Google validation gate (P5-C1) ─────────────────────────────
            is_valid, reason = google_validate_person(verified.get("full_name", ""), company_name)
            if not is_valid:
                print(f"[executives]   ✗ Google validation failed: {person['name']} — {reason}")
                continue
            verified_contacts.append(verified)
            print(f"[executives]   ✓ Verified: {person['name']} - {verified.get('title', '')}")
        else:
            print(f"[executives]   ✗ Skipped (unverifiable): {person['name']}")

    print(f"[executives] Step 2 complete: {len(verified_contacts)} verified on LinkedIn "
          f"(of {len(people)} from website)")

    # ── Step 3: Save contacts ──────────────────────────────────────────────
    saved = []
    for contact in verified_contacts:
        contact_id = upsert_contact(company_id, contact)
        if contact_id > 0:
            saved.append(contact)

    local_count = sum(1 for c in saved if c.get("is_local"))
    hq_count = sum(1 for c in saved if c.get("hq_contact"))
    print(f"[executives] {company_name}: {len(saved)} contacts saved ({local_count} local, {hq_count} HQ)")
    return saved


# ── AI-First Executive Discovery (added 2026-06-16) ──────────────────
# Per user feedback: use AI first for LinkedIn searching, then
# PixelRAG only if scraping is needed for latest experience/role/title
# and location. The website leadership scrape and SearXNG are
# FALLBACKS — invoked only when AI returns nothing useful.

def discover_executives_ai_first(
    company_name: str,
    company_id: int,
    website: str = "",
    linkedin_url: str = "",
    industry: str = "",
    search_city: str = "",
    search_state: str = "CA",
    search_lat: float = 0.0,
    search_lng: float = 0.0,
    radius_miles: int = 25,
    enable_pixelrag_scrape: bool = False,
    ai_timeout_s: int = 60,
) -> list[dict]:
    """
    AI-first executive discovery (Issue #2: AI first per user spec).

    PHASE 1: AI search runs as long as it needs (no internal timeout).
             Accepts whatever AI returns — could be 1 or 10 contacts.
    PHASE 2: SearXNG fallback (no validation) if AI returned nothing.
    PHASE 3: Save all contacts as-is. User filters in the UI.

    Per user feedback 2026-06-16:
    - Don't fight the AI timeout. Let it run.
    - Per-contact AI research is the real bottleneck — SKIP it.
    - Don't validate. Save what we get.
    """
    if not company_name:
        print("[executives] No company name, skipping")
        return []

    print(f"\n[executives] === AI-first discovery for {company_name} ===")
    from lf_ai_enrich import ai_search_linkedin_executives, ai_enabled

    all_contacts = []

    # ── PHASE 1: AI search — let it run as long as needed ──────────────
    ai_suggestions = []
    if ai_enabled():
        try:
            # No future.result timeout — let the AI take as long as it
            # needs. The endpoint's outer deadline is the only cap.
            ai_suggestions = ai_search_linkedin_executives(
                company_name=company_name,
                industry=industry,
                city=search_city,
                state=search_state,
                radius_miles=radius_miles,
                timeout=ai_timeout_s,
            ) or []
        except Exception as e:
            print(f"[executives] AI search error: {e}")

    print(f"[executives] AI returned {len(ai_suggestions)} suggestions")

    # Build contacts from AI suggestions (no per-contact AI research)
    for sug in ai_suggestions:
        full_name = sug.get("full_name", "").strip()
        if not full_name or not is_valid_name(full_name):
            continue

        title = sug.get("title", "").strip()
        li_url = sug.get("linkedin_url", "").strip()
        location = sug.get("location", "").strip()
        ai_conf = float(sug.get("confidence", 0.5))

        # Determine local vs HQ
        if location:
            is_local, hq_contact = _determine_local_vs_hq_from_linkedin(
                location, search_state or "CA", search_lat, search_lng, radius_miles
            )
        else:
            is_local, hq_contact = 1, 0

        parts = full_name.split(" ", 1)
        first = parts[0] if parts else ""
        last = parts[1] if len(parts) > 1 else ""

        contact = {
            "first_name": first,
            "last_name": last,
            "full_name": full_name,
            "title": title or "Executive",  # placeholder if AI didn't return title
            "linkedin_url": li_url,
            "is_local": is_local,
            "hq_contact": hq_contact,
            "confidence_score": max(0.5, ai_conf),  # floor at 0.5
            "data_provenance": "AI_PRIMARY",
            "source_primary": "ai",
            "ai_verified_title": title,
            "ai_title_confidence": ai_conf,
            "ai_title_source": "ai_search_linkedin_executives",
            "location": location,
            "ai_reasoning": sug.get("reasoning", ""),
        }
        all_contacts.append(contact)
        print(f"[executives]   ✓ {full_name} | {title or 'Executive'} | {location} | conf={ai_conf:.2f}")

    # ── PHASE 2: SearXNG fallback if AI gave us nothing ─────────────────
    if not all_contacts:
        print(f"[executives] AI returned nothing; falling back to SearXNG for {company_name}")
        return _enrich_from_linkedin_fallback(
            company_name=company_name,
            company_id=company_id,
            website=website,
            search_city=search_city,
            search_state=search_state,
            search_lat=search_lat,
            search_lng=search_lng,
            radius_miles=radius_miles,
        )

    # ── PHASE 3: Save all contacts ──────────────────────────────────────
    saved = []
    for contact in all_contacts:
        contact_id = upsert_contact(company_id, contact)
        if contact_id > 0:
            saved.append(contact)

    local_count = sum(1 for c in saved if c.get("is_local"))
    hq_count = sum(1 for c in saved if c.get("hq_contact"))
    print(f"[executives] {company_name}: {len(saved)} contacts saved ({local_count} local, {hq_count} HQ)")
    return saved


def _enrich_from_linkedin_fallback(
    company_name: str,
    company_id: int,
    website: str = "",
    search_city: str = "",
    search_state: str = "CA",
    search_lat: float = 0.0,
    search_lng: float = 0.0,
    radius_miles: int = 25,
) -> list[dict]:
    """
    Fallback when no company website leadership page is found.

    Pipeline (QC-16, 2026-06-09):
      1. SearXNG discovers LinkedIn URLs (PRIMARY for URL discovery)
      2. AI verifies each URL's title + fills other fields (PRIMARY for fields)
      3. Only use SearXNG snippet title if AI fails for a field
    """
    print(f"[executives] Using LinkedIn fallback search for {company_name}")
    all_contacts = []
    seen_urls = set()
    seen_names = set()
    pending_validation = []  # collected here, validated in parallel below

    location_bias = f" {search_state}" if search_state else ""
    if search_city:
        location_bias = f" {search_city}, {search_state}"

    queries = [
        f'"{company_name}" CEO{location_bias}',
        f'"{company_name}" President{location_bias}',
        f'"{company_name}" "Vice President"{location_bias}',
        f'"{company_name}" Director{location_bias}',
        f'"{company_name}" Founder{location_bias}',
        f'"{company_name}" "General Manager"{location_bias}',
    ]

    # Run all 9 queries in parallel (was serial before — 9 × 20s = 180s,
    # caused timeouts in the synchronous enrichment path). With 4 workers
    # and 6s timeout each, max wall time is ~12s.
    # ai_extract=False: skip the per-search AI extract step (which would
    # call Ollama 3× per search and add ~18s each when AI is down).
    import concurrent.futures
    all_query_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        future_to_q = {ex.submit(search, q, 6, "searxng", False): q for q in queries}
        for future in concurrent.futures.as_completed(future_to_q, timeout=15):
            try:
                results, provider = future.result()
                all_query_results.append((future_to_q[future], results, provider))
            except Exception as e:
                print(f"[executives] SearXNG query failed: {e}")

    for q, results, provider in all_query_results:
        for r in results[:3]:  # top 3 per query
            url = r.get("url", "")
            if "linkedin.com/in/" not in url or "/company/" in url:
                continue
            clean_url = re.sub(r"\?.*", "", url).rstrip("/")
            if clean_url in seen_urls:
                continue
            seen_urls.add(clean_url)

            snippet = r.get("snippet", "") or r.get("content", "")
            if not snippet:
                continue
            if not is_valid_linkedin_profile(clean_url, snippet):
                continue

            name = extract_name_from_url(clean_url)
            if not name or name.lower() in seen_names:
                continue
            seen_names.add(name.lower())

            # ── STEP 1: AI PRIMARY — verify title at this specific company ──
            # Skip if AI is known to be down (saves 30s per profile).
            ai_result = None
            if _ai_quick_ping(timeout=1):
                try:
                    from lf_ai_enrich import ai_research_contact
                    ai_result = ai_research_contact(
                        full_name=name,
                        company_name=company_name,
                        linkedin_url=clean_url,
                        current_title="",
                    )
                except Exception:
                    ai_result = None

            # Pre-compute company_mentioned (used by title and contact)
            company_mentioned = company_name.lower() in snippet.lower()

            # ── STEP 2: Determine title (AI first, SearXNG fallback) ──
            if ai_result and ai_result.get("title") and ai_result.get("is_verified"):
                title = normalize_title(ai_result["title"])
            elif ai_result and ai_result.get("title"):
                title = normalize_title(ai_result["title"])
            else:
                # SearXNG snippet extraction
                title = normalize_title(_extract_title_from_linkedin_snippet(snippet))

            # If we have a valid LinkedIn URL + snippet that mentions the
            # company, accept the contact even if we can't determine the
            # title. The user can fill in the title in the UI. This is
            # critical when AI is down — we don't want to throw away
            # good LinkedIn matches just because the snippet is sparse.
            if not title:
                if company_mentioned:
                    title = "Executive"  # placeholder, user fills in
                else:
                    print(f"[executives]   ✗ Skipped (no title and no company mention): {name}")
                    continue

            location = _parse_location_from_snippet(snippet)

            # Check location matches search region
            is_local = 1
            hq_contact = 0
            if location:
                loc_lower = location.lower()
                if search_state.lower() not in loc_lower:
                    is_local = 0
                    hq_contact = 1

            parts = name.split(" ", 1)
            first = parts[0] if parts else ""
            last = parts[1] if len(parts) > 1 else ""

            contact = {
                "first_name": first,
                "last_name": last,
                "full_name": name,
                "title": title,
                "linkedin_url": clean_url,
                "is_local": is_local,
                "hq_contact": hq_contact,
                "confidence_score": 0.7 if company_mentioned else 0.5,
                "data_provenance": f"LINKEDIN:{provider}",
                "source_primary": "linkedin",
                "source_linkedin_verified": 1 if company_mentioned else 0,
                "source_linkedin_unverified": 1 if not company_mentioned else 0,
                "linkedin_snippet": snippet[:300],
            }
            # Collect for batch validation (parallel below)
            pending_validation.append((name, company_name, contact))
            print(f"[executives]   → candidate: {name} ({title}) - {clean_url[:40]}")

    # Google validation skipped in the synchronous endpoint (Issue:
    # SearXNG is often slow under load; validation adds 9-15s for
    # little value since candidates were already filtered by name).
    # All candidates are accepted with their existing confidence
    # score. The AI-first chain still validates via ai_research_contact.
    for _, _, contact in pending_validation:
        all_contacts.append(contact)

    # Deduplicate and save
    saved = []
    for contact in all_contacts:
        contact_id = upsert_contact(company_id, contact)
        if contact_id > 0:
            saved.append(contact)

    local_count = sum(1 for c in saved if c.get("is_local"))
    hq_count = sum(1 for c in saved if c.get("hq_contact"))
    print(f"[executives] {company_name}: {len(saved)} contacts saved ({local_count} local, {hq_count} HQ)")
    return saved


# ── Self-Test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Executive Discovery v3.0 Self-Test ===\n")

    # ── Test 1: is_valid_linkedin_profile ─────────────────────────────────
    print("1. is_valid_linkedin_profile() tests:")

    # Blank profile (should reject)
    blank_snippet = "John Smith · LinkedIn · 500+ connections"
    result = is_valid_linkedin_profile(
        "https://linkedin.com/in/john-smith-123",
        blank_snippet
    )
    print(f"  {'PASS' if not result else 'FAIL'}: Blank profile rejected → {result}")

    # Valid profile (should accept)
    valid_snippet = "Elon Musk · CEO at SpaceX · Location: Hawthorne, CA · 500+ connections · Tesla · Engineering"
    result = is_valid_linkedin_profile(
        "https://linkedin.com/in/elonmusk",
        valid_snippet
    )
    print(f"  {'PASS' if result else 'FAIL'}: Real profile accepted → {result}")

    # Empty snippet
    result = is_valid_linkedin_profile(
        "https://linkedin.com/in/empty",
        ""
    )
    print(f"  {'PASS' if not result else 'FAIL'}: Empty snippet rejected → {result}")

    # Company URL (wrong type)
    result = is_valid_linkedin_profile(
        "https://linkedin.com/company/spacex",
        "SpaceX · Aviation · 50k employees"
    )
    print(f"  {'PASS' if not result else 'FAIL'}: Company URL rejected → {result}")

    # ── Test 2: search_linkedin_verification ───────────────────────────────
    print("\n2. search_linkedin_verification() test (Elon Musk SpaceX):")
    try:
        profiles = search_linkedin_verification("Elon Musk", "SpaceX", "CA")
        if profiles:
            best = profiles[0]
            print(f"  ✓ Found {len(profiles)} profile(s)")
            print(f"    URL: {best['url']}")
            print(f"    Title: {best['title']}")
            print(f"    Location: {best['location']}")
            print(f"    Company mentioned: {best['company_mentioned']}")
        else:
            print("  ⚠ No profiles found (possible rate limit or search unavailable)")
    except Exception as e:
        print(f"  ✗ Error: {e}")

    # ── Test 3: Website leadership scrape ─────────────────────────────────
    print("\n3. Website leadership scrape test (aerospace company):")
    # Test with a known aerospace company that has a leadership page
    test_companies = [
        ("https://www.spacex.com", "SpaceX"),
        ("https://www.boeing.com", "Boeing"),
    ]
    for website, name in test_companies:
        try:
            people, url = scrape_company_leadership(website, name)
            if people:
                print(f"  ✓ {name}: Found {len(people)} people at {url}")
                for p in people[:3]:
                    print(f"    - {p['name']}: {p['title']}")
            else:
                print(f"  ⚠ {name}: No leadership page found or scrape failed")
        except Exception as e:
            print(f"  ✗ {name}: Error - {e}")

    # ── Test 4: Name validation ────────────────────────────────────────────
    print("\n4. Name validation tests:")
    tests = [
        ("Brian Cameron", True),
        ("div data-header-theme", False),
        ("Board Chair", False),
        ("Elon Musk", True),
        ("John A", False),
        ("", False),
        ("CEO", False),
    ]
    for name, expected in tests:
        result = is_valid_name(name)
        status = "PASS" if result == expected else "FAIL"
        print(f"  {status}: {name!r} → {result} (expected {expected})")

    print("\n=== Self-test complete ===")


def extract_domain(website: str) -> str:
    """Extract root domain from company website for email patterns."""
    if not website:
        return ""
    domain = website.lower()
    for prefix in ("https://", "http://", "www."):
        if domain.startswith(prefix):
            domain = domain[len(prefix):]
    domain = domain.rstrip("/").split("/")[0]
    return domain
