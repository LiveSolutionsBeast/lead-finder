#!/usr/bin/env python3
"""
lf_search_providers.py - Multi-provider search router with rotation
Priority (updated 2026-06-14):
  1. PixelRAG  (visual RAG over company pages — PRIMARY for lead discovery)
  2. Degoog    (self-hosted text aggregator)
  3. 4get      (self-hosted text sidecar)
  4. SearXNG   (self-hosted text web search, strict fallback)
  5. Brave     (text web API, free tier 1000/mo)
  6. SerpAPI   (text web API, free tier 250/mo, last resort)

The fallback chain is the "SearXNG strictly reserved" mode the user
specified — visual search finds ~80% of execs/leadership names from
company About/Team pages, and the text providers only kick in when the
visual index has no hit for the query (e.g., brand-new company with no
rendered page yet). Degoog and 4get are tried strictly before SearXNG
per the user spec.

AI Enrichment (post-search, not a provider):
  - When ai_extract is True (default if config enables it), raw results
    are passed through AI to extract structured fields.
  - Cloud model chain: minimax-m3:cloud -> deepseek-v4-pro:cloud -> glm-5.2:cloud.
  - Falls back gracefully to raw results if all AI models fail.

Future: AI agents will orchestrate this section (sequential: PixelRAG ->
AI extract -> if gaps -> SearXNG -> AI extract -> if gaps -> human) but
this session implements only the deterministic router.
"""

import json
import logging
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

from lf_config import (
    searxng_query_url, degoog_query_url, fourget_query_url,
    brave_search_api_key, serpapi_api_key,
    brave_search_monthly_limit, serpapi_monthly_limit,
    search_only_free,
    pixelrag_enabled, pixelrag_screenshot_url, pixelrag_extract_url,
)


# ── In-process search cache (added 2026-06-16) ─────────────────────────
# Prevents duplicate provider calls (especially slow PixelRAG /extract
# calls) for the same query within a 5-minute window. The cache is
# process-local; the FastAPI server keeps it across requests.
_SEARCH_CACHE: dict = {}
_SEARCH_CACHE_TTL_S = 300
_SEARCH_CACHE_MAX = 200


def _cache_get(query: str, prefer: str, ai_extract):
    key = (query, prefer, ai_extract)
    entry = _SEARCH_CACHE.get(key)
    if not entry:
        return None
    if time.time() - entry["ts"] > _SEARCH_CACHE_TTL_S:
        _SEARCH_CACHE.pop(key, None)
        return None
    return entry


