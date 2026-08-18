#!/usr/bin/env python3
"""
lf_stages.py - Pipeline Stage Constants, Model Routing, and Stage Helpers
=========================================================================

Centralizes the lead-finder pipeline stage vocabulary and model routing
so that every AI call can be tied to the right entity stage and the right
LLM. Introduced as part of the OWUI toolset refactor (QC-20, 2026-07-09).

Contact pipeline stages (in order):
  discovered -> enriched -> verified -> email_patterned -> validated -> exported

Company pipeline stages (in order):
  discovered -> enriched -> sanity_checked

Model routing (by stage + operation):
  - Agentic verification loop (stage=verify, operation=contact_research):
      deepseek-v4-flash:cloud primary; deepseek-v4-pro:cloud for refinement
  - Deep research operations (pattern_inference, website_discovery,
    business_type_normalize, company_sanity, company_search):
      deepseek-v4-pro:cloud primary
  - General / gap fill:
      cloud_model_primary (minimax-m3:cloud) -> fallback chain
"""

from typing import Optional

from lf_config import get

# ── Stage Constants ─────────────────────────────────────────────────────────

# Contact stages
CONTACT_STAGE_DISCOVERED = "discovered"
CONTACT_STAGE_ENRICHED = "enriched"
CONTACT_STAGE_VERIFIED = "verified"
CONTACT_STAGE_EMAIL_PATTERNED = "email_patterned"
CONTACT_STAGE_VALIDATED = "validated"
CONTACT_STAGE_EXPORTED = "exported"

CONTACT_STAGES = (
    CONTACT_STAGE_DISCOVERED,
    CONTACT_STAGE_ENRICHED,
    CONTACT_STAGE_VERIFIED,
    CONTACT_STAGE_EMAIL_PATTERNED,
    CONTACT_STAGE_VALIDATED,
    CONTACT_STAGE_EXPORTED,
)

# Company stages
COMPANY_STAGE_DISCOVERED = "discovered"
COMPANY_STAGE_ENRICHED = "enriched"
COMPANY_STAGE_SANITY_CHECKED = "sanity_checked"

COMPANY_STAGES = (
    COMPANY_STAGE_DISCOVERED,
    COMPANY_STAGE_ENRICHED,
    COMPANY_STAGE_SANITY_CHECKED,
)

# AI sanity status values (QC-23 reconciliation: ok | flagged | pending)
AI_SANITY_OK = "ok"
AI_SANITY_FLAGGED = "flagged"
AI_SANITY_PENDING = "pending"
AI_SANITY_STATUSES = (AI_SANITY_OK, AI_SANITY_FLAGGED, AI_SANITY_PENDING)

# Operations that need deep reasoning (deep research model primary)
DEEP_RESEARCH_OPERATIONS = {
    "pattern_inference",
    "website_discovery",
    "business_type_normalize",
    "company_sanity",
    "company_search",
    "contact_research",  # only when refinement/gaps >= 3, see resolve_model_chain
}

# ── Stage Validation / Transition ─────────────────────────────────────────

def is_valid_contact_stage(stage: Optional[str]) -> bool:
    return stage in CONTACT_STAGES


def is_valid_company_stage(stage: Optional[str]) -> bool:
    return stage in COMPANY_STAGES


def next_contact_stage(current: Optional[str]) -> str:
    """Return the next contact stage in the pipeline."""
    if not current or current not in CONTACT_STAGES:
        return CONTACT_STAGE_DISCOVERED
    idx = CONTACT_STAGES.index(current)
    if idx + 1 < len(CONTACT_STAGES):
        return CONTACT_STAGES[idx + 1]
    return CONTACT_STAGES[-1]


def next_company_stage(current: Optional[str]) -> str:
    """Return the next company stage in the pipeline."""
    if not current or current not in COMPANY_STAGES:
        return COMPANY_STAGE_DISCOVERED
    idx = COMPANY_STAGES.index(current)
    if idx + 1 < len(COMPANY_STAGES):
        return COMPANY_STAGES[idx + 1]
    return COMPANY_STAGES[-1]


def reconcile_ai_sanity_status(status: Optional[str]) -> str:
    """
    Normalize legacy ai_sanity_status values to the canonical set.
    Historical values: 'ok', 'flagged', 'issues', 'pending', None.
    Canonical values: 'ok', 'flagged', 'pending'.
    """
    if not status:
        return AI_SANITY_PENDING
    s = status.strip().lower()
    if s == AI_SANITY_OK:
        return AI_SANITY_OK
    if s in (AI_SANITY_FLAGGED, "issues", "problem", "warn"):
        return AI_SANITY_FLAGGED
    if s == AI_SANITY_PENDING:
        return AI_SANITY_PENDING
    # Unknown value: treat as flagged so it is visible for review
    return AI_SANITY_FLAGGED


# ── Model Routing ───────────────────────────────────────────────────────────

def cloud_model_primary() -> str:
    return get("ai_cloud_model_primary", "minimax-m3:cloud")


def cloud_model_fallback_1() -> str:
    return get("ai_cloud_model_fallback_1", "deepseek-v4-pro:cloud")


def cloud_model_fallback_2() -> str:
    return get("ai_cloud_model_fallback_2", "deepseek-v4-flash:cloud")


def agent_verify_model() -> str:
    return get("ai_agent_verify_model", cloud_model_fallback_2())


def agent_verify_refine_model() -> str:
    return get("ai_agent_verify_refine_model", cloud_model_fallback_1())


def cloud_model_chain() -> list[str]:
    """Default fallback chain."""
    return [cloud_model_primary(), cloud_model_fallback_1(), cloud_model_fallback_2()]


def resolve_model_chain(operation: str = "general",
                        stage: Optional[str] = None,
                        gap_count: int = 0) -> list[str]:
    """
    Return the ordered list of models to try for a given operation+stage.

    Args:
        operation: the AI operation type (e.g. 'contact_research', 'ranking',
                   'pattern_inference', 'gap_fill').
        stage: the pipeline stage this call is acting in (e.g. 'verify').
        gap_count: number of missing fields; used to decide refinement tier.

    Returns:
        A list of model names to try in order.
    """
    # Agentic verification loop (QC-19): Flash primary, Pro for refinement
    if operation == "contact_research" and stage in ("verify", "agentic", "verification"):
        if gap_count >= 3:
            return [agent_verify_refine_model(), agent_verify_model(), cloud_model_primary()]
        return [agent_verify_model(), agent_verify_refine_model(), cloud_model_primary()]

    if operation in ("ranking", "profile_rank", "sanity_check"):
        return [agent_verify_refine_model(), cloud_model_primary(), cloud_model_fallback_2()]

    # Deep research operations: Pro primary for multi-hop reasoning
    if operation in {"website_discovery", "business_type_normalize",
                     "company_sanity", "company_search", "deep_research"}:
        return [cloud_model_fallback_1(), cloud_model_fallback_2(), cloud_model_primary()]

    # Fast pattern inference: Flash / Minimax first. The SMTP proof gate is the
    # ultimate hallucination guard, so speed is preferred over reasoning depth.
    if operation == "pattern_inference_fast":
        return [cloud_model_fallback_2(), cloud_model_primary(), cloud_model_fallback_1()]

    # Pattern inference default: still Pro first for cases where search evidence is
    # provided and deeper reasoning helps, but fall back fast.
    if operation == "pattern_inference":
        return [cloud_model_fallback_1(), cloud_model_fallback_2(), cloud_model_primary()]

    # Default / general / gap_fill: primary model first
    return cloud_model_chain()
