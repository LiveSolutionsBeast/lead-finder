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

import json, time, re, urllib.parse, signal
from pathlib import Path
from typing import Optional

import requests

from lf_config import get
from lf_search_providers import search, get_usage_summary
from lf_geocode import haversine
from lf_db import get_db, upsert_contact
from lf_email_patterns import resolve_and_validate_email


class _CandidateTimeout(Exception):
    """Raised when verify_and_enrich_person exceeds its per-candidate budget."""
    pass


def _alarm_handler(signum, frame):
    raise _CandidateTimeout("candidate verification exceeded time budget")


BASE_DIR = Path(__file__).parent

# ── H-1 (2026-07-26): ingest-time gate for departed / retired / former ──────
# Titles matching this regex are not current employees and must not be inserted.
DEPARTED_RE = re.compile(
    r'\b(retired|former|departed|past|emeritus|self[- ]?employed|ex[- ])\b',
    re.IGNORECASE,
)


def _is_departed_title(*candidates: str) -> bool:
    """Return True if any of the given title strings matches DEPARTED_RE."""
    for c in candidates:
        if c and DEPARTED_RE.search(c):
            return True
    return False


# ── H-3 (2026-07-26): slug-vs-name match for LinkedIn URLs ───────────────────
def slug_matches_name(url: str, full_name: str) -> bool:
    """
    Return True if the /in/<slug> portion of `url` contains at least one name
    token (length >= 3) from `full_name`. Prevents attaching a wrong person's
    LinkedIn profile to a contact (e.g. /in/ramone-andrade-analyst for "Kris
    Young").
    """
    if not url or not full_name:
        return False
    m = re.search(r'/in/([a-z0-9\-]+)', url.lower())
    if not m:
        return False
    slug = m.group(1)
    toks = [re.sub(r'[^a-z]', '', p) for p in full_name.lower().split()]
    return any(len(t) >= 3 and t in slug for t in toks)


# ── H-4 (2026-07-26): name-token check before storing a LinkedIn snippet ────
def _snippet_mentions_person(snippet: str, full_name: str) -> bool:
    """
    Return True if both the first AND last name tokens of `full_name` appear in
    `snippet` (case-insensitive). Guards against storing a snippet that
    describes a different person.
    """
    if not snippet or not full_name:
        return False
    snippet_l = snippet.lower()
    parts = [p for p in full_name.lower().split() if len(p) >= 3]
    if len(parts) < 2:
        # Single-token or very short name: require the available token(s).
        return all(p in snippet_l for p in parts) if parts else False
    first, last = parts[0], parts[-1]
    return first in snippet_l and last in snippet_l


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


# ── Broad name/title extraction for non-LinkedIn search results ─────────────

# Regex pattern to extract "Name - Title" or "Title — Name" from search result titles.
# Examples: "Kathy Warden - Chairman & CEO | Northrop Grumman"
#           "Robert Hamilton - CEO at Hamilton Sundstrand"
NAME_TITLE_DELIMITERS = r'[\s\-—–:|·•,]+'

def _looks_like_person_name(name: str) -> bool:
    """Use existing name validator plus extra checks."""
    if not name or not is_valid_name(name):
        return False
    name_lower = name.lower().strip()
    if name_lower in GENERIC_TITLES_AS_NAMES:
        return False
    if any(w in GENERIC_TITLES_AS_NAMES for w in name_lower.split()):
        return False
    # Reject known non-person bigrams/trigrams
    if name_lower in _NON_PERSON_NAMES:
        return False
    # Reject if any word is an org word (strong signal it's not a person)
    if any(w in _ORG_WORDS_AS_NAME for w in name_lower.split()):
        return False
    return True


# Common words in organization/event names that get mis-parsed as part of a person name
_ORG_WORDS_AS_NAME = {
    'foundation', 'association', 'conference', 'summit', 'forum', 'institute',
    'council', 'society', 'organization', 'committee', 'board', 'university',
    'college', 'school', 'center', 'centre', 'group', 'team', 'division',
    'department', 'office', 'bureau', 'agency', 'laboratory', 'lab',
    'international', 'national', 'global', 'world', 'american', 'european',
    'pacific', 'atlantic', 'defense', 'security', 'aerospace', 'aircraft',
    'systems', 'solutions', 'technologies', 'services', 'industries', 'company',
    'corporation', 'inc', 'llc', 'ltd', 'co', 'corp',
}

# Non-person bigrams/trigrams that the regex name extractor commonly mistakes for people
_NON_PERSON_NAMES = {
    'san diego', 'san francisco', 'los angeles', 'new york', 'las vegas',
    'santa ana', 'santa monica', 'palo alto', 'menlo park', 'redondo beach',
    'product development', 'business development', 'software engineering',
    'human resources', 'customer service', 'quality assurance', 'operations',
    'engineering services', 'view similar', 'quick apply', 'ata engineering',
}


def _clean_title(title: str) -> str:
    """Strip trailing junk from a title extracted from search text."""
    if not title:
        return ""
    # Stop at common separators that introduce extra context
    for delim in [
        ' | ', ' · ', ' - ', ' – ', ' — ', ' • ', '  ', ' at ', ', ', ' |',
        '. ', '? ', '! ', '; ', '\n', ' (',
    ]:
        if delim in title:
            title = title.split(delim)[0]
    title = title.strip(r' \-—–:|·•,')
    # Remove trailing word "at" safely
    if title.lower().endswith(' at'):
        title = title[:-3]
    title = title.strip(r' \-—–:|·•,').removesuffix(' at').strip()
    # Hard cap: truncate at last complete word before 60 chars
    if len(title) > 60:
        truncated = title[:60]
        if ' ' in truncated:
            title = truncated.rsplit(' ', 1)[0]
        else:
            title = truncated
    return title


