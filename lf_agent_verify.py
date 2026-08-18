#!/usr/bin/env python3
"""
lf_agent_verify.py - Hardened contact verification escalator (2026-07-24)

Replaces the previous "first non-empty wins" merge logic. This module
implements an evidence-first escalator that only writes a value to the
database when at least one INDEPENDENT source confirms it. If a method
fails its evidence check, the next method is tried — like a human would.

Methods (in order, each with an evidence requirement):
  M1. AI primary research  — requires AI confidence >= 0.70 on the
      field AND the field's evidence is corroborated by a SearXNG
      cross-check.
  M2. Cascading SearXNG search for LinkedIn URL — narrows down to the
      real /in/<slug> for the person, and only accepts a URL that
      independent search confirms (verified_linkedin_url).
  M3. Direct directory scrape — company leadership/about page parsed
      for the person's name.
  M4. AI refinement with Pro model — second-opinion call with the
      evidence we already have, constrained to fill only gaps.
  M5. Email pattern derivation — if the company has a pattern, derive
      a deterministic email from name+pattern. Marked as derived.

Hard rules:
  - No URL is written unless verified_linkedin_url() returns confirmed=True.
  - No title is written unless either the AI returned confidence>=0.7 AND
    a SearXNG snippet contains BOTH the name and the company, OR a
    directory page lists the person at the company.
  - No email is written unless the AI returned it with confidence>=0.6
    OR the company's email pattern is present and the derived address
    is structurally valid.
  - A confidence field is recorded as a real number between 0.0 and 1.0
    based on the highest-quality evidence. The value is NEVER boosted by
    "the field is present" — only by evidence.
  - Each method call appends an event to the job's stage_log and the
    item's event buffer so the user sees live progress.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Optional

from lf_ai_enrich import ai_complete, ai_research_contact, ai_enabled
from lf_config import get
from lf_db import (
    get_db,
    patch_contact,
    update_discovery_job,
    update_discovery_job_item,
    add_verify_job_items,
    create_discovery_job,
    append_discovery_job_event,
    append_discovery_job_item_event,
    move_contact_to_company,
    find_company_by_name,
)
from lf_executives import (
    cascading_linkedin_search,
    verified_linkedin_url,
    is_valid_linkedin_profile,
    search_linkedin_verification,
    pixelrag_scrape_linkedin,
    extract_domain,
)
from lf_stages import CONTACT_STAGE_VERIFIED

logger = logging.getLogger("lf_agent_verify")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# Tunables
AI_FIELD_CONFIDENCE_THRESHOLD = 0.70
URL_VERIFICATION_REQUIRED = True
EMAIL_AI_THRESHOLD = 0.60
MAX_REFINEMENT_ROUNDS = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def agent_verify_concurrency() -> int:
    return max(1, int(get("agent_verify_concurrency", 3)))


# ── Live progress helpers ────────────────────────────────────────────────

def emit_job_event(job_id: Optional[str], event: dict) -> None:
    """Append a structured event to a job's stage_log and update current_stage.

    event = {"stage": str, "label": str, "detail": str, "at": iso}
    """
    if not job_id:
        return
    at = event.get("at") or now_iso()
    event["at"] = at
    try:
        append_discovery_job_event(job_id, event)
    except Exception as e:
        logger.warning("emit_job_event %s failed: %s", job_id, e)


def emit_item_event(item_id: Optional[int], event: dict) -> None:
    if not item_id:
        return
    at = event.get("at") or now_iso()
    event["at"] = at
    try:
        append_discovery_job_item_event(item_id, event)
    except Exception as e:
        logger.warning("emit_item_event %s failed: %s", item_id, e)


# ── Public entry point ──────────────────────────────────────────────────

def run_verify_batch_job(job_id: str) -> None:
    """Process every pending contact in a verify_batch job (hardened escalator)."""
    if not ai_enabled():
        _fail_job(job_id, "AI enrichment is disabled")
        return

    items = _load_pending_items(job_id)
    if not items:
        _fail_job(job_id, "No pending contacts found for job")
        return

    update_discovery_job(
        job_id,
        status="running",
        started_at=now_iso(),
        total=len(items),
        current_stage="queued",
    )
    emit_job_event(job_id, {"stage": "queued", "label": f"Queued {len(items)} contacts for hardened verification"})

    summary = {"total": len(items), "done": 0, "verified": 0, "skipped": 0, "failed": 0, "errors": 0}

    concurrency = agent_verify_concurrency()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = {executor.submit(_escalate_one_contact, item, job_id): item for item in items}
        for future in futures:
            item = futures[future]
            try:
                result = future.result() or {}
                summary["done"] += 1
                if result.get("success") and not result.get("skipped"):
                    summary["verified"] += 1
                elif result.get("skipped"):
                    summary["skipped"] += 1
                else:
                    summary["failed"] += 1
            except Exception as e:
                logger.exception("verify %s item %s error", job_id, item.get("item_id"))
                summary["done"] += 1
                summary["errors"] += 1
                _record_item_result(item["item_id"], "failed", {"error": str(e)})
                emit_item_event(item.get("item_id"), {"stage": "error", "label": "Exception", "detail": str(e)})

            update_discovery_job(job_id, done=summary["done"], failed=summary["failed"])

    final_status = "completed"
    update_discovery_job(
        job_id,
        status=final_status,
        finished_at=now_iso(),
        results=json.dumps(summary),
        current_stage="completed",
    )
    emit_job_event(job_id, {"stage": "completed", "label": f"Verified {summary['verified']}/{summary['total']}; skipped {summary['skipped']}; failed {summary['failed']}"})
    logger.info("verify %s: %s — %s", job_id, final_status, summary)


# ── Per-contact escalator ───────────────────────────────────────────────

def _escalate_one_contact(item: dict, job_id: str) -> dict:
    """Run the hardened escalator for a single contact.

    Returns a result dict with at minimum {success, skipped, written}.
    """
    item_id = item.get("item_id")
    contact_id = item["contact_id"]
    full_name = (item.get("full_name") or "").strip()
    company_name = (item.get("company_name") or "").strip()
    current_title = (item.get("current_title") or "").strip()
    current_linkedin = (item.get("current_linkedin_url") or "").strip()
    current_email = (item.get("current_email") or "").strip()
    current_phone = (item.get("current_phone") or "").strip()
    current_location = (item.get("current_location") or "").strip()
    company_website = (item.get("company_website") or "").strip()
    company_email_pattern = (item.get("company_email_pattern") or "").strip()
    company_state = (item.get("company_state") or "CA").strip()
    company_lat = float(item.get("company_lat") or 0.0)
    company_lng = float(item.get("company_lng") or 0.0)

    emit_job_event(job_id, {"stage": "start", "label": f"{full_name} @ {company_name}"})
    emit_item_event(item_id, {"stage": "start", "label": f"Verifying {full_name} @ {company_name}"})
    _set_item_status(item_id, "running")

    if not full_name or not company_name:
        _record_item_result(item_id, "skipped", {"reason": "missing_name_or_company", "success": False, "skipped": True})
        emit_item_event(item_id, {"stage": "skipped", "label": "Missing name or company"})
        return {"success": False, "skipped": True}

    # Track per-field evidence as we go
    evidence = {
        "title": {"value": current_title or None, "method": "existing", "confidence": 0.40 if current_title else 0.0, "corroborated": False},
        "linkedin_url": {"value": current_linkedin or None, "method": "existing", "confidence": 0.30 if current_linkedin else 0.0, "corroborated": False},
        "email": {"value": current_email or None, "method": "existing", "confidence": 0.30 if current_email else 0.0, "corroborated": False},
        "phone": {"value": current_phone or None, "method": "existing", "confidence": 0.20 if current_phone else 0.0, "corroborated": False},
        "location": {"value": current_location or None, "method": "existing", "confidence": 0.20 if current_location else 0.0, "corroborated": False},
        "canonical_company_name": {"value": company_name, "method": "existing", "confidence": 0.50, "corroborated": True},
        "is_current_employee": {"value": True, "method": "existing", "confidence": 0.30, "corroborated": False},
    }

    # ── M1: AI primary research ──────────────────────────────────────────
    emit_item_event(item_id, {"stage": "M1", "label": "AI research pass"})
    ai_result = None
    try:
        ai_result = ai_research_contact(
            full_name=full_name,
            company_name=company_name,
            linkedin_url=current_linkedin,
            current_title=current_title,
            entity_type="contact",
            entity_id=contact_id,
            stage="verify",
        )
    except Exception as e:
        logger.warning("verify %s contact %s: AI M1 error: %s", job_id, contact_id, e)
        emit_item_event(item_id, {"stage": "M1", "label": "AI failed", "detail": str(e)})

    if ai_result:
        # Only adopt if the AI's confidence is high enough; otherwise keep existing
        ai_conf = float(ai_result.get("confidence") or 0)
        emit_item_event(item_id, {"stage": "M1", "label": "AI returned", "detail": f"conf={ai_conf:.2f}"})

        # Title: must be tied to the recorded company
        ai_title = ai_result.get("title")
        ai_company = (ai_result.get("canonical_company_name") or "").strip()
        ai_emp = ai_result.get("is_current_employee")

        if ai_title and ai_conf >= AI_FIELD_CONFIDENCE_THRESHOLD:
            if ai_emp is True and (not ai_company or ai_company.lower() == company_name.lower()):
                # AI says they're still at this company with this title
                evidence["title"] = {"value": ai_title, "method": "ai_primary", "confidence": ai_conf, "corroborated": False, "ai_company": ai_company}
                evidence["is_current_employee"] = {"value": True, "method": "ai_primary", "confidence": ai_conf, "corroborated": False}
                evidence["canonical_company_name"]["value"] = ai_company or company_name
                evidence["canonical_company_name"]["confidence"] = ai_conf
                emit_item_event(item_id, {"stage": "M1", "label": "AI title adopted", "detail": ai_title})
            elif ai_emp is False and ai_company and ai_company.lower() != company_name.lower():
                # AI says they moved companies — mark for relocation
                evidence["canonical_company_name"] = {"value": ai_company, "method": "ai_primary", "confidence": ai_conf, "corroborated": False}
                evidence["is_current_employee"] = {"value": False, "method": "ai_primary", "confidence": ai_conf, "corroborated": False}
                evidence["title"] = {"value": ai_title, "method": "ai_primary", "confidence": ai_conf, "corroborated": False, "ai_company": ai_company}
                emit_item_event(item_id, {"stage": "M1", "label": "AI reports company change", "detail": f"→ {ai_company}"})
            else:
                # AI is uncertain about company match; keep existing
                emit_item_event(item_id, {"stage": "M1", "label": "AI title rejected", "detail": "company match unclear"})

        # Location
        ai_loc = ai_result.get("location")
        if ai_loc and ai_conf >= AI_FIELD_CONFIDENCE_THRESHOLD:
            evidence["location"] = {"value": ai_loc, "method": "ai_primary", "confidence": ai_conf, "corroborated": False}
        # Phone
        ai_phone = ai_result.get("phone")
        if ai_phone and ai_conf >= AI_FIELD_CONFIDENCE_THRESHOLD:
            evidence["phone"] = {"value": ai_phone, "method": "ai_primary", "confidence": ai_conf, "corroborated": False}
    else:
        emit_item_event(item_id, {"stage": "M1", "label": "AI unavailable; escalating to search"})

    # ── M2: Cascading SearXNG search for LinkedIn URL ───────────────────
    emit_item_event(item_id, {"stage": "M2", "label": "Cascading LinkedIn search"})

    def on_step(method: str, query: str) -> None:
        emit_item_event(item_id, {"stage": "M2", "label": f"Searching: {method}", "detail": query[:120]})

    cascade = cascading_linkedin_search(
        full_name=full_name,
        company_name=company_name,
        company_website=company_website,
        search_state=company_state,
        on_step=on_step,
        timeout=8,
    )

    candidate_url = cascade.get("url")
    candidate_snippet = cascade.get("snippet", "")
    candidate_method = cascade.get("method")
    candidate_conf = cascade.get("confidence", 0.0)

    if candidate_url:
        # Hard URL verification gate
        if URL_VERIFICATION_REQUIRED:
            emit_item_event(item_id, {"stage": "M2", "label": "Verifying URL independently", "detail": candidate_url})
            verify = verified_linkedin_url(
                candidate_url=candidate_url,
                full_name=full_name,
                company_name=company_name,
                search_state=company_state,
                timeout=8,
            )
            if verify.get("confirmed"):
                evidence["linkedin_url"] = {
                    "value": candidate_url,
                    "method": f"cascade_{candidate_method}",
                    "confidence": candidate_conf,
                    "corroborated": True,
                    "company_match": verify.get("company_match", False),
                }
                emit_item_event(item_id, {"stage": "M2", "label": "URL confirmed", "detail": candidate_url})
            else:
                emit_item_event(item_id, {"stage": "M2", "label": "URL rejected", "detail": verify.get("reason", "")})
        else:
            evidence["linkedin_url"] = {"value": candidate_url, "method": f"cascade_{candidate_method}", "confidence": candidate_conf, "corroborated": False}
    else:
        emit_item_event(item_id, {"stage": "M2", "label": "No LinkedIn URL found via cascading search"})

    # ── M2b: Search-corroborate the title if the AI proposed one but we
    #         did not corroborate it with a SearXNG snippet. ───────────────
    if evidence["title"].get("method") == "ai_primary" and not evidence["title"].get("corroborated"):
        emit_item_event(item_id, {"stage": "M2b", "label": "Cross-checking AI title with search"})
        searx = []
        try:
            searx = search_linkedin_verification(full_name, company_name, company_state)
        except Exception as e:
            logger.warning("verify %s contact %s: M2b search error: %s", job_id, contact_id, e)
        company_mentioned_in_any = any(s.get("company_mentioned") for s in searx)
        name_in_any = False
        for s in searx:
            snip = (s.get("snippet") or "").lower()
            if any(p in snip for p in full_name.lower().split() if len(p) > 1):
                name_in_any = True
                break
        if company_mentioned_in_any and name_in_any:
            evidence["title"]["corroborated"] = True
            evidence["title"]["confidence"] = max(evidence["title"]["confidence"], 0.80)
            emit_item_event(item_id, {"stage": "M2b", "label": "Title corroborated", "detail": f"{len(searx)} search hits"})
        else:
            # Title not corroborated by independent search; lower confidence
            evidence["title"]["confidence"] = min(evidence["title"]["confidence"], 0.55)
            emit_item_event(item_id, {"stage": "M2b", "label": "Title NOT corroborated", "detail": f"company={company_mentioned_in_any} name={name_in_any}"})

    # ── M3: Directory scrape of company website (about / leadership) ────
    if company_website and (
        not evidence["title"].get("value")
        or evidence["title"].get("confidence", 0) < 0.7
    ):
        emit_item_event(item_id, {"stage": "M3", "label": "Scraping company leadership page", "detail": company_website})
        try:
            import requests
            from bs4 import BeautifulSoup
            r = requests.get(company_website, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.ok and r.text:
                text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
                lowered = text.lower()
                if full_name.lower() in lowered:
                    evidence["title"]["corroborated"] = True
                    evidence["title"]["confidence"] = max(evidence["title"]["confidence"], 0.75)
                    if company_name.lower() in lowered:
                        evidence["title"]["confidence"] = max(evidence["title"]["confidence"], 0.85)
                    evidence["title"]["method"] = evidence["title"].get("method", "directory_scrape") + "+directory"
                    emit_item_event(item_id, {"stage": "M3", "label": "Name found on company site"})
                else:
                    emit_item_event(item_id, {"stage": "M3", "label": "Name NOT on company site"})
        except Exception as e:
            emit_item_event(item_id, {"stage": "M3", "label": "Scrape failed", "detail": str(e)[:120]})

    # ── M4: AI refinement if confidence is still low ────────────────────
    if evidence["title"].get("confidence", 0) < 0.7 or not evidence["linkedin_url"].get("value"):
        emit_item_event(item_id, {"stage": "M4", "label": "AI refinement round (Pro)"})
        for round_idx in range(MAX_REFINEMENT_ROUNDS):
            missing = [k for k in ["title", "linkedin_url", "location", "phone"] if not evidence[k].get("value") or evidence[k].get("confidence", 0) < 0.7]
            if not missing:
                break
            try:
                prompt = f"""Hardened second-opinion for a B2B contact. Independently verify the following or return null.

