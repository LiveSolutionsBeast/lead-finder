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
from typing import Optional
from urllib.parse import urlparse

from lf_search_providers import search
from lf_db import get_db


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
    
    email = pattern.replace("{first}", first_norm)
    email = email.replace("{last}", last_norm)
    email = email.replace("{f}", f_norm)
    email = email.replace("{l}", l_norm)
    email = email.replace("{first_name}", first_norm)
    email = email.replace("{last_name}", last_norm)

    return email


def discover_and_store_pattern(company_id: int, company_name: str, website: str) -> tuple[Optional[str], float]:
    """
    Discover email pattern for a company and store it in DB. (rewritten 2026-06-07, QC-13)

    AI-led: discover_email_pattern() now uses AI as primary inference engine.
    This wrapper just enriches the call with city/state/industry + known employees
    from the DB, then stores the result.

    Returns (pattern, confidence) tuple.
    """
    from lf_db import get_db
    from lf_executives import extract_domain

    domain = extract_domain(website)
    if not domain:
        # Fallback: try to find website by searching for company domain.
        # Skip if the company name looks like it has a known suffix (Inc, LLC, Corp)
        # that won't yield a clean domain search.
        try:
            import sys; sys.path.insert(0, '/home/anthonyturgman/lead-finder')
            from lf_search_providers import search as _search
            # Cleaner search: just the company name (no "official website" suffix)
            # Tightened timeout to avoid hanging
            results, _ = _search(f'"{company_name}" contact', timeout=5)
            for r in results[:3]:  # only check top 3
                url = r.get("url", "")
                if url.startswith("http"):
                    # Skip aggregator URLs
                    from urllib.parse import urlparse
                    host = urlparse(url).hostname or ""
                    host = host.lower().lstrip("www.")
                    aggregators = ("yelp.com", "facebook.com", "linkedin.com", "google.com",
                                   "mapquest.com", "yellowpages.com", "bbb.org", "manta.com",
                                   "chamberofcommerce.com", "indeed.com", "crunchbase.com")
                    if any(host == agg or host.endswith("." + agg) for agg in aggregators):
                        continue
                    domain = extract_domain(url)
                    if domain:
                        print(f"[email] Found website via search: {domain}")
                        break
        except Exception as e:
            print(f"[email] Website search fallback failed: {e}")
            pass

    if not domain:
        print(f"[email] No domain for {company_name}, cannot discover pattern")
        return None, 0.0

    # Look up additional context: city, state, business_type, known employees
    city, state, business_type = "", "", ""
    known_employees = []
    try:
        conn_ctx = get_db()
        cur = conn_ctx.cursor()
        row = cur.execute(
            "SELECT city, state, business_type FROM companies WHERE id=?", (company_id,)
        ).fetchone()
        if row:
            city, state, business_type = (row[0] or ""), (row[1] or ""), (row[2] or "")
        emp_rows = cur.execute(
            "SELECT first_name, last_name FROM contacts WHERE company_id=? LIMIT 5",
            (company_id,)
        ).fetchall()
        for fn, ln in emp_rows:
            if fn and ln:
                known_employees.append(f"{fn} {ln}")
        conn_ctx.close()
    except Exception as e:
        print(f"[email] Context lookup failed: {e}")

    pattern, confidence = discover_email_pattern(
        domain=domain,
        company_name=company_name,
        industry=business_type,  # use business_type as industry hint
        city=city,
        state=state,
        known_employees=known_employees if known_employees else None,
    )
    if not pattern:
        return None, 0.0

    # Store in DB
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE companies SET email_pattern=?, email_pattern_confidence=?, email_pattern_source=? WHERE id=?",
                (pattern, confidence, "discovered", company_id))
    conn.commit()
    conn.close()

    print(f"[email] Pattern discovered for {company_name}: {pattern} (confidence={confidence})")
    return pattern, confidence


def derive_emails_for_company(company_id: int) -> int:
    """
    Derive emails for all contacts at a company using stored pattern.
    Returns count of emails derived.
    
    If email_validation.enabled AND auto_validate_on_derive are True in config,
    automatically runs SMTP validation on each derived email and stores results.
    """
    conn = get_db()
    cur = conn.cursor()

    # Get pattern
    row = cur.execute("SELECT email_pattern FROM companies WHERE id=?", (company_id,)).fetchone()
    if not row or not row[0]:
        conn.close()
        return 0

    pattern = row[0]

    # Get contacts without emails
    contacts = cur.execute(
        "SELECT id, first_name, last_name FROM contacts WHERE company_id=? AND (email IS NULL OR email='')",
        (company_id,)
    ).fetchall()

    count = 0
    derived_emails = []  # Track (contact_id, email) for optional auto-validation
    for contact_id, first_name, last_name in contacts:
        email = derive_email(first_name or "", last_name or "", pattern)
        if email:
            cur.execute("UPDATE contacts SET email=?, is_derived_email=1 WHERE id=?",
                        (email, contact_id))
            count += 1
            derived_emails.append((contact_id, email))

    conn.commit()
    conn.close()
    print(f"[email] Derived {count} emails for company {company_id}")

    # Auto-validate if enabled (config-gated)
    if derived_emails:
        _auto_validate_derived(company_id, derived_emails)

    return count


def _auto_validate_derived(company_id: int, derived: list[tuple[int, str]]):
    """
    Optional auto-validation of newly derived emails.
    Only runs when email_validation.enabled AND auto_validate_on_derive are both True.
    Non-blocking: failures are logged but never break the derive pipeline.
    """
    from lf_config import get as _get_cfg
    ev_cfg = _get_cfg("email_validation", {})
    if not ev_cfg.get("enabled", False) or not ev_cfg.get("auto_validate_on_derive", False):
        return

    print(f"[email] Auto-validating {len(derived)} derived emails for company {company_id}")
    try:
        from lf_email_validator import check_email
        from lf_db import get_db as _get_db, set_cached_validation as _cache

        for contact_id, email in derived:
            try:
                result = check_email(email).to_dict()
                # Cache to email_validation_cache
                try:
                    _cache(result)
                except Exception:
                    pass
                # Update contact row with validation status + auto-set ready-for-export flag
                try:
                    ready = 1 if result["status"] == "Okay to Send" else 0
                    rejected_reason = (
                        result.get("analysis", "") if result["status"] == "Do Not Send" else None
                    )
                    _conn = _get_db()
                    _cur = _conn.cursor()
                    _cur.execute(
                        "UPDATE contacts SET smtp_validation_status=?, smtp_validated_at=?, "
                        "smtp_validation_code=?, email_ready_for_export=?, email_rejected_reason=? "
                        "WHERE id=?",
                        (result["status"], result["validated_at"], result["smtp_code"],
                         ready, rejected_reason, contact_id),
                    )
                    _conn.commit()
                    _conn.close()
                except Exception as e:
                    print(f"[email] Auto-validate db-update failed for contact {contact_id}: {e}")
            except Exception as e:
                print(f"[email] Auto-validate failed for contact {contact_id}: {e}")
    except Exception as e:
        print(f"[email] Auto-validation setup failed: {e}")


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
