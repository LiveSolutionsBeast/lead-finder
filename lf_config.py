#!/usr/bin/env python3
"""
lf_config.py - Lead Finder Configuration Loader
===============================================
Loads settings from lf_config.json. All runtime config in one place.
"""

import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "lf_config.json"

_config = None


def _load_dotenv(path: Path) -> None:
    """Load a simple KEY=VALUE .env file into os.environ (no external deps)."""
    if not path.exists():
        return
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("//"):
                continue
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if val.startswith(("'", '"')) and val.endswith(("'", '"')) and len(val) >= 2:
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val


# Load local env files on import so secret config helpers see them.
_load_dotenv(BASE_DIR / ".env")
_load_dotenv(BASE_DIR / ".env.local")


def load_config() -> dict:
    global _config
    if _config is None:
        with open(CONFIG_PATH) as f:
            _config = json.load(f)
    return _config


def _env_name_for_config_key(key: str) -> str:
    """Return the conventional env-var name for a config key.

    For keys like ``lf_api_key`` we also support ``LF_API_KEY``. Keys that
    already carry an ``_envvar`` suffix in config.json are handled explicitly
    by callers.
    """
    return key.upper()


def _resolve_secret(key: str, default=None):
    """Resolve a secret/config value: env var wins, then config.json, then default."""
    env_name = _env_name_for_config_key(key)
    val = os.environ.get(env_name)
    if val is not None:
        return val
    # Allow explicit envvar pointer in config, e.g. "lf_api_key_envvar": "LF_API_KEY"
    env_pointer = load_config().get(f"{key}_envvar")
    if env_pointer:
        val = os.environ.get(env_pointer)
        if val is not None:
            return val
    return load_config().get(key, default)


def reload_config() -> dict:
    """Force reload config from disk."""
    global _config
    _config = None
    return load_config()


def get(key: str, default=None):
    """Get a config value. Secrets and sensitive keys are resolved from env first."""
    if key.endswith("_envvar"):
        return load_config().get(key, default)
    # Secret-like keys that should prefer environment variables
    secret_keys = {
        "lf_api_key",
        "google_maps_api_key",
        "google_custom_search_api_key",
        "serpapi_api_key",
        "brave_search_api_key",
        "tavily_api_key",
        "exa_api_key",
        "firecrawl_api_key",
        "serper_api_key",
    }
    if key in secret_keys:
        return _resolve_secret(key, default)
    return load_config().get(key, default)


def require(key: str):
    val = get(key)
    if val is None:
        raise ValueError(f"Required config key missing: {key}")
    return val


def google_maps_api_key() -> str:
    return get("google_maps_api_key") or os.environ.get(get("google_maps_api_key_envvar", "GOOGLE_MAPS_API_KEY"), "")


def lf_api_key() -> str:
    """API key used by the server and web UI. Prefer env var, fall back to config.json."""
    return get("lf_api_key", "")


def api_key_envvar() -> str:
    return get("lf_api_key_envvar", "LF_API_KEY")


def searxng_url() -> str:
    return get("searxng_url", "http://127.0.0.1:8888")


def searxng_query_url() -> str:
    return get("searxng_query_url", "http://127.0.0.1:8888/search?q=<query>&format=json")


def degoog_url() -> str:
    return get("degoog_url", "http://localhost:4444")


def degoog_query_url() -> str:
    return get("degoog_query_url", "http://localhost:4444/api/search?q=<query>&type=web")


def fourget_url() -> str:
    """Base URL for the 4get-hijacked sidecar (SearXNG-compatible)."""
    return get("fourget_url", "http://localhost:8081")


def fourget_query_url() -> str:
    """4get sidecar harness endpoint (POST JSON to /harness.php).
    The 4get-hijacked container is NOT SearXNG-compatible on GET /search;
    it exposes /harness.php which accepts JSON: {engine, category, params}.
    """
    return get("fourget_query_url", "http://localhost:8081/harness.php")