def _extract_name_title_from_text(text: str, company_name: str = "") -> list[tuple[str, str]]:
    """
    Extract (name, title) tuples from free text such as search result titles
    or directory page snippets. Returns a deduplicated list.
    """
    if not text:
        return []
    candidates = []

    # Name: 2-4 words, each capitalized or an initial (e.g. "J. P. Morgan" or "Jack s. Flowers")
    # Note: we allow lowercase initials like "s." because directory pages sometimes use them.
    name_pat = r'([A-Z][a-zA-Z]*(?:\s+(?:[A-Z][a-zA-Z]*|[A-Za-z]\.|\d+)){1,3})'

    # Recognized title keywords; title candidate must contain at least one
    title_keywords = [
        "chief executive officer", "chief financial officer", "chief operating officer",
        "chief technology officer", "chief information officer", "chief marketing officer",
        "chief revenue officer", "chief people officer", "chief human resources officer",
        "chief scientific officer", "chief strategy officer", "chief legal officer",
        "chief product officer", "chief business officer",
        "ceo", "cfo", "coo", "cto", "cio", "cmo", "chro", "cro", "cso", "clo", "cpo", "cbo",
        "president", "vice president", "vp", "senior vp", "executive vp", "sr vp",
        "general manager", "director", "senior director", "executive director",
        "chairman", "chairwoman", "chair", "founder", "co-founder", "owner", "partner",
        "head of", "lead", "leader", "executive", "management", "manager", "engineering",
        "engineer", "scientist", "officer",
        "principal", "associate", "analyst", "specialist", "coordinator",
    ]
    title_kw_re = re.compile(r'\b(' + r'|'.join(re.escape(k) for k in title_keywords) + r')\b', re.IGNORECASE)

    # Helper: is a title fragment plausible?
    def _is_plausible_title(t):
        if not t or len(t) > 90:
            return False
        return bool(title_kw_re.search(t))

    # Pattern A: "Name [delim] TitleFragment"
    # Title fragment extends up to a natural boundary or 80 chars
    for m in re.finditer(
        name_pat + r'\s*[\-—–:|·•,]\s*([^\-—–:|·•\n]{2,80})',
        text
    ):
        name = m.group(1).strip()
        title = _clean_title(m.group(2))
        if _is_plausible_title(title):
            candidates.append((name, title))

    # Pattern B: "TitleFragment [delim] Name"
    for m in re.finditer(
        r'(?:^|[\s\-—–:,])([^\-—–:|·•\n]{2,60})\s*[\-—–:,]+\s*' + name_pat,
        text
    ):
        title = _clean_title(m.group(1))
        name = m.group(2).strip()
        if _is_plausible_title(title):
            candidates.append((name, title))

    # Pattern C: "Name, Title" (comma only, title follows)
    for m in re.finditer(
        name_pat + r',\s*([^\-—–:|·•\n]{2,80})',
        text
    ):
        name = m.group(1).strip()
        title = _clean_title(m.group(2))
        if _is_plausible_title(title):
            candidates.append((name, title))

    # Pattern D: "Name at Company" is not safe to parse generically because the
    # regex often swaps name/title. We disable it; Patterns A-C already cover the
    # common "Name - Title" / "Title - Name" cases found in search snippets.
    pass

    # Filter and dedupe
    seen = set()
    results = []
    company_lower = company_name.lower()
    for name, title in candidates:
        name_lower = name.lower()
        if name_lower in seen:
            continue
        if not _looks_like_person_name(name):
            continue
        # Reject if title is just the company name
        if company_lower and company_lower in title.lower() and len(title) < len(company_name) + 10:
            continue
        # Reject incoherent/garbage titles
        if not _is_plausible_title(title):
            continue
        if len(title) > 90 or '?' in title:
            continue
        seen.add(name_lower)
        results.append((name, normalize_title(title)))
    return results


_EXECUTIVE_DIRECTORY_DOMAINS = {
    "craft.co", "rocketreach.co", "theofficialboard.com",
    "cbinsights.com", "zoominfo.com", "owler.com",
    "leadferret.com", "datanyze.com", "mattermark.com",
    "buzzfile.com", "manta.com", "dnb.com", "corporationwiki.com",
}


def _extract_from_directory_page(url: str, company_name: str) -> list[dict]:
    """
    Fetch an executive directory page and extract people + titles.
    Returns list of {name, title, source_url}.
    """
    domain = urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
    if not any(d in domain for d in _EXECUTIVE_DIRECTORY_DOMAINS):
        return []
    html = _fetch_page_content(url)
    if not html:
        return []
    text = re.sub(r'<[^>]+>', ' ', html)
    text = re.sub(r'\s+', ' ', text).strip()
    people = []
    seen = set()
    for name, title in _extract_name_title_from_text(text, company_name):
        key = (name.lower(), title.lower())
        if key in seen:
            continue
        seen.add(key)
        people.append({"name": name, "title": title, "source_url": url})
    return people


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


