#!/usr/bin/env python3
"""
lf_pipeline.py - Lead Finder Explicit Enrichment & Search Chains
=================================================================

Defines explicit, ordered, multi-stage chains for company search and
executive enrichment, with explicit fallback relationships. Each chain
runs through a sequence of stages; if a stage is marked as a fallback for
a stage that succeeded, it is skipped. If a stage fails, the next stage
in the chain attempts to fill the gap.

Issue #2 fix: provides a single source of truth for "what runs in what
order" so the enrichment/search hierarchy is no longer implicit in code.

Chains:
  - COMPANY_SEARCH:    AI research -> Google Maps verify by proximity
                       -> Google Maps details -> AI website validation
  - COMPANY_ENRICH:    AI website discovery -> website leadership scrape
                       -> AI contact research -> LinkedIn URL fallback
                       -> Google validation -> AI sanity check
  - CONTACT_DISCOVERY: website scrape (current) -> AI contact research
                       (primary) -> LinkedIn URL verify (SearXNG fallback)
                       -> Google validation -> AI sanity check

PixelRAG is intentionally NOT in the default chains — it remains an
opt-in visual enrichment tool only, per user feedback (poor results for
company search).

Each stage call records its outcome (success/skipped/failed), duration,
and any extracted data into the pipeline log, which the frontend
progress display can consume via the discovery_jobs.stage_log column.
"""
from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Optional


# ── Stage result types ──────────────────────────────────────────────────
@dataclass
class StageResult:
    stage: str
    status: str            # "complete" | "skipped" | "failed" | "running"
    duration_ms: int = 0
    detail: str = ""
    data: dict = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


@dataclass
class PipelineResult:
    chain: str
    stages: list[StageResult] = field(default_factory=list)
    started_at: str = ""
    ended_at: str = ""
    success: bool = True

    def to_dict(self) -> dict:
        return {
            "chain": self.chain,
            "stages": [s.to_dict() for s in self.stages],
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "success": self.success,
        }


# ── Chain definitions ───────────────────────────────────────────────────
# Each chain is a list of stage dicts:
#   - stage: human-readable name (used as key in pipeline log)
#   - primary: bool — primary stages always run
#   - fallback_for: str or None — name of stage this falls back for; if
#     that primary stage succeeded, this stage is skipped
#   - fn: callable(context) -> (status, data_dict) where status is
#     "complete" or "failed". "skipped" is set automatically when
#     fallback_for's primary succeeded.

CHAIN_COMPANY_SEARCH: list[dict] = [
    {
        "stage": "ai_research",
        "primary": True,
        "fallback_for": None,
        "description": "AI research: known companies in {industry} near {city}, {state} within {radius_miles} mi",
    },
    {
        "stage": "google_maps_verify",
        "primary": True,
        "fallback_for": None,
        "description": "Google Maps Text Search verifies each AI suggestion by name + proximity (haversine filter)",
    },
    {
        "stage": "google_maps_details",
        "primary": True,
        "fallback_for": None,
        "description": "Google Maps Place Details: website, phone, address, rating for verified companies",
    },
    {
        "stage": "ai_website_validation",
        "primary": False,
        "fallback_for": "google_maps_details",
        "description": "AI flags aggregator URLs and suggests real company websites",
    },
    {
        "stage": "ai_business_type_normalize",
        "primary": False,
        "fallback_for": "google_maps_details",
        "description": "AI normalizes raw Google Places 'type' to canonical business category",
    },
]

