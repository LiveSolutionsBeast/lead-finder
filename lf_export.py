#!/usr/bin/env python3
"""
lf_export.py - Lead Finder Dual CSV Export Module
==================================================
Exports two CSV files per session:
  1. contacts CSV (Salesforce-ready) — no email column
  2. email_patterns CSV — all 21 patterns per contact
"""

import csv, json, re
from pathlib import Path
from datetime import datetime, timezone

from lf_email_patterns import generate_candidates, EMAIL_PATTERNS, derive_email

BASE_DIR = Path(__file__).parent
EXPORT_DIR = BASE_DIR / "exports"

# Business suffixes to strip when guessing domain from company name
_BUSINESS_SUFFIXES = [
    "Inc.", "Inc", "LLC", "L.L.C.", "Corp.", "Corp", "Ltd.", "Ltd",
    "Limited", "Company", "Co.", "Co", "Group", "Technologies",
    "Solutions", "Systems", "International", "Enterprises", "Holdings",
    "Partners", "Associates", "Industries", "LP", "L.P.", "PLC",
    "Pty Ltd", "Incorporated", "Corporation",
]

_SPECIAL_CHARS_RE = re.compile(r"[^a-z0-9]")


def _guess_domain(company_name: str) -> str:
    """
    Heuristically guess a domain from company name when website is unavailable.
    
    Steps:
    1. Remove common business suffixes (Inc, LLC, Corp, etc.)
    2. Lowercase, remove spaces and special characters
    3. Append ".com"
    
    Examples:
      "Varda Space Industries" -> "vardaspace.com"
      "Acme Corp Inc."       -> "acmecorp.com"
      "Bob's Welding LLC"    -> "bobswelding.com"
    """
    if not company_name:
        return ""
    name = company_name.strip()
    # Strip trailing punctuation (periods, commas) for cleaner suffix matching
    name = re.sub(r"[.,]+$", "", name).strip()
    # Remove business suffixes
    for suffix in sorted(_BUSINESS_SUFFIXES, key=len, reverse=True):
        # Match suffix at end, optionally preceded by comma or space
        pattern = re.compile(r",?\s*" + re.escape(suffix) + r"$", re.IGNORECASE)
        name = pattern.sub("", name).strip()
    # Strip trailing punctuation again after suffix removal
    name = re.sub(r"[.,]+$", "", name).strip()
    # Keep only alphanumeric, lowercase
    name = _SPECIAL_CHARS_RE.sub("", name.lower())
    if not name:
        return ""
    return f"{name}.com"


def _ensure_export_dir():
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)


def export_contacts_csv(contacts: list[dict], session_id: str, output_dir: Path = None) -> str:
    """
    Write Salesforce-ready contacts CSV.
    Columns: FirstName, LastName, Title, Company, Street, City, State, PostalCode,
             Country, StateCode, CountryCode, Website, Description, LeadSource,
             Status, Industry, Lead_Source_Description__c, LinkedIn_URL
    Returns file path.
    """
    if output_dir is None:
        output_dir = EXPORT_DIR
    _ensure_export_dir()

    fname = f"lf_contacts_{session_id}.csv"
    path = output_dir / fname

    fieldnames = [
        "FirstName", "LastName", "Title", "Company", "Street", "City", "State",
        "PostalCode", "Country", "StateCode", "CountryCode", "Website",
        "Description", "LeadSource", "Status", "Industry",
        "Lead_Source_Description__c", "LinkedIn_URL",
    ]

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for c in contacts:
            row = {
                "FirstName": c.get("first_name", "") or "",
                "LastName": c.get("last_name", "") or "",
                "Title": c.get("title", "") or c.get("Title", "") or "",
                "Company": c.get("company_name", "") or c.get("Company", "") or "",
                "Street": c.get("street", "") or "",
                "City": c.get("city", "") or "",
                "State": c.get("state", "") or "",
                "PostalCode": c.get("postal_code", "") or "",
                "Country": c.get("country", "USA") or "USA",
                "StateCode": c.get("state", "") or "",
                "CountryCode": c.get("country", "USA") or "USA",
                "Website": c.get("website", "") or "",
                "Description": c.get("description", "") or "",
                "LeadSource": "Lead Finder",
                "Status": "New",
                "Industry": c.get("industry", "") or "",
                "Lead_Source_Description__c": f"LF-session-{session_id}",
                "LinkedIn_URL": c.get("linkedin_url", "") or "",
            }
            writer.writerow(row)

    return str(path)


