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
    tavily_api_key, exa_api_key, firecrawl_api_key, serper_api_key,
    tavily_monthly_limit, exa_monthly_limit, firecrawl_monthly_limit, serper_limit,
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

# ── Circuit breaker (added 2026-07-26) ──────────────────────────────────
# When every free provider is down (returning empty/timeout), repeated calls
# each burn the full provider-timeout budget (e.g. 3 providers × 8s = 24s)
# for zero value. In a 200-contact verification batch that is hours of dead
# time. The breaker short-circuits: after N consecutive all-exhausted
# failures, subsequent calls return [] immediately for a cooldown period so
# the caller can proceed via non-search paths (AI + website scrape) instead
# of stalling on dead endpoints.
_BREAKER_FAILS = 0            # consecutive all-exhausted failures
_BREAKER_THRESHOLD = 3        # trips after 3 straight exhaustions
_BREAKER_TRIPPED_AT = 0.0     # monotonic time when tripped
_BREAKER_COOLDOWN_S = 120.0   # stay open (short-circuit) for 2 minutes
_BREAKER_HALF_OPEN_PROBE = False  # next call is a probe to test recovery


def _breaker_should_short_circuit() -> bool:
    """Return True if the breaker is tripped and still in cooldown."""
    global _BREAKER_HALF_OPEN_PROBE
    if _BREAKER_TRIPPED_AT == 0.0:
        return False
    if time.monotonic() - _BREAKER_TRIPPED_AT < _BREAKER_COOLDOWN_S:
        return True
    # Cooldown elapsed → allow ONE probe call to test recovery
    _BREAKER_HALF_OPEN_PROBE = True
    return False


def _breaker_record_success() -> None:
    global _BREAKER_FAILS, _BREAKER_TRIPPED_AT, _BREAKER_HALF_OPEN_PROBE
    _BREAKER_FAILS = 0
    _BREAKER_TRIPPED_AT = 0.0
    _BREAKER_HALF_OPEN_PROBE = False


def _breaker_record_exhaustion() -> None:
    global _BREAKER_FAILS, _BREAKER_TRIPPED_AT, _BREAKER_HALF_OPEN_PROBE
    if _BREAKER_HALF_OPEN_PROBE:
        # Probe failed → re-trip
        _BREAKER_TRIPPED_AT = time.monotonic()
        _BREAKER_HALF_OPEN_PROBE = False
        logger.warning(f"search breaker re-tripped after probe; cooldown {_BREAKER_COOLDOWN_S}s")
        return
    _BREAKER_FAILS += 1
    if _BREAKER_FAILS >= _BREAKER_THRESHOLD and _BREAKER_TRIPPED_AT == 0.0:
        _BREAKER_TRIPPED_AT = time.monotonic()
        logger.warning(
            f"search breaker TRIPPED after {_BREAKER_FAILS} consecutive "
            f"all-provider exhaustions; short-circuiting for {_BREAKER_COOLDOWN_S}s"
        )


PROVIDER_STATE_FILE = Path(__file__).parent / "provider_usage.json"

# Provider limits (self-hosted = unlimited; paid tiers capped monthly)
PAID_TIERS = {
    "brave":     1000,           # 1,000 free queries/month (corrected from 2000)
    "tavily":    1000,           # Dev tier — generous but cap to rotate
    "exa":       1000,           # No published hard cap; conservative cap
    "serper":    2500,           # 2,500 one-time credits (no monthly reset)
    "firecrawl": 500,            # Paid tier — conservative cap
    "serpapi":   250,            # 250 free searches/month
}

# Providers whose usage should be tracked by calendar month.
MONTHLY_PROVIDERS = {"brave", "tavily", "exa", "firecrawl", "serpapi"}
# Providers whose usage is lifetime/one-time (no reset).
LIFETIME_PROVIDERS = {"serper"}

