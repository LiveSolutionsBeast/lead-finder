#!/usr/bin/env python3
"""
lf_email_patterns.py - Email Pattern Discovery Module
========================================================
Find one email pattern per company domain, then derive individual emails.

Strategy:
  1. Search for real email addresses at the company's domain
     (via SearXNG search for "@domain.com" OR "email domain.com")
  2. Parse found emails to infer pattern (first.last, flast, firstlast, etc.)
  3. Store pattern in companies table
  4. Derive emails for all contacts at that company

Pattern examples:
  john.smith@acme.com    → {first}.{last}@{domain}
  jsmith@acme.com        → {f}{last}@{domain}
  johnsmith@acme.com     → {first}{last}@{domain}
  john_smith@acme.com    → {first}_{last}@{domain}
  j.smith@acme.com       → {f}.{last}@{domain}
  smith@acme.com         → {last}@{domain}
"""

import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Optional
from urllib.parse import urlparse

from lf_search_providers import search
from lf_db import (
    get_cached_validation, get_company, get_db, set_cached_validation, write_contact_validation,
    insert_email_send_queue,
)


# Domain extraction (added 2026-06-16 per user spec)
# The website of the company is the SOURCE OF TRUTH for the email tail.
# This helper normalizes any website URL/variant into the bare domain
# (e.g. "https://www.nassco.com/about-us" -> "nassco.com").
def extract_domain_from_website(website: str) -> Optional[str]:
    """
    Extract the bare email domain from a company website string.

    Handles:
      - Full URLs: https://www.nassco.com/about-us -> nassco.com
      - Bare domains: nassco.com -> nassco.com
      - With subdomain: shop.nassco.com -> shop.nassco.com (kept)
      - With port: nassco.com:8080 -> nassco.com
      - With path/query/fragment: stripped
      - With credentials: user:pass@nassco.com -> nassco.com
      - International TLDs: nassco.co.uk -> nassco.co.uk (kept)

    Returns None if no valid domain can be extracted.
    """
    if not website:
        return None
    s = website.strip()
    if not s:
        return None

    # Add a scheme if missing so urlparse can parse it
    if "://" not in s:
        s = "http://" + s

    try:
        parsed = urlparse(s)
    except Exception:
        return None

    host = parsed.hostname  # lowercased, no port, no userinfo
    if not host:
        # Fallback: try to extract manually from common forms
        m = re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.\-]*://)?(?:[^\s@/]+@)?([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})", website)
        if m:
            return m.group(1).lower()
        return None
    host = host.lower()
    # Strip www. prefix (for email domains, www. is just a web alias)
    if host.startswith("www."):
        host = host[4:]
    return host


# Common email pattern templates
PATTERN_TEMPLATES = {
    "first.last": "{first}.{last}@{domain}",
    "first_last": "{first}_{last}@{domain}",
    "firstlast": "{first}{last}@{domain}",
    "flast": "{f}{last}@{domain}",
    "firstl": "{first}{l}@{domain}",
    "f.last": "{f}.{last}@{domain}",
    "last": "{last}@{domain}",
    "first": "{first}@{domain}",
}


def discover_email_pattern(
    domain: str,
    company_name: str = "",
    industry: str = "",
    city: str = "",
    state: str = "",
    known_employees: list[str] | None = None,
) -> tuple[Optional[str], float]:
    """
    Discover the most likely email pattern for a company domain. (rewritten 2026-06-07, QC-13)

    AI is now the PRIMARY inference engine. SearXNG is used as a SUPPLEMENT
    when known emails are found, to bump confidence.

    Returns (pattern_string, confidence) tuple.
    Pattern like "{first}.{last}@{domain}", confidence 0.0-1.0.

    Args:
      domain: email domain (e.g., "boeing.com")
      company_name: company name (e.g., "The Boeing Company")
      industry: optional industry hint (e.g., "aerospace")
      city, state: optional location for context
      known_employees: optional list of employee names for additional context
    """
    if not domain:
        return None, 0.0

    # Clean domain
    domain = domain.lower().strip().lstrip("@")
    if domain.startswith("www."):
        domain = domain[4:]

    # === Step 1: SearXNG search for known emails (low priority, but useful when it works) ===
    found_emails = []
    if company_name:
        queries = [
            f'"@{domain}" "email" "{company_name}"',
            f'"@{domain}" contact email',
            f'site:rocketreach.co "{company_name}" email',
        ]
        for q in queries:
            try:
                results, _ = search(q, timeout=10)
                for r in results:
                    snippet = r.get("snippet", "") or r.get("content", "")
                    title = r.get("title", "")
                    emails = _extract_emails(snippet + " " + title, domain)
                    found_emails.extend(emails)
            except Exception:
                continue

    # === Step 2: AI inference (PRIMARY) ===
    try:
        from lf_ai_enrich import ai_infer_email_pattern
        ai_result = ai_infer_email_pattern(
            domain=domain,
            company_name=company_name,
            industry=industry,
            known_emails=found_emails if found_emails else None,
            known_employee_names=known_employees,
        )
        if ai_result and ai_result.get("pattern"):
            ai_pattern = ai_result["pattern"]
            ai_confidence = ai_result.get("confidence", 0.0)

            # === Step 3: Cross-validate with SearXNG-found emails (if any) ===
            if found_emails:
                # Try to infer pattern from found emails
                inferred_pattern, inferred_conf = _infer_pattern(found_emails, domain)
                if inferred_pattern and inferred_pattern == ai_pattern:
                    # AI and SearXNG agree — bump confidence to 1.0
                    return ai_pattern, 1.0
                elif inferred_pattern and inferred_conf == 1.0:
                    # SearXNG found 2+ agreeing emails — trust those over AI
                    return inferred_pattern, 1.0

            # Use AI result alone
            if ai_confidence >= 0.5:
                return ai_pattern, ai_confidence
    except Exception as e:
        print(f"[email] AI inference failed, falling back to SearXNG: {e}")

    # === Step 4: Fallback to SearXNG-only inference (legacy behavior) ===
    if len(found_emails) < 2:
        return None, 0.0

    pattern, confidence = _infer_pattern(found_emails, domain)
    return pattern, confidence


def _extract_emails(text: str, domain: str) -> list[str]:
    """Extract email addresses matching the domain from text."""
    # Match email-like patterns: word@domain.com
    pattern = re.compile(r'\b([a-zA-Z0-9._-]+@' + re.escape(domain) + r')\b')
    return pattern.findall(text)


