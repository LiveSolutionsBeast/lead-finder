"""
lf_matcher.py — 4-case matching engine for the LinkedIn plugin
==============================================================
Per CONTRACTS.md section 5, the priority order for disambiguation is:

  1. linkedin_slug match  — strongest, immutable
  2. Normalized name + same company
  3. Normalized name + fuzzy company match (Levenshtein <= 3)
  4. Email match (only if existing has validated email)

The popup shows the user the candidate matches and lets them pick one of:
  - update_existing               (case 1 or 2 or 3 or 4 → already-present contact)
  - new_contact_existing_company  (case 2/3 with company match, new contact)
  - new_company_new_contact       (no candidate)
  - discard                       (close popup)

The matcher returns a structured `MatchResult` describing the strongest hit
plus any near-ties so the popup can show them as a dropdown.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from lf_name_match import (
    normalize_name,
    fuzzy_company_match,
    candidates_from_rows,
)


# Match strength (lower = stronger, for sorting)
STRENGTH_LINKEDIN_SLUG = 1
STRENGTH_NAME_SAME_COMPANY = 2
STRENGTH_NAME_FUZZY_COMPANY = 3
STRENGTH_EMAIL_VALIDATED = 4
STRENGTH_NONE = 99


@dataclass
class MatchCandidate:
    """A single candidate match the user might pick."""
    contact_id: int
    company_id: int
    full_name: str
    company_name: str
    matched_field: str           # 'linkedin_slug' | 'name+company' | 'name+fuzzy' | 'email'
    strength: int                 # STRENGTH_* constant
    match_distance: int = 0       # Levenshtein distance for fuzzy company match
    is_manually_edited: bool = False
    smtp_validation_status: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CompanyCandidate:
    company_id: int
    name: str
    match_distance: int = 0
    matched_field: str = "fuzzy_company"  # 'exact' | 'fuzzy_company'

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MatchResult:
    """What the matcher returns to the popup."""
    # The strongest single candidate, if any.
    best_contact: Optional[MatchCandidate] = None
    # All near-ties at the same strength as `best_contact` (for the dropdown).
    contact_ties: list[MatchCandidate] = field(default_factory=list)
    # Company matches (for 'new_contact_existing_company' radio button).
    company_candidates: list[CompanyCandidate] = field(default_factory=list)
    # The strongest company match (closest distance).
    best_company: Optional[CompanyCandidate] = None
    # The full normalized name (echoed back for the popup to display).
    normalized_name: str = ""
    # Diagnostic notes (e.g. why we picked what we picked).
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "best_contact": self.best_contact.to_dict() if self.best_contact else None,
            "contact_ties": [c.to_dict() for c in self.contact_ties],
            "company_candidates": [c.to_dict() for c in self.company_candidates],
            "best_company": self.best_company.to_dict() if self.best_company else None,
            "normalized_name": self.normalized_name,
            "notes": self.notes,
        }


def _row_to_contact_candidate(row: dict, strength: int, matched_field: str,
                              match_distance: int = 0) -> MatchCandidate:
    return MatchCandidate(
        contact_id=row["id"],
        company_id=row.get("company_id") or 0,
        full_name=row.get("full_name") or f"{row.get('first_name','')} {row.get('last_name','')}".strip(),
        company_name=row.get("_company_name") or row.get("company_name") or "",
        matched_field=matched_field,
        strength=strength,
        match_distance=match_distance,
        is_manually_edited=bool(row.get("is_manually_edited")),
        smtp_validation_status=row.get("smtp_validation_status"),
    )


def find_matches(
    *,
    linkedin_slug: str = "",
    full_name: str = "",
    current_company: str = "",
    email: str = "",
    get_contact_by_slug,
    find_contacts_by_name,
    get_companies_matching,
    find_contact_by_email,
    get_company,  # (id) -> dict | None — used to fetch company name for the candidate
) -> MatchResult:
    """
    Run the 4-priority disambiguation and return a MatchResult.

    All data access is injected as callables so the matcher is easy to test.
    Callers (the API endpoint) pass in the lf_db functions.
    """
    result = MatchResult()
    norm = normalize_name(full_name or "")
    result.normalized_name = norm

    # Priority 1: linkedin_slug match (strongest, immutable)
    if linkedin_slug:
        row = get_contact_by_slug(linkedin_slug)
        if row:
            # Fetch company name for display
            company_row = get_company(row.get("company_id")) if row.get("company_id") else None
            if company_row:
                row["_company_name"] = company_row.get("name", "")
            result.best_contact = _row_to_contact_candidate(
                row, STRENGTH_LINKEDIN_SLUG, "linkedin_slug"
            )
            result.notes.append(f"LinkedIn slug match → contact #{row['id']}")
            return result

    # Priority 2/3: name-based
    name_rows = find_contacts_by_name(norm) if norm else []

    # Priority 2: name + SAME company (exact company-name match)
    same_company_candidates: list[MatchCandidate] = []
    for r in name_rows:
        if not current_company or not r.get("company_id"):
            continue
        comp = get_company(r["company_id"])
        comp_name = (comp or {}).get("name", "")
        if comp_name and comp_name.strip().lower() == current_company.strip().lower():
            r["_company_name"] = comp_name
            same_company_candidates.append(
                _row_to_contact_candidate(r, STRENGTH_NAME_SAME_COMPANY, "name+company")
            )
    if same_company_candidates:
        # Pick the lowest contact id as best; the rest are ties.
        same_company_candidates.sort(key=lambda c: c.contact_id)
        result.best_contact = same_company_candidates[0]
        result.contact_ties = same_company_candidates[1:]
        result.notes.append(
            f"Name+company match → contact #{result.best_contact.contact_id}"
        )
        return result

    # Priority 3: name + FUZZY company match (Levenshtein <= 3)
    fuzzy_company_candidates: list[MatchCandidate] = []
    for r in name_rows:
        if not r.get("company_id"):
            continue
        comp = get_company(r["company_id"])
        comp_name = (comp or {}).get("name", "")
        if not comp_name:
            continue
        hits = fuzzy_company_match(current_company or comp_name, [{"name": comp_name}], max_distance=3)
        if hits:
            r["_company_name"] = comp_name
            fuzzy_company_candidates.append(
                _row_to_contact_candidate(
                    r, STRENGTH_NAME_FUZZY_COMPANY, "name+fuzzy",
                    match_distance=hits[0].get("match_distance", 0),
                )
            )
    if fuzzy_company_candidates:
        fuzzy_company_candidates.sort(key=lambda c: (c.match_distance, c.contact_id))
        result.best_contact = fuzzy_company_candidates[0]
        result.contact_ties = fuzzy_company_candidates[1:]
        result.notes.append(
            f"Name+fuzzy-company match → contact #{result.best_contact.contact_id} "
            f"(distance={result.best_contact.match_distance})"
        )
        # The fuzzy company itself is also a candidate for 'new_contact_existing_company'
        # so the popup can offer that radio as an alternative to 'update_existing'.
        result.company_candidates.append(CompanyCandidate(
            company_id=result.best_contact.company_id,
            name=result.best_contact.company_name,
            match_distance=result.best_contact.match_distance,
            matched_field="fuzzy_company",
        ))
        result.best_company = result.company_candidates[-1]
        return result

    # Priority 4: validated-email match
    if email:
        email_row = find_contact_by_email(email)
        if email_row:
            comp = get_company(email_row.get("company_id")) if email_row.get("company_id") else None
            if comp:
                email_row["_company_name"] = comp.get("name", "")
            result.best_contact = _row_to_contact_candidate(
                email_row, STRENGTH_EMAIL_VALIDATED, "email"
            )
            result.notes.append(f"Email match (validated) → contact #{email_row['id']}")
            return result

    # No contact match — look for company matches so we can offer
    # 'new_contact_existing_company' as a popup option.
    if current_company:
        company_rows = get_companies_matching(current_company) or []
        company_dicts = candidates_from_rows(company_rows)
        fuzzy_hits = fuzzy_company_match(current_company, company_dicts, max_distance=3)
        # Also include any exact name match in the candidate list.
        exact_hits = [
            CompanyCandidate(
                company_id=c["id"],
                name=c["name"],
                match_distance=0,
                matched_field="exact",
            )
            for c in company_dicts
            if c.get("name", "").strip().lower() == current_company.strip().lower()
        ]
        # Merge, dedupe on company_id, keep the best distance per company.
        seen: dict[int, CompanyCandidate] = {}
        for c in exact_hits:
            seen[c.company_id] = c
        for fh in fuzzy_hits:
            cid = fh.get("id")
            if cid is None or cid in seen:
                continue
            seen[cid] = CompanyCandidate(
                company_id=cid,
                name=fh.get("name", ""),
                match_distance=fh.get("match_distance", 0),
                matched_field="fuzzy_company",
            )
        result.company_candidates = sorted(
            seen.values(), key=lambda c: (c.match_distance, c.name)
        )
        if result.company_candidates:
            result.best_company = result.company_candidates[0]
            result.notes.append(
                f"Company match → {result.best_company.name} "
                f"(distance={result.best_company.match_distance})"
            )

    result.notes.append("No contact match; user must create new")
    return result