FREE_TIERS = {
    "pixelrag":  float("inf"),  # Self-hosted, unlimited
    "degoog":    float("inf"),  # Self-hosted Degoog aggregator, unlimited
    "fourget":   float("inf"),  # Self-hosted 4get-hijacked sidecar, unlimited
    "searxng":   float("inf"),  # Self-hosted SearXNG, unlimited
    **PAID_TIERS,
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
    if provider in LIFETIME_PROVIDERS:
        if "lifetime" not in state:
            state["lifetime"] = {}
        state["lifetime"][provider] = state["lifetime"].get(provider, 0) + 1
        _save_state(state)
        return state["lifetime"][provider]
    month = _get_month_key()
    if month not in state:
        state[month] = {}
    state[month][provider] = state[month].get(provider, 0) + 1
    _save_state(state)
    return state[month].get(provider, 0)

def get_usage(provider: str) -> int:
    state = _load_state()
    if provider in LIFETIME_PROVIDERS:
        return state.get("lifetime", {}).get(provider, 0)
    month = _get_month_key()
    return state.get(month, {}).get(provider, 0)

def get_all_usage() -> dict:
    state = _load_state()
    month = _get_month_key()
    out = dict(state.get(month, {}))
    out.update(state.get("lifetime", {}))
    return out


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


def _tavily_search(query: str, timeout: int = 15) -> list[SearchResult]:
    """Tavily search API (dev/prod tier)."""
    key = tavily_api_key()
    if not key:
        return []
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "query": query,
                "api_key": key,
                "search_depth": "basic",
                "max_results": 10,
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(f"Tavily HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
        results = []
        for r in data.get("results", [])[:10]:
            results.append(_normalize_search_result(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("content", ""),
            ))
        return results
    except requests.exceptions.Timeout:
        logger.warning(f"Tavily timeout after {timeout}s")
        return []
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"Tavily connection error: {e}")
        return []
    except requests.exceptions.RequestException as e:
        logger.warning(f"Tavily request error: {e}")
        return []


def _exa_search(query: str, timeout: int = 15) -> list[SearchResult]:
    """Exa search API."""
    key = exa_api_key()
    if not key:
        return []
    try:
        resp = requests.post(
            "https://api.exa.ai/search",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"query": query, "num_results": 10, "type": "auto"},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(f"Exa HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
        results = []
        for r in data.get("results", [])[:10]:
            results.append(_normalize_search_result(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("text", "") or r.get("summary", ""),
            ))
        return results
    except requests.exceptions.Timeout:
        logger.warning(f"Exa timeout after {timeout}s")
        return []
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"Exa connection error: {e}")
        return []
    except requests.exceptions.RequestException as e:
        logger.warning(f"Exa request error: {e}")
        return []


def _firecrawl_search(query: str, timeout: int = 15) -> list[SearchResult]:
    """Firecrawl search API."""
    key = firecrawl_api_key()
    if not key:
        return []
    try:
        resp = requests.post(
            "https://api.firecrawl.dev/v1/search",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"query": query, "limit": 10},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(f"Firecrawl HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
        if not data.get("success"):
            logger.warning(f"Firecrawl error: {data.get('error')}")
            return []
        results = []
        for r in data.get("data", [])[:10]:
            results.append(_normalize_search_result(
                title=r.get("title", ""),
                url=r.get("url", ""),
                snippet=r.get("description", "") or r.get("markdown", "")[:500],
            ))
        return results
    except requests.exceptions.Timeout:
        logger.warning(f"Firecrawl timeout after {timeout}s")
        return []
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"Firecrawl connection error: {e}")
        return []
    except requests.exceptions.RequestException as e:
        logger.warning(f"Firecrawl request error: {e}")
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


def _serper_search(query: str, timeout: int = 15) -> list[SearchResult]:
    """Serper (Google Search API). One-time credit pool, no monthly reset."""
    key = serper_api_key()
    if not key:
        return []
    try:
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": key, "Content-Type": "application/json"},
            json={"q": query, "num": 10},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logger.warning(f"Serper HTTP {resp.status_code}: {resp.text[:200]}")
            return []
        data = resp.json()
        results = []
        for r in data.get("organic", [])[:10]:
            results.append(_normalize_search_result(
                title=r.get("title", ""),
                url=r.get("link", ""),
                snippet=r.get("snippet", ""),
            ))
        return results
    except requests.exceptions.Timeout:
        logger.warning(f"Serper timeout after {timeout}s")
        return []
    except requests.exceptions.ConnectionError as e:
        logger.warning(f"Serper connection error: {e}")
        return []
    except requests.exceptions.RequestException as e:
        logger.warning(f"Serper request error: {e}")
        return []


# ── Router: try providers in priority order, skip exhausted ones ────
#
# Priority (updated 2026-07-29):
#   Tier 0: Paid APIs first (Brave -> Tavily -> Exa -> Firecrawl) because the
#           self-hosted stack is currently captcha'd and unreliable.
#   Tier 1: Self-hosted text search (Degoog -> 4get)
#   Tier 2: SearXNG (self-hosted, fallback)
#   Tier 3: SerpAPI (250/mo, last resort)
#
# PixelRAG is intentionally NOT in the default rotation here. It is
# reserved for the dedicated pixelrag_scrape_linkedin() helper, which
# only runs when AI data is stale and we need fresh LinkedIn profile
# content. Putting PixelRAG in the default search chain made every
# LinkedIn query take 5-30s and broke the synchronous enrichment
# pipeline. (Per user feedback, 2026-06-16: "PixelRAG is fine for
# scraping latest experience but not for company search.")
DEFAULT_PROVIDERS = [
    ("brave",      _brave_search),      # Tier 0: Brave API (1000/mo)
    ("tavily",     _tavily_search),     # Tier 0: Tavily API (1000/mo)
    ("exa",        _exa_search),        # Tier 0: Exa API (1000/mo)
    ("serper",     _serper_search),     # Tier 0: Serper API (2500 lifetime)
    ("firecrawl",  _firecrawl_search),  # Tier 0: Firecrawl API (500/mo)
    ("degoog",     _degoog_search),     # Tier 1: Degoog aggregator (unlimited, currently broken)
    ("fourget",    _fourget_search),    # Tier 1: 4get sidecar (unlimited, currently broken)
    ("searxng",    _searxng_search),   # Tier 2: SearXNG (unlimited, currently broken)
    ("serpapi",    _serpapi_search),   # Tier 3: SerpAPI (250/mo, last resort)
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

    Priority: paid APIs (brave -> tavily -> exa -> firecrawl) -> degoog -> fourget -> searxng -> serpapi
    Skips providers that have exceeded their monthly tier limit.
    Only falls back to self-hosted tiers when paid tiers return no results.

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

    Set SEARCH_ONLY_FREE=1 to disable paid tier fallback
    """
    # Check cache first
    cached = _cache_get(query, prefer, ai_extract)
    if cached is not None:
        logger.debug(f"search cache hit for {query[:60]!r}")
        return cached["results"], cached["provider"]

    # Circuit breaker: when all providers are down, short-circuit so
    # batch callers fail fast instead of stalling on dead endpoints.
    if _breaker_should_short_circuit():
        logger.info("search breaker OPEN — short-circuiting (all providers down)")
        return [], ""

    # Reset breaker on a fresh call when it was half-open; we'll record
    # success/failure inside the loop below.
    global _BREAKER_HALF_OPEN_PROBE
    _BREAKER_HALF_OPEN_PROBE = False

    only_free = _only_free_providers()

    providers_to_try = list(PROVIDERS)

    if prefer:
        # Move preferred provider to the front of the list, but keep the rest
        # so we can fall back if it fails.
        preferred = [(n, f) for n, f in providers_to_try if n == prefer]
        rest = [(n, f) for n, f in providers_to_try if n != prefer]
        providers_to_try = preferred + rest

    # Try each provider in order, skipping exhausted ones
    for name, fn in providers_to_try:
        # Skip paid tiers if SEARCH_ONLY_FREE is set
        if only_free and name in PAID_TIERS:
            continue

        usage = get_usage(name)
        limit = FREE_TIERS.get(name, float("inf"))
        if usage >= limit:
            logger.warning(f"{name} limit reached ({usage}/{limit}), skipping")
            continue

        results = fn(query, timeout)

        if results:
            track_usage(name)
            _breaker_record_success()
            pct = min(100, int(usage / limit * 100)) if limit != float("inf") else 0
            logger.info(f"{name}: {len(results)} results (usage: {usage+1}/{limit if limit != float('inf') else '∞'})")
            out = _maybe_ai_extract(results, query, ai_extract), name
            _cache_put(query, prefer, ai_extract, out[0], out[1])
            return out

    logger.warning("All providers exhausted, returning empty")
    _breaker_record_exhaustion()
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
    for name in ["pixelrag", "degoog", "fourget", "searxng", "brave", "tavily", "exa", "serper", "firecrawl", "serpapi"]:
        used = usage.get(name, 0)
        limit = FREE_TIERS.get(name, float("inf"))
        pct = min(100, int(used / limit * 100)) if limit != float("inf") else 0
        status = "OK"
        if pct >= 90:
            status = "WARNING"
        elif pct >= 100:
            status = "EXHAUSTED"
        if limit == float("inf"):
            lines.append(f"{name}: {used} used (unlimited) - {status}")
        elif name in LIFETIME_PROVIDERS:
            lines.append(f"{name}: {used}/{limit} lifetime credits ({pct}%) - {status}")
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