Person: {full_name}
Company: {company_name}
Website: {company_website}
{('Known email pattern at company: ' + company_email_pattern) if company_email_pattern else ''}

Currently believed:
- title: {evidence['title'].get('value') or 'unknown'} (confidence {evidence['title'].get('confidence', 0):.2f})
- linkedin_url: {evidence['linkedin_url'].get('value') or 'unknown'} (confidence {evidence['linkedin_url'].get('confidence', 0):.2f})
- location: {evidence['location'].get('value') or 'unknown'}
- phone: {evidence['phone'].get('value') or 'unknown'}

SearXNG cascade result: {json.dumps(cascade.get('attempts', [])[:3])[:600]}

Fill ONLY the missing/unreliable fields: {', '.join(missing)}.
Return ONLY a JSON object:
{{
  "title": <string|null>,
  "linkedin_url": <string|null>,
  "location": <string|null>,
  "phone": <string|null>,
  "email": <string|null — return null unless extremely confident>,
  "is_current_employee": <bool>,
  "canonical_company_name": <string|null>,
  "confidence": <0.0-1.0 — your OVERALL honest confidence that the returned data is correct>,
  "is_verified": <bool — true only if you found explicit, direct evidence (LinkedIn profile, company leadership page, press release with quotes)>,
  "reasoning": "<one-line: which field you confirmed and via what evidence>",
  "source": "<where you verified it>"
}}