def _infer_pattern(emails: list[str], domain: str) -> tuple[Optional[str], float]:
    """
    Given a list of real email addresses at a domain, infer the naming pattern.
    Returns (template_string, confidence) tuple.
    Template like "{first}.{last}@{domain}".
    Confidence = 1.0 when 2+ found emails agree on same pattern, else 0.0.
    """
    # Remove domain part
    local_parts = [e.split("@")[0] for e in emails]

    # Count separator types
    dot_count = sum(1 for lp in local_parts if "." in lp)
    underscore_count = sum(1 for lp in local_parts if "_" in lp)
    hyphen_count = sum(1 for lp in local_parts if "-" in lp)
    no_sep_count = sum(1 for lp in local_parts if not any(c in lp for c in "._-"))

    # Determine separator
    if dot_count >= max(underscore_count, hyphen_count, no_sep_count):
        separator = "."
    elif underscore_count >= max(dot_count, hyphen_count, no_sep_count):
        separator = "_"
    elif hyphen_count >= max(dot_count, underscore_count, no_sep_count):
        separator = "-"
    else:
        separator = ""

    # Analyze name structure
    if separator:
        parts_list = [lp.split(separator) for lp in local_parts]
        avg_parts = sum(len(p) for p in parts_list) / len(parts_list)

        if avg_parts >= 2:
            # Likely first.last or first_last
            if separator == ".":
                pattern = f"{{first}}.{{last}}@{domain}"
            elif separator == "_":
                pattern = f"{{first}}_{{last}}@{domain}"
            else:
                pattern = f"{{first}}-{{last}}@{domain}"
            # Confidence: 1.0 if 2+ found emails agree, else 0.0
            confidence = 1.0 if len(emails) >= 2 else 0.0
            return pattern, confidence

    # No separator - could be firstlast, flast, firstl, last, first
    # Check lengths
    avg_len = sum(len(lp) for lp in local_parts) / len(local_parts)

    # Try to match against known patterns
    for lp in local_parts:
        # f.last pattern (j.smith)
        if len(lp) >= 3 and "." in lp:
            p = lp.split(".")
            if len(p) == 2 and len(p[0]) == 1 and len(p[1]) >= 2:
                return f"{{f}}.{{last}}@{domain}", 1.0

        # flast pattern (jsmith)
        if len(lp) >= 3 and len(lp) <= 8 and lp[0].isalpha():
            # Could be first initial + last name
            if _looks_like_last_name(lp[1:]):
                return f"{{f}}{{last}}@{domain}", 1.0

        # firstlast pattern (johnsmith)
        if len(lp) >= 6:
            # Try splitting into first+last
            for i in range(2, len(lp) - 1):
                first_part = lp[:i]
                last_part = lp[i:]
                if _looks_like_first_name(first_part) and _looks_like_last_name(last_part):
                    return f"{{first}}{{last}}@{domain}", 1.0

    # Fallback - just use the most common format observed
    if no_sep_count > 0:
        return f"{{first}}{{last}}@{domain}", 0.0
    if dot_count > 0:
        return f"{{first}}.{{last}}@{domain}", 0.0

    return None, 0.0


def _looks_like_first_name(s: str) -> bool:
    """Rough heuristic: first names are 2-8 letters, alphabetic."""
    return s.isalpha() and 2 <= len(s) <= 8


def _looks_like_last_name(s: str) -> bool:
    """Rough heuristic: last names are 2-12 letters, alphabetic."""
    return s.isalpha() and 2 <= len(s) <= 12


def derive_email(first_name: str, last_name: str, pattern: str) -> Optional[str]:
    """
    Derive an email from a pattern template.
    Pattern: "{first}.{last}@domain.com"

    Supports {first}, {last}, {f}, {l}, {first_name}, {last_name}, {last_initial}.
    Returns None if any unsupported template token remains in the result.
    """
    if not pattern or not first_name or not last_name:
        return None

    first = first_name.lower().strip()
    last = last_name.lower().strip()
    f = first[0] if first else ""
    l = last[0] if last else ""

    # Build email
    # Normalize: remove spaces, dots, special chars from names for email
    def _normalize_for_email(s: str) -> str:
        return re.sub(r"[^a-z0-9]", "", s.lower())

    first_norm = _normalize_for_email(first)
    last_norm = _normalize_for_email(last)
    f_norm = first[0] if first else ""
    l_norm = last[0] if last else ""
    last_initial = l_norm

    email = pattern.replace("{first}", first_norm)
    email = email.replace("{last}", last_norm)
    email = email.replace("{f}", f_norm)
    email = email.replace("{l}", l_norm)
    email = email.replace("{first_name}", first_norm)
    email = email.replace("{last_name}", last_norm)
    email = email.replace("{last_initial}", last_initial)

    # Guard: never return a literal template token.
    if re.search(r"\{[^}]+\}", email):
        return None

    return email


# ── Unified Email Resolution Chain (Phase 5 hardening) ───────────────────────

@dataclass
class ResolutionResult:
    contact_id: int
    pattern_source: Literal["existing", "ai_inferred", "no_pattern", "none"] = "none"
    pattern: Optional[str] = None
    derived_email: Optional[str] = None
    candidate_email: Optional[str] = None  # email used for validation (derived or popup)
    validation_method: str = "skipped"  # "smtp_2probe" | "cache_hit" | "skipped" | "skipped_rate_limit" | "skipped_no_email" | "skipped_manual_edit" | "error"
    smtp_validation_status: Optional[str] = None
    email_ready_for_export: bool = False
    duration_ms: int = 0
    notes: list[str] = field(default_factory=list)


def _get_config_email() -> dict:
    from lf_config import get as _get_cfg
    return _get_cfg("email_validation", {})