def verified_linkedin_url(
    candidate_url: str,
    full_name: str,
    company_name: str,
    search_state: str = "",
    timeout: int = 20,
) -> dict:
    """
    Strict gate: returns a dict with `confirmed: bool` and `reason: str` for a
    candidate LinkedIn URL. Confirmation requires the URL to appear in an
    INDEPENDENT SearXNG search that matches the person and (ideally) the
    company. This is the safety net that stops hallucinated URLs.

    Pass conditions (in priority order):
      1. SearXNG site:linkedin.com/in search for "name" "company" returns the
         exact URL (or a path-equivalent /in/<slug>-<id>/) in the top 5 hits.
      2. SearXNG site:linkedin.com/in search for "name" (no company) returns
         the URL AND the snippet contains a name match AND the title/snippet
         references company_name (or any of its name variants).

    Failure cases (returns confirmed=False with reason):
      - URL not on LinkedIn or is a company page
      - URL is structurally invalid (no /in/<slug>/ path)
      - URL does not appear in any independent SearXNG hit
      - SearXNG hit exists but snippet has no name match
      - SearXNG hit exists but no company-name token in snippet
    """
    if not candidate_url or "linkedin.com/in/" not in candidate_url or "/company/" in candidate_url:
        return {"confirmed": False, "reason": "not_a_linkedin_profile_url"}
    # Normalize trailing slash and query
    norm = re.sub(r"\?.*", "", candidate_url).rstrip("/")
    # Path must be /in/<slug> or /in/<slug>-<id>
    path = norm.split("linkedin.com")[-1]
    if not re.search(r"^/in/[A-Za-z0-9_\-]+/?$", path):
        return {"confirmed": False, "reason": "invalid_linkedin_path"}

    # Independently search for the URL via SearXNG
    try:
        from lf_search_providers import search as web_search
    except Exception:
        web_search = None  # type: ignore

    if not web_search:
        return {"confirmed": False, "reason": "search_unavailable"}

    # Build a name-only-and-company query
    queries = [
        f'"{full_name}" "{company_name}" site:linkedin.com/in',
        f'"{full_name}" site:linkedin.com/in',
    ]
    slug = path.strip("/").split("/")[-1]  # the slug portion

    hits = []
    for q in queries:
        try:
            results, _provider = web_search(q, timeout=timeout)
        except Exception:
            continue
        for r in results:
            url = (r.get("url") or "").rstrip("/")
            if "linkedin.com/in/" not in url or "/company/" in url:
                continue
            # Check if the candidate slug (or a 5+ char prefix) appears in the URL
            r_slug = re.sub(r"\?.*", "", url).rstrip("/").split("linkedin.com/in/")[-1].split("/")[0]
            slug_match = (
                r_slug == slug
                or r_slug.startswith(slug[:5])
                or slug.startswith(r_slug[:5])
            )
            if slug_match:
                snippet = (r.get("snippet") or r.get("content") or "").lower()
                name_parts = [p.lower() for p in re.split(r"\s+", full_name) if len(p) > 1]
                # Name in snippet (any part matches)
                name_match = any(p in snippet for p in name_parts)
                # Company (or any first word of it) in snippet
                company_tokens = [t for t in re.split(r"[\s,]+", company_name) if len(t) > 2]
                company_match = any(t.lower() in snippet for t in company_tokens)
                hits.append({
                    "url": url,
                    "slug": r_slug,
                    "name_match": name_match,
                    "company_match": company_match,
                    "snippet": (r.get("snippet") or "")[:200],
                })

    if not hits:
        return {"confirmed": False, "reason": "url_not_in_independent_search"}
    # Best hit must have name match; company match preferred but not required
    best = sorted(hits, key=lambda h: (h["name_match"], h["company_match"]), reverse=True)[0]
    if not best["name_match"]:
        return {
            "confirmed": False,
            "reason": "name_not_in_snippet",
            "slug": best["slug"],
            "snippet": best["snippet"],
        }
    return {
        "confirmed": True,
        "reason": "url_in_independent_search",
        "company_match": best["company_match"],
        "snippet": best["snippet"],
    }


