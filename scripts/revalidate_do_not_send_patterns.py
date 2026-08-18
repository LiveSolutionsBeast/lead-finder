#!/usr/bin/env python3
"""
scripts/revalidate_do_not_send_patterns.py
==========================================
Re-derive and re-probe contacts currently marked 'Do Not Send' to see if a
better email pattern or domain resolves to a deliverable address.

Discovery tiers (redundant):
  1. Search + AI inference (`discover_email_pattern`) against the working domain.
  2. Reverse-engineer the pattern from any 'Okay to Send' contact at the same
     company (anchor) and apply it.
  3. Common local-part heuristics with name-parsing fixes.

Hardening / proof gate:
  - A contact is only upgraded from 'Do Not Send' to 'Okay to Send' when the
    new candidate returns `status='Okay to Send'` from a real 2-probe SMTP
    validation (specific local part accepted with SMTP code 250).
  - 'Maybe' or 'Do Not Send' candidates are logged but never overwrite a
    previous definitive result.
  - Manually-edited contacts are never touched.
  - Search results are cached per domain within the run to avoid duplicate
    provider calls.
  - Optional global concurrency/rate limiter via `--concurrency` (default 1
    serial, no surprises for MX hosts).

Domain correction:
  - For 'No MX' failures, prefer the cleaned `companies.website` domain.
  - Also try the original domain as a fallback.

Name-parsing variants:
  - last token (e.g. 'O Connell' -> 'oconnell')
  - drop embedded initial (e.g. 'J Tracy' -> 'tracy')
  - first-initial variants

Usage:
    python3 scripts/revalidate_do_not_send_patterns.py --dry-run --limit 10
    python3 scripts/revalidate_do_not_send_patterns.py --search-enabled --limit 30
    python3 scripts/revalidate_do_not_send_patterns.py
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lf_db import (  # noqa: E402
    get_db,
    init_db,
    set_cached_validation,
    write_contact_validation,
)
from lf_email_patterns import (  # noqa: E402
    derive_email,
    discover_email_pattern,
    extract_domain_from_website,
)
from lf_email_validator import check_email  # noqa: E402

DB_PATH = ROOT / "lf.db"
LOG_DIR = ROOT / "scripts"

COMMON_PATTERNS = [
    "{first}.{last}@{domain}",
    "{f}.{last}@{domain}",
    "{first}{last}@{domain}",
    "{f}{last}@{domain}",
]

# Extra patterns used only when name-parsing issues are suspected.
NAME_CLEAN_PATTERNS = [
    "{first}.{last}@{domain}",
    "{f}.{last}@{domain}",
]


def _clean_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _working_domains(website: str | None, original_email: str, reason: str | None) -> list[str]:
    """Return domains to try, ordered best-first.

    For 'No MX' failures we trust the website domain and also keep the original
    as a fallback. For all other rejection reasons we only use the original
    email's domain; adding alternate domains multiplies probe volume without
    fixing a real mailbox problem.
    """
    out: list[str] = []
    original_domain = (original_email.split("@")[-1] if "@" in original_email else "").lower().strip()
    website_domain = None
    if website:
        try:
            website_domain = extract_domain_from_website(website)
        except Exception:
            website_domain = None

    if reason == "No MX":
        if website_domain and website_domain != original_domain:
            out.append(website_domain)
        out.append(original_domain)
    else:
        out.append(original_domain)

    # Deduplicate while preserving order.
    seen = set()
    uniq = []
    for d in out:
        if d and d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def _reverse_pattern(email: str, first: str, last: str) -> str | None:
    """Guess the pattern that produced a known-good email."""
    if "@" not in email:
        return None
    local, domain = email.split("@", 1)
    first_norm = _clean_name(first)
    last_norm = _clean_name(last)
    f = first_norm[:1]

    checks = [
        (f"{first_norm}.{last_norm}", f"{{first}}.{{last}}@{domain}"),
        (f"{f}.{last_norm}", f"{{f}}.{{last}}@{domain}"),
        (f"{first_norm}{last_norm}", f"{{first}}{{last}}@{domain}"),
        (f"{f}{last_norm}", f"{{f}}{{last}}@{domain}"),
    ]
    for expected, pattern in checks:
        if local == expected:
            return pattern
    return None


def _name_variants(first: str, last: str) -> list[tuple[str, str, str]]:
    """Return (first, last, label) variants.

    Keeps the list short: only add clean variants when the original last name
    looks compound or the first name is unusually long/short.
    """
    first_clean = _clean_name(first)
    last_clean = _clean_name(last)
    variants = [(first_clean, last_clean, "as_given")]

    # Split last name on spaces/hyphens and try last token (real surname)
    last_tokens = re.split(r"[\s\-]+", last_clean)
    last_tokens = [t for t in last_tokens if t]
    if len(last_tokens) > 1:
        variants.append((first_clean, last_tokens[-1], "last_token"))

    # If last name looks like initial+rest, try dropping the leading initial.
    if len(last_clean) >= 3 and last_clean[0].isalpha() and last_clean[1:].isalpha():
        variants.append((first_clean, last_clean[1:], "drop_last_initial"))

    return variants


class _PatternCache:
    """In-run cache for discovered patterns to avoid repeated search/AI calls."""
    def __init__(self):
        self._store: dict[str, tuple[str | None, float]] = {}

    def get(self, domain: str, company_name: str) -> tuple[str | None, float]:
        if domain in self._store:
            return self._store[domain]
        pattern, confidence = discover_email_pattern(domain, company_name=company_name)
        self._store[domain] = (pattern, confidence)
        return pattern, confidence


@dataclass
class RetryResult:
    processed: int = 0
    upgraded: int = 0
    candidates_probed: int = 0
    skipped_manual_edit: int = 0
    skipped_no_domain: int = 0
    details: list[dict] = field(default_factory=list)


def run(limit: int | None = None, dry_run: bool = False, search_enabled: bool = True) -> RetryResult:
    init_db()
    conn = get_db()
    conn.row_factory = sqlite3.Row

    # Only retry categories that a different pattern/domain could plausibly fix.
    FIXABLE_REASONS = ("Not Found", "No MX")
    sql = """
        SELECT ct.id, ct.full_name, ct.first_name, ct.last_name, ct.email,
               ct.is_manually_edited, ct.email_rejected_reason,
               c.id as company_id, c.name as company_name, c.website, c.email_pattern
        FROM contacts ct
        JOIN companies c ON c.id = ct.company_id
        WHERE ct.smtp_validation_status = 'Do Not Send'
          AND ct.email IS NOT NULL AND ct.email != ''
          AND ct.is_test = 0
          AND ct.email_rejected_reason IN (?, ?)
        ORDER BY ct.company_id, ct.id
    """
    params = list(FIXABLE_REASONS)
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    result = RetryResult()
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    started = time.monotonic()
    pattern_cache = _PatternCache()

    # Load anchors: known-good Okay contacts per company.
    conn2 = get_db()
    conn2.row_factory = sqlite3.Row
    anchors: dict[int, tuple[str, str, str]] = {}
    for r in conn2.execute("""
        SELECT c.id as company_id, ct.email, ct.first_name, ct.last_name
        FROM contacts ct
        JOIN companies c ON c.id = ct.company_id
        WHERE ct.smtp_validation_status = 'Okay to Send'
          AND ct.email IS NOT NULL AND ct.email != ''
    """).fetchall():
        cid = r["company_id"]
        if cid not in anchors:
            anchors[cid] = (r["email"], r["first_name"] or "", r["last_name"] or "")
    conn2.close()

    print(f"=== Re-deriving {len(rows)} Do Not Send contacts ===")
    print(f"Companies with Okay anchors: {len(anchors)}")

    for i, r in enumerate(rows, 1):
        cid = r["id"]
        first = (r["first_name"] or "").strip()
        last = (r["last_name"] or "").strip()
        original_email = r["email"].strip()
        reason = r["email_rejected_reason"] or ""
        company_id = r["company_id"]
        company_name = r["company_name"]
        website = r["website"]
        is_manual = bool(r["is_manually_edited"])

        print(f"\n[{i}/{len(rows)}] id={cid} {r['full_name']} <{original_email}> ({company_name})")
        print(f"    reason={reason}")
        result.processed += 1

        if is_manual:
            print("    SKIP: manually edited")
            result.skipped_manual_edit += 1
            result.details.append({"id": cid, "email": original_email, "skipped": True, "reason": "manual_edit"})
            continue

        domains = _working_domains(website, original_email, reason)
        if not domains:
            print("    SKIP: no usable domain")
            result.skipped_no_domain += 1
            result.details.append({"id": cid, "email": original_email, "skipped": True, "reason": "no_domain"})
            continue

        # Build candidate list across domains and pattern sources.
        candidates: list[tuple[str, str, str]] = []  # (email, pattern_str, source_label)
        seen = set()

        for domain in domains:
            # Tier 1: common patterns (fast, no external API).
            variants = _name_variants(first, last)
            for fc, lc, variant_label in variants:
                patterns_to_try = NAME_CLEAN_PATTERNS if variant_label != "as_given" else COMMON_PATTERNS
                for tpl in patterns_to_try:
                    pattern_str = tpl.replace("{domain}", domain)
                    cand = derive_email(fc, lc, pattern_str)
                    if cand and "@" in cand and cand.lower() not in seen:
                        seen.add(cand.lower())
                        candidates.append((cand, pattern_str, f"common_{variant_label}"))

            # Tier 2: anchor-derived pattern.
            if company_id in anchors:
                anchor_email, af, al = anchors[company_id]
                anchor_pattern = _reverse_pattern(anchor_email, af, al)
                if anchor_pattern and "@" in anchor_pattern:
                    anchor_domain = anchor_pattern.split("@")[-1]
                    if anchor_domain.lower() == domain.lower():
                        for fc, lc, variant_label in _name_variants(first, last):
                            cand = derive_email(fc, lc, anchor_pattern)
                            if cand and cand.lower() not in seen:
                                seen.add(cand.lower())
                                candidates.append((cand, anchor_pattern, f"anchor_{variant_label}"))

            # Tier 3: search/AI discovered pattern (expensive, last resort).
            if search_enabled:
                try:
                    discovered, conf = pattern_cache.get(domain, company_name)
                    if discovered and "{" not in discovered:
                        # discover_email_pattern returns a template with {tokens}
                        discovered = discovered  # keep as-is, derive_email handles tokens
                    if discovered:
                        for fc, lc, variant_label in _name_variants(first, last):
                            cand = derive_email(fc, lc, discovered)
                            if cand and "@" in cand and cand.lower() not in seen:
                                seen.add(cand.lower())
                                candidates.append((cand, discovered, f"search_ai_{variant_label}"))
                except Exception as e:
                    print(f"    search/AI failed for {domain}: {e}")

        # Hard cap to prevent runaway probe volume on compound names.
        MAX_CANDIDATES = 16
        if len(candidates) > MAX_CANDIDATES:
            candidates = candidates[:MAX_CANDIDATES]

        if not candidates:
            print("    SKIP: no candidate emails generated")
            result.details.append({"id": cid, "email": original_email, "skipped": True, "reason": "no_candidates"})
            continue

        upgraded = False
        for cand_email, pattern_str, source_label in candidates:
            if "@" not in cand_email:
                continue
            # Don't probe the exact same email again if it already failed (it's in the DB).
            if cand_email.lower() == original_email.lower():
                continue
            result.candidates_probed += 1
            print(f"    TRY [{source_label}]: {cand_email}  (pattern={pattern_str})")
            try:
                probe_res = check_email(cand_email)
            except Exception as e:
                print(f"      PROBE ERROR: {e}")
                continue

            d = probe_res.to_dict()
            d.setdefault("email", cand_email)
            print(f"      -> status={d['status']} analysis={d['analysis']} "
                  f"code={d.get('smtp_code')} mx={d.get('validation_mx_host')} "
                  f"latency={d.get('validation_latency_ms')}ms")

            if probe_res.status == "Okay to Send":
                print(f"    UPGRADE: {original_email} -> {cand_email}")
                upgraded = True
                result.upgraded += 1
                if not dry_run:
                    try:
                        write_contact_validation(cid, d)
                        set_cached_validation(d)
                        _maybe_update_company_pattern(company_id, pattern_str, dry_run)
                        print("      DB WRITE OK")
                    except Exception as e:
                        print(f"      DB WRITE FAILED: {e}")
                result.details.append({
                    "id": cid,
                    "original_email": original_email,
                    "new_email": cand_email,
                    "pattern": pattern_str,
                    "source": source_label,
                    "upgraded": True,
                    "status": d["status"],
                    "mx_host": d.get("validation_mx_host"),
                    "latency_ms": d.get("validation_latency_ms"),
                })
                break

        if not upgraded:
            print("    no upgrade found")
            result.details.append({"id": cid, "email": original_email, "upgraded": False})

    elapsed = round(time.monotonic() - started, 1)
    summary = {
        "ts": ts,
        "processed": result.processed,
        "upgraded": result.upgraded,
        "candidates_probed": result.candidates_probed,
        "skipped_manual_edit": result.skipped_manual_edit,
        "skipped_no_domain": result.skipped_no_domain,
        "elapsed_seconds": elapsed,
        "dry_run": dry_run,
        "search_enabled": search_enabled,
        "details": result.details,
    }
    log_path = LOG_DIR / f"revalidate_do_not_send_patterns_log_{ts}.json"
    with open(log_path, "w") as f:
        json.dump(summary, f, indent=2)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Processed:           {result.processed}")
    print(f"Candidates probed:   {result.candidates_probed}")
    print(f"Upgraded to Okay:    {result.upgraded}")
    print(f"Skipped manual:      {result.skipped_manual_edit}")
    print(f"Skipped no domain:   {result.skipped_no_domain}")
    print(f"Elapsed:             {elapsed}s")
    if dry_run:
        print("[DRY RUN] No database writes were performed.")
    print(f"Log written: {log_path}")
    return result


def _maybe_update_company_pattern(company_id: int, pattern: str, dry_run: bool) -> None:
    """Update company pattern if we found a proven one.

    Accepts concrete templates like '{first}.{last}@domain.com' as valid.
    Rejects patterns containing literal unsubstituted braces other than the
    known tokens (e.g. '{last_initial}').
    """
    if dry_run or "@" not in pattern:
        return
    known_tokens = {"{first}", "{last}", "{f}", "{l}", "{first_name}", "{last_name}"}
    # Find any brace group that is not a known token.
    for m in re.finditer(r"\{[^}]*\}", pattern):
        if m.group(0) not in known_tokens:
            return
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT email_pattern, email_pattern_confidence FROM companies WHERE id=?",
        (company_id,),
    ).fetchone()
    if not row:
        conn.close()
        return
    current = row["email_pattern"] or ""
    current_conf = row["email_pattern_confidence"] or 0.0
    should_update = not current or current_conf < 1.0 or "{" in current
    if should_update:
        conn.execute(
            """UPDATE companies SET
                email_pattern = ?,
                email_pattern_confidence = 1.0,
                email_pattern_source = 'proven_by_smtp_okay_contact'
               WHERE id = ?""",
            (pattern, company_id),
        )
        conn.commit()
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Only process first N contacts")
    ap.add_argument("--dry-run", action="store_true", help="Run probes but do not write")
    ap.add_argument("--search-enabled", action="store_true", default=False,
                    help="Use search+AI to discover patterns (default False)")
    args = ap.parse_args()
    run(limit=args.limit, dry_run=args.dry_run, search_enabled=args.search_enabled)
    return 0


if __name__ == "__main__":
    sys.exit(main())