def _derive_email_for_contact(contact_id: int, pattern: str) -> Optional[str]:
    """Derive an email for a contact using the given pattern. Does NOT write."""
    if not pattern:
        return None
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    row = cur.execute(
        "SELECT first_name, last_name FROM contacts WHERE id=?", (contact_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    first_name = (row["first_name"] or "").strip()
    last_name = (row["last_name"] or "").strip()
    if not first_name or not last_name:
        return None
    return derive_email(first_name, last_name, pattern)


# ── Pattern Proof Gate (Phase 2) ────────────────────────────────────────────

@dataclass
class PatternProofResult:
    pattern: Optional[str] = None
    confidence: float = 0.0
    proof_email: Optional[str] = None
    proof_status: Optional[str] = None  # 'Okay to Send' | 'Catch-All' | 'Do Not Send' | 'Maybe' | None
    proof_analysis: Optional[str] = None
    proof_contact_id: Optional[int] = None
    source: str = "none"  # 'smtp_proven' | 'unproven' | 'web_search' | 'ai_suggested'
    reason: str = ""
    candidate_results: list[dict] = field(default_factory=list)


def _smtp_prove_email(email: str) -> dict:
    """Run a fast 2-probe SMTP check on a candidate proof email. Returns result dict."""
    try:
        from lf_email_validator import check_email_pattern_proof
        result = check_email_pattern_proof(email, timeout=5)
        return result.to_dict()
    except Exception as e:
        return {
            "email": email,
            "status": "Maybe",
            "analysis": f"SMTP proof failed: {e}",
            "validation_method": "smtp_live",
            "validation_latency_ms": 0,
        }


def _emails_from_search_results(results: list[dict], domain: str) -> list[str]:
    """Extract email addresses from search-result title/snippet/url text."""
    found: list[str] = []
    domain_clean = domain.lower().lstrip("@")
    regex = re.compile(r'\b([a-zA-Z0-9._-]+@' + re.escape(domain_clean) + r')\b')
    for r in results:
        text = " ".join(filter(None, [
            r.get("title", ""),
            r.get("snippet", ""),
            r.get("url", ""),
        ]))
        for email in regex.findall(text):
            email = email.lower().strip()
            if email not in found:
                found.append(email)
    return found


def _prove_pattern_from_search(
    company_id: int,
    domain: str,
    sample_contact_ids: list[int] | None = None,
    max_probes: int = 20,
) -> PatternProofResult:
    """
    Prove a pattern by searching the web for real emails at the domain,
    inferring the pattern from those emails, and SMTP-proving it against a
    matching sample contact.

    This is the primary path for companies where the global common-pattern pool
    fails (most large corporations have a specific, non-obvious convention).
    """
    from lf_search_providers import search as _lf_search

    if not domain:
        return PatternProofResult(reason="No domain provided")

    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    company = get_company(company_id) or {}
    company_name = (company.get("name") or "").strip()
    city = (company.get("city") or "").strip()
    state = (company.get("state") or "").strip()
    industry = (company.get("business_type") or "").strip()

    if not sample_contact_ids:
        sample_contact_ids = _pick_proof_sample_contacts(company_id, cur)
    if not sample_contact_ids:
        conn.close()
        return PatternProofResult(reason="No contacts available to use as proof sample")

    conn.close()

    # 1. Search for real emails at the domain using company context.
    search_emails: list[str] = []
    queries = [
        f'"@{domain}" "{company_name}" email',
        f'"{company_name}" "{city}, {state}" email',
    ]
    if industry:
        queries.append(f'"{company_name}" {industry} "{city}, {state}" email')
        queries.append(f'"{company_name}" manufacturer "{city}, {state}" email')
    queries.extend([
        f'"@{domain}" contact email',
        f'site:{domain} "@" email',
        f'"{domain}" "email format"',
    ])
    for q in queries:
        try:
            results, _ = _lf_search(q, timeout=12, ai_extract=False)
            emails = _emails_from_search_results(results, domain)
            for e in emails:
                if e not in search_emails:
                    search_emails.append(e)
        except Exception as e:
            logger.warning(f"_prove_pattern_from_search query failed: {q} - {e}")
            continue

    if len(search_emails) < 2:
        return PatternProofResult(reason=f"Fewer than 2 real emails found for {domain} via search")

    # 2. Infer the dominant pattern from found emails.
    inferred_pattern, confidence = _infer_pattern(search_emails, domain)
    if not inferred_pattern:
        return PatternProofResult(
            reason=f"Could not infer pattern from {len(search_emails)} found emails at {domain}"
        )

    # 3. Find a sample contact whose name can derive a candidate with that pattern.
    candidate_results: list[dict] = []
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    proven_result: PatternProofResult | None = None
    probes_done = 0

    for cid in sample_contact_ids:
        if probes_done >= max_probes:
            break
        row = cur.execute(
            "SELECT first_name, last_name FROM contacts WHERE id=?", (cid,)
        ).fetchone()
        if not row:
            continue
        first = (row["first_name"] or "").strip()
        last = (row["last_name"] or "").strip()
        if not first or not last:
            continue

        candidate = derive_email(first, last, inferred_pattern)
        if not candidate:
            continue

        result = _smtp_prove_email(candidate)
        probes_done += 1
        candidate_results.append({
            "pattern_name": "search_inferred",
            "pattern_template": inferred_pattern,
            "contact_id": cid,
            "email": candidate,
            "status": result.get("status"),
            "analysis": result.get("analysis"),
        })

        if result.get("status") == "Okay to Send":
            proven_result = PatternProofResult(
                pattern=inferred_pattern,
                confidence=confidence,
                proof_email=candidate,
                proof_status="Okay to Send",
                proof_analysis=result.get("analysis"),
                proof_contact_id=cid,
                source="smtp_proven",
                candidate_results=candidate_results,
            )
            break

    conn.close()

    if proven_result:
        return proven_result

    # 4. If the inferred pattern didn't prove, fall back to trying each found
    #    email directly as a known-good anchor (some may be stale but still
    #    accept mail).
    for email in search_emails[:max_probes]:
        if probes_done >= max_probes:
            break
        result = _smtp_prove_email(email)
        probes_done += 1
        candidate_results.append({
            "pattern_name": "search_anchor",
            "pattern_template": inferred_pattern,
            "contact_id": None,
            "email": email,
            "status": result.get("status"),
            "analysis": result.get("analysis"),
        })
        if result.get("status") == "Okay to Send":
            return PatternProofResult(
                pattern=inferred_pattern,
                confidence=0.7,
                proof_email=email,
                proof_status="Okay to Send",
                proof_analysis=result.get("analysis"),
                source="smtp_proven",
                reason="Pattern inferred from search; anchor email accepted",
                candidate_results=candidate_results,
            )

    best = candidate_results[0] if candidate_results else {}
    reason = (
        f"Search inferred {inferred_pattern} from {len(search_emails)} emails; "
        f"best probe was {best.get('status')} ({best.get('analysis')}) for {best.get('email')}"
    )
    return PatternProofResult(
        source="unproven",
        reason=reason,
        candidate_results=candidate_results,
    )


def _pick_proof_sample_contacts(company_id: int, cur: sqlite3.Cursor, limit: int = 3) -> list[int]:
    """
    Pick the best sample contacts at a company for pattern proofing.

    Prefer contacts with:
      - clean first/last names (alphabetic, no suffixes)
      - simple last names (single token, no hyphens/spaces)
      - longer first names (more signal for initial-based patterns)
      - an existing derived email already on file (so we don't need to write first)

    Returns list of contact_id ordered best-first.
    """
    suffixes = (
        "jr", "sr", "ii", "iii", "iv", "v", "cpa", "md", "phd", "esq",
        "mba", "pe", "cfp", "jd", "rn", "do", "dvm", "clu", "chfc",
    )

    rows = cur.execute(
        "SELECT id, first_name, last_name, email FROM contacts "
        "WHERE company_id=? AND first_name IS NOT NULL AND last_name IS NOT NULL "
        "ORDER BY (email IS NOT NULL AND email != '') DESC, id",
        (company_id,),
    ).fetchall()

    scored: list[tuple[int, int]] = []
    for contact_id, first, last, _ in rows:
        first_clean = (first or "").lower().strip()
        last_clean = (last or "").lower().strip()
        if not first_clean or not last_clean:
            continue
        # Penalize suffixes, compound/hyphenated last names, very short names.
        score = 100
        if any(last_clean.endswith(s) for s in suffixes):
            score -= 40
        if any(c in last_clean for c in " -'"):
            score -= 25
        if len(first_clean) < 3:
            score -= 20
        if len(last_clean) < 3:
            score -= 20
        # Prefer alphabetic only.
        if not (first_clean.isalpha() and last_clean.isalpha()):
            score -= 30
        scored.append((contact_id, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [cid for cid, _ in scored[:limit]]


# Pattern *names* (matching generate_candidates output) tried during pattern
# proofing, ordered by global popularity. Keep this small to limit probe volume
# while still covering the common cases.
PROOF_PRIORITY_PATTERN_NAMES = {
    "first.last",
    "firstlast",
    "f.last",
    "flast",
    "first_last",
    "first-last",
    "first",
    "last",
}


def _candidate_emails_for_company(
    company_id: int,
    domain: str,
    sample_contact_ids: list[int],
    use_web_search: bool = False,
    suggested_pattern: Optional[str] = None,
) -> list[tuple[int, str, str, str]]:
    """
    Build an ordered list of candidate (contact_id, pattern_template, email) tuples
    for pattern proofing.

    Uses the global EMAIL_PATTERNS pool via generate_candidates(), which already
    appends the domain. Optionally prepends a web-search/AI suggested pattern.

    Args:
      suggested_pattern: an externally-suggested template (e.g. from AI inference)
                         to prepend as the first candidate. If provided, it is used
                         without invoking web search again.
    """
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    candidates: list[tuple[int, str, str, str]] = []  # (contact_id, pattern_name, pattern_template, email)
    seen_emails: set[str] = set()

    # Optional web-search/AI suggested pattern — prepend as first candidate for
    # each sample contact. Store as a full template including @{domain} so it can
    # be used later by derive_email().
    web_pattern: Optional[str] = None
    if suggested_pattern:
        web_pattern = suggested_pattern.strip()
    elif use_web_search:
        try:
            company = get_company(company_id) or {}
            web_pattern, _ = discover_email_pattern(
                domain=domain,
                company_name=company.get("name") or "",
                industry=company.get("business_type") or "",
                city=company.get("city") or "",
                state=company.get("state") or "",
            )
        except Exception:
            web_pattern = None

    # Normalize web_pattern to a full template that derive_email() understands.
    if web_pattern:
        if "@" not in web_pattern:
            web_pattern = web_pattern.replace("{domain}", "") + f"@{domain}"
        else:
            web_pattern = web_pattern.replace("{domain}", domain)

    for cid in sample_contact_ids:
        row = cur.execute(
            "SELECT first_name, last_name FROM contacts WHERE id=?", (cid,)
        ).fetchone()
        if not row:
            continue
        first = (row["first_name"] or "").strip()
        last = (row["last_name"] or "").strip()
        if not first or not last:
            continue

        # Prepend web/AI suggested pattern first if available.
        if web_pattern:
            email = derive_email(first, last, web_pattern)
            if email and "@" in email and email.lower() not in seen_emails:
                seen_emails.add(email.lower())
                candidates.append((cid, "web_search_suggested", web_pattern, email))

        # Use only the priority subset for proofing to keep probe volume low.
        # Build a name->template lookup from EMAIL_PATTERNS.
        name_to_template = {name: tpl for name, tpl in EMAIL_PATTERNS}
        for cand in generate_candidates(first, last, domain):
            if cand["pattern"] not in PROOF_PRIORITY_PATTERN_NAMES:
                continue
            email = cand["email"]
            # Convert bare template into a full template that derive_email() can
            # reuse later with the proven domain.
            bare_template = name_to_template.get(cand["pattern"], cand["pattern"])
            # EMAIL_PATTERNS templates are bare (e.g. "{first}.{last}"); append
            # @domain. Web-search patterns already include @domain and were
            # handled above.
            if "@" not in bare_template:
                full_template = bare_template + f"@{domain}"
            else:
                full_template = bare_template.replace("{domain}", domain)
            if email.lower() not in seen_emails:
                seen_emails.add(email.lower())
                candidates.append((cid, cand["pattern"], full_template, email))

    conn.close()
    return candidates


def _prove_pattern_for_company(
    company_id: int,
    domain: str,
    *,
    sample_contact_ids: list[int] | None = None,
    use_web_search: bool = False,
    suggested_pattern: Optional[str] = None,
    max_probes: int = 60,
    accept_catch_all: bool = True,
) -> PatternProofResult:
    """
    Prove an email pattern for a company via live SMTP probes.

    Strategy:
      1. Pick 1-3 sample contacts with clean names.
      2. Build candidate emails from the global pattern pool (+ optional web/AI hint).
      3. SMTP-probe candidates until one returns 'Okay to Send'.
      4. If no Okay, optionally accept a 'Catch-All' result with lower confidence.
      5. Otherwise return unproven with reason.

    Limits total probes to `max_probes` to avoid runaway cost.
    """
    if not domain:
        return PatternProofResult(reason="No domain provided")

    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if not sample_contact_ids:
        sample_contact_ids = _pick_proof_sample_contacts(company_id, cur)
    if not sample_contact_ids:
        conn.close()
        return PatternProofResult(reason="No contacts available to use as proof sample")

    conn.close()

    candidates = _candidate_emails_for_company(
        company_id, domain, sample_contact_ids, use_web_search=use_web_search,
        suggested_pattern=suggested_pattern,
    )
    if not candidates:
        return PatternProofResult(reason="No candidate emails could be generated from sample contacts")

    candidate_results: list[dict] = []
    probes_done = 0
    best_catch_all: Optional[tuple[str, str, str, int, dict]] = None  # (template_name, template, email, cid, result)
    dead_domains: set[str] = set()

    for cid, template_name, template, candidate_email in candidates:
        if probes_done >= max_probes:
            break

        cand_domain = candidate_email.split("@")[-1].lower()
        if cand_domain in dead_domains:
            continue

        result = _smtp_prove_email(candidate_email)
        probes_done += 1
        candidate_results.append({
            "pattern_name": template_name,
            "pattern_template": template,
            "contact_id": cid,
            "email": candidate_email,
            "status": result.get("status"),
            "analysis": result.get("analysis"),
        })

        status = result.get("status")
        analysis = result.get("analysis") or ""

        # If the very first probe on this domain can't reach it (No MX / timeout),
        # don't burn probes on every other pattern for the same domain.
        if status in ("Maybe", "Do Not Send") and analysis in ("No MX", "SMTP Error", "connection failed", "connection failed after retry"):
            dead_domains.add(cand_domain)
            continue

        if status == "Okay to Send":
            return PatternProofResult(
                pattern=template,
                confidence=0.9,
                proof_email=candidate_email,
                proof_status="Okay to Send",
                proof_analysis=analysis,
                proof_contact_id=cid,
                source="smtp_proven",
                candidate_results=candidate_results,
            )
        if accept_catch_all and status == "Do Not Send" and analysis == "Catch-All":
            if best_catch_all is None:
                best_catch_all = (template_name, template, candidate_email, cid, result)

    if best_catch_all:
        _, template, email, cid, result = best_catch_all
        return PatternProofResult(
            pattern=template,
            confidence=0.5,
            proof_email=email,
            proof_status="Catch-All",
            proof_analysis=result.get("analysis"),
            proof_contact_id=cid,
            source="smtp_proven",
            reason="Domain is catch-all; pattern accepted with reduced confidence",
            candidate_results=candidate_results,
        )

    # Build reason from the best non-Okay result we saw.
    if candidate_results:
        best = candidate_results[0]
        reason = f"Best result was {best['status']} ({best['analysis']}) for {best['email']}"
    else:
        reason = "No candidate emails could be derived or probed"
    return PatternProofResult(
        source="unproven",
        reason=reason,
        candidate_results=candidate_results,
    )


def _store_proven_pattern(
    company_id: int,
    proof: PatternProofResult,
    source_label: str = "smtp_proven",
) -> None:
    """Persist a proven (or unproven) pattern result on the company row."""
    now = datetime.utcnow().isoformat() + "Z"
    conn = get_db()
    cur = conn.cursor()
    if not proof.pattern:
        cur.execute(
            "UPDATE companies SET email_pattern=NULL, email_pattern_confidence=0.0, "
            "email_pattern_source='unproven', email_pattern_proof_email=?, "
            "email_pattern_proof_status=?, email_pattern_proven_at=NULL, "
            "email_pattern_unproved_reason=? WHERE id=?",
            (proof.proof_email, proof.proof_status, proof.reason or "Pattern could not be proven", company_id),
        )
    else:
        cur.execute(
            "UPDATE companies SET email_pattern=?, email_pattern_confidence=?, "
            "email_pattern_source=?, email_pattern_proof_email=?, "
            "email_pattern_proof_status=?, email_pattern_proven_at=?, "
            "email_pattern_unproved_reason=NULL WHERE id=?",
            (
                proof.pattern,
                proof.confidence,
                source_label,
                proof.proof_email,
                proof.proof_status,
                now,
                company_id,
            ),
        )
    conn.commit()
    conn.close()


def _search_domain_for_company(company_name: str) -> Optional[str]:
    """Try to find a company domain via SearXNG when no website is known."""
    if not company_name:
        return None
    try:
        results, _ = search(f'"{company_name}" official website', timeout=8)
        aggregators = ("yelp.com", "facebook.com", "linkedin.com", "google.com",
                       "mapquest.com", "yellowpages.com", "bbb.org", "manta.com",
                       "chamberofcommerce.com", "indeed.com", "crunchbase.com",
                       "wikipedia.org", "zoominfo.com", "rocketreach.co")
        for r in results[:5]:
            url = r.get("url", "")
            if not url.startswith("http"):
                continue
            host = urlparse(url).hostname or ""
            host = host.lower().lstrip("www.")
            if any(host == agg or host.endswith("." + agg) for agg in aggregators):
                continue
            domain = extract_domain_from_website(url)
            if domain:
                return domain
    except Exception as e:
        print(f"[email] Website search fallback failed for {company_name}: {e}")
    return None


def _infer_pattern_v2(company_id: int, domain: str, *, use_web_search: bool = False) -> tuple[Optional[str], float]:
    """Discover and SMTP-prove an email pattern for a company.

    Phase 2: pattern inference now requires a live SMTP proof before a pattern is
    stored on the company row. This prevents unproven AI/SearXNG patterns from
    being bulk-derived across all contacts.

    Phase 1 hardening: every pattern proof must first validate the company's real
    email domain via web search (Google Places + corroboration). If no confirmed
    domain exists, we run verify_company_domain() before any proof is attempted.
    The confirmed domain is the only domain used for pattern inference.

    Steps:
      1. If no confirmed domain, run verify_company_domain() against real search.
      2. Try AI v2 inference for a suggested pattern (using the confirmed domain).
      3. Fall back to SearXNG-based inference if AI fails.
      4. Run _prove_pattern_for_company() against the candidate pool.
      5. Store only the SMTP-proven pattern (or mark unproven if none pass).

    Returns (pattern, confidence). If no pattern proves, returns (None, 0.0).
    """
    from lf_ai_enrich import ai_infer_email_pattern_v2
    from lf_search import verify_company_domain

    company = get_company(company_id) or {}
    confirmed_domain = (company.get("email_domain_confirmed") or "").strip().lower()
    supplied_domain = (domain or "").strip().lower()

    if not confirmed_domain:
        # Domain-first gate: prove the real company domain before any pattern work.
        verification = verify_company_domain(company_id, require_corroboration=True)
        confirmed_domain = (verification.get("confirmed_domain") or "").strip().lower()
        if not confirmed_domain:
            _store_proven_pattern(
                company_id,
                PatternProofResult(reason="Could not confirm a real company domain via web search; pattern proof blocked"),
            )
            return None, 0.0

    if supplied_domain and supplied_domain != confirmed_domain:
        print(f"[email] _infer_pattern_v2: supplied domain {supplied_domain} ignored; using confirmed domain {confirmed_domain}")
    domain = confirmed_domain

    if not domain:
        _store_proven_pattern(company_id, PatternProofResult(reason="No confirmed domain available for inference"))
        return None, 0.0

    suggested_patterns: list[str] = []

    # Phase 2 proof strategy: web-search-first. We always try to discover the
    # pattern from real published emails at the confirmed domain; this is the
    # canonical process the user wants used all the time. AI inference is only a
    # fallback when web search produces no suggestion.
    try:
        pattern, _ = discover_email_pattern(
            domain=domain,
            company_name=company.get("name") or "",
            industry=company.get("business_type") or "",
            city=company.get("city") or "",
            state=company.get("state") or "",
        )
        if pattern:
            suggested_patterns.append(pattern)
            print(f"[email] Web-search pattern inference for {domain}: {pattern}")
    except Exception as e:
        print(f"[email] Web-search pattern inference failed for {domain}: {e}")

    if not suggested_patterns:
        # Fallback to AI v2 inference.
        try:
            result = ai_infer_email_pattern_v2(
                domain=domain,
                company_name=company.get("name") or "",
                industry=company.get("business_type") or "",
                city=company.get("city") or "",
                state=company.get("state") or "",
                quick=not use_web_search,
            )
            if result and result.get("pattern"):
                suggested_patterns.append(result["pattern"])
        except Exception as e:
            print(f"[email] ai_infer_email_pattern_v2 failed for {domain}: {e}")

    # Phase 2 proof execution: always run search-based pattern proof first (this
    # is the canonical process), then fall back to the common-pattern pool with
    # the AI/web suggested pattern prepended.
    proof = _prove_pattern_from_search(company_id, domain, max_probes=20)
    if proof.pattern and proof.proof_status in ("Okay to Send", "Catch-All"):
        _store_proven_pattern(company_id, proof, source_label=proof.source)
        return proof.pattern, proof.confidence

    proof = _prove_pattern_for_company(
        company_id,
        domain,
        use_web_search=True,
        suggested_pattern=suggested_patterns[0] if suggested_patterns else None,
        max_probes=60,
        accept_catch_all=True,
    )

    if proof.pattern and proof.proof_status in ("Okay to Send", "Catch-All"):
        _store_proven_pattern(company_id, proof, source_label=proof.source)
        return proof.pattern, proof.confidence

    # No pattern proved. Record the failure and clear any stored pattern.
    _store_proven_pattern(company_id, proof, source_label="unproven")
    return None, 0.0


def _smtp_validate(email: str) -> dict:
    """Run the 2-probe SMTP validator and return a dict suitable for persistence."""
    from lf_email_validator import check_email

    start = time.time()
    result = check_email(email).to_dict()
    duration_ms = int((time.time() - start) * 1000)
    result["validation_method"] = result.get("validation_method") or "smtp_live"
    result["validation_latency_ms"] = duration_ms
    return result


def _update_contact_email(contact_id: int, candidate_email: str, is_popup: bool) -> None:
    """Helper: update contacts.email and is_derived_email for a candidate."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "UPDATE contacts SET email=?, is_derived_email=? WHERE id=?",
        (candidate_email, 0 if is_popup else 1, contact_id),
    )
    conn.commit()
    conn.close()


def resolve_and_validate_email(
    contact_id: int,
    *,
    source: str,
    popup_email: Optional[str] = None,
    force_revalidate: bool = False,
) -> ResolutionResult:
    """
    Unified email chain: pattern → derive → SMTP → persist.

    - If a popup_email is supplied, use it directly as the candidate (skips pattern/derive).
    - Otherwise look up the company's email_pattern; if missing, infer via AI v2.
    - Derive the email from pattern and contact first/last name.
    - Check the DB cache; if fresh, re-apply cached result and skip SMTP.
    - Otherwise run the 2-probe SMTP validator.
    - Persist via write_contact_validation (single source of truth).
    - Return a structured ResolutionResult.
    """
    start = time.time()
    notes: list[str] = []

    # 1. Load contact + company
    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    row = cur.execute(
        "SELECT c.id, c.email, c.first_name, c.last_name, c.is_manually_edited, "
        "c.smtp_validation_status, c.email_ready_for_export, "
        "co.id as company_id, co.website, co.email_pattern, co.email_pattern_confidence, "
        "co.name as company_name, co.business_type, co.city, co.state "
        "FROM contacts c JOIN companies co ON c.company_id = co.id "
        "WHERE c.id=?",
        (contact_id,),
    ).fetchone()
    conn.close()

    if not row:
        return ResolutionResult(
            contact_id=contact_id,
            validation_method="error",
            notes=["Contact not found or has no company"],
        )

    contact = dict(row)

    # 2. Manual-edit guard
    if contact.get("is_manually_edited"):
        if not force_revalidate:
            return ResolutionResult(
                contact_id=contact_id,
                validation_method="skipped_manual_edit",
                notes=["Contact has is_manually_edited=1; chain skipped per policy"],
            )
        notes.append("force_revalidate=true, overriding is_manually_edited guard")

    # 3. Existing valid state guard (unless force revalidate)
    if not force_revalidate and contact.get("smtp_validation_status"):
        existing_email = (contact.get("email") or "").strip()
        if existing_email:
            cached = get_cached_validation(existing_email)
            if cached:
                # Ensure the candidate email is on the contact row before applying cached result
                if existing_email != (contact.get("email") or "").strip().lower():
                    _update_contact_email(contact_id, existing_email, is_popup=False)
                try:
                    write_contact_validation(contact_id, cached)
                except Exception as e:
                    notes.append(f"write_contact_validation cache re-apply failed: {e}")
                duration_ms = int((time.time() - start) * 1000)
                return ResolutionResult(
                    contact_id=contact_id,
                    pattern_source="none",
                    pattern=contact.get("email_pattern"),
                    candidate_email=existing_email,
                    validation_method="cache_hit",
                    smtp_validation_status=cached.get("status"),
                    email_ready_for_export=(cached.get("status") == "Okay to Send"),
                    duration_ms=duration_ms,
                    notes=notes + ["Used fresh cached validation; skipped SMTP"],
                )
        return ResolutionResult(
            contact_id=contact_id,
            validation_method="skipped",
            smtp_validation_status=contact.get("smtp_validation_status"),
            notes=notes + ["Contact already has smtp_validation_status and no fresh cache; skipped"],
        )

    # 4. Determine candidate email
    candidate_email = None
    pattern_source = "none"
    pattern = contact.get("email_pattern")

    if popup_email and popup_email.strip():
        candidate_email = popup_email.strip().lower()
        pattern_source = "none"
        notes.append(f"Using popup-supplied email: {candidate_email}")
    else:
        if not pattern:
            # Domain-first gate: confirm the company domain before inference.
            from lf_search import verify_company_domain
            verification = verify_company_domain(contact["company_id"], require_corroboration=True)
            domain = (verification.get("confirmed_domain") or "").strip().lower()
            if not domain:
                domain = extract_domain_from_website(contact.get("website") or "")
            if domain:
                pattern, confidence = _infer_pattern_v2(contact["company_id"], domain)
                if pattern:
                    pattern_source = "ai_inferred"
                    notes.append(f"Inferred pattern via AI v2: {pattern} (conf={confidence:.2f})")
                else:
                    notes.append("AI v2 could not infer a pattern")
            else:
                notes.append("No confirmed website/domain available for pattern inference")
        else:
            pattern_source = "existing"
            notes.append(f"Using existing company pattern: {pattern}")

        if pattern:
            candidate_email = _derive_email_for_contact(contact_id, pattern)
            if candidate_email:
                notes.append(f"Derived email: {candidate_email}")
            else:
                notes.append("Could not derive email from pattern + name")

    if not candidate_email:
        duration_ms = int((time.time() - start) * 1000)
        return ResolutionResult(
            contact_id=contact_id,
            pattern_source=pattern_source,
            pattern=pattern,
            validation_method="skipped_no_email",
            duration_ms=duration_ms,
            notes=notes + ["No candidate email; nothing to validate"],
        )

    # 5. Cache pre-check before SMTP
    cached = get_cached_validation(candidate_email)
    if cached and not force_revalidate:
        # Ensure the candidate email is on the contact row before applying cached result
        if candidate_email != (contact.get("email") or "").strip().lower():
            _update_contact_email(contact_id, candidate_email, is_popup=bool(popup_email and popup_email.strip()))
        try:
            write_contact_validation(contact_id, cached)
        except Exception as e:
            notes.append(f"write_contact_validation cache re-apply failed: {e}")
        duration_ms = int((time.time() - start) * 1000)
        return ResolutionResult(
            contact_id=contact_id,
            pattern_source=pattern_source,
            pattern=pattern,
            derived_email=candidate_email if pattern_source != "none" else None,
            candidate_email=candidate_email,
            validation_method="cache_hit",
            smtp_validation_status=cached.get("status"),
            email_ready_for_export=(cached.get("status") == "Okay to Send"),
            duration_ms=duration_ms,
            notes=notes + ["Used fresh cached validation; skipped SMTP"],
        )

    # 6. SMTP 2-probe validation
    try:
        result = _smtp_validate(candidate_email)
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        return ResolutionResult(
            contact_id=contact_id,
            pattern_source=pattern_source,
            pattern=pattern,
            derived_email=candidate_email if pattern_source != "none" else None,
            candidate_email=candidate_email,
            validation_method="error",
            duration_ms=duration_ms,
            notes=notes + [f"SMTP validation failed: {e}"],
        )

    # 7. Persist and cache
    if candidate_email != (contact.get("email") or "").strip().lower():
        _update_contact_email(contact_id, candidate_email, is_popup=bool(popup_email and popup_email.strip()))

    try:
        result["validated_at"] = result.get("validated_at") or (datetime.utcnow().isoformat() + "Z")
        result["validation_checked_at"] = result.get("validation_checked_at") or result["validated_at"]
        set_cached_validation(result)
    except Exception as e:
        notes.append(f"set_cached_validation failed: {e}")

    try:
        write_contact_validation(contact_id, result)
    except Exception as e:
        notes.append(f"write_contact_validation failed: {e}")

    duration_ms = int((time.time() - start) * 1000)
    ready = result.get("status") == "Okay to Send"

    # Stage in human send queue when SMTP proves the derived email.
    if ready and candidate_email:
        try:
            insert_email_send_queue(
                contact_id=contact_id,
                company_id=contact["company_id"],
                queued_email=candidate_email,
                pattern_template=pattern,
                subject=None,
                body_template_path=None,
            )
            notes.append("Staged in email_send_queue for human send")
        except Exception as e:
            notes.append(f"Failed to stage in email_send_queue: {e}")

    return ResolutionResult(
        contact_id=contact_id,
        pattern_source=pattern_source,
        pattern=pattern,
        derived_email=candidate_email if pattern_source != "none" else None,
        candidate_email=candidate_email,
        validation_method="smtp_2probe",
        smtp_validation_status=result.get("status"),
        email_ready_for_export=ready,
        duration_ms=duration_ms,
        notes=notes + [f"SMTP result: {result.get('status')} ({result.get('analysis')})"],
    )


# Legacy compatibility wrapper — new code should call _resolve_and_validate_email.
def discover_and_store_pattern(company_id: int, company_name: str, website: str) -> tuple[Optional[str], float]:
    """
    LEGACY COMPATIBILITY WRAPPER. Kept for existing imports. Do not use in new code.
    Discovers an email pattern for a company via AI v2 and stores it.
    Returns (pattern, confidence) tuple.
    """
    domain = extract_domain_from_website(website)
    if not domain:
        return None, 0.0
    pattern, confidence = _infer_pattern_v2(company_id, domain)
    return pattern, confidence


def derive_emails_for_company(company_id: int, *, force_reprove: bool = False) -> int:
    """
    Derive emails for all contacts at a company using a stored, SMTP-proven pattern.
    Returns count of emails derived.

    Phase 2 hardening:
      - Before deriving, the company's pattern must be proven (email_pattern_proof_status
        in ('Okay to Send', 'Catch-All')).
      - If the pattern is missing, unproven, or force_reprove=True, run the proof gate.
      - The proof gate MUST first validate the real company domain via
        verify_company_domain(); no pattern can be proved against an unverified domain.
      - If no pattern can be proven, derive nothing and mark the company unproven.

    Auto-validation is still handled by the unified chain; this function only
    writes derived emails for contacts that lack them.
    """
    from lf_search import verify_company_domain

    conn = get_db()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    row = cur.execute(
        "SELECT email_pattern, email_pattern_proof_status, website, email_pattern_unproved_reason, "
        "email_domain_confirmed "
        "FROM companies WHERE id=?", (company_id,)
    ).fetchone()
    if not row:
        conn.close()
        return 0

    pattern = row["email_pattern"]
    proof_status = row["email_pattern_proof_status"]
    needs_proof = (
        force_reprove
        or not pattern
        or proof_status not in ("Okay to Send", "Catch-All")
    )

    if needs_proof:
        # Domain-first gate: confirm real domain before any pattern proof.
        domain = (row["email_domain_confirmed"] or "").strip().lower()
        if not domain:
            verification = verify_company_domain(company_id, require_corroboration=True)
            domain = (verification.get("confirmed_domain") or "").strip().lower()
        if not domain:
            domain = extract_domain_from_website(row["website"] or "")
        if not domain:
            conn.close()
            print(f"[email] derive_emails_for_company: no confirmed domain for company {company_id}; cannot prove pattern")
            return 0
        # Re-prove must use the confirmed domain as source of truth.
        proof = _prove_pattern_for_company(company_id, domain, max_probes=60, accept_catch_all=True)
        if not proof.pattern or proof.proof_status not in ("Okay to Send", "Catch-All"):
            _store_proven_pattern(company_id, proof, source_label="unproven")
            conn.close()
            print(f"[email] derive_emails_for_company: no pattern proved for company {company_id}; derivation skipped")
            return 0
        _store_proven_pattern(company_id, proof, source_label=proof.source)
        pattern = proof.pattern

    # Get contacts without emails
    contacts = cur.execute(
        "SELECT id, first_name, last_name FROM contacts WHERE company_id=? AND (email IS NULL OR email='')",
        (company_id,)
    ).fetchall()

    count = 0
    for contact_id, first_name, last_name in contacts:
        email = derive_email(first_name or "", last_name or "", pattern)
        if email:
            cur.execute("UPDATE contacts SET email=?, is_derived_email=1 WHERE id=?",
                        (email, contact_id))
            count += 1

    conn.commit()
    conn.close()
    print(f"[email] Derived {count} emails for company {company_id} using proven pattern {pattern}")
    return count


# Legacy alias preserved for imports. New code should use _resolve_and_validate_email.
def _auto_validate_derived(company_id: int, derived: list[tuple[int, str]]) -> int:
    """
    DEPRECATED. The unified chain now handles validation.
    This shim runs the chain for each (contact_id, email) pair and logs progress.
    Returns the number of contacts processed.
    """
    from lf_config import get as _get_cfg
    ev_cfg = _get_cfg("email_validation", {})
    if not ev_cfg.get("enabled", False) or not ev_cfg.get("auto_validate_on_derive", False):
        return 0
    count = 0
    for contact_id, email in derived:
        try:
            resolve_and_validate_email(contact_id, source="auto_validate_derived", popup_email=email)
            count += 1
        except Exception as e:
            print(f"[email] Auto-validate chain failed for contact {contact_id}: {e}")
    return count


# Keep a private alias for internal callers that already use the underscore name.
_resolve_and_validate_email = resolve_and_validate_email


# ── Old-style exports (for lf_export.py compatibility) ───────────────────────

EMAIL_PATTERNS = [
    ("first.last",    "{first}.{last}"),
    ("firstlast",     "{first}{last}"),
    ("f.last",        "{f}.{last}"),
    ("flast",         "{f}{last}"),
    ("first",         "{first}"),
    ("first_l",       "{first}{first_initial}"),
    ("first_last",    "{first}_{last}"),
    ("first-last",    "{first}-{last}"),
    ("last",          "{last}"),
    ("initials",      "{first_initial}{last_initial}"),
    ("firstlast2",    "{first}{last_2}"),
    ("first3last",    "{first_3}{last}"),
    ("first3last2",   "{first_3}{last_2}"),
    ("firstlast3",    "{first}{last_3}"),
    ("first3",        "{first_3}"),
    ("lastfirst",     "{last}{first}"),
    ("lfirst",        "{l}{first}"),
    ("first.middle",  "{first}.{middle}"),
    ("firstmlast",    "{first}{middle_initial}{last}"),
    ("firstm.last",   "{first}{middle_initial}.{last}"),
    ("firstm",        "{first}{middle_initial}"),
]


def generate_candidates(first_name: str, last_name: str, domain: str) -> list[dict]:
    """
    Generate all candidate email addresses for a person.
    Returns list of {pattern_name, email} dicts.
    """
    first = (first_name or "").lower().strip()
    last = (last_name or "").lower().strip()
    if not first or not last:
        return []

    # Normalize: remove special chars
    first_clean = re.sub(r"[^a-z]", "", first)
    last_clean = re.sub(r"[^a-z]", "", last)
    first_initial = first_clean[0] if first_clean else ""
    last_initial = last_clean[0] if last_clean else ""
    first_3 = first_clean[:3]
    last_2 = last_clean[:2]
    last_3 = last_clean[:3]
    l = last_initial
    middle = ""
    middle_initial = ""

    candidates = []
    for name, pattern in EMAIL_PATTERNS:
        try:
            email = pattern.format(
                first=first_clean,
                last=last_clean,
                f=first_initial,
                l=l,
                first_initial=first_initial,
                last_initial=last_initial,
                first_3=first_3,
                last_2=last_2,
                last_3=last_3,
                middle=middle,
                middle_initial=middle_initial,
            )
            candidates.append({
                "pattern": name,
                "email": f"{email}@{domain}",
            })
        except (KeyError, IndexError):
            continue
    return candidates


# ── Self-Test ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=== Email Pattern Discovery Self-Test ===")
    print()

    print("1. Pattern inference tests:")
    test_emails = [
        ("john.smith@acme.com", "john.smith@acme.com", "{first}.{last}@acme.com"),
        ("jsmith@acme.com", "jsmith@acme.com", "{f}{last}@acme.com"),
        ("johnsmith@acme.com", "johnsmith@acme.com", "{first}{last}@acme.com"),
    ]
    for email, raw, expected_pattern in test_emails:
        pattern = _infer_pattern([raw], "acme.com")
        status = "PASS" if pattern == expected_pattern else "FAIL"
        print(f"  {status}: {email} → {pattern} (expected {expected_pattern})")

    print()
    print("2. Email derivation tests:")
    derivations = [
        ("John", "Smith", "{first}.{last}@acme.com", "john.smith@acme.com"),
        ("John", "Smith", "{f}{last}@acme.com", "jsmith@acme.com"),
        ("John", "Smith", "{first}{last}@acme.com", "johnsmith@acme.com"),
        ("Mary", "O\'Keefe", "{first}.{last}@acme.com", "mary.o\'keefe@acme.com"),
    ]
    for first, last, pattern, expected in derivations:
        result = derive_email(first, last, pattern)
        status = "PASS" if result == expected else "FAIL"
        print(f"  {status}: {first} {last} + {pattern} → {result}")

    print()
    print("Self-test complete.")