CHAIN_COMPANY_ENRICH: list[dict] = [
    {
        "stage": "ai_website_discovery",
        "primary": True,
        "fallback_for": None,
        "description": "AI discovers/validates the official company website (vs aggregator URLs)",
    },
    {
        "stage": "website_leadership_scrape",
        "primary": True,
        "fallback_for": None,
        "description": "Scrape company website for leadership page (names + titles)",
    },
    {
        "stage": "ai_contact_research",
        "primary": True,
        "fallback_for": None,
        "description": "AI researches each contact: title, LinkedIn, email, location, current employee flag",
    },
    {
        "stage": "linkedin_url_fallback",
        "primary": False,
        "fallback_for": "ai_contact_research",
        "description": "SearXNG LinkedIn URL discovery (only if AI didn't return a LinkedIn URL)",
    },
    {
        "stage": "google_validate_person",
        "primary": True,
        "fallback_for": None,
        "description": "Google search gate: confirm the person actually works at the company",
    },
    {
        "stage": "ai_sanity_check_company",
        "primary": False,
        "fallback_for": None,
        "description": "AI sanity check: website matches name, business type plausible, city/state consistent",
    },
]


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _run_stage(stage_def: dict, context: dict) -> StageResult:
    """Execute a single stage based on its stage name and the context.

    context is mutated in place: stages append their output to
    context['stage_outputs'] and may add fields to context['company'] or
    context['contact'].
    """
    name = stage_def["stage"]
    t0 = time.time()
    res = StageResult(stage=name, status="running")

    try:
        if name == "ai_research":
            from lf_ai_enrich import ai_research_companies
            # Run AI in a thread with a HARD deadline so a hung Ollama
            # call cannot block the synchronous search chain past
            # 5s. If the deadline is exceeded, the stage is marked
            # 'failed' and the chain falls through to Google Maps
            # verify which runs the plain industry+city queries.
            # Note: Python threads can't be forcibly killed; the
            # future.result() returns immediately at the deadline
            # but the AI call continues in the background.
            import concurrent.futures
            deadline_s = 5
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                future = ex.submit(
                    ai_research_companies,
                    industry=context["industry"],
                    city=context.get("city", ""),
                    state=context.get("state", "CA"),
                    radius_miles=context.get("radius_miles", 25),
                    timeout=8,  # inner Ollama timeout
                )
                try:
                    out = future.result(timeout=deadline_s)
                except concurrent.futures.TimeoutError:
                    out = None
                    res.status = "failed"
                    res.detail = f"AI research exceeded {deadline_s}s deadline; falling through to Google Maps"
                    res.error = "ai_research_timeout"
                except Exception as e:
                    out = None
                    res.status = "failed"
                    res.detail = f"AI research error: {type(e).__name__}: {e}"
                    res.error = str(e)
            context["ai_suggestions"] = out or []
            if res.status != "failed":
                res.status = "complete"
                res.detail = f"AI suggested {len(context['ai_suggestions'])} companies"
            res.data = {"count": len(context["ai_suggestions"])}

        elif name == "google_maps_verify":
            from lf_search import _verify_company_by_proximity
            suggestions = context.get("ai_suggestions", [])
            # Always also run plain Google Places queries (industry + city) to
            # catch companies the AI didn't know about. AI is primary but
            # Google Maps is the ground-truth verifier.
            verified = _verify_company_by_proximity(
                suggestions=suggestions,
                industry=context["industry"],
                city=context["city"],
                state=context.get("state", "CA"),
                radius_miles=context.get("radius_miles", 25),
                max_results_per_query=context.get("max_results_per_query", 20),
                extra_queries=context.get("extra_queries"),
            )
            # Normalize: each place gets a 'name' field (from displayName)
            # for downstream code that expects it.
            for p in verified:
                if not p.get("name"):
                    dn = p.get("displayName")
                    if isinstance(dn, dict):
                        p["name"] = dn.get("text", "")
            context["verified_places"] = verified
            res.status = "complete"
            res.detail = f"Google Maps verified {len(verified)} places within radius"
            res.data = {"count": len(verified)}

        elif name == "google_maps_details":
            from lf_search import get_place_details_fast
            from lf_config import google_maps_api_key
            import concurrent.futures
            api_key = google_maps_api_key()
            places = context.get("verified_places", [])

            # Fetch place details in parallel using the fast (no
            # rate-limit-lock) variant. Google Places (New) supports
            # concurrent requests; with max_workers=5 we stay safely
            # under typical QPS quotas and finish 11 companies in
            # ~2-3s instead of 35s.
            def _fetch(p):
                details = get_place_details_fast(api_key, p.get("id") or p.get("place_id", ""))
                if details:
                    p["website"] = details.get("websiteUri", "") or details.get("website", "")
                    p["phone"] = details.get("internationalPhoneNumber", "") or p.get("phone", "")
                    p["rating"] = details.get("rating") or p.get("rating")
                    p["user_rating_count"] = details.get("userRatingCount") or p.get("user_rating_count")
                return p

            if places:
                with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                    enriched = list(ex.map(_fetch, places))
            else:
                enriched = places
            context["verified_places"] = enriched
            res.status = "complete"
            res.detail = f"Fetched details for {len(enriched)} companies (parallel fast)"
            res.data = {"count": len(enriched)}

        elif name == "ai_website_validation":
            if context.get("verified_places"):
                from lf_ai_enrich import ai_discover_company_website
                for p in context["verified_places"]:
                    ai_out = ai_discover_company_website(
                        company_name=p.get("name", ""),
                        city=context.get("city", ""),
                        state=context.get("state", ""),
                        current_website=p.get("website", ""),
                    )
                    if ai_out and ai_out.get("is_real_website") is False and ai_out.get("website"):
                        # AI suggested a different (real) website
                        p["website"] = ai_out["website"]
                        p["ai_website_reasoning"] = ai_out.get("reasoning", "")
                res.status = "complete"
                res.detail = f"AI validated {len(context['verified_places'])} websites"
            else:
                res.status = "skipped"
                res.detail = "No places to validate"

        elif name == "ai_business_type_normalize":
            from lf_ai_enrich import ai_normalize_business_type
            for p in context.get("verified_places", []):
                ai_out = ai_normalize_business_type(
                    raw_type=p.get("business_type", "") or p.get("primaryType", ""),
                    company_name=p.get("name", ""),
                )
                if ai_out and ai_out.get("canonical_type"):
                    p["canonical_business_type"] = ai_out["canonical_type"]
                    p["category"] = ai_out.get("category", "")
            res.status = "complete"
            res.detail = "Normalized business types"

        elif name == "ai_website_discovery":
            from lf_ai_enrich import ai_discover_company_website
            company = context.get("company", {})
            ai_out = ai_discover_company_website(
                company_name=company.get("name", ""),
                city=company.get("city", ""),
                state=company.get("state", ""),
                current_website=company.get("website", ""),
            )
            context["ai_website_discovery"] = ai_out
            if ai_out and ai_out.get("website"):
                # Only update if AI is more confident or current is missing
                if not company.get("website") or ai_out.get("is_real_website"):
                    company["website"] = ai_out["website"]
            res.status = "complete"
            res.detail = ai_out.get("reasoning", "AI website discovery complete") if ai_out else "AI unavailable"

        elif name == "website_leadership_scrape":
            from lf_executives import scrape_company_leadership
            company = context["company"]
            people, leadership_url = scrape_company_leadership(
                website=company.get("website", ""),
                company_name=company.get("name", ""),
            )
            context["scraped_people"] = people or []
            context["leadership_url"] = leadership_url
            res.status = "complete"
            res.detail = f"Found {len(people or [])} people on website"
            res.data = {"count": len(people or []), "leadership_url": leadership_url}

        elif name == "ai_contact_research":
            from lf_ai_enrich import ai_research_contact
            company = context["company"]
            researched = []
            for person in context.get("scraped_people", []):
                ai_out = ai_research_contact(
                    full_name=person.get("name", ""),
                    company_name=company.get("name", ""),
                    linkedin_url=person.get("linkedin_url", ""),
                    current_title=person.get("title", ""),
                )
                if ai_out:
                    # Merge AI research with scraped person data
                    person.update({
                        "title": ai_out.get("title") or person.get("title"),
                        "linkedin_url": ai_out.get("linkedin_url") or person.get("linkedin_url"),
                        "email": ai_out.get("email"),
                        "phone": ai_out.get("phone"),
                        "location": ai_out.get("location"),
                        "is_current_employee": ai_out.get("is_current_employee", True),
                        "ai_confidence": ai_out.get("confidence", 0),
                        "ai_reasoning": ai_out.get("reasoning", ""),
                        "ai_source": ai_out.get("source", "ai_primary"),
                    })
                    researched.append(person)
                else:
                    # AI failed, keep the scraped person as-is
                    researched.append(person)
            context["researched_contacts"] = researched
            res.status = "complete"
            res.detail = f"AI researched {len(researched)} contacts"
            res.data = {"count": len(researched)}

        elif name == "linkedin_url_fallback":
            # Only runs if ai_contact_research didn't fill linkedin_url
            from lf_executives import search_linkedin_verification
            fallback_count = 0
            for person in context.get("researched_contacts", []):
                if person.get("linkedin_url"):
                    continue  # AI already gave a LinkedIn URL
                linkedin_url = search_linkedin_verification(
                    name=person.get("name", ""),
                    company_name=context["company"].get("name", ""),
                )
                if linkedin_url:
                    person["linkedin_url"] = linkedin_url
                    person["ai_source"] = "searxng_fallback"
                    fallback_count += 1
            res.status = "complete"
            res.detail = f"LinkedIn URL fallback added for {fallback_count} contacts"
            res.data = {"count": fallback_count}

        elif name == "google_validate_person":
            from lf_executives import google_validate_person
            validated = []
            rejected = []
            for person in context.get("researched_contacts", []):
                is_valid, reason = google_validate_person(
                    name=person.get("name", ""),
                    company_name=context["company"].get("name", ""),
                )
                if is_valid:
                    validated.append(person)
                else:
                    rejected.append({"person": person, "reason": reason})
            context["validated_contacts"] = validated
            context["rejected_contacts"] = rejected
            res.status = "complete"
            res.detail = f"Google validation: {len(validated)} accepted, {len(rejected)} rejected"
            res.data = {"accepted": len(validated), "rejected": len(rejected)}

        elif name == "ai_sanity_check_company":
            from lf_ai_enrich import ai_sanity_check_company
            company = context.get("company", {})
            ai_out = ai_sanity_check_company(company, context=context.get("search_context", ""))
            context["ai_sanity_check"] = ai_out
            res.status = "complete"
            res.detail = ai_out.get("reasoning", "Sanity check complete") if ai_out else "AI unavailable"

        else:
            res.status = "failed"
            res.error = f"Unknown stage: {name}"

    except Exception as e:
        res.status = "failed"
        res.error = f"{type(e).__name__}: {e}"
        res.detail = traceback.format_exc(limit=2).replace("\n", " ")

    res.duration_ms = int((time.time() - t0) * 1000)
    return res