def export_email_patterns_csv(contacts: list[dict], session_id: str, output_dir: Path = None) -> str:
    """
    Write email patterns CSV: FirstName, LastName, Company, Domain, Pattern_1..Pattern_21.
    All 21 patterns as separate columns (one row per contact).
    Returns file path.
    """
    if output_dir is None:
        output_dir = EXPORT_DIR
    _ensure_export_dir()

    fname = f"lf_email_patterns_{session_id}.csv"
    path = output_dir / fname

    fieldnames = [
        "FirstName", "LastName", "Company", "Domain",
        "Pattern_1", "Pattern_2", "Pattern_3", "Pattern_4", "Pattern_5",
        "Pattern_6", "Pattern_7", "Pattern_8", "Pattern_9", "Pattern_10",
        "Pattern_11", "Pattern_12", "Pattern_13", "Pattern_14", "Pattern_15",
        "Pattern_16", "Pattern_17", "Pattern_18", "Pattern_19", "Pattern_20",
        "Pattern_21",
    ]

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for c in contacts:
            first = c.get("first_name", "") or ""
            last = c.get("last_name", "") or ""
            company = c.get("company_name", "") or c.get("Company", "") or ""

            # Get domain from company website or guess from company name
            domain = c.get("website", "") or c.get("domain", "") or ""
            if not domain:
                domain = _guess_domain(company)
            if domain:
                if domain.startswith("http"):
                    from urllib.parse import urlparse
                    try:
                        domain = urlparse(domain).netloc
                    except Exception:
                        domain = ""
                domain = domain.lower().strip().lstrip("@")
                if domain.count(".") >= 2:
                    domain = domain.split(".", 1)[1]

            candidates = generate_candidates(first, last, domain)

            row = {
                "FirstName": first,
                "LastName": last,
                "Company": company,
                "Domain": domain,
            }

            for i, (_, _) in enumerate(EMAIL_PATTERNS, start=1):
                row[f"Pattern_{i}"] = candidates[i-1]["email"] if i <= len(candidates) else ""

            writer.writerow(row)

    return str(path)


def export_ready_for_export_csv(contacts: list[dict], output_dir: Path = None) -> str:
    """
    Write a Salesforce-ready CSV of contacts validated as 'Okay to Send'.
    Includes email and validation metadata.
    Returns file path.
    """
    if output_dir is None:
        output_dir = EXPORT_DIR
    _ensure_export_dir()

    fname = f"lf_ready_for_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    path = output_dir / fname

    fieldnames = [
        "FirstName", "LastName", "Title", "Company", "City", "State",
        "Website", "Email", "LinkedIn_URL", "Email_Pattern",
        "Pattern_Confidence", "Validated_At",
    ]

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for c in contacts:
            row = {
                "FirstName": c.get("full_name", "").split()[0] if c.get("full_name") else "",
                "LastName": " ".join(c.get("full_name", "").split()[1:]) if c.get("full_name") else "",
                "Title": c.get("title", "") or "",
                "Company": c.get("company_name", "") or "",
                "City": c.get("city", "") or "",
                "State": c.get("state", "") or "",
                "Website": c.get("website", "") or "",
                "Email": c.get("email", "") or "",
                "LinkedIn_URL": c.get("linkedin_url", "") or "",
                "Email_Pattern": c.get("email_pattern", "") or "",
                "Pattern_Confidence": c.get("email_pattern_confidence", "") or "",
                "Validated_At": c.get("smtp_validated_at", "") or "",
            }
            writer.writerow(row)

    return str(path)