def google_custom_search_api_key() -> str:
    return get("google_custom_search_api_key", "")


def serpapi_api_key() -> str:
    return get("serpapi_api_key", "")


def brave_search_api_key() -> str:
    return get("brave_search_api_key", "")


def brave_search_monthly_limit() -> int:
    return get("brave_search_monthly_limit", 1000)


def serpapi_monthly_limit() -> int:
    return get("serpapi_monthly_limit", 250)


def tavily_api_key() -> str:
    return get("tavily_api_key", "")


def tavily_monthly_limit() -> int:
    return get("tavily_monthly_limit", 1000)


def exa_api_key() -> str:
    return get("exa_api_key", "")


def exa_monthly_limit() -> int:
    return get("exa_monthly_limit", 1000)


def firecrawl_api_key() -> str:
    return get("firecrawl_api_key", "")


def firecrawl_monthly_limit() -> int:
    return get("firecrawl_monthly_limit", 500)


def serper_api_key() -> str:
    return get("serper_api_key", "")


def serper_limit() -> int:
    return get("serper_limit", 2500)


# ── PixelRAG Visual RAG (added 2026-06-14) ──────────────────────────────────
# Visual RAG search for company pages, designed to find executive names
# and titles rendered on About/Leadership/Team pages. PRIMARY provider for
# lead-discovery searches (execs, directors, VPs, GMs) per user spec.

def pixelrag_url() -> str:
    """Base URL for the PixelRAG ad-hoc visual API (inside ai-net docker network)."""
    return get("pixelrag_url", "http://pixelrag:30001")


def pixelrag_query_url() -> str:
    """DEPRECATED: the old /search endpoint required a pre-built FAISS index.
    The new ad-hoc API uses /screenshot and /extract directly."""
    return get("pixelrag_query_url", "http://pixelrag:30001/search")


def pixelrag_screenshot_url() -> str:
    """Ad-hoc screenshot endpoint: render any URL to tiled JPEGs."""
    return get("pixelrag_screenshot_url", "http://pixelrag:30001/screenshot")


def pixelrag_extract_url() -> str:
    """Ad-hoc visual search endpoint: render URL + query, return matching tiles."""
    return get("pixelrag_extract_url", "http://pixelrag:30001/extract")


def pixelrag_enabled() -> bool:
    return get("pixelrag_enabled", True) is True


def search_only_free() -> bool:
    return get("search_only_free", False) is True


def rate_limit(key: str, default: int) -> int:
    return get("rate_limits", {}).get(key, default)


# ── AI Enrichment Config (added 2026-06-06) ─────────────────────────────────
# Cloud-only chain (per user spec 2026-06-06). No local models.
# Primary: minimax-m3:cloud (fast, free)
# Fallback 1: deepseek-v4-pro:cloud (more reasoning power)
# Fallback 2: glm-5.2:cloud (alternative reasoning)

def ai_ollama_url() -> str:
    return get("ai_ollama_url", "http://localhost:11434")


def ai_cloud_model_primary() -> str:
    return get("ai_cloud_model_primary", "minimax-m3:cloud")


def ai_cloud_model_fallback_1() -> str:
    return get("ai_cloud_model_fallback_1", "deepseek-v4-pro:cloud")


def ai_cloud_model_fallback_2() -> str:
    return get("ai_cloud_model_fallback_2", "glm-5.2:cloud")


def ai_enrichment_enabled() -> bool:
    """AI enrichment is ON by default (per user spec 2026-06-06)."""
    return get("ai_enrichment_enabled", True) is True


def ai_sanity_check_enabled() -> bool:
    """AI sanity/integrity checking is ON by default (per user spec 2026-06-06)."""
    return get("ai_sanity_check_enabled", True) is True


def ai_timeout_seconds() -> int:
    return int(get("ai_timeout_seconds", 30))