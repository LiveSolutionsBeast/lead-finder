#!/usr/bin/env python3
"""
lf_config.py - Lead Finder Configuration Loader
===============================================
Loads settings from lf_config.json. All runtime config in one place.
"""

import json
from pathlib import Path

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "lf_config.json"

_config = None


def load_config() -> dict:
    global _config
    if _config is None:
        with open(CONFIG_PATH) as f:
            _config = json.load(f)
    return _config


def reload_config() -> dict:
    """Force reload config from disk."""
    global _config
    _config = None
    return load_config()


def get(key: str, default=None):
    return load_config().get(key, default)


def require(key: str):
    val = get(key)
    if val is None:
        raise ValueError(f"Required config key missing: {key}")
    return val


def google_maps_api_key() -> str:
    return get("google_maps_api_key") or get("google_maps_api_key_envvar")


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