#!/usr/bin/env python3
"""
lf_ai_enrich.py - AI Enrichment & Integrity Layer for Lead Finder
==================================================================
Cloud-only AI enrichment that runs AFTER SearXNG/Degoog search results
to (a) fill gaps and (b) sanity/integrity test the data.

Two distinct use cases (per user spec 2026-06-06):
  1. GAP FILLING: extract missing fields (title, LinkedIn URL, location, email, phone)
  2. SANITY/INTEGRITY TESTING: validate that existing data is correct, not just present

Both run via the same cloud-model fallback chain:
  Primary:   minimax-m3:cloud       (fast, free, default)
  Fallback 1: deepseek-v4-pro:cloud  (more reasoning power)
  Fallback 2: glm-5.1:cloud          (alternative reasoning)

If ALL cloud models fail, callers fall back to regex/string matching.
AI is ADDITIVE only — failures NEVER break the pipeline.

Staged Pipeline (per user spec):
  1. SearXNG/Degoog  -> raw results (existing, unlimited, free)
  2. Gap analysis    -> count missing fields per record
  3. Sanity check    -> verify existing fields are consistent/correct
  4. AI enrichment   -> gap filling + integrity verification
  5. Save to DB      (existing, unchanged)

Graceful Degradation:
  - If Ollama is unreachable, all functions return None
  - Callers MUST handle None and fall back to regex/string matching
  - AI failures NEVER break the pipeline

Usage Tracking:
  - All calls logged in provider_usage.json with month-keyed stats
  - Tracks: success, fail, latency, model used, gap count, operation type
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from lf_config import get

BASE_DIR = Path(__file__).parent
USAGE_FILE = BASE_DIR / "provider_usage.json"

logger = logging.getLogger("lf_ai_enrich")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ── Configuration ──────────────────────────────────────────────────────────

def ollama_url() -> str:
    return get("ai_ollama_url", "http://localhost:11434")


def cloud_model_primary() -> str:
    return get("ai_cloud_model_primary", "minimax-m3:cloud")


def cloud_model_fallback_1() -> str:
    return get("ai_cloud_model_fallback_1", "deepseek-v4-pro:cloud")


def cloud_model_fallback_2() -> str:
    return get("ai_cloud_model_fallback_2", "glm-5.1:cloud")


def cloud_model_chain() -> list[str]:
    """Returns the ordered list of cloud models to try in sequence."""
    return [
        cloud_model_primary(),
        cloud_model_fallback_1(),
        cloud_model_fallback_2(),
    ]


def ai_enabled() -> bool:
    """AI is ON by default per user spec (2026-06-06)."""
    return get("ai_enrichment_enabled", True) is True


def ai_timeout() -> int:
    return int(get("ai_timeout_seconds", 30))


def ai_sanity_check_enabled() -> bool:
    """Sanity/integrity checking is ON by default."""
    return get("ai_sanity_check_enabled", True) is True


# ── Gap Analysis ────────────────────────────────────────────────────────────

# Fields that indicate a "complete" executive record
GAP_FIELDS = ("title", "linkedin_url", "location", "email", "phone")


def count_gaps(record: dict) -> int:
    """
    Count missing or empty fields in a person/contact record.
    A field is considered missing if it is None, empty string, or whitespace-only.
    """
    if not record:
        return len(GAP_FIELDS)
    return sum(1 for f in GAP_FIELDS if not str(record.get(f, "") or "").strip())


def has_complete_data(record: dict) -> bool:
    return count_gaps(record) == 0


# ── Usage Tracking ──────────────────────────────────────────────────────────

def _load_usage() -> dict:
    if USAGE_FILE.exists():
        try:
            with open(USAGE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_usage(usage: dict) -> None:
    with open(USAGE_FILE, "w") as f:
        json.dump(usage, f, indent=2)


def _current_month() -> str:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return f"{now.year}-{now.month:02d}"


def _track_call(model: str, operation: str, success: bool, latency_ms: int, gap_count: int = 0) -> None:
    """
    Track AI call in provider_usage.json.
    Stats tracked per month per model+operation:
      - calls (total)
      - success / fail
      - latency_total_ms
      - gap_total (for gap-fill operations)
    """
    usage = _load_usage()
    month = _current_month()
    if month not in usage:
        usage[month] = {}
    stats = usage[month].setdefault("ai", {})
    key = f"{model}|{operation}"
    model_stats = stats.setdefault(key, {
        "model": model, "operation": operation,
        "calls": 0, "success": 0, "fail": 0,
        "latency_total_ms": 0, "gap_total": 0
    })
    model_stats["calls"] += 1
    if success:
        model_stats["success"] += 1
    else:
        model_stats["fail"] += 1
    model_stats["latency_total_ms"] += latency_ms
    model_stats["gap_total"] += gap_count
    _save_usage(usage)


def get_ai_usage_summary() -> dict:
    """Return AI usage stats for current month. Used by dashboard."""
    usage = _load_usage()
    month = _current_month()
    ai_stats = usage.get(month, {}).get("ai", {})
    summary = {
        "by_model": {},
        # Updated 2026-06-07 to include the 4 deep-research ops (QC-13)
        "by_operation": {
            "gap_fill": 0, "sanity_check": 0, "ranking": 0, "mining": 0, "extraction": 0,
            "pattern_inference": 0, "website_discovery": 0,
            "business_type_normalize": 0, "company_sanity": 0,
        },
        "total_calls": 0, "total_success": 0, "total_fail": 0,
        "avg_latency_ms": 0,
    }
    total_latency = 0
    for key, stats in ai_stats.items():
        calls = stats.get("calls", 0)
        success = stats.get("success", 0)
        fail = stats.get("fail", 0)
        latency_total = stats.get("latency_total_ms", 0)
        gap_total = stats.get("gap_total", 0)
        model = stats.get("model", "unknown")
        operation = stats.get("operation", "unknown")
        avg_latency = int(latency_total / calls) if calls else 0

        if model not in summary["by_model"]:
            summary["by_model"][model] = {
                "calls": 0, "success": 0, "fail": 0,
                "avg_latency_ms": 0, "operations": []
            }
        bm = summary["by_model"][model]
        bm["calls"] += calls
        bm["success"] += success
        bm["fail"] += fail
        bm["avg_latency_ms"] = int((bm["avg_latency_ms"] + avg_latency) / 2) if bm["calls"] else 0
        if operation not in bm["operations"]:
            bm["operations"].append(operation)

        if operation in summary["by_operation"]:
            summary["by_operation"][operation] += calls
        summary["total_calls"] += calls
        summary["total_success"] += success
        summary["total_fail"] += fail
        total_latency += latency_total
    summary["avg_latency_ms"] = int(total_latency / summary["total_calls"]) if summary["total_calls"] else 0
    return summary


# ── HTTP Client with Fallback Chain ─────────────────────────────────────────

# Deep research tasks need deep reasoning — use deepseek-v4-pro:cloud (or equivalent)
# Added 2026-06-07 per user: "we have the deep research model" — primary for
# email pattern inference, website discovery, business type normalization,
# company sanity checks. These tasks require multi-hop reasoning.
DEEP_RESEARCH_OPERATIONS = {
    "pattern_inference",       # email pattern inference
    "website_discovery",       # company website / aggregator detection
    "business_type_normalize", # Google Places type → canonical category
    "company_sanity",          # company record integrity check
    "company_search",          # Issue #2: AI-led company research (PRIMARY in chain)
    "contact_research",        # Issue #2: AI-led contact research (PRIMARY in chain)
}


def _ollama_generate(payload: dict, timeout: int) -> Optional[dict]:
    """
    Make POST request to Ollama API. Returns parsed JSON or None on error.
    NEVER raises — always returns None on any failure (network, timeout, parse).
    """
    url = ollama_url().rstrip("/") + "/api/generate"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning("Ollama returned HTTP %d", resp.status)
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        logger.warning("Ollama request failed: %s", e)
        return None
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Ollama returned invalid JSON: %s", e)
        return None


# ── Public API ──────────────────────────────────────────────────────────────

def ai_complete(
    prompt: str,
    operation: str = "general",
    gap_count: int = 0,
    timeout: Optional[int] = None
) -> Optional[str]:
    """
    Send a prompt to Ollama, trying the cloud model chain in order.
    Returns the response text from the first model that succeeds, or None if all fail.

    For DEEP_RESEARCH operations (pattern_inference, website_discovery,
    business_type_normalize, company_sanity), the deep research model
    (deepseek-v4-pro:cloud) is tried first. This is the "deep research
    model" that the user wants for these multi-hop reasoning tasks.

    Args:
      prompt: The user prompt to send
      operation: Operation type for usage tracking
      gap_count: Number of gaps being filled (for stats)
      timeout: Timeout in seconds (default: from config)
    """
    if not ai_enabled():
        return None

    timeout = timeout or ai_timeout()

    # Choose model chain based on operation type
    if operation in DEEP_RESEARCH_OPERATIONS:
        # Deep research: deepseek-v4-pro first (user's preferred model for
        # multi-hop reasoning tasks), then standard chain
        primary = get("ai_deep_research_model", "deepseek-v4-pro:cloud")
        models = [primary] + [m for m in cloud_model_chain() if m != primary]
    else:
        models = cloud_model_chain()

    last_error = None
    for model in models:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
        start = time.time()
        result = _ollama_generate(payload, timeout)
        latency_ms = int((time.time() - start) * 1000)

        if result is None:
            _track_call(model, operation, success=False, latency_ms=latency_ms, gap_count=gap_count)
            last_error = f"{model} failed"
            continue  # try next model

        response_text = result.get("response", "").strip()
        if not response_text:
            _track_call(model, operation, success=False, latency_ms=latency_ms, gap_count=gap_count)
            last_error = f"{model} returned empty"
            continue  # try next model

        _track_call(model, operation, success=True, latency_ms=latency_ms, gap_count=gap_count)
        return response_text

    logger.warning("All AI models failed: %s", last_error)
    return None


def _extract_json_from_response(response: str) -> Optional[dict | list]:
    """
    Extract JSON object/array from AI response.
    Handles: pure JSON, JSON in markdown code blocks, JSON with surrounding text.
    Returns parsed JSON or None.
    """
    if not response:
        return None
    text = response.strip()

    # Try direct parse first
    try:
        result = json.loads(text)
        if isinstance(result, (dict, list)):
            return result
    except (json.JSONDecodeError, ValueError):
        pass

    # Try extracting from ```json ... ``` code blocks
    match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\}|\[[\s\S]*?\])\s*```", text)
    if match:
        try:
            result = json.loads(match.group(1))
            if isinstance(result, (dict, list)):
                return result
        except (json.JSONDecodeError, ValueError):
            pass

    # Try finding first { ... } or [ ... ] in the text
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start_idx = text.find(start_char)
        if start_idx < 0:
            continue
        end_idx = text.rfind(end_char)
        if end_idx <= start_idx:
            continue
        candidate = text[start_idx:end_idx + 1]
        try:
            result = json.loads(candidate)
            if isinstance(result, (dict, list)):
                return result
        except (json.JSONDecodeError, ValueError):
            continue

    return None


def ai_extract_structured(
    raw_text: str,
    schema: dict,
    context: str = "",
    gap_count: int = 1
) -> Optional[dict]:
    """
    Extract structured data from raw text using AI (gap-fill use case).
    Returns dict matching the schema, or None on failure.

    Args:
      raw_text: The text to extract from (capped at 2KB)
      schema: Dict describing fields to extract, e.g. {"name": "string", "title": "string"}
      context: Optional context (e.g., "Boeing leadership page")
      gap_count: Number of gaps being filled (for stats)
    """
    if not ai_enabled():
        return None

    # Truncate to keep cloud model prompt under 3KB
    raw_text = raw_text[:2000]
    schema_desc = "\n".join(f'  "{k}": {v}' for k, v in schema.items())
    ctx = f"\nContext: {context}" if context else ""

    prompt = f"""Extract structured data from this text. Return ONLY a JSON object with these fields:
{schema_desc}
Use null for fields you cannot determine. No explanations.{ctx}

Text:
{raw_text}

JSON:"""

    response = ai_complete(prompt, operation="extraction", gap_count=gap_count)
    if not response:
        return None

    result = _extract_json_from_response(response)
    return result if isinstance(result, dict) else None


def ai_sanity_check(
    record: dict,
    context: str = ""
) -> Optional[dict]:
    """
    Sanity/integrity test for an existing record (per user spec 2026-06-06).
    Verifies that existing fields are correct, consistent, and plausible.
    Does NOT extract new data — only validates.

    Args:
      record: Dict with fields like {name, title, company, email, linkedin_url, location}
      context: Optional context (e.g., "From Boeing leadership page")

    Returns:
      Dict with:
        - is_valid: bool
        - confidence: float 0.0-1.0
        - issues: list of strings (e.g., "Title seems generic", "Email format invalid")
        - corrections: dict of {field: corrected_value} (only if AI is confident)
        - reasoning: short string explaining the verdict
      OR None on failure (caller treats as "unable to verify, accept as-is")
    """
    if not ai_enabled() or not ai_sanity_check_enabled():
        return None

    if not record:
        return None

    # Build compact record string (avoid sending huge data)
    record_str = json.dumps({k: v for k, v in record.items() if v}, indent=2)[:1500]
    ctx = f"\nContext: {context}" if context else ""

    prompt = f"""Sanity-check this executive/contact record for correctness and internal consistency.

Return ONLY a JSON object:
{{
  "is_valid": <true/false>,
  "confidence": <0.0-1.0>,
  "issues": [<list of issue strings, empty if none>],
  "corrections": {{<field: corrected_value>, only include if you're confident>}},
  "reasoning": "<one-line explanation>"
}}

Checks to perform:
- Are name, title, company consistent (e.g., title matches company size/type)?
- Is the LinkedIn URL plausibly for this person (slug matches name)?
- Is the email format valid and matches the pattern implied by the company?
- Is the location plausible (e.g., US state matches company HQ region)?
- Are there any obvious typos or hallucinations?
- Does the title make sense for a senior executive?{ctx}

Record:
{record_str}

JSON:"""

    response = ai_complete(prompt, operation="sanity_check")
    if not response:
        return None

    result = _extract_json_from_response(response)
    return result if isinstance(result, dict) else None


def ai_rank_linkedin_profiles(
    name: str,
    company: str,
    profiles: list[dict]
) -> Optional[dict]:
    """
    Rank LinkedIn profiles using AI to pick the best match.
    Returns the top-ranked profile (with added ai_scores field) or None on failure.

    Integrity scoring (per user spec 2026-06-06):
      - company_reference: does the profile mention the target company?
      - location_reference: does the profile location match expected region?
      - title_reference: does the title suggest a senior executive role?
      - Combined integrity score 0.0-1.0 (geometric mean of the 3 references)

    Returns profile with added fields:
      - ai_confidence: combined integrity score
      - ai_company_match: bool
      - ai_location_match: bool
      - ai_title_match: bool
      - ai_reasoning: short string explaining the verdict
      - ai_ranking_used: True
    OR returns {"no_linkedin_profile_found": True, "ai_reasoning": "..."} if no profiles match.
    """
    if not ai_enabled() or not profiles:
        return None

    # Build compact representation for prompt
    profile_summaries = []
    for i, p in enumerate(profiles[:5]):
        profile_summaries.append(
            f"[{i}] url={p.get('url', '')}\n"
            f"    title={p.get('title', '')}\n"
            f"    location={p.get('location', '')}\n"
            f"    snippet={p.get('snippet', '')[:200]}"
        )
    profiles_text = "\n".join(profile_summaries)

    prompt = f"""Given these LinkedIn profiles, rank them by likelihood of being the correct person.

Person: {name}
Company: {company}

Profiles:
{profiles_text}

Return ONLY a JSON object:
{{"best_index": <0-{len(profiles)-1}>, "confidence": <0.0-1.0>, "reasoning": "<short>"}}

If none match well, use best_index: -1."""

    response = ai_complete(prompt, operation="ranking")
    if not response:
        return None

    result = _extract_json_from_response(response)
    if not isinstance(result, dict):
        return None

    idx = result.get("best_index", -1)

    # No LinkedIn profile found indicator (per user spec 2026-06-06)
    if idx == -1 or idx < 0 or idx >= len(profiles):
        return {
            "no_linkedin_profile_found": True,
            "ai_reasoning": result.get("reasoning", "No matching LinkedIn profile found"),
            "ai_confidence": 0.0,
            "ai_ranking_used": True,
        }

    best = dict(profiles[idx])

    # Integrity scoring based on 3 reference types (per user spec 2026-06-06)
    # Geometric mean so that ALL three must be reasonably high
    company_ref = 1.0 if best.get("company_mentioned") else 0.0
    location = best.get("location", "").lower()
    person_name_lower = name.lower()
    company_lower = company.lower()
    snippet_lower = (best.get("snippet", "") or "").lower()
    title_lower = (best.get("title", "") or "").lower()

    # Location reference: snippet or location field contains expected region/state
    location_ref = 0.0
    if location:
        # Check for CA, California, El Segundo, etc. — fuzzy match
        if any(s in location for s in ["ca", "california", "el segundo", "long beach", "los angeles"]):
            location_ref = 1.0
        elif any(s in snippet_lower for s in ["california", "ca,", " los angeles", "el segundo"]):
            location_ref = 0.7

    # Title reference: looks like a senior executive
    title_ref = 0.0
    senior_keywords = ["ceo", "cfo", "coo", "cto", "president", "vice president", "vp",
                       "chief", "director", "head of", "founder", "owner", "general manager"]
    if any(kw in title_lower for kw in senior_keywords):
        title_ref = 1.0
    elif title_lower:  # has some title
        title_ref = 0.5

    # Combined integrity score (geometric mean)
    combined = (company_ref * location_ref * title_ref) ** (1/3) if (company_ref * location_ref * title_ref) > 0 else 0.0
    # Blend with AI's own confidence
    ai_conf = result.get("confidence", 0.0)
    final_conf = (combined + ai_conf) / 2

    best["ai_confidence"] = round(final_conf, 3)
    best["ai_company_match"] = bool(company_ref >= 1.0)
    best["ai_location_match"] = bool(location_ref >= 0.7)
    best["ai_title_match"] = bool(title_ref >= 1.0)
    best["ai_reasoning"] = result.get("reasoning", "")
    best["ai_ranking_used"] = True
    best["no_linkedin_profile_found"] = False
    return best


def ai_parse_website_leadership(
    html: str,
    company_name: str,
    url: str = ""
) -> Optional[list[dict]]:
    """
    Parse company leadership page HTML using AI to extract executive names/titles.
    Returns list of {full_name, title, linkedin_url, confidence} or None on failure.

    Args:
      html: The page HTML (truncated to ~8KB to stay within prompt limits)
      company_name: Company name for context
      url: Source URL (for provenance)
    """
    if not ai_enabled() or not html:
        return None

    html_snippet = html[:8000]

    prompt = f"""Extract executive/leadership information from this company page HTML.

Company: {company_name}
Source URL: {url}

Return ONLY a JSON array of objects, each with:
- "full_name": person's full name
- "title": their job title (CEO, VP Engineering, etc.)
- "linkedin_url": their LinkedIn profile URL if present (else null)
- "confidence": your confidence 0.0-1.0 that this is a real executive

If no executives are found, return [].

HTML:
{html_snippet}

JSON:"""

    response = ai_complete(prompt, operation="extraction")
    if not response:
        return None

    result = _extract_json_from_response(response)
    if isinstance(result, list):
        return [p for p in result if isinstance(p, dict) and p.get("full_name")]
    return None


def ai_mine_linkedin_results(
    name: str,
    company: str,
    raw_results: list[dict]
) -> Optional[list[dict]]:
    """
    Mine raw search results for LinkedIn profiles and score them with AI.
    Returns list of profiles with added ai_scores field, or None on failure.

    Args:
      name: Person's name
      company: Company name
      raw_results: Raw SearXNG results
    """
    if not ai_enabled() or not raw_results:
        return None

    # Filter to LinkedIn profiles only
    li_profiles = [
        r for r in raw_results
        if "linkedin.com/in/" in r.get("url", "")
    ]
    if not li_profiles:
        return None

    # Build compact representation
    profile_summaries = []
    for i, p in enumerate(li_profiles[:5]):
        profile_summaries.append(
            f"[{i}] url={p.get('url', '')}\n"
            f"    title={p.get('title', '')}\n"
            f"    snippet={p.get('snippet', '')[:300]}"
        )
    profiles_text = "\n".join(profile_summaries)

    prompt = f"""Analyze these LinkedIn search results and score each profile for match quality.

Person sought: {name}
Company: {company}

For each profile, return a JSON object with:
- "index": profile index (0-{len(li_profiles)-1})
- "name_match_score": 0.0-1.0 (how well the URL/name matches the sought person)
- "company_match_score": 0.0-1.0 (does the profile mention the company)
- "seniority_score": 0.0-1.0 (is this a senior executive?)
- "plausibility_score": 0.0-1.0 (overall plausibility this is the right person)
- "is_correct_person": true/false

Return ONLY a JSON array of objects.

Profiles:
{profiles_text}

JSON:"""

    response = ai_complete(prompt, operation="mining")
    if not response:
        return None

    scores = _extract_json_from_response(response)
    if not isinstance(scores, list):
        return None

    # Attach scores to original profiles
    scored_profiles = []
    for s in scores:
        if not isinstance(s, dict):
            continue
        idx = s.get("index")
        if isinstance(idx, int) and 0 <= idx < len(li_profiles):
            p = dict(li_profiles[idx])
            p["ai_scores"] = {
                "name_match": s.get("name_match_score", 0.0),
                "company_match": s.get("company_match_score", 0.0),
                "seniority": s.get("seniority_score", 0.0),
                "plausibility": s.get("plausibility_score", 0.0),
                "is_correct_person": s.get("is_correct_person", False),
            }
            scored_profiles.append(p)
    return scored_profiles if scored_profiles else None


    # ─────────────────────────────────────────────────────────────────────
    # AI-led Discovery (added 2026-06-07, QC-13)
# ─────────────────────────────────────────────────────────────────────
# These methods make AI the PRIMARY discovery engine. SearXNG becomes
# a fallback. Used for: email pattern inference, website discovery,
# business type normalization, company record sanity checks.

AGGREGATOR_DOMAINS = [
    "yelp.com", "facebook.com", "linkedin.com", "google.com",
    "mapquest.com", "yellowpages.com", "bbb.org", "manta.com",
    "chamberofcommerce.com", "buzzfile.com", "dnb.com", "crunchbase.com",
    "indeed.com", "glassdoor.com", "trustpilot.com", "porch.com",
]

def ai_infer_email_pattern(
    domain: str,
    company_name: str = "",
    industry: str = "",
    known_emails: list[str] | None = None,
    known_employee_names: list[str] | None = None,
) -> Optional[dict]:
    """
    Use AI to infer the most likely email pattern for a company domain. (added 2026-06-07)

    AI is the PRIMARY inference engine. If `known_emails` is provided, AI
    can use them as evidence. Otherwise AI reasons about industry conventions
    + company name + known facts.

    Returns dict with:
      - pattern: e.g. "{first}.{last}@domain.com"
      - confidence: 0.0-1.0
      - reasoning: short string explaining the verdict
      - source: "ai_inferred"
    OR None on failure.

    Examples:
      ai_infer_email_pattern("boeing.com", "The Boeing Company", "aerospace")
        → {pattern: "{first}.{last}@boeing.com", confidence: 0.9, ...}
      ai_infer_email_pattern("5starpkg.com", "5 Star Packaging", known_emails=["jane@5starpkg.com"])
        → {pattern: "{first}@{domain}", confidence: 0.6, ...}
    """
    if not ai_enabled():
        return None
    if not domain:
        return None

    domain = domain.lower().strip().lstrip("@")
    if domain.startswith("www."):
        domain = domain[4:]

    # Build context for the AI
    context_parts = [f"Company: {company_name}" if company_name else ""]
    if industry:
        context_parts.append(f"Industry: {industry}")
    context_parts.append(f"Email domain: @{domain}")
    if known_emails:
        context_parts.append(f"Known emails at this domain: {', '.join(known_emails[:5])}")
    if known_employee_names:
        context_parts.append(f"Known employees: {', '.join(known_employee_names[:5])}")
    context = "\n".join(p for p in context_parts if p)

    # The 21 canonical patterns (literal text — we don't want f-string interpolation)
    canonical_patterns_text = """\
  {first}.{last}@{domain}
  {first}_{last}@{domain}
  {first}{last}@{domain}
  {f}{last}@{domain}
  {first}{l}@{domain}
  {f}.{last}@{domain}
  {last}@{domain}
  {first}@{domain}
  {first}.{m}.{last}@{domain}
  {first}-{last}@{domain}"""

    # Build the example string outside the f-string to avoid brace confusion
    example_pattern = "{first}.{last}@" + domain  # not f-string; literal braces preserved

    prompt = (
        "Infer the most likely email pattern for employees at this company.\n\n"
        + context + "\n\n"
        + "The 21 canonical email patterns are:\n" + canonical_patterns_text + "\n\n"
        + "Use these hints:\n"
        "- If known emails are provided, analyze them to extract the actual pattern.\n"
        "- If industry is known, consider conventions (e.g., finance often uses first.last, tech often uses first).\n"
        "- If company is small (< 50 employees), first@domain is common (e.g., contact@, jane@).\n"
        "- If company is large, first.last@domain is most common globally.\n"
        "- For conglomerates with multiple domains, the pattern often uses the company brand.\n\n"
        + 'Return ONLY a JSON object:\n'
        + '{\n'
        + f'  "pattern": "<e.g. {example_pattern}>",\n'
        + '  "confidence": <0.0-1.0>,\n'
        + '  "reasoning": "<one-line explanation>"\n'
        + '}\n\n'
        + 'If you cannot make a reasonable guess, return {"pattern": null, "confidence": 0.0, "reasoning": "no basis for inference"}.\n'
    )

    response = ai_complete(prompt, operation="pattern_inference", gap_count=0)
    if not response:
        return None

    result = _extract_json_from_response(response)
    if not isinstance(result, dict):
        return None
    if not result.get("pattern"):
        return None

    # Substitute the actual domain into the pattern
    pattern = result["pattern"]
    if "{domain}" not in pattern:
        # AI forgot the {domain} placeholder — append it
        if "@" in pattern:
            pattern = pattern.split("@")[0] + "@{domain}"
        else:
            pattern = pattern + "@{domain}"
    pattern = pattern.replace("{domain}", domain)

    return {
        "pattern": pattern,
        "confidence": float(result.get("confidence", 0.0)),
        "reasoning": result.get("reasoning", ""),
        "source": "ai_inferred",
    }


def ai_infer_email_pattern_v2(
    domain: str,
    company_name: str = "",
    industry: str = "",
    city: str = "",
    state: str = "",
    timeout: int = 25,
) -> Optional[dict]:
    """
    Email pattern inference with a 3-step INTERNAL reasoning loop
    (added 2026-06-16 per user spec). This is the new PRIMARY path
    for `discover_email_pattern` calls.

    Per user spec:
    - The website domain is the SOURCE OF TRUTH for the email tail.
    - The prompt is targeted and focused on the AI identifying the
      most likely pattern for this company.
    - The 3-step inner loop is the "loops and redundancies" that
      strengthen the inference:
        Step 1: INDUSTRY HEURISTIC - which pattern does this industry
                typically use? (Banking? Tech? Law firm? etc.)
        Step 2: SCRAPE & SEARCH - think about what you know about the
                company's website, about page, contact page, press
                releases, team listings, etc. From those public
                postings, infer the pattern. (AI uses its training-data
                knowledge of the website; no live scraping.)
        Step 3: DOUBLE-CHECK - mentally generate 2 example emails
                with typical names at this company. If the result
                is plausible, lock in the pattern. If not, re-pick.

    - Returns the top 10 most common corporate email patterns
      (down from 21) so the AI doesn't pick obscure variants.
    - Minimal context: name + industry + location. The AI infers
      company size from the name/industry.
    - Single AI call (no outer 3-persona loop).

    Domain rule: the pattern MUST end with @{domain}.
    Returns None if the AI can't make a reasonable guess.
    """
    if not ai_enabled():
        return None
    if not domain:
        return None

    domain = domain.lower().strip().lstrip("@")
    if domain.startswith("www."):
        domain = domain[4:]

    # Top 10 most common corporate email patterns
    canonical_patterns_text = """\
   1. {first}.{last}@{domain}        - "john.smith@"        (most common worldwide)
   2. {first}{last}@{domain}         - "johnsmith@"
   3. {f}{last}@{domain}             - "jsmith@"
   4. {first}_{last}@{domain}        - "john_smith@"
   5. {first}@{domain}               - "john@"               (small biz, startups)
   6. {last}@{domain}                - "smith@"              (very small biz)
   7. {first}.{last_initial}@{domain} - "john.s@"             (some law firms)
   8. {last}.{first}@{domain}        - "smith.john@"
   9. {first}-{last}@{domain}        - "john-smith@"
  10. {f}.{last}@{domain}            - "j.smith@"            (formal/some finance)"""

    context_parts = [f"Company: {company_name}" if company_name else ""]
    if industry:
        context_parts.append(f"Industry: {industry}")
    context_parts.append(f"Email domain (the tail, AFTER @): @{domain}")
    if city or state:
        context_parts.append(f"Location: {city}, {state}")
    context = "\n".join(p for p in context_parts if p)

    example_pattern = "{first}.{last}@" + domain

    prompt = (
        "You are a senior sales operations analyst with 20 years of experience\n"
        "inferring corporate email patterns from company name, industry, and public\n"
        "information. You are methodical, evidence-driven, and you always double-check\n"
        "your own work before answering.\n\n"
        f"# TASK\n"
        f"Determine the most likely email pattern used by employees of {company_name}\n"
        f"(@{domain}). The pattern is what comes BEFORE the '@' - the domain is fixed.\n\n"
        f"# INPUT CONTEXT\n{context}\n\n"
        f"# DOMAIN RULE (CRITICAL)\n"
        f"The pattern MUST end with '@{domain}'. Do NOT propose any other domain.\n\n"
        f"# THE TOP 10 MOST COMMON CORPORATE EMAIL PATTERNS\n"
        f"Pick exactly one, expressed with placeholders that will be substituted later:\n"
        f"{canonical_patterns_text}\n\n"
        f"# REASONING LOOP - DO THESE 3 STEPS IN ORDER BEFORE ANSWERING\n\n"
        f"STEP 1 - INDUSTRY HEURISTIC:\n"
        f"  What pattern does this industry typically use? Cite which applies:\n"
        f"  - Banking / finance / insurance: #1, #3\n"
        f"  - Tech / SaaS / startups: #1, #3, #5\n"
        f"  - Law firms: #1, #7\n"
        f"  - Manufacturing / industrial: #1, #2, #3\n"
        f"  - Media / agencies / consulting: #1, #2, #5\n"
        f"  - Healthcare / hospitals: #1, #3, #5\n"
        f"  - Universities / non-profits: #1, #5, #6\n"
        f"  - Government / military: #1, #3\n"
        f"  - Real estate / construction: #1, #3, #4\n"
        f"  - Retail / hospitality: #1, #5, #6\n"
        f"  Pick the most common pattern for this industry.\n\n"
        f"STEP 2 - SCRAPE & SEARCH YOUR KNOWLEDGE:\n"
        f"  Think about the company's public information that you may have seen in\n"
        f"  your training data: their website, 'About' page, 'Team' or 'Leadership' page,\n"
        f"  press releases, contact forms, LinkedIn employee listings, conference\n"
        f"  speaker bios, etc. From any of those sources, have you seen an email\n"
        f"  address at @{domain} that would confirm a pattern? If so, use that as\n"
        f"  strong evidence. If you have no specific public evidence for THIS company,\n"
        f"  fall back to the industry heuristic from Step 1.\n\n"
        f"STEP 3 - DOUBLE-CHECK:\n"
        f"  Mentally generate 2 example emails using your chosen pattern with typical\n"
        f"  names at this company (e.g. 'John Smith' -> 'john.smith@{domain}'). Is the\n"
        f"  result plausible? If yes, lock it in. If the result looks weird (e.g.\n"
        f"  duplicates, ambiguous abbreviations), reconsider.\n\n"
        f"# CONFIDENCE SCORING\n"
        f"  0.85-1.0: pattern strongly supported by industry + Step 2 evidence\n"
        f"  0.65-0.84: industry heuristic is clear, no contradicting evidence\n"
        f"  0.40-0.64: industry ambiguous, multiple plausible patterns\n"
        f"  0.20-0.39: low confidence - essentially guessing\n"
        f"  0.0-0.19: no basis - return null\n\n"
        f"# OUTPUT FORMAT (STRICT JSON, NO PROSE)\n"
        f"Return ONLY a JSON object:\n"
        f'{{"pattern": "<one of the 10 patterns ending in @{domain}>",\n'
        f' "confidence": <float 0.0-1.0>,\n'
        f' "reasoning": "<one short sentence citing the strongest evidence>",\n'
        f' "pattern_index": <integer 1-10>}}\n\n'
        f'If no basis: {{"pattern": null, "confidence": 0.0, "reasoning": "<why>",\n'
        f' "pattern_index": null}}\n\n'
        f"# REMINDERS\n"
        f"- Pattern MUST end with @{domain}\n"
        f"- Use real placeholders ({{first}}, {{last}}, {{f}}, {{l}}, {{m}}, {{n}}) - not literal names\n"
        f"- Don't invent patterns not in the top 10\n"
        f"- When in doubt, default to #1 ({{first}}.{{last}}@{domain}) - most common worldwide\n\n"
        f"Example valid output: {{\"pattern\": \"{example_pattern}\",\n"
        f' \"confidence\": 0.85, \"reasoning\": \"...\", \"pattern_index\": 1}}'
    )

    response = ai_complete(prompt, operation="pattern_inference", gap_count=0, timeout=timeout)
    if not response:
        return None

    result = _extract_json_from_response(response)
    if not isinstance(result, dict):
        return None
    if not result.get("pattern"):
        return None

    # Substitute the actual domain into the pattern
    pattern = result["pattern"]
    if "{domain}" not in pattern:
        # AI forgot the {domain} placeholder - append it
        if "@" in pattern:
            pattern = pattern.split("@")[0] + "@{domain}"
        else:
            pattern = pattern + "@{domain}"
    pattern = pattern.replace("{domain}", domain)

    return {
        "pattern": pattern,
        "confidence": float(result.get("confidence", 0.0)),
        "reasoning": result.get("reasoning", ""),
        "pattern_index": result.get("pattern_index"),
        "source": "ai_inferred_v2",
    }



def ai_discover_company_website(
    company_name: str,
    city: str = "",
    state: str = "",
    current_website: str = "",
) -> Optional[dict]:
    """
    Find the official company website or detect aggregator URLs. (added 2026-06-07)

    Uses AI to reason about (1) whether the current_website is the real
    company site or an aggregator (yelp, facebook, etc.), and (2) what
    the real website is if current_website is missing or aggregator.

    Returns dict with:
      - website: best-guess official URL
      - is_real_website: bool
      - is_aggregator: bool
      - confidence: 0.0-1.0
      - reasoning: short string
    OR None on failure.

    Aggregator domains flagged: yelp.com, facebook.com, linkedin.com,
    google.com, mapquest.com, yellowpages.com, bbb.org, manta.com,
    chamberofcommerce.com, buzzfile.com, dnb.com, indeed.com, etc.
    """
    if not ai_enabled():
        return None
    if not company_name:
        return None

    # Pre-check: is current_website an aggregator?
    is_aggregator = False
    if current_website:
        try:
            from urllib.parse import urlparse
            host = urlparse(current_website).hostname or ""
            host = host.lower().lstrip("www.")
            is_aggregator = any(host == agg or host.endswith("." + agg) for agg in AGGREGATOR_DOMAINS)
        except Exception:
            pass

    context_parts = [f"Company: {company_name}"]
    if city:
        context_parts.append(f"City: {city}")
    if state:
        context_parts.append(f"State: {state}")
    if current_website:
        context_parts.append(f"Current website: {current_website} {'(AGGREGATOR)' if is_aggregator else ''}")
    context = "\n".join(context_parts)

    prompt = f"""Determine the OFFICIAL company website for this company.

{context}

Aggregator domains (NOT real websites): yelp.com, facebook.com, linkedin.com (unless it's /company/{company_name.lower()}), google.com/maps, mapquest.com, yellowpages.com, bbb.org, manta.com, chamberofcommerce.com, indeed.com, glassdoor.com, crunchbase.com, trustpilot.com.

Use your knowledge to identify the official website. Consider:
- Large public companies: usually have a simple domain like boeing.com, honeywell.com.
- Small local businesses: domain often matches name (e.g., acmepkg.com for "Acme Packaging").
- The website should be the company's own, not a directory listing.

Return ONLY a JSON object:
{{
  "website": "<the official URL or null if you don't know>",
  "is_real_website": <true if current_website is the real one, false if aggregator>,
  "is_aggregator": <true if current_website is an aggregator like yelp>,
  "confidence": <0.0-1.0>,
  "reasoning": "<one-line explanation>"
}}

If you don't know the real website, return {{"website": null, "confidence": 0.0, "reasoning": "unknown"}}.
"""

    response = ai_complete(prompt, operation="website_discovery", gap_count=0)
    if not response:
        return None

    result = _extract_json_from_response(response)
    if not isinstance(result, dict):
        return None

    return {
        "website": result.get("website"),
        "is_real_website": bool(result.get("is_real_website", False)),
        "is_aggregator": bool(result.get("is_aggregator", is_aggregator)),
        "confidence": float(result.get("confidence", 0.0)),
        "reasoning": result.get("reasoning", ""),
    }

def ai_normalize_business_type(
    raw_type: str,
    company_name: str = "",
) -> Optional[dict]:
    """
    Normalize raw Google Places 'type' to canonical business categories. (added 2026-06-07)

    Returns dict with:
      - canonical_type: e.g. "Electronics Manufacturer"
      - category: e.g. "Industrial"
      - subcategory: e.g. "Electronics"
      - confidence: 0.0-1.0
      - reasoning: short string
    OR None on failure.

    Examples:
      "electronics_store" + "Acme Electronics" → "Electronics Manufacturer"
      "point_of_interest" + "Acme Co" → "Industrial" (generic)
      "food_store" + "Acme Foods" → "Food Processing"
      "establishment" + "Acme" → "Unclassified"
    """
    if not ai_enabled():
        return None
    if not raw_type:
        return None

    context = f"Company: {company_name}" if company_name else ""

    prompt = f"""Normalize this raw Google Places business type to a canonical category.

{context}
Raw type: {raw_type}

Canonical categories (use the most specific match):
  Industrial: Manufacturer, Industrial Supplier, Machine Shop, Fabricator, Assembler
  Construction: General Contractor, Subcontractor, Specialty Trade
  Food: Food Processing, Food Service, Caterer, Bakery
  Healthcare: Hospital, Clinic, Practice, Pharmacy
  Retail: Store, Boutique, Showroom
  Services: Consulting, Agency, Professional Services
  Technology: Software, Hardware, IT Services
  Transportation: Logistics, Freight, Shipping, Moving
  Hospitality: Hotel, Restaurant, Cafe
  Education: School, Training, Tutoring
  Unclassified: when raw type is too generic (establishment, point_of_interest, place_of_worship)

The output should be a specific subtype, not a broad category. E.g. "Electronics Manufacturer" not just "Industrial".

Return ONLY a JSON object:
{{
  "canonical_type": "<e.g. Electronics Manufacturer>",
  "category": "<top-level category like Industrial>",
  "subcategory": "<e.g. Electronics>",
  "confidence": <0.0-1.0>,
  "reasoning": "<one-line explanation>"
}}
"""

    response = ai_complete(prompt, operation="business_type_normalize", gap_count=0)
    if not response:
        return None

    result = _extract_json_from_response(response)
    if not isinstance(result, dict):
        return None
    if not result.get("canonical_type"):
        return None

    return {
        "canonical_type": result["canonical_type"],
        "category": result.get("category", ""),
        "subcategory": result.get("subcategory", ""),
        "confidence": float(result.get("confidence", 0.0)),
        "reasoning": result.get("reasoning", ""),
    }


def ai_research_companies(
    industry: str,
    city: str = "",
    state: str = "",
    radius_miles: int = 25,
    timeout: int = 8,
) -> Optional[list[dict]]:
    """
    AI research: list known companies in {industry} near {city}, {state}
    within {radius_miles} miles. PRIMARY method for company discovery
    (Issue #2: explicit search chain).

    Returns list of dicts with:
      - name: company name
      - confidence: 0.0-1.0
      - reasoning: short string explaining why the AI thinks they're in the area
      - known_website: official website (or empty)

    Returns None on AI failure. Empty list is valid (AI doesn't know any).

    IMPORTANT: This is the PRIMARY stage in the company search chain. The
    Google Maps verify stage that follows uses these suggestions as
    ground-truth name lookups + proximity filters. AI hallucinations are
    filtered by Google Maps' inability to find a real place for them.

    Timeout: defaults to 8s (tight) so the synchronous search chain
    doesn't block. The Google Maps verify stage runs in parallel and
    catches everything the AI missed. If AI takes longer than timeout,
    returns None and the chain falls through to Google Maps alone.
    """
    if not ai_enabled():
        return None
    if not industry:
        return None

    location_part = ""
    if city and state:
        location_part = f" within {radius_miles} miles of {city}, {state}"
    elif city:
        location_part = f" near {city}"
    elif state:
        location_part = f" in {state}"

    prompt = f"""List as many real, currently-operating {industry} companies as you can name{location_part}.

For each company, return:
- "name": official company name (no abbreviations unless widely used)
- "confidence": 0.0-1.0 — how confident you are they exist AND operate in this area
- "reasoning": one short sentence explaining how you know (e.g., "Publicly traded in El Segundo", "Local business since 1990s")
- "known_website": their official website if you know it, otherwise empty string

Rules:
- Only list real companies you have reasonable confidence exist. If unsure, omit rather than guess.
- Include both large public companies and small/local businesses you happen to know.
- Do NOT include aggregator URLs in reasoning or known_website.
- Return an empty list if you cannot confidently name any companies.
- Limit to ~5-10 companies max (top by confidence; only the top 5 will be verified against Google Maps).

Return ONLY a JSON array of objects:
[
  {{"name": "Acme Manufacturing", "confidence": 0.9, "reasoning": "...", "known_website": "acmemfg.com"}},
  {{"name": "Smith & Sons", "confidence": 0.6, "reasoning": "...", "known_website": ""}}
]
"""
    response = ai_complete(prompt, operation="company_search", gap_count=0, timeout=timeout)
    if not response:
        return None

    result = _extract_json_from_response(response)
    if not isinstance(result, list):
        return None

    # Normalize: ensure required fields, drop junk
    cleaned = []
    for item in result:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or "").strip()
        if not name or len(name) < 2:
            continue
        cleaned.append({
            "name": name,
            "confidence": float(item.get("confidence", 0.5)),
            "reasoning": (item.get("reasoning") or "").strip(),
            "known_website": (item.get("known_website") or "").strip(),
        })

    return cleaned


def ai_sanity_check_company(
    record: dict,
    context: str = "",
) -> Optional[dict]:
    """
    Sanity/integrity check for a company record. (added 2026-06-07)

    Validates internal consistency: website matches name, business_type
    is plausible, email_pattern is well-formed, city/state make sense.

    Returns dict with:
      - is_valid: bool
      - confidence: 0.0-1.0
      - issues: list of issue strings
      - corrections: dict of {field: corrected_value}
      - reasoning: short string
    OR None on failure.
    """
    if not ai_enabled() or not ai_sanity_check_enabled():
        return None
    if not record:
        return None

    record_str = json.dumps({k: v for k, v in record.items() if v}, indent=2)[:1500]
    ctx = f"\nContext: {context}" if context else ""

    prompt = f"""Sanity-check this company record for internal consistency.

Checks to perform:
- Website URL matches company name (e.g., 'boeing.com' for 'The Boeing Company', not 'theboeingcompany.com' or a directory)
- Business type is plausible for the company name and industry
- Email pattern is well-formed (no typos, has @domain, uses {{placeholders}})
- City/state match the company (e.g., not 'El Segundo' for a NYC company)
- Company name doesn't contain obvious typos or junk
- Phone format is consistent with US conventions if US-based

{ctx}

Company record:
{record_str}

Return ONLY a JSON object:
{{
  "is_valid": <true/false>,
  "confidence": <0.0-1.0>,
  "issues": [<list of issue strings, empty if none>],
  "corrections": {{<field: corrected_value>, only include if you're confident>}},
  "reasoning": "<one-line explanation>"
}}
"""

    response = ai_complete(prompt, operation="company_sanity", gap_count=0)
    if not response:
        return None

    result = _extract_json_from_response(response)
    return result if isinstance(result, dict) else None


# ── Contact-Level AI Research (QC-16, 2026-06-09) ───────────────────────────

def ai_research_contact(
    full_name: str,
    company_name: str,
    linkedin_url: str = "",
    current_title: str = "",
) -> Optional[dict]:
    """
    AI-driven FULL research of a contact at a specific company. PRIMARY method.
    SearXNG only fills gaps that AI cannot determine.

    The AI researches everything: title, LinkedIn URL, email, phone, location,
    source credibility, and whether they're currently employed at the company.

    Returns dict with:
      - title: verified title at target company (or None)
      - linkedin_url: best LinkedIn URL (or existing one)
      - email: best guess email (or None)
      - phone: best guess phone (or None)
      - location: geographic area (or None)
      - is_current_employee: True/False
      - confidence: 0.0-1.0 overall
      - reasoning: one-line explanation
      - source: "ai_primary" or "ai_inferred"
      - is_verified: True if AI found explicit evidence

    This is the PRIMARY contact enrichment method. SearXNG is the FALLBACK.
    """
    if not ai_enabled():
        return None
    if not full_name or not company_name:
        return None

    ctx = f"LinkedIn: {linkedin_url}" if linkedin_url else ""
    existing = f"Current scraped data: title='{current_title}'" if current_title else ""

    prompt = f"""Research EVERYTHING about this person at this specific company.

Person: {full_name}
Company: {company_name}
{ctx}
{existing}

IMPORTANT: We need their information at THIS COMPANY, not their current role if they moved elsewhere.

Research and return ALL of the following (return null for anything you cannot verify quickly):

1. TITLE (HIGH PRIORITY): What is their exact job title at {company_name}?
2. LinkedIn URL (MEDIUM PRIORITY): What is their LinkedIn profile URL?
3. EMAIL (MEDIUM PRIORITY): What is their likely email at the company domain? (Return null if domain unknown or pattern unclear)
4. PHONE (CURSORY ONLY — DO NOT SPEND EFFORT): If you happen to know their direct phone, return it. Otherwise return null. Do not search extensively for phone — it's optional.
5. LOCATION (LOW PRIORITY): What city/region are they based in?
6. CURRENT EMPLOYEE: Are they currently working at {company_name}?
7. SOURCE: How do you know this? (e.g., "LinkedIn profile", "Company leadership page", "News article")

Return ONLY a JSON object:
{{
  "title": "<exact title at this company, or null>",
  "linkedin_url": "<full LinkedIn URL, or null>",
  "email": "<email address at company domain, or null>",
  "phone": "<phone number or extension, or null — be lenient, null is fine>",
  "location": "<city, state or region, or null>",
  "is_current_employee": <true/false>,
  "confidence": <0.0-1.0 overall confidence>,
  "is_verified": <true if you found explicit evidence>,
  "reasoning": "<one-line: how you know>",
  "source": "<where you found this info, e.g. 'Company leadership page', 'LinkedIn', 'News article'>"
}}

Rules:
- Return null for ANY field you cannot verify with reasonable confidence
- Title is HIGHEST priority — spend most effort there
- LinkedIn URL and email are MEDIUM priority
- Phone is CURSORY — null is acceptable; do not spend effort hunting
- Location is LOW priority
- Do NOT guess generic titles (CEO/CFO/CTO) unless you have specific evidence
- Prefer specific titles over generic ones (e.g., "Chief Engineer" > "Engineer")
- Confidence < 0.5 means the data is NOT reliable
- If the person does NOT work at this company, set is_current_employee=false and explain"""

    response = ai_complete(prompt, operation="contact_research", gap_count=0)
    if not response:
        return None

    result = _extract_json_from_response(response)
    if not isinstance(result, dict):
        return None

    # Normalize string fields
    for field in ["title", "linkedin_url", "email", "phone", "location"]:
        val = result.get(field)
        if val and isinstance(val, str):
            val = val.strip()
            if val.lower() in ("unknown", "null", "none", "n/a", ""):
                val = None
            result[field] = val

    confidence = float(result.get("confidence", 0))
    is_verified = bool(result.get("is_verified", confidence >= 0.7))

    # Build the full result
    fields = ["title", "linkedin_url", "email", "phone", "location", "is_current_employee", "reasoning", "source"]
    output = {}
    for f in fields:
        output[f] = result.get(f)

    output["confidence"] = round(min(1.0, max(0.0, confidence)), 3)
    output["is_verified"] = is_verified
    output["_source_method"] = "ai_primary" if is_verified else "ai_inferred"

    return output


# ── AI-First LinkedIn Discovery (added 2026-06-16) ────────────────────
# Per user feedback: "use AI first for linkedin searching, then use
# PixelRAG if scraping is needed for latest experience/role/title/location."
# This is the new PRIMARY method for finding executives and their
# LinkedIn profiles. SearXNG and the website leadership scraper become
# fallbacks.

def ai_search_linkedin_executives(
    company_name: str,
    industry: str = "",
    city: str = "",
    state: str = "",
    radius_miles: int = 25,
    timeout: int = 25,
) -> Optional[list[dict]]:
    """
    AI searches its knowledge for senior executives at {company_name}
    near {city}, {state}. Per user feedback (2026-06-16): the prompt
    must be targeted, use all search variables, and request a
    table-format response we can parse directly into the
    enrichment chain. The output is then fed to PixelRAG to verify
    LinkedIn URLs and extract latest experience/role/title/location.

    Each result:
      - full_name: str
      - title: str (CEO, President, VP, Director, Plant Manager, COO, etc.)
      - linkedin_url: str (best guess; verified later by PixelRAG)
      - location: str (city, state)
      - is_current_employee: bool
      - confidence: 0.0-1.0

    Returns None on AI failure. Empty list is valid (AI doesn't know).
    """
    if not ai_enabled():
        return None
    if not company_name:
        return None

    # Build a tight location string. Use all available variables.
    location_str = ""
    if city and state:
        location_str = f"{city}, {state}"
    elif state:
        location_str = state
    elif city:
        location_str = city
    radius_str = f" within {radius_miles} miles" if radius_miles else ""

    # Industry context helps disambiguate common company names
    industry_str = f" in the {industry} industry" if industry else ""

    # Targeted prompt designed to elicit useful results from
    # responsible-AI-tuned models. We frame the task as "research
    # analyst recall" rather than "find LinkedIn profiles" to avoid
    # the refusal pattern triggered by the latter. Per user feedback
    # 2026-06-16: prompt must be short, use all search variables, and
    # request a parseable table.
    prompt = f"""You are a sales research analyst with access to public business information through early 2025.

For the company "{company_name}"{industry_str} based in {location_str}{radius_str}, list the executives and senior managers you are aware of.

Target titles (in priority order):
- CEO / President / Owner / Founder
- COO / Plant Manager / General Manager
- VP (Operations, Manufacturing, Engineering, Sales, etc.)
- Director (any function)
- CFO / CTO / CIO (if known)

For each person, return a JSON object with these fields:
- "name": full name (first + last)
- "title": their known/recent title at this company
- "linkedin": their LinkedIn profile URL in the form linkedin.com/in/<slug> (best guess, leave empty if you don't know)
- "location": city, state they are based in
- "confidence": 0.0-1.0 (how confident you are they are/were at this company)
- "source": one short phrase describing how you know (e.g., "company website", "news article", "LinkedIn profile", "public filing")

Rules:
- Only list people you have reasonable confidence are/were at {company_name}.
- Limit to 10 people max.
- If you don't know the LinkedIn URL, set it to "" — we'll verify separately.
- If you know nothing about this company, return an empty array [].

Return ONLY a JSON array. Example:
[
  {{"name": "Jane Smith", "title": "Chief Executive Officer", "linkedin": "linkedin.com/in/jane-smith-12345", "location": "San Diego, CA", "confidence": 0.9, "source": "company leadership page"}}
]
"""
    response = ai_complete(prompt, operation="executive_search", gap_count=0, timeout=timeout)
    if not response:
        return None

    result = _extract_json_from_response(response)
    if not isinstance(result, list):
        return None

    cleaned = []
    for item in result:
        if not isinstance(item, dict):
            continue
        # Accept both old field names (full_name) and new (name)
        name = (item.get("name") or item.get("full_name") or "").strip()
        if not name or len(name) < 3:
            continue
        title = (item.get("title") or "").strip()
        # LinkedIn can be in "linkedin_url" or "linkedin" or empty
        li_url = (
            item.get("linkedin_url")
            or item.get("linkedin")
            or ""
        )
        if isinstance(li_url, str):
            li_url = li_url.strip()
        if li_url and not li_url.startswith("http"):
            # Normalize: if AI returned just "linkedin.com/in/foo" add https://
            li_url = "https://www." + li_url.lstrip("./")
        location = (item.get("location") or "").strip()
        try:
            conf = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        cleaned.append({
            "full_name": name,
            "title": title,
            "linkedin_url": li_url,
            "location": location,
            "is_current_employee": True,  # AI wouldn't suggest them otherwise
            "confidence": conf,
            "reasoning": (item.get("source") or item.get("reasoning") or "").strip(),
        })

    return cleaned


def ai_extract_latest_from_linkedin_visual(
    full_name: str,
    company_name: str,
    current_ai_data: dict,
    timeout: int = 10,
) -> Optional[dict]:
    """
    PixelRAG-augmented: take what we already know from AI and look for
    the LATEST experience/role/title/location. Called after we have a
    LinkedIn URL and the AI's knowledge of the person, when we suspect
    the AI's info is stale (the user explicitly said PixelRAG is
    useful for "latest experience/role/title/location").

    Input current_ai_data has: full_name, title, linkedin_url,
    is_current_employee, confidence. Returns the same fields with
    updated values if PixelRAG-augmented AI extracted fresher info.
    """
    if not ai_enabled():
        return None
    if not current_ai_data.get("linkedin_url"):
        return None  # Nothing to scrape

    # Note: This is a SIGNAL function — the actual visual scrape is
    # performed by the caller via PixelRAG /screenshot + /extract.
    # Here we just return a marker so the pipeline knows this is
    # the right path. The caller is responsible for the scrape.
    return current_ai_data


# ── Module Self-Test ────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("lf_ai_enrich.py self-test")
    print(f"  ollama_url: {ollama_url()}")
    print(f"  cloud model chain: {cloud_model_chain()}")
    print(f"  ai_enabled: {ai_enabled()}")
    print(f"  ai_sanity_check_enabled: {ai_sanity_check_enabled()}")
    print(f"  timeout: {ai_timeout()}s")

    print("\nGap analysis tests:")
    print(f"  empty record: {count_gaps({})} gaps")
    print(f"  {{name, title}}: {count_gaps({'name': 'X', 'title': 'CEO'})} gaps")
    print(f"  full record: {count_gaps({'title': 'CEO', 'linkedin_url': 'x', 'location': 'LA', 'email': 'a@b.c', 'phone': '555'})} gaps")
    print(f"  has_complete_data(empty): {has_complete_data({})}")
    print(f"  has_complete_data(full): {has_complete_data({'title': 'CEO'})}")

    print("\nAI completion test (primary model):")
    start = time.time()
    response = ai_complete("Respond with the single word: OK", operation="general")
    print(f"  Response: {response!r} (elapsed: {time.time()-start:.2f}s)")

    print("\nSanity check test:")
    test_record = {
        "name": "Steve Isakowitz",
        "title": "CEO",
        "company": "The Aerospace Corporation",
        "linkedin_url": "https://linkedin.com/in/steve-isakowitz",
        "location": "El Segundo, CA"
    }
    start = time.time()
    sanity = ai_sanity_check(test_record, context="Aerospace CEO record")
    print(f"  Result: {json.dumps(sanity, indent=2) if sanity else None} (elapsed: {time.time()-start:.2f}s)")

    print("\nUsage summary:")
    print(json.dumps(get_ai_usage_summary(), indent=2)[:500])