def run_chain(chain_name: str, context: dict, chain_def: list[dict]) -> PipelineResult:
    """Run a named chain through all its stages with explicit fallback logic.

    context: a dict that is mutated in place. Stages append to
    context['stage_log'] (list of dict) so the frontend can read it from
    the discovery_jobs row.
    """
    result = PipelineResult(chain=chain_name, started_at=_now_iso())
    context.setdefault("stage_log", [])
    # Track which primary stages succeeded so we know whether to skip
    # their fallbacks
    primary_success: dict[str, bool] = {}

    for stage_def in chain_def:
        stage_name = stage_def["stage"]
        fallback_for = stage_def.get("fallback_for")

        # If this is a fallback stage and the primary it falls back for
        # succeeded, skip it.
        if fallback_for and primary_success.get(fallback_for, False):
            skip_res = StageResult(
                stage=stage_name,
                status="skipped",
                detail=f"Skipped (primary '{fallback_for}' succeeded)",
            )
            result.stages.append(skip_res)
            context["stage_log"].append(skip_res.to_dict())
            continue

        # Run the stage
        stage_res = _run_stage(stage_def, context)
        result.stages.append(stage_res)
        context["stage_log"].append(stage_res.to_dict())

        if stage_def.get("primary") and stage_res.status == "complete":
            primary_success[stage_name] = True
        if stage_res.status == "failed":
            result.success = False

    result.ended_at = _now_iso()
    return result


# ── Convenience entry points ────────────────────────────────────────────
def search_companies_chain(
    industry: str,
    city: str,
    state: str = "CA",
    radius_miles: int = 25,
    max_results_per_query: int = 20,
    extra_queries: list = None,
) -> PipelineResult:
    """Run the full company search chain. Returns PipelineResult with the
    final `verified_places` available in the context (also returned via
    context dict from the run)."""
    context = {
        "industry": industry,
        "city": city,
        "state": state,
        "radius_miles": radius_miles,
        "max_results_per_query": max_results_per_query,
        "extra_queries": extra_queries,
    }
    return run_chain("company_search", context, CHAIN_COMPANY_SEARCH), context


def enrich_company_chain(company: dict, search_context: str = "") -> tuple[PipelineResult, dict]:
    """Run the full company enrichment chain for a single company record."""
    context = {
        "company": dict(company),  # copy so we can mutate
        "search_context": search_context,
    }
    return run_chain("company_enrich", context, CHAIN_COMPANY_ENRICH), context