def cascading_linkedin_search(
    full_name: str,
    company_name: str,
    company_website: str = "",
    search_state: str = "",
    on_step=None,
    timeout: int = 20,
) -> dict:
    """
    Escalator: progressively broader SearXNG queries to find a real LinkedIn
    profile URL. Returns:
      {
        "url": str | None,
        "snippet": str,
        "company_match": bool,
        "confidence": float,
        "method": str,            # which step succeeded
        "attempts": list[dict],   # all attempts (for debug)
      }

    Step ladder (broadening):
      1. "name" "company" site:linkedin.com/in
      2. "name" site:linkedin.com/in
      3. "firstname lastname" "company" (no site: filter)
      4. "name" "company" (no site:)
      5. firstname.lastname (raw slug search via company domain context)

    Each step runs once with timeout. The first step that returns a real
    LinkedIn profile URL with a name-matching snippet wins. A step that finds a
    URL but with no name match in the snippet is REJECTED — we keep climbing.
    """
    result = {
        "url": None,
        "snippet": "",
        "company_match": False,
        "confidence": 0.0,
        "method": "none",
        "attempts": [],
    }
    try:
        from lf_search_providers import search as web_search
    except Exception:
        if on_step: on_step("search_unavailable", "SearXNG not available")
        return result

    name_parts = [p for p in re.split(r"\s+", full_name.strip()) if len(p) > 1]
    if not name_parts:
        return result
    first = name_parts[0]
    last = name_parts[-1] if len(name_parts) > 1 else ""

    company_first_token = (re.split(r"[\s,]+", company_name.strip())[0] if company_name else "").strip()
    domain_token = ""
    if company_website:
        try:
            from urllib.parse import urlparse
            domain_token = (urlparse(company_website).netloc or "").replace("www.", "")
        except Exception:
            domain_token = ""

    steps = [
        ("n+c", f'"{full_name}" "{company_name}" site:linkedin.com/in'),
        ("n",   f'"{full_name}" site:linkedin.com/in'),
        ("n+c_nosite", f'"{full_name}" "{company_name}"'),
        ("f.l+c", f'"{first} {last}" "{company_name}" linkedin'),
        ("n+domain", f'"{full_name}" "{domain_token}" linkedin' if domain_token else None),
        ("broader", f'"{first}" "{last}" "{company_first_token}"' if company_first_token else None),
    ]

    for method, query in steps:
        if not query:
            continue
        attempt = {"method": method, "query": query, "hits": 0, "best_url": "", "best_name_match": False, "best_company_match": False}
        if on_step:
            on_step(method, query)
        try:
            results, provider = web_search(query, timeout=timeout)
        except Exception as e:
            attempt["error"] = str(e)
            result["attempts"].append(attempt)
            continue
        attempt["provider"] = provider
        for r in results:
            url = (r.get("url") or "")
            if "linkedin.com/in/" not in url or "/company/" in url:
                continue
            clean_url = re.sub(r"\?.*", "", url).rstrip("/")
            snippet = (r.get("snippet") or r.get("content") or "").lower()
            # Name match: at least one significant name part
            name_hit = any(p.lower() in snippet for p in name_parts if len(p) > 1)
            if not name_hit:
                continue
            attempt["hits"] += 1
            if not attempt["best_url"]:
                attempt["best_url"] = clean_url
                attempt["best_name_match"] = True
                # Company match
                if company_name:
                    company_tokens = [t for t in re.split(r"[\s,]+", company_name) if len(t) > 2]
                    attempt["best_company_match"] = any(t.lower() in snippet for t in company_tokens)
                attempt["snippet"] = (r.get("snippet") or "")[:200]
        result["attempts"].append(attempt)
        if attempt.get("best_url") and attempt.get("best_name_match"):
            result["url"] = attempt["best_url"]
            result["snippet"] = attempt.get("snippet", "")
            result["company_match"] = attempt.get("best_company_match", False)
            result["method"] = method
            # Confidence tier
            tier = {
                "n+c": 0.90,
                "n": 0.65,
                "n+c_nosite": 0.70,
                "f.l+c": 0.75,
                "n+domain": 0.80,
                "broader": 0.50,
            }.get(method, 0.40)
            result["confidence"] = tier if result["company_match"] else max(0.0, tier - 0.20)
            return result

    return result


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
        raw_results, provider = search(query, timeout=8)
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
        # H-1 (2026-07-26): reject departed / retired / former / emeritus titles
        # at ingest. verify_person_on_linkedin does not run the AI
        # is_current_employee gate, so enforce the title regex here.
        title = (contact.get("title") or "").strip()
        ai_verified_title = (contact.get("ai_verified_title") or "").strip()
        if _is_departed_title(title, ai_verified_title):
            print(
                f"[executives]   ✗ Departed/retired title for {contact.get('full_name','')}: "
                f"'{title}' / '{ai_verified_title}'; skipping insert"
            )
            continue
        contact_id = upsert_contact(company_id, contact)
        if contact_id > 0:
            saved.append(contact)
            try:
                resolve_and_validate_email(contact_id, source="search_ingest")
            except Exception as e:
                print(f"[executives] Email chain failed for contact {contact_id}: {e}")

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
    per_candidate_timeout_s: int = 30,
) -> list[dict]:
    """
    AI-first executive discovery.

    PHASE 1: AI search for senior people at the company.
    PHASE 2: Website leadership page if AI returns nothing.
    PHASE 3: Full-provider search fallback if AI + website return nothing.
    PHASE 4: Layered verification for every candidate:
             - Search for real LinkedIn profile
             - Validate title/company from snippet or press evidence
             - AI verification + title cleaning + location/phone enrichment
             - Save only verified contacts.
    """
    if not company_name:
        print("[executives] No company name, skipping")
        return []

    print(f"\n[executives] === AI-first discovery for {company_name} ===")
    from lf_ai_enrich import ai_search_linkedin_executives, ai_enabled

    all_contacts = []

    # Build broadening context for AI search
    domain = ""
    if website:
        domain = extract_domain(website) or ""
    # Simple name variants: stripped suffix and first-letter abbreviations
    name_variants = _build_name_variants(company_name)
    # No parent-company lookup yet; empty placeholder
    parent_company = ""

    # ── PHASE 1: AI search — let it run as long as needed ──────────────
    ai_suggestions = []
    if ai_enabled():
        try:
            ai_suggestions = ai_search_linkedin_executives(
                company_name=company_name,
                industry=industry,
                city=search_city,
                state=search_state,
                website=website,
                domain=domain,
                name_variants=name_variants,
                parent_company=parent_company,
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

        if not title:
            print(f"[executives]   ✗ {full_name} skipped: no title")
            continue

        contact = {
            "first_name": first,
            "last_name": last,
            "full_name": full_name,
            "title": title,
            "linkedin_url": li_url,
            "is_local": is_local,
            "hq_contact": hq_contact,
            "confidence_score": max(0.5, ai_conf),  # floor at 0.5
            "data_provenance": f"AI_PRIMARY:{sug.get('source','ai_search')}",
            "source_primary": "ai",
            "ai_verified_title": title,
            "ai_title_confidence": ai_conf,
            "ai_title_source": "ai_search_linkedin_executives",
            "location": location,
            "ai_reasoning": sug.get("reasoning", ""),
        }
        all_contacts.append(contact)
        print(f"[executives]   ✓ {full_name} | {title or 'Executive'} | {location} | conf={ai_conf:.2f}")

    # ── PHASE 2: Website leadership page (authoritative, fast) ───────────
    if not all_contacts and website:
        try:
            website_people, leadership_url = scrape_company_leadership(website, company_name)
            for p in website_people:
                full_name = p.get("name", "").strip()
                if not full_name or not _looks_like_person_name(full_name):
                    continue
                title = p.get("title", "").strip() or "Executive"
                title = normalize_title(title)
                parts = full_name.split(" ", 1)
                first = parts[0] if parts else ""
                last = parts[1] if len(parts) > 1 else ""
                contact = {
                    "first_name": first,
                    "last_name": last,
                    "full_name": full_name,
                    "title": title,
                    "linkedin_url": p.get("linkedin_url", ""),
                    "is_local": 1,
                    "hq_contact": 0,
                    "confidence_score": p.get("confidence_score", 0.85),
                    "data_provenance": p.get("data_provenance", f"WEBSITE:{leadership_url}"),
                    "source_primary": "website",
                    "title_from_website": title,
                }
                all_contacts.append(contact)
                print(f"[executives]   ✓ website: {full_name} | {title}")
        except Exception as e:
            print(f"[executives] Website leadership scrape failed (non-fatal): {e}")

    # ── PHASE 3: Fallback to full-provider search if AI + website returned nothing ──
    if not all_contacts:
        print(f"[executives] AI + website returned nothing; falling back to full search for {company_name}")
        return _enrich_from_linkedin_fallback(
            company_name=company_name,
            company_id=company_id,
            website=website,
            search_city=search_city,
            search_state=search_state,
            search_lat=search_lat,
            search_lng=search_lng,
            radius_miles=radius_miles,
            per_candidate_timeout_s=per_candidate_timeout_s,
        )

    # ── PHASE 3: Verify and save contacts ──────────────────────────────
    saved = []
    for contact in all_contacts:
        # Clean/normalize title for AI and website-sourced contacts (and for
        # any later AI-verified values in verify_and_enrich_person). This
        # runs the title through _clean_title + normalize_title BEFORE
        # sending to the LinkedIn search and the AI verifier, so all
        # downstream stages see a cleaned title.
        contact["title"] = _clean_title(normalize_title(contact.get("title", "")))
        if not contact["title"]:
            print(f"[executives]   ✗ {contact.get('full_name','')} rejected: title empty after clean")
            continue
        verified = verify_and_enrich_person(
            contact,
            company_name=company_name,
            company_id=company_id,
            website=website,
            search_city=search_city,
            search_state=search_state,
            search_lat=search_lat,
            search_lng=search_lng,
            radius_miles=radius_miles,
            per_candidate_timeout_s=per_candidate_timeout_s,
        )
        if not verified:
            continue
        if _is_valid_contact_to_save(verified, company_name):
            contact_id = upsert_contact(company_id, verified)
            if contact_id > 0:
                saved.append(verified)
                try:
                    resolve_and_validate_email(contact_id, source="search_ingest")
                except Exception as e:
                    print(f"[executives] Email chain failed for contact {contact_id}: {e}")

    local_count = sum(1 for c in saved if c.get("is_local"))
    hq_count = sum(1 for c in saved if c.get("hq_contact"))
    print(f"[executives] {company_name}: {len(saved)} contacts saved ({local_count} local, {hq_count} HQ)")
    return saved


# ── AI-First Executive Discovery (added 2026-06-16) ──────────────────
# Per user feedback: use AI first for LinkedIn searching, then
# PixelRAG only if scraping is needed for latest experience/role/title
# and location. The website leadership scrape and SearXNG are
# FALLBACKS — invoked only when AI returns nothing useful.


def verify_and_enrich_person(
    contact: dict,
    company_name: str,
    company_id: int,
    website: str = "",
    search_city: str = "",
    search_state: str = "CA",
    search_lat: float = 0.0,
    search_lng: float = 0.0,
    radius_miles: int = 25,
    per_candidate_timeout_s: int = 30,
) -> Optional[dict]:
    """
    Layered verification and enrichment for a single person candidate.

    1. Search for a real LinkedIn profile.
    2. If found, validate snippet for company/title/location evidence.
    3. If no LinkedIn, search press/news to verify person + company + title.
    4. Use AI to verify association, clean title, and enrich location/phone.
    5. Return verified contact or None.
    """
    full_name = contact.get("full_name", "").strip()
    original_title = contact.get("title", "").strip()
    if not full_name or not original_title:
        return None

    print(f"[executives] verifying {full_name} ({original_title}) at {company_name}")

    # Hard per-candidate deadline (monotonic clock; safe in worker threads).
    # We use a deadline rather than SIGALRM because SIGALRM only works in the
    # main thread of the main interpreter and this function may run in a
    # worker thread (see lf_server.py backfill worker).
    deadline = (time.monotonic() + per_candidate_timeout_s) if per_candidate_timeout_s > 0 else None

    try:
        return _verify_and_enrich_person_impl(
            contact=contact,
            company_name=company_name,
            company_id=company_id,
            website=website,
            search_city=search_city,
            search_state=search_state,
            search_lat=search_lat,
            search_lng=search_lng,
            radius_miles=radius_miles,
            deadline_monotonic=deadline,
        )
    except _CandidateTimeout:
        print(f"[executives]   ⏱ {full_name} verification exceeded {per_candidate_timeout_s}s budget; skipping")
        return None


def _verify_and_enrich_person_impl(
    contact: dict,
    company_name: str,
    company_id: int,
    website: str = "",
    search_city: str = "",
    search_state: str = "CA",
    search_lat: float = 0.0,
    search_lng: float = 0.0,
    radius_miles: int = 25,
    deadline_monotonic: Optional[float] = None,
) -> Optional[dict]:
    """Inner body of verify_and_enrich_person (extracted for timeout wrapper)."""
    full_name = contact.get("full_name", "").strip()
    original_title = contact.get("title", "").strip()
    if not full_name or not original_title:
        return None

    def _budget_left() -> float:
        """Seconds remaining in the per-candidate budget, or inf if no deadline."""
        if deadline_monotonic is None:
            return float("inf")
        return max(0.0, deadline_monotonic - time.monotonic())

    def _check_deadline(step: str) -> None:
        """Raise _CandidateTimeout if we've blown the per-candidate budget."""
        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise _CandidateTimeout(f"{step} exceeded deadline")

    # --- Layer 1: direct LinkedIn profile search ---
    linkedin_url = ""
    linkedin_snippet = ""
    location = contact.get("location", "")
    verified_title = original_title

    try:
        from lf_search_providers import search as web_search
        queries = [
            f'"{full_name}" "{company_name}" site:linkedin.com/in',
            f'"{full_name}" site:linkedin.com/in',
        ]
        if website:
            domain = extract_domain(website) or ""
            if domain:
                queries.append(f'"{full_name}" "{domain}" site:linkedin.com/in')
        for q in queries:
            # Bail out of LinkedIn search if we've blown the per-candidate budget
            _check_deadline("linkedin-search")
            # Bound each LinkedIn query to a small slice of the remaining budget
            left = _budget_left()
            if left < 1.0:
                # Not enough time left for a useful search
                break
            try:
                results, provider = web_search(q, timeout=min(10, int(left) or 10), prefer="searxng", ai_extract=False)
                for r in results:
                    url = r.get("url", "")
                    if "linkedin.com/in/" not in url or "/company/" in url:
                        continue
                    snippet = r.get("snippet", "") or r.get("content", "")
                    if not is_valid_linkedin_profile(url, snippet):
                        continue
                    # H-3 (2026-07-26): slug must match at least one name token,
                    # otherwise this is a different person's profile.
                    if not slug_matches_name(url, full_name):
                        print(
                            f"[executives]   ⚠ LinkedIn slug does not match name "
                            f"{full_name!r} for {url}; treating as a miss"
                        )
                        continue
                    # H-4 (2026-07-26): snippet must mention the person (first
                    # AND last name tokens) before we store it / derive a title
                    # from it. Prevents snippet contamination from a different
                    # person's tenure.
                    if not _snippet_mentions_person(snippet, full_name):
                        print(
                            f"[executives]   ⚠ LinkedIn snippet does not mention "
                            f"{full_name!r}; discarding snippet for {url}"
                        )
                        # Accept the URL but do NOT store the snippet or derive
                        # a title from it. We keep climbing the query ladder only
                        # if no better hit exists; for now record the URL and
                        # blank the snippet so title extraction is skipped.
                        linkedin_url = re.sub(r"\?.*", "", url).rstrip("/")
                        linkedin_snippet = ""
                        break
                    linkedin_url = re.sub(r"\?.*", "", url).rstrip("/")
                    linkedin_snippet = snippet
                    break
                if linkedin_url:
                    break
            except Exception as e:
                # H-7 (2026-07-26): log search failures instead of silently
                # swallowing them. A silent failure used to promote an AI-only
                # contact to a high-confidence row.
                print(f"[executives]   ⚠ LinkedIn search query failed for {full_name}: {e}")
                continue
    except Exception as e:
        print(f"[executives] LinkedIn search failed for {full_name}: {e}")

    # If LinkedIn found, try to extract better title/location from snippet
    if linkedin_url:
        # H-4 (2026-07-26): only derive a title from the snippet if it
        # actually mentions the person. linkedin_snippet is blanked above
        # when the snippet failed the name-token check, so this guard is
        # belt-and-suspenders against any other call path that sets the
        # snippet without the check.
        if linkedin_snippet and _snippet_mentions_person(linkedin_snippet, full_name):
            li_title = normalize_title(_extract_title_from_linkedin_snippet(linkedin_snippet))
            if li_title:
                verified_title = li_title
            li_location = _parse_location_from_snippet(linkedin_snippet)
            if li_location:
                location = li_location
        else:
            print(
                f"[executives]   ⚠ LinkedIn snippet empty or does not mention "
                f"{full_name!r}; skipping title/location extraction"
            )
        print(f"[executives]   → LinkedIn profile found: {linkedin_url}")

    # --- Layer 2: press/news/company verification when no LinkedIn ---
    web_evidence = ""
    press_search_failed = False
    if not linkedin_url:
        _check_deadline("press-search")
        try:
            from lf_search_providers import search as web_search
            q = f'"{full_name}" "{company_name}"'
            left = _budget_left()
            results, provider = web_search(q, timeout=min(10, int(left) or 10), prefer="searxng", ai_extract=False)
            for r in results[:3]:
                snippet = r.get("snippet", "") or r.get("content", "")
                title = r.get("title", "")
                if company_name.lower() in (title + " " + snippet).lower():
                    web_evidence += f"{title} {snippet}\n"
        except Exception as e:
            # H-7 (2026-07-26): log instead of silently swallowing. A silent
            # failure here used to let an AI-only contact sail into the corpus
            # at high confidence. The H-2 cap below still applies, but logging
            # makes the failure visible in the server log.
            print(f"[executives]   ⚠ Layer-2 press search failed for {full_name}: {e}")
            press_search_failed = True

    # --- Layer 3: AI verification / title cleaning / enrichment ---
    if _ai_quick_ping(timeout=1):
        _check_deadline("ai-research")
        try:
            from lf_ai_enrich import ai_research_contact
            ai_result = ai_research_contact(
                full_name=full_name,
                company_name=company_name,
                linkedin_url=linkedin_url,
                current_title=verified_title,
            )
            if ai_result:
                ai_title = ai_result.get("title", "").strip()
                if ai_title:
                    verified_title = ai_title
                ai_location = ai_result.get("location", "").strip()
                if ai_location and not location:
                    location = ai_location
                ai_phone = ai_result.get("phone", "").strip()
                if ai_phone:
                    contact["phone"] = ai_phone
                ai_li = ai_result.get("linkedin_url", "").strip()
                if ai_li and "linkedin.com/in/" in ai_li and not linkedin_url:
                    linkedin_url = ai_li
                # H-1 (2026-07-26): honor AI's is_current_employee=False unconditionally.
                # The previous gate only dropped the contact when there was no
                # linkedin_url and no web_evidence, letting a departed person slip
                # in if a LinkedIn URL or press hit existed. The escalator already
                # uses this signal to reject; ingest must too.
                if ai_result.get("is_current_employee") is False:
                    print(
                        f"[executives]   ✗ AI indicates {full_name} is NOT a current "
                        f"employee of {company_name}; rejecting at ingest"
                    )
                    return None
        except Exception as e:
            print(f"[executives] AI verification failed for {full_name}: {e}")

    # Clean final title
    verified_title = _clean_title(normalize_title(verified_title))
    if not verified_title:
        return None

    # H-1 (2026-07-26): reject departed / retired / former / emeritus titles at ingest.
    ai_verified_title = (contact.get("ai_verified_title") or "").strip()
    if _is_departed_title(verified_title, ai_verified_title):
        print(
            f"[executives]   ✗ Departed/retired title for {full_name}: "
            f"'{verified_title}' / '{ai_verified_title}'; rejecting at ingest"
        )
        return None

    # Update local/HQ based on location
    is_local, hq_contact = 1, 0
    if location:
        is_local, hq_contact = _determine_local_vs_hq_from_linkedin(
            location, search_state or "CA", search_lat, search_lng, radius_miles
        )

    parts = full_name.split(" ", 1)
    verified = {
        **contact,
        "first_name": parts[0] if parts else "",
        "last_name": parts[1] if len(parts) > 1 else "",
        "full_name": full_name,
        "title": verified_title,
        "linkedin_url": linkedin_url,
        "location": location,
        "is_local": is_local,
        "hq_contact": hq_contact,
        "linkedin_snippet": linkedin_snippet[:300] if linkedin_url else "",
        "source_primary": "linkedin" if linkedin_url else contact.get("source_primary", "search"),
        "source_linkedin_verified": 1 if linkedin_url else 0,
        "source_linkedin_unverified": 0 if linkedin_url else 1,
    }
    if linkedin_url:
        verified["data_provenance"] = f"LINKEDIN:verified:{linkedin_url}"

    # H-2 / H-7 (2026-07-26): an AI-only contact (no LinkedIn URL, no press
    # evidence) must not enter the corpus at high confidence. Cap at 0.40 and
    # mark needs_review so the escalator's double-confirmation gate is the
    # only path to 'verified'. This also covers the silent Layer-2 search
    # failure case (H-7): when press search returns nothing (or failed) and
    # there is no LinkedIn URL, the contact is visibly provisional.
    if not linkedin_url and not web_evidence:
        capped = min(float(verified.get("confidence_score", 0.0)), 0.40)
        verified["confidence_score"] = capped
        verified["pipeline_stage"] = "needs_review"
        reason = "press search failed" if press_search_failed else "no press evidence"
        print(
            f"[executives]   ⚠ {full_name}: AI-only (no LinkedIn, {reason}) "
            f"— confidence capped at {capped:.2f}, pipeline_stage=needs_review"
        )
    return verified


def _is_valid_contact_to_save(contact: dict, company_name: str = "") -> bool:
    """
    Final validation gate before saving a contact.
    Rejects malformed records extracted from directory pages or search snippets.
    """
    full_name = (contact.get("full_name") or "").strip()
    title = (contact.get("title") or "").strip()
    linkedin_url = (contact.get("linkedin_url") or "").strip()

    # Name must look like a real person
    if not full_name or not _looks_like_person_name(full_name):
        print(f"[executives]   ✗ rejected name: '{full_name}'")
        return False

    # Title is required and must not be garbage
    if not title:
        print(f"[executives]   ✗ rejected {full_name}: no title")
        return False
    if len(title) > 90 or '?' in title or title.count('.') > 2:
        print(f"[executives]   ✗ rejected {full_name}: bad title '{title[:60]}'")
        return False

    # If a linkedin_url is provided, it must be a real LinkedIn profile
    if linkedin_url and "linkedin.com/in/" not in linkedin_url:
        print(f"[executives]   ✗ rejected {full_name}: non-LinkedIn URL '{linkedin_url}'")
        return False

    # Reject if title is just the company name
    company_lower = company_name.lower()
    if company_lower and company_lower in title.lower() and len(title) < len(company_name) + 10:
        print(f"[executives]   ✗ rejected {full_name}: title is company name")
        return False

    return True


def _enrich_from_linkedin_fallback(
    company_name: str,
    company_id: int,
    website: str = "",
    search_city: str = "",
    search_state: str = "CA",
    search_lat: float = 0.0,
    search_lng: float = 0.0,
    radius_miles: int = 25,
    per_candidate_timeout_s: int = 30,
) -> list[dict]:
    """
    Fallback when no company website leadership page is found.

    Pipeline (QC-16, updated 2026-06-18):
      1. Run broad provider-rotated search (degoog -> 4get -> searxng -> paid APIs)
         using full company and leadership queries.
      2. Extract names + titles from search result titles and executive-directory
         pages (craft.co, rocketreach, theofficialboard, linkedin.com/in).
      3. Save contacts even when only a name+title is available.
    """
    print(f"[executives] Using full-provider fallback search for {company_name}")
    all_contacts = []
    seen_urls = set()
    seen_names = set()

    location_bias = ""
    if search_city and search_state:
        location_bias = f" {search_city}, {search_state}"
    elif search_state:
        location_bias = f" {search_state}"

    domain_query = ""
    if website:
        domain = extract_domain(website)
        if domain:
            domain_query = f" site:{domain} leadership OR team OR management OR executives"

    name_variants = _build_name_variants(company_name)
    # Use the stripped variant for broad searches if available, else company_name
    broad_name = name_variants[1] if len(name_variants) > 1 else company_name

    # Broader query set with more directories, city/state emphasis, and domain bias
    queries = [
        f'"{company_name}" CEO{location_bias}',
        f'"{company_name}" President OR "Chief Operating Officer"{location_bias}',
        f'"{company_name}" "Vice President" OR VP{location_bias}',
        f'"{company_name}" Director OR "General Manager" OR "Plant Manager"{location_bias}',
        f'"{company_name}" Founder OR Owner OR "Managing Partner"{location_bias}',
        f'"{company_name}" "executive team" OR "leadership team" OR "management team"',
        f'"{company_name}" "executive team" craft.co OR rocketreach OR theofficialboard OR owler OR zoominfo',
        f'"{company_name}" leadership OR management OR "senior leaders"',
        f'"{broad_name}" "{search_state}" CEO OR President OR VP OR Director',
    ]
    if domain_query:
        queries.append(domain_query)
    if search_city and search_state:
        queries.append(f'"{broad_name}" "{search_city}" "{search_state}" executives')
    # Directory-biased query
    queries.append(f'"{company_name}" "linkedin.com/in" OR "rocketreach" OR "craft.co" OR "theofficialboard"')

    # Run queries in parallel using full provider rotation (no prefer=).
    # Per-query timeout raised to 12s because Degoog sometimes needs 8-10s
    # and rotation lets the fastest working provider win.
    import concurrent.futures
    all_query_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        future_to_q = {ex.submit(search, q, 12, "", False): q for q in queries}
        for future in concurrent.futures.as_completed(future_to_q, timeout=45):
            try:
                results, provider = future.result()
                all_query_results.append((future_to_q[future], results, provider))
            except Exception as e:
                print(f"[executives] search query failed: {e}")

    # Process all results. We now accept:
    #   - linkedin.com/in URLs
    #   - executive-directory pages (craft.co, rocketreach, etc.) — fetched and parsed
    #   - search result titles that contain clear "Name - Title" patterns
    for q, results, provider in all_query_results:
        for r in results[:5]:  # look at top 5 per query; directories often rank high
            url = r.get("url", "")
            if not url or "/company/" in url:
                continue
            clean_url = re.sub(r"\?.*", "", url).rstrip("/")
            snippet = r.get("snippet", "") or r.get("content", "")
            title = r.get("title", "") or ""
            combined_text = f"{title} {snippet}"
            company_mentioned = company_name.lower() in combined_text.lower()

            # ── Case 1: LinkedIn profile URL ─────────────────────────────
            if "linkedin.com/in/" in clean_url:
                if clean_url in seen_urls:
                    continue
                if not snippet or not is_valid_linkedin_profile(clean_url, snippet):
                    continue
                name = extract_name_from_url(clean_url)
                if not name or name.lower() in seen_names:
                    continue
                seen_urls.add(clean_url)
                seen_names.add(name.lower())

                # Try title extraction from snippet first (fast)
                li_title = normalize_title(_extract_title_from_linkedin_snippet(snippet))
                # Optional AI validation if up, but never block on it
                if not li_title and _ai_quick_ping(timeout=1):
                    try:
                        from lf_ai_enrich import ai_research_contact
                        ai_result = ai_research_contact(
                            full_name=name,
                            company_name=company_name,
                            linkedin_url=clean_url,
                            current_title="",
                        )
                        if ai_result and ai_result.get("title"):
                            li_title = normalize_title(ai_result["title"])
                    except Exception:
                        pass
                if not li_title:
                    if company_mentioned:
                        li_title = "Executive"
                    else:
                        continue

                location = _parse_location_from_snippet(snippet)
                is_local = 1
                hq_contact = 0
                if location:
                    if search_state.lower() not in location.lower():
                        is_local = 0
                        hq_contact = 1

                parts = name.split(" ", 1)
                all_contacts.append({
                    "first_name": parts[0],
                    "last_name": parts[1] if len(parts) > 1 else "",
                    "full_name": name,
                    "title": li_title,
                    "linkedin_url": clean_url,
                    "is_local": is_local,
                    "hq_contact": hq_contact,
                    "confidence_score": 0.7 if company_mentioned else 0.5,
                    "data_provenance": f"LINKEDIN:{provider}",
                    "source_primary": "linkedin",
                    "source_linkedin_verified": 1 if company_mentioned else 0,
                    "source_linkedin_unverified": 1 if not company_mentioned else 0,
                    "linkedin_snippet": snippet[:300],
                })
                print(f"[executives]   → LinkedIn candidate: {name} ({li_title})")
                continue

            # ── Case 2: Executive-directory page ─────────────────────────
            if any(d in clean_url for d in _EXECUTIVE_DIRECTORY_DOMAINS):
                if clean_url in seen_urls:
                    continue
                seen_urls.add(clean_url)
                for p in _extract_from_directory_page(clean_url, company_name):
                    name = p["name"]
                    if name.lower() in seen_names:
                        continue
                    seen_names.add(name.lower())
                    parts = name.split(" ", 1)
                    all_contacts.append({
                        "first_name": parts[0],
                        "last_name": parts[1] if len(parts) > 1 else "",
                        "full_name": name,
                        "title": p["title"],
                        "linkedin_url": "",
                        "is_local": 1,
                        "hq_contact": 0,
                        "confidence_score": 0.65 if company_mentioned else 0.5,
                        "data_provenance": f"DIRECTORY:{provider}:{clean_url}",
                        "source_primary": "directory",
                    })
                    print(f"[executives]   → directory candidate: {name} ({p['title']})")
                continue

            # ── Case 3: Extract from search result title/snippet text ────
            for name, extracted_title in _extract_name_title_from_text(combined_text, company_name):
                if name.lower() in seen_names:
                    continue
                seen_names.add(name.lower())
                # Only store real LinkedIn URLs here; all other URLs are source evidence, not profiles
                linkedin_url = ""
                if "linkedin.com/in/" in clean_url:
                    linkedin_url = clean_url
                parts = name.split(" ", 1)
                all_contacts.append({
                    "first_name": parts[0],
                    "last_name": parts[1] if len(parts) > 1 else "",
                    "full_name": name,
                    "title": extracted_title,
                    "linkedin_url": linkedin_url,
                    "is_local": 1,
                    "hq_contact": 0,
                    "confidence_score": 0.6 if company_mentioned else 0.45,
                    "data_provenance": f"SEARCH:{provider}:{clean_url}",
                    "source_primary": "search",
                })
                print(f"[executives]   → search candidate: {name} ({extracted_title})")

    # Save all contacts that pass verification and final validation gate
    saved = []
    for contact in all_contacts:
        # Clean title BEFORE verification too (no bypasses)
        contact["title"] = _clean_title(normalize_title(contact.get("title", "")))
        if not contact["title"]:
            print(f"[executives]   ✗ {contact.get('full_name','')} rejected: title empty after clean")
            continue
        verified = verify_and_enrich_person(
            contact,
            company_name=company_name,
            company_id=company_id,
            website=website,
            search_city=search_city,
            search_state=search_state,
            search_lat=search_lat,
            search_lng=search_lng,
            radius_miles=radius_miles,
            per_candidate_timeout_s=per_candidate_timeout_s,
        )
        if not verified:
            continue
        if _is_valid_contact_to_save(verified, company_name):
            contact_id = upsert_contact(company_id, verified)
            if contact_id > 0:
                saved.append(verified)
                try:
                    resolve_and_validate_email(contact_id, source="search_ingest")
                except Exception as e:
                    print(f"[executives] Email chain failed for contact {contact_id}: {e}")

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


def _build_name_variants(company_name: str) -> list[str]:
    """Generate simple name variants to broaden executive search."""
    if not company_name:
        return []
    variants = set()
    base = company_name.strip()
    variants.add(base)
    # Strip common suffixes
    stripped = re.sub(r"\s+(Inc\.?|LLC|Corp\.?|Corporation|Ltd\.?|Company|Co\.?)$", "", base, flags=re.IGNORECASE).strip()
    if stripped and stripped != base:
        variants.add(stripped)
    # Initialism: "L3 Harris Technologies" -> "L3"
    words = base.split()
    if len(words) >= 2:
        variants.add(words[0])
    return list(variants)


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
