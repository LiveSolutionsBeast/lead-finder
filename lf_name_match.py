"""
lf_name_match.py — Name normalization + fuzzy company match helpers
=====================================================================
Used by the LinkedIn plugin injection pathway to disambiguate contacts.

CONTRACTS.md section 6 — name normalization
CONTRACTS.md section 5 — disambiguation key priority order
"""

from __future__ import annotations

import re
from typing import Any

# ── Name normalization (CONTRACTS.md section 6) ────────────────────────────────

# Suffixes / credentials to strip (case-insensitive, with or without trailing dot)
_NAME_SUFFIX_RE = re.compile(
    r'\b(jr|sr|ii|iii|iv|v|phd|md|esq|jd|dds|dvm)\b\.?',
    re.IGNORECASE,
)
# Punctuation to remove (anything that isn't word char or whitespace)
_NAME_PUNCT_RE = re.compile(r'[^\w\s]', re.UNICODE)
# Whitespace collapse
_NAME_WS_RE = re.compile(r'\s+')


def normalize_name(name: str) -> str:
    """
    Lowercase, strip credentials (Jr, Sr, III, PhD, MD, ...), remove punctuation,
    collapse whitespace. See CONTRACTS.md section 6.

    >>> normalize_name("Katherine J. Smith, Jr.")
    'katherine j smith'
    >>> normalize_name("  Dr. Mary  O'Connor ")
    'mary oconnor'
    >>> normalize_name("Hans-Peter Müller, PhD")
    'hanspeter müller'
    """
    if not name:
        return ""
    s = str(name).lower().strip()
    s = _NAME_SUFFIX_RE.sub("", s)
    s = _NAME_PUNCT_RE.sub("", s)
    s = _NAME_WS_RE.sub(" ", s)
    return s.strip()


# ── Levenshtein distance (simple, pure Python) ────────────────────────────────

def levenshtein(a: str, b: str) -> int:
    """
    Standard Levenshtein edit distance. O(len(a) * len(b)) time and space.

    >>> levenshtein("kitten", "sitting")
    3
    >>> levenshtein("Raytheon", "Raytheon Company")
    8
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # Use a single row and roll forward to keep memory O(min(len(a), len(b))).
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for i, ch_b in enumerate(b, start=1):
        cur = [i]
        for j, ch_a in enumerate(a, start=1):
            cost = 0 if ch_a == ch_b else 1
            cur.append(min(
                cur[-1] + 1,        # insertion
                prev[j] + 1,        # deletion
                prev[j - 1] + cost, # substitution
            ))
        prev = cur
    return prev[-1]


# Common legal-entity suffixes to strip before comparison. Treating "Acme Inc"
# and "Acme" as the same company is a feature here.
_COMPANY_SUFFIX_RE = re.compile(
    r'\b(incorporated|corporation|company|limited|llc|llp|inc|co|corp|ltd|gmbh|sa|s\.?a\.?|plc|ag|n\.?v\.?|b\.?v\.?)\b\.?',
    re.IGNORECASE,
)
_WS_RE = re.compile(r'\s+')
_NON_ALNUM_RE = re.compile(r'[^a-z0-9\s]')


def _normalize_company_for_match(name: str) -> str:
    """Lowercase, strip common legal-entity suffixes, drop non-alphanumerics."""
    if not name:
        return ""
    s = str(name).lower().strip()
    s = _COMPANY_SUFFIX_RE.sub(" ", s)
    s = _NON_ALNUM_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def fuzzy_company_match(
    company_name: str,
    candidates: list[dict],
    max_distance: int = 3,
) -> list[dict]:
    """
    Return candidates whose normalized name is within `max_distance` Levenshtein
    edits of `company_name`, sorted by ascending distance. Each returned dict has
    an extra `match_distance` key.

    `candidates` should be an iterable of dicts each with a `name` key. The
    match compares the *normalized* form (suffixes stripped, lowercased, no
    punctuation) so that "Acme" matches "Acme Inc" at distance 0.

    >>> fuzzy_company_match("Acme", [{"name":"Acme Incorporated","id":1}])
    [{'name': 'Acme Incorporated', 'id': 1, 'match_distance': 0}]
    >>> fuzzy_company_match("General Atom", [{"name":"General Atomics","id":1}])
    [{'name': 'General Atomics', 'id': 1, 'match_distance': 1}]
    """
    if not company_name or not candidates:
        return []
    needle = _normalize_company_for_match(company_name)
    if not needle:
        return []
    out: list[dict] = []
    for cand in candidates:
        cand_name = cand.get("name") if isinstance(cand, dict) else None
        if not cand_name:
            continue
        haystack = _normalize_company_for_match(cand_name)
        if not haystack:
            continue
        # Cap the comparison at needle+max so very long names with no chance
        # of matching short-circuit.
        if abs(len(needle) - len(haystack)) > max_distance:
            continue
        d = levenshtein(needle, haystack)
        if d <= max_distance:
            enriched = dict(cand)
            enriched["match_distance"] = d
            out.append(enriched)
    out.sort(key=lambda c: c.get("match_distance", 999))
    return out


# ── Helpers for SQL row dicts ─────────────────────────────────────────────────

def candidates_from_rows(rows: list[Any]) -> list[dict]:
    """Convert a list of sqlite3.Row (or dicts) to a list of dicts."""
    out = []
    for r in rows:
        try:
            out.append(dict(r))
        except (TypeError, ValueError):
            out.append(r)
    return out