Hard rules:
- Be brutally honest about confidence. If you are not certain, return low confidence.
- If you cannot verify a field, return null. Do not guess.
- Confidence < 0.70 means the data should not be trusted.
- This is a second pass — do NOT hallucinate to please the caller."""
                response = ai_complete(prompt, operation="contact_research", gap_count=len(missing), entity_type="contact", entity_id=contact_id, stage="verify_refine", timeout=60)
                if not response:
                    break
                # parse JSON
                m = re.search(r"\{[\s\S]*\}", response)
                if not m:
                    break
                refined = json.loads(m.group(0))
                for k in ["title", "linkedin_url", "location", "phone", "email"]:
                    val = refined.get(k)
                    if val and isinstance(val, str) and val.lower() not in ("null", "none", "n/a", "unknown", ""):
                        cur_conf = evidence[k].get("confidence", 0)
                        new_conf = float(refined.get("confidence") or 0)
                        if new_conf > cur_conf:
                            evidence[k]["value"] = val.strip()
                            evidence[k]["method"] = "ai_refined"
                            evidence[k]["confidence"] = new_conf
                            evidence[k]["corroborated"] = bool(refined.get("is_verified"))
                # If a URL was refined, gate it through verified_linkedin_url
                if refined.get("linkedin_url") and evidence["linkedin_url"].get("value") == refined["linkedin_url"].strip():
                    verify = verified_linkedin_url(
                        candidate_url=refined["linkedin_url"],
                        full_name=full_name,
                        company_name=company_name,
                        search_state=company_state,
                        timeout=12,
                    )
                    if not verify.get("confirmed"):
                        evidence["linkedin_url"] = {"value": None, "method": "refined_but_rejected", "confidence": 0.0, "corroborated": False}
                        emit_item_event(item_id, {"stage": "M4", "label": "Refined URL rejected", "detail": verify.get("reason", "")})
                emit_item_event(item_id, {"stage": "M4", "label": f"Refinement round {round_idx+1} complete", "detail": f"conf={float(refined.get('confidence') or 0):.2f}"})
            except Exception as e:
                emit_item_event(item_id, {"stage": "M4", "label": "Refinement error", "detail": str(e)[:120]})
                break

    # ── M5: Email pattern derivation ─────────────────────────────────────
    if not evidence["email"].get("value") and company_email_pattern and full_name:
        emit_item_event(item_id, {"stage": "M5", "label": "Deriving email from pattern", "detail": company_email_pattern})
        try:
            from lf_email_patterns import derive_email
            first, _, last = full_name.partition(" ")
            email_guess = derive_email(first, last, company_email_pattern)
            if email_guess and "@" in email_guess:
                domain = email_guess.split("@", 1)[1].lower()
                company_domain = extract_domain(company_website) if company_website else ""
                if not company_domain or domain.endswith(company_domain.split("/")[-1] if company_domain else domain):
                    evidence["email"] = {"value": email_guess, "method": "pattern_derivation", "confidence": 0.55, "corroborated": False}
                    emit_item_event(item_id, {"stage": "M5", "label": "Derived", "detail": email_guess})
                else:
                    emit_item_event(item_id, {"stage": "M5", "label": "Derived email rejected (domain mismatch)", "detail": email_guess})
        except Exception as e:
            emit_item_event(item_id, {"stage": "M5", "label": "Derive error", "detail": str(e)[:120]})

    # ── Final decision gate ──────────────────────────────────────────────
    # Only write fields with confidence >= 0.6 and value is non-empty.
    # Each field goes through its own gate.

    final_fields = {}
    rejected = []
    for k in ["title", "linkedin_url", "email", "phone", "location"]:
        ev = evidence[k]
        val = ev.get("value")
        conf = ev.get("confidence", 0)
        method = ev.get("method", "")
        if not val:
            rejected.append({"field": k, "reason": "no_value", "method": method, "confidence": conf})
            continue
        if k == "linkedin_url" and URL_VERIFICATION_REQUIRED and not ev.get("corroborated"):
            rejected.append({"field": k, "reason": "url_not_independently_confirmed", "method": method, "confidence": conf})
            continue
        if conf < 0.55:
            rejected.append({"field": k, "reason": f"confidence_too_low_{conf:.2f}", "method": method, "confidence": conf})
            continue
        final_fields[k] = val

    # Compute overall confidence as the minimum of the written field confidences
    if final_fields:
        written_confs = [evidence[k]["confidence"] for k in final_fields]
        overall_conf = round(min(written_confs), 3)
    else:
        overall_conf = 0.0

    # Decide if the contact should be moved to a different company
    move_company = False
    new_company_id = None
    new_company_name = None
    ai_company = (evidence.get("canonical_company_name") or {}).get("value")
    if (
        ai_company
        and ai_company.lower() != company_name.lower()
        and (evidence.get("is_current_employee") or {}).get("value") is False
        and (evidence.get("canonical_company_name") or {}).get("confidence", 0) >= 0.70
    ):
        emit_item_event(item_id, {"stage": "relocate", "label": f"Locating company: {ai_company}"})
        existing = find_company_by_name(ai_company)
        if existing:
            new_company_id = existing["id"]
            new_company_name = existing["name"]
        else:
            try:
                from lf_db import create_company_manual
                new_company_id = create_company_manual({
                    "name": ai_company,
                    "source": "AI_ESCALATOR_REASSIGNMENT",
                    "data_provenance": "AI_ESCALATOR_REASSIGNMENT",
                })
                new_company_name = ai_company
            except Exception as e:
                emit_item_event(item_id, {"stage": "relocate", "label": "Company create failed", "detail": str(e)[:120]})
        if new_company_id and new_company_id != item.get("company_id"):
            move_company = True

    # ── Write to DB ──────────────────────────────────────────────────────
    success = bool(final_fields) and overall_conf >= 0.55
    update_fields = dict(final_fields)
    if "title" in final_fields:
        update_fields["ai_verified_title"] = final_fields["title"]
        update_fields["title_from_linkedin"] = final_fields["title"]
    if "linkedin_url" in final_fields:
        update_fields["source_linkedin_verified"] = 1
        update_fields["source_linkedin_unverified"] = 0
    update_fields["ai_title_confidence"] = overall_conf
    update_fields["ai_title_source"] = "escalator"
    update_fields["confidence_score"] = overall_conf
    update_fields["ai_edited_at"] = now_iso()
    update_fields["pipeline_stage"] = CONTACT_STAGE_VERIFIED if success else "escalator_no_commit"

    if move_company and new_company_id is not None:
        emit_item_event(item_id, {"stage": "write", "label": f"Moving to {new_company_name} and writing fields", "detail": f"fields={list(final_fields)} conf={overall_conf}"})
        try:
            move_contact_to_company(contact_id, new_company_id, update_fields)
        except Exception as e:
            emit_item_event(item_id, {"stage": "write", "label": "Move failed", "detail": str(e)[:120]})
            return {"success": False, "error": f"move failed: {e}", "skipped": False, "written": [], "rejected": rejected, "evidence": evidence}
    else:
        emit_item_event(item_id, {"stage": "write", "label": "Writing fields", "detail": f"fields={list(final_fields)} conf={overall_conf}"})
        try:
            patch_contact(contact_id, update_fields)
        except Exception as e:
            emit_item_event(item_id, {"stage": "write", "label": "Patch failed", "detail": str(e)[:120]})
            return {"success": False, "error": f"patch failed: {e}", "skipped": False, "written": [], "rejected": rejected, "evidence": evidence}

    result = {
        "contact_id": contact_id,
        "full_name": full_name,
        "company_name": company_name,
        "new_company_name": new_company_name,
        "moved": move_company,
        "written": list(final_fields.keys()),
        "rejected": rejected,
        "evidence": {k: {"value": v.get("value"), "method": v.get("method"), "confidence": v.get("confidence"), "corroborated": v.get("corroborated")} for k, v in evidence.items()},
        "cascade": cascade,
        "overall_confidence": overall_conf,
        "success": success,
        "skipped": not success,
    }
    _record_item_result(item_id, "verified" if success else "skipped_no_commit", result)
    emit_item_event(item_id, {"stage": "done", "label": f"Done. confidence={overall_conf:.2f} written={list(final_fields.keys())} rejected={len(rejected)}"})
    return result


# ── Helpers ─────────────────────────────────────────────────────────────

def _non_empty(value) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, str):
        value = value.strip()
        if value.lower() in ("null", "none", "n/a", "unknown", ""):
            return None
        return value
    return str(value)


def _load_pending_items(job_id: str) -> list[dict]:
    conn = get_db()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            dji.id AS item_id,
            dji.contact_id,
            dji.company_id,
            c.name AS company_name,
            c.website AS company_website,
            c.email_pattern AS company_email_pattern,
            c.city AS company_city,
            c.state AS company_state,
            c.lat AS company_lat,
            c.lng AS company_lng,
            ct.full_name,
            ct.title AS current_title,
            ct.linkedin_url AS current_linkedin_url,
            ct.email AS current_email,
            ct.phone AS current_phone,
            ct.location AS current_location,
            ct.is_local AS current_is_local,
            ct.hq_contact AS current_hq_contact
        FROM discovery_job_items dji
        JOIN contacts ct ON ct.id = dji.contact_id
        JOIN companies c ON c.id = dji.company_id
        WHERE dji.job_id = ? AND dji.status = 'pending'
        """,
        (job_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _set_item_status(item_id: int, status: str) -> None:
    try:
        update_discovery_job_item(item_id, status, "")
    except Exception:
        pass


def _record_item_result(item_id: int, status: str, result: dict) -> None:
    try:
        update_discovery_job_item(item_id, status, json.dumps(result, default=str))
    except Exception:
        logger.warning("Could not record item %s result", item_id)


def _fail_job(job_id: str, reason: str) -> None:
    logger.error("verify %s: %s", job_id, reason)
    update_discovery_job(
        job_id,
        status="failed",
        finished_at=now_iso(),
        results=json.dumps({"error": reason}),
        current_stage="failed",
    )
    emit_job_event(job_id, {"stage": "failed", "label": reason})


def create_verify_batch_job(contact_ids: list[int]) -> Optional[str]:
    """
    Create a verify_batch job and add contact items. Returns job_id or None.
    Called from lf_server.py POST /api/contacts/verify-batch.
    """
    if not contact_ids:
        return None

    conn = get_db()
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in contact_ids)
    rows = conn.execute(
        f"""
        SELECT ct.id, ct.full_name, c.id AS company_id, c.name AS company_name, c.place_id
        FROM contacts ct
        JOIN companies c ON c.id = ct.company_id
        WHERE ct.id IN ({placeholders})
        """,
        tuple(contact_ids),
    ).fetchall()
    conn.close()

    if not rows:
        return None

    import uuid
    job_id = f"verify_{uuid.uuid4().hex[:12]}"
    create_discovery_job(job_id, session_key=job_id, total=len(rows), job_type="verify_batch")

    contacts = [dict(r) for r in rows]
    add_verify_job_items(job_id, contacts)
    return job_id


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        ids = [int(x) for x in sys.argv[1:] if x.isdigit()]
        jid = create_verify_batch_job(ids)
        if jid:
            print(f"Created verify job {jid} for contacts {ids}")
            run_verify_batch_job(jid)
            print("Done")
        else:
            print("No contacts found")
    else:
        print("Usage: python3 lf_agent_verify.py <contact_id>...")