def export_company_email_patterns_csv(company_id: int, company_name: str, website: str, email_pattern: str, contacts: list[dict], output_dir: Path = None) -> str:
    """
    Write email patterns CSV for a single company.
    If email_pattern is known: single Email column with derived emails.
    If email_pattern is unknown: all 21 Pattern_1..21 columns for validation.
    Returns file path.
    """
    if output_dir is None:
        output_dir = EXPORT_DIR
    _ensure_export_dir()

    safe_name = re.sub(r"[^a-zA-Z0-9_-]", "", company_name.replace(" ", "_"))[:40]
    fname = f"lf_email_patterns_{safe_name}_{company_id}.csv"
    path = output_dir / fname

    # Determine domain
    domain = website or ""
    if domain.startswith("http"):
        from urllib.parse import urlparse
        try:
            domain = urlparse(domain).netloc
        except Exception:
            domain = ""
    domain = domain.lower().strip().lstrip("@")
    if domain.count(".") >= 2:
        domain = domain.split(".", 1)[1]
    if not domain:
        domain = _guess_domain(company_name)

    has_known_pattern = bool(email_pattern)

    if has_known_pattern:
        fieldnames = ["FirstName", "LastName", "Company", "Domain", "Email", "Pattern_Used", "Confidence"]
    else:
        fieldnames = [
            "FirstName", "LastName", "Company", "Domain",
            "Pattern_1", "Pattern_2", "Pattern_3", "Pattern_4", "Pattern_5",
            "Pattern_6", "Pattern_7", "Pattern_8", "Pattern_9", "Pattern_10",
            "Pattern_11", "Pattern_12", "Pattern_13", "Pattern_14", "Pattern_15",
            "Pattern_16", "Pattern_17", "Pattern_18", "Pattern_19", "Pattern_20",
            "Pattern_21",
        ]

    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for c in contacts:
            first = c.get("first_name", "") or ""
            last = c.get("last_name", "") or ""

            if has_known_pattern:
                email = derive_email(first, last, email_pattern) or ""
                row = {
                    "FirstName": first,
                    "LastName": last,
                    "Company": company_name,
                    "Domain": domain,
                    "Email": email,
                    "Pattern_Used": email_pattern,
                    "Confidence": "VALIDATED" if email_pattern else "UNKNOWN",
                }
            else:
                candidates = generate_candidates(first, last, domain)
                row = {
                    "FirstName": first,
                    "LastName": last,
                    "Company": company_name,
                    "Domain": domain,
                }
                for i, (_, _) in enumerate(EMAIL_PATTERNS, start=1):
                    row[f"Pattern_{i}"] = candidates[i-1]["email"] if i <= len(candidates) else ""

            writer.writerow(row)

    return str(path)


def export_session(session_key: str, output_dir: Path = None) -> dict:
    """
    Export both CSVs for a given session.
    Returns dict with keys: contacts_file, email_patterns_file, contacts_count, patterns_count.
    """
    if output_dir is None:
        output_dir = EXPORT_DIR

    from lf_db import get_session, get_session_companies, get_session_contacts
    import uuid

    session = get_session(session_key)
    if not session:
        return {"error": f"Session {session_key} not found"}

    companies = get_session_companies(session_key)
    contacts = get_session_contacts(session_key)

    # Merge contact info with company info
    enriched_contacts = []
    for ct in contacts:
        # Find the company this contact belongs to
        company_name = ""
        for comp in companies:
            if comp.get("id") == ct.get("company_id"):
                company_name = comp.get("name", "")
                website = comp.get("website", "")
                city = comp.get("city", "")
                state = comp.get("state", "")
                break

        enriched_contacts.append({
            "first_name": ct.get("first_name", ""),
            "last_name": ct.get("last_name", ""),
            "title": ct.get("title", ""),
            "company_name": company_name,
            "street": "",
            "city": city,
            "state": state,
            "postal_code": "",
            "country": "USA",
            "website": website,
            "description": "",
            "industry": session.get("industry", ""),
            "linkedin_url": ct.get("linkedin_url", ""),
        })

    contacts_file = export_contacts_csv(enriched_contacts, session_key, output_dir)
    patterns_file = export_email_patterns_csv(enriched_contacts, session_key, output_dir)

    return {
        "session_key": session_key,
        "contacts_file": contacts_file,
        "email_patterns_file": patterns_file,
        "contacts_count": len(enriched_contacts),
        "companies_count": len(companies),
    }