def _cache_put(query: str, prefer: str, ai_extract, results, provider: str):
    if len(_SEARCH_CACHE) >= _SEARCH_CACHE_MAX:
        # Evict oldest 25%
        sorted_keys = sorted(_SEARCH_CACHE.items(), key=lambda kv: kv[1]["ts"])
        for k, _ in sorted_keys[: _SEARCH_CACHE_MAX // 4]:
            _SEARCH_CACHE.pop(k, None)
    _SEARCH_CACHE[(query, prefer, ai_extract)] = {
        "ts": time.time(),
        "results": results,
        "provider": provider,
    }

logger = logging.getLogger(__name__)

PROVIDER_STATE_FILE = Path(__file__).parent / "provider_usage.json"

# Free tier limits (self-hosted = unlimited)
FREE_TIERS = {
    "pixelrag":  float("inf"),  # Self-hosted, unlimited
    "degoog":    float("inf"),  # Self-hosted Degoog aggregator, unlimited
    "fourget":   float("inf"),  # Self-hosted 4get-hijacked sidecar, unlimited
    "searxng":   float("inf"),  # Self-hosted SearXNG, unlimited
    "brave":     1000,           # 1,000 free queries/month (corrected from 2000)
    "serpapi":   250,            # 250 free searches/month
}

# Type alias for a single search result
SearchResult = dict


def _load_state() -> dict:
    if PROVIDER_STATE_FILE.exists():
        with open(PROVIDER_STATE_FILE) as f:
            return json.load(f)
    return {}

def _save_state(state: dict) -> None:
    with open(PROVIDER_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def _get_month_key() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.year}-{now.month:02d}"

def track_usage(provider: str) -> int:
    state = _load_state()
    month = _get_month_key()
    if month not in state:
        state[month] = {}
    state[month][provider] = state[month].get(provider, 0) + 1
    _save_state(state)
    return state[month].get(provider, 0)

def get_usage(provider: str) -> int:
    state = _load_state()
    month = _get_month_key()
    return state.get(month, {}).get(provider, 0)

def get_all_usage() -> dict:
    state = _load_state()
    month = _get_month_key()
    return state.get(month, {})


def _normalize_search_result(title: str = "", url: str = "", snippet: str = "",
                             score: Optional[float] = None,
                             source: Optional[str] = None,
                             image_b64: Optional[str] = None) -> SearchResult:
    """Build a standardized search result dict."""
    result: SearchResult = {
        "title": title,
        "url": url,
        "snippet": snippet,
    }
    if score is not None:
        result["score"] = score
    if source is not None:
        result["source"] = source
    if image_b64 is not None:
        result["image_b64"] = image_b64
    return result


# ── Provider implementations ─────────────────────────────────────────

def _pixelrag_extract_for_query(query: str, timeout: int = 120) -> list[SearchResult]:
    """
    Ad-hoc visual search via PixelRAG (PRIMARY for lead discovery).

    The new PixelRAG container exposes two ad-hoc endpoints:
      - /screenshot: render a URL to screenshot tiles
      - /extract: render a URL + query, then embed the query and each tile
        with Qwen3-VL-Embedding-2B and return the top-K matching tiles.

    Because there is no pre-built FAISS index, we first need a candidate
    URL. We use the text query itself as a search-engine-style query to
    find a relevant company page (About, Team, Leadership), then ask PixelRAG
    /extract to visually rank the tiles for the same query.

    In practice this is implemented as a two-stage call from the orchestration
    layer (lf_server.py / Open WebUI tool). This helper is the low-level
    wrapper around /extract for a given URL.

    Returns an empty list if PixelRAG is disabled, unreachable, or returns no
    matches.
    """
    if not pixelrag_enabled():
        return []
    # This low-level helper is intentionally simple: caller must supply url.
    # The high-level _pixelrag_search() builds the URL from query + text fallback.
    return []


def _pixelrag_extract(url: str, query: str, top_k: int = 5, timeout: int = 180) -> list[SearchResult]:
    """Call PixelRAG /extract for a specific URL + query."""
    if not pixelrag_enabled():
        return []
    try:
        resp = requests.post(
            pixelrag_extract_url(),
            json={
                "url": url,
                "query": query,
                "top_k": top_k,
                "tile_height": 1568,
                "viewport_width": 1280,
                "quality": 85,
                "wait_seconds": 1.0,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(f"PixelRAG /extract HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
        matches = data.get("matches", [])
        results = []
        for m in matches:
            results.append(_normalize_search_result(
                title=f"Visual match (score {m.get('score', 0):.3f})",
                url=data.get("url", url),
                snippet=f"Tile {m.get('tile_index', '?')} on {data.get('url', url)}",
                score=m.get("score", 0),
                source="pixelrag",
                image_b64=m.get("image_b64"),
            ))
        return results
    except requests.exceptions.Timeout:
        logger.warning(f"PixelRAG /extract timeout after {timeout}s")
        return []
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"PixelRAG /extract connection error: {e}")
        return []
    except requests.exceptions.RequestException as e:
        logger.warning(f"PixelRAG /extract request error: {e}")
        return []


def _pixelrag_screenshot(url: str, timeout: int = 90) -> dict:
    """Call PixelRAG /screenshot for a specific URL. Returns raw response."""
    if not pixelrag_enabled():
        return {}
    try:
        resp = requests.post(
            pixelrag_screenshot_url(),
            json={
                "url": url,
                "tile_height": 1568,
                "viewport_width": 1280,
                "quality": 85,
                "wait_seconds": 1.0,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(f"PixelRAG /screenshot HTTP {resp.status_code}: {resp.text[:200]}")
            return {}
        return resp.json()
    except requests.exceptions.Timeout:
        logger.warning(f"PixelRAG /screenshot timeout after {timeout}s")
        return {}
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"PixelRAG /screenshot connection error: {e}")
        return {}
    except requests.exceptions.RequestException as e:
        logger.warning(f"PixelRAG /screenshot request error: {e}")
        return {}


def _pixelrag_search(query: str, timeout: int = 5) -> list[SearchResult]:
    """
    High-level PixelRAG ad-hoc visual search.

    Because the ad-hoc API needs a URL, we first try a quick text-only
    fallback to pick a candidate page (Degoog -> 4get -> SearXNG). We look
    for URLs that smell like About/Team/Leadership pages and run PixelRAG
    /extract against them. The first URL that returns visual matches wins.

    The `timeout` parameter controls the slow visual /extract phase
    (default 5s — PixelRAG is CPU-bound and slow; we keep this fast so
    the synchronous search chain never hangs. For interactive
    on-demand visual queries, callers can pass a longer timeout via
    the search() prefer='pixelrag' path).

    If no candidate page can be found or none of them yield visual matches,
    we fall back to ordinary text search results so the caller still gets
    something useful.

    Note (2026-06-16): PixelRAG is opt-in via prefer='pixelrag' or by
    enabling the pixelrag_enabled config flag. The search() default
    rotation skips PixelRAG because it is too slow for synchronous
    LinkedIn search. PixelRAG is reserved for visual scraping
    (latest experience/role/title/location from a LinkedIn profile)
    via the dedicated pixelrag_scrape_linkedin() helper.
    """
    if not pixelrag_enabled():
        return []

    # 1) Try to find a likely company leadership/team page from text providers.
    #    Keep this phase fast (15s/provider) so a slow 4get/SearXNG does not
    #    prevent us from reaching PixelRAG at all.
    CANDIDATE_TIMEOUT = 15
    candidate_urls = []
    for provider_name, provider_fn in [
        ("degoog", _degoog_search),
        ("fourget", _fourget_search),
        ("searxng", _searxng_search),
    ]:
        try:
            text_results = provider_fn(query, timeout=CANDIDATE_TIMEOUT)
            for r in text_results[:10]:
                url = r.get("url", "")
                if not url:
                    continue
                # Prefer About / Team / Leadership / People pages
                lowered = url.lower()
                if any(k in lowered for k in ["/about", "/team", "/leadership", "/people", "/company", "/staff"]):
                    candidate_urls.append((provider_name, url))
                elif not candidate_urls:
                    # keep the first result as a generic candidate only if no better found yet
                    candidate_urls.append((provider_name, url))
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            logger.warning(f"PixelRAG candidate fetch via {provider_name} failed: {e}")
            continue
        except requests.exceptions.RequestException as e:
            logger.warning(f"PixelRAG candidate fetch via {provider_name} failed: {e}")
            continue

    # 2) Run PixelRAG /extract on candidate URLs until we get matches.
    #    Use at least 120s even if the caller asked for a short overall timeout;
    #    CPU rendering + embedding on the first URL can take 30-90s.
    extract_timeout = max(timeout, 120)
    seen = set()
    for provider_name, url in candidate_urls:
        if url in seen:
            continue
        seen.add(url)
        matches = _pixelrag_extract(url, query, top_k=5, timeout=extract_timeout)
        if matches:
            logger.info(f"PixelRAG visual match via {provider_name} candidate {url}: {len(matches)} matches")
            return matches

    # 3) No visual matches — fall back to text results so the router can continue
    logger.info("PixelRAG ad-hoc visual search returned no matches, falling back to text")
    return []


def _degoog_search(query: str, timeout: int = 15) -> list[SearchResult]:
    """
    Self-hosted Degoog aggregator (free, unlimited).
    SearXNG-compatible API at /api/search. Returns merged results from
    multiple engines (Google, DuckDuckGo, Brave, Bing, etc.).
    """
    durl = degoog_query_url().replace("<query>", urllib.parse.quote_plus(query))
    try:
        resp = requests.get(durl, timeout=timeout)
        if resp.status_code != 200:
            logger.warning(f"Degoog HTTP {resp.status_code}")
            return []
        data = resp.json()
        results = []
        for r in data.get("results", [])[:20]:
            results.append(_normalize_search_result(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("snippet", "") or r.get("content", ""),
            ))
        return results
    except requests.exceptions.Timeout:
        logger.warning(f"Degoog timeout after {timeout}s")
        return []
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"Degoog connection error: {e}")
        return []
    except requests.exceptions.RequestException as e:
        logger.warning(f"Degoog request error: {e}")
        return []


def _fourget_search(query: str, timeout: int = 15) -> list[SearchResult]:
    """
    Self-hosted 4get-hijacked sidecar (free, unlimited).

    The 4get-hijacked container is NOT SearXNG-compatible on GET /search.
    It exposes a low-level harness endpoint at /harness.php that accepts a
    JSON POST: {"engine": "<engine>", "category": "web", "params": {"s": query}}.
    We call the duckduckgo engine by default (it currently returns results;
    brave/bing often fail or require special setup). Results are normalized
    to the same {title, url, snippet} shape as the other providers.
    """
    furl = fourget_query_url()
    # If the user configured an old-style /search URL, fall back to the harness path
    if "/search" in furl and "harness.php" not in furl:
        furl = (furl.split("/search")[0] + "/harness.php").rstrip("/")
    try:
        resp = requests.post(
            furl,
            json={"engine": "duckduckgo", "category": "web", "params": {"s": query}},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(f"4get HTTP {resp.status_code}")
            return []
        data = resp.json()
        if data.get("status") == "error":
            logger.warning(f"4get error: {data.get('message', 'unknown')}")
            return []
        results = []
        for r in data.get("web", [])[:20]:
            results.append(_normalize_search_result(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("description", "") or r.get("snippet", ""),
            ))
        return results
    except requests.exceptions.Timeout:
        logger.warning(f"4get timeout after {timeout}s")
        return []
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"4get connection error: {e}")
        return []
    except requests.exceptions.RequestException as e:
        logger.warning(f"4get request error: {e}")
        return []


def _searxng_search(query: str, timeout: int = 15) -> list[SearchResult]:
    """
    Self-hosted SearXNG (free, unlimited).
    Fallback text web search when Degoog and 4get do not return enough.
    """
    qurl = searxng_query_url().replace("<query>", urllib.parse.quote_plus(query))
    try:
        resp = requests.get(qurl, timeout=timeout)
        if resp.status_code != 200:
            return []
        data = resp.json()
        results = []
        for r in data.get("results", [])[:20]:
            results.append(_normalize_search_result(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", ""),
            ))
        return results
    except requests.exceptions.Timeout:
        logger.warning(f"SearXNG timeout after {timeout}s")
        return []
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"SearXNG connection error: {e}")
        return []
    except requests.exceptions.RequestException as e:
        logger.warning(f"SearXNG request error: {e}")
        return []


def _brave_search(query: str, timeout: int = 15) -> list[SearchResult]:
    """Brave Search API (free tier: 1,000/month)."""
    key = brave_search_api_key()
    if not key:
        return []
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": key},
            params={"q": query, "count": 20},
            timeout=timeout,
        )
        if resp.status_code == 403:
            logger.warning("Brave API key rejected (403)")
            return []
        if resp.status_code != 200:
            logger.warning(f"Brave API HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
        results = []
        for r in data.get("web", {}).get("results", [])[:20]:
            results.append(_normalize_search_result(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("description", ""),
            ))
        return results
    except requests.exceptions.Timeout:
        logger.warning(f"Brave API timeout after {timeout}s")
        return []
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"Brave API connection error: {e}")
        return []
    except requests.exceptions.RequestException as e:
        logger.warning(f"Brave API request error: {e}")
        return []


def _serpapi_search(query: str, timeout: int = 15) -> list[SearchResult]:
    """SerpAPI (free tier: 250/month). Last resort fallback."""
    key = serpapi_api_key()
    if not key:
        return []
    try:
        resp = requests.get(
            "https://serpapi.com/search",
            params={"q": query, "api_key": key, "engine": "google", "num": 20},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(f"SerpAPI HTTP {resp.status_code}")
            return []
        data = resp.json()
        results = []
        for r in data.get("organic_results", [])[:20]:
            results.append(_normalize_search_result(
                title=r.get("title", ""),
                url=r.get("link", ""),
                snippet=r.get("snippet", ""),
            ))
        return results
    except requests.exceptions.Timeout:
        logger.warning(f"SerpAPI timeout after {timeout}s")
        return []
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"SerpAPI connection error: {e}")
        return []
    except requests.exceptions.RequestException as e:
        logger.warning(f"SerpAPI request error: {e}")
        return []


# ── Router: try providers in priority order, skip exhausted ones ────

# Tier 1: Self-hosted text search (Degoog -> 4get, strictly before SearXNG)
# Tier 2: SearXNG (self-hosted, fallback)
# Tier 3-4: Paid text APIs (only when search_only_free=false)
#
# PixelRAG is intentionally NOT in the default rotation here. It is
# reserved for the dedicated pixelrag_scrape_linkedin() helper, which
# only runs when AI data is stale and we need fresh LinkedIn profile
# content. Putting PixelRAG in the default search chain made every
# LinkedIn query take 5-30s and broke the synchronous enrichment
# pipeline. (Per user feedback, 2026-06-16: "PixelRAG is fine for
# scraping latest experience but not for company search.")
DEFAULT_PROVIDERS = [
    ("degoog",   _degoog_search),       # Tier 1: Degoog aggregator (unlimited)
    ("fourget",  _fourget_search),      # Tier 2: 4get sidecar (unlimited)
    ("searxng",  _searxng_search),      # Tier 3: SearXNG (unlimited, fallback)
    ("brave",    _brave_search),        # Tier 4: Brave API (1000/mo)
    ("serpapi",  _serpapi_search),      # Tier 5: SerpAPI (250/mo, last resort)
]

# Backward compatibility: PROVIDERS is the same list (no PixelRAG).
PROVIDERS = DEFAULT_PROVIDERS

# Set search_only_free=true in lf_config.json to disable paid tier fallback
def _only_free_providers() -> bool:
    return search_only_free()


def search(query: str, timeout: int = 15, prefer: str = "", ai_extract: Optional[bool] = None) -> tuple[list[SearchResult], str]:
    """
    Multi-provider search with rotation and usage tracking.
    Returns (results, provider_used).

    Priority: pixelrag -> degoog -> fourget -> searxng -> brave -> serpapi
    Skips providers that have exceeded their monthly free tier limit.
    Only falls back to paid tiers when free tiers return no results.

    AI Enrichment (added 2026-06-06):
      When ai_extract is True (default if config enables it), raw SearXNG results
      are passed through AI to extract structured fields (names, titles, LinkedIn URLs).
      Gap-based: AI is invoked proportional to how many fields are missing.
      Cloud model chain: minimax-m3:cloud -> deepseek-v4-pro:cloud -> glm-5.2:cloud.
      Falls back gracefully to raw results if all AI models fail.

    Args:
      query: Search query string
      timeout: Request timeout in seconds
      prefer: Force a specific provider (for testing)
      ai_extract: Override config default (None = use config, True/False = explicit)

    Set SEARCH_ONLY_FREE=1 to disable paid tier fallback ( Brave and SerpAPI )
    """
    # Check cache first
    cached = _cache_get(query, prefer, ai_extract)
    if cached is not None:
        logger.debug(f"search cache hit for {query[:60]!r}")
        return cached["results"], cached["provider"]

    if prefer:
        for name, fn in PROVIDERS:
            if name == prefer:
                results = fn(query, timeout)
                if results:
                    track_usage(name)
                    out = _maybe_ai_extract(results, query, ai_extract), name
                    _cache_put(query, prefer, ai_extract, out[0], out[1])
                    return out
        return [], ""

    only_free = _only_free_providers()

    # Try each provider in order, skipping exhausted ones
    for name, fn in PROVIDERS:
        # Skip paid tiers if SEARCH_ONLY_FREE is set
        if only_free and name in ("brave", "serpapi"):
            continue

        usage = get_usage(name)
        limit = FREE_TIERS.get(name, float("inf"))
        if usage >= limit:
            logger.warning(f"{name} limit reached ({usage}/{limit}), skipping")
            continue

        results = fn(query, timeout)

        if results:
            track_usage(name)
            pct = min(100, int(usage / limit * 100)) if limit != float("inf") else 0
            logger.info(f"{name}: {len(results)} results (usage: {usage+1}/{limit if limit != float('inf') else '∞'})")
            out = _maybe_ai_extract(results, query, ai_extract), name
            _cache_put(query, prefer, ai_extract, out[0], out[1])
            return out

    logger.warning("All providers exhausted, returning empty")
    return [], ""


def _maybe_ai_extract(results: list[SearchResult], query: str, ai_extract_override: Optional[bool]) -> list[SearchResult]:
    """
    Apply AI enrichment to search results if enabled.
    Returns results with additional 'ai_extracted' field if AI succeeds.
    On any failure, returns the original results unchanged.
    """
    # Decide whether to invoke AI
    if ai_extract_override is False:
        return results
    if ai_extract_override is None:
        # Use config default
        from lf_ai_enrich import ai_enabled
        if not ai_enabled():
            return results

    if not results:
        return results

    try:
        from lf_ai_enrich import ai_extract_structured, count_gaps

        # Check if any result has significant gaps
        # We treat each result as a "person record" with these fields:
        # title, snippet, url (URL is treated as 'linkedin_url' analog)
        gap_count = 0
        for r in results[:5]:  # check up to 5
            gap_count += count_gaps({
                "title": r.get("title"),
                "linkedin_url": r.get("url"),
                "location": "",  # SearXNG snippets rarely include location
                "email": "",
                "phone": "",
            })

        if gap_count < 1:
            return results  # data is already complete

        # Build raw text from top results
        raw_text = "\n\n".join(
            f"Title: {r.get('title','')}\nURL: {r.get('url','')}\nSnippet: {r.get('snippet','')}"
            for r in results[:5]
        )

        extracted = ai_extract_structured(
            raw_text=raw_text,
            schema={
                "people": "array of {name, title, company, linkedin_url, location, confidence}",
                "summary": "string (one-line description of findings)"
            },
            context=f"Search query: {query}",
            gap_count=min(gap_count, 5)
        )

        if extracted:
            # Attach to first result (so callers can find it)
            # The full result list is preserved
            results[0]["ai_extracted"] = extracted
            results[0]["ai_gap_count"] = gap_count
    except Exception as e:
        # Graceful degradation: any error -> return original results
        logger.warning(f"AI extraction failed (non-fatal): {e}")

    return results


def get_usage_summary() -> list[str]:
    """Return human-readable usage summary for dashboard."""
    usage = get_all_usage()
    lines = []
    for name in ["pixelrag", "degoog", "fourget", "searxng", "brave", "serpapi"]:
        used = usage.get(name, 0)
        limit = FREE_TIERS[name]
        pct = min(100, int(used / limit * 100)) if limit != float("inf") else 0
        status = "OK"
        if pct >= 90:
            status = "WARNING"
        elif pct >= 100:
            status = "EXHAUSTED"
        if limit == float("inf"):
            lines.append(f"{name}: {used} used (unlimited) - {status}")
        else:
            lines.append(f"{name}: {used}/{limit} ({pct}%) - {status}")
    return lines


if __name__ == "__main__":
    # Quick test
    r, p = search("Varda Space Industries CEO")
    logger.info(f"Provider: {p}, Results: {len(r)}")
    for x in r[:3]:
        logger.info(f"  {x['title'][:60]} | {x['url'][:60]}")
    for line in get_usage_summary():
        logger.info(line)
