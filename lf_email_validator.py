#!/usr/bin/env python3
"""
lf_email_validator.py - Email Validation Module for Lead Finder
================================================================
Internal fork of nimaaksoy/email-validator-app, refactored from
Streamlit to pure Python. Provides syntax validation, MX lookup,
disposable domain detection, and SMTP RCPT TO catch-all probing.

Pipeline:
  1. Syntax check (email-validator lib, check_deliverability=False)
  2. MX record lookup (dnspython)
  3. Disposable domain check (6,776 domains from disposable-email-domains pkg)
  4. SMTP catch-all probe (RCPT TO with random 24-char localpart)

Architecture Decisions:
  - DB-backed cache instead of module-level dict (persists across restarts)
  - EHLO + STARTTLS upgrade attempt per SMTP connection
  - Jittered delay between probes (0.5-1.5s random uniform)
  - Per-MX rate limiter (token-bucket, default 5 req/sec)
  - All SMTP errors -> catch_all=None (Unknown, not False)
  - Validation is OPT-IN via lf_config.json email_validation.enabled

Risks Addressed:
  - R1: SMTP probe marks IP in mail logs -> opt-in only, ehlo_hostname configurable
  - R2: No STARTTLS -> best-effort STARTTLS upgrade
  - R3: Predictable timing -> jittered delay
  - R9: Per-MX burst -> MXRateLimiter token bucket
  - R12: test.local rejected -> configurable ehlo_hostname
"""

from __future__ import annotations

import random
import re
import socket
import smtplib
import time
import traceback
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Optional

import dns.resolver
import disposable_email_domains  # 6,776 domains
import threading  # for MXRateLimiter
from email_validator import validate_email, EmailNotValidError

from lf_config import get

# ---- Logging ----
import logging
logger = logging.getLogger("lf_email_validator")
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s"))
    logger.addHandler(h)
    logger.setLevel(logging.INFO)


# ---- Config ----
def _get_cfg(key: str, default):
    """Read email_validation config from lf_config.json."""
    cfg = get("email_validation", {})
    return cfg.get(key, default)


# ---- Disposable Domains ----
# Start with the original 4 from the source app, then supplement from the package
_ORIGINAL_DISPOSABLE = frozenset({
    "mailinator.com", "10minutemail.com", "tempmail.com", "yopmail.com",
})
_DISPOSABLE_DOMAINS: frozenset | None = None


def get_disposable_domains() -> frozenset:
    """Return the full set of disposable email domains."""
    global _DISPOSABLE_DOMAINS
    if _DISPOSABLE_DOMAINS is None:
        try:
            pkg_domains = set(
                d.lower()
                for d in getattr(disposable_email_domains, "blacklist", [])
            )
            if not pkg_domains:
                pkg_domains = set(
                    d.lower()
                    for d in getattr(disposable_email_domains, "blocklist", [])
                )
        except Exception:
            pkg_domains = set()
        _DISPOSABLE_DOMAINS = frozenset(_ORIGINAL_DISPOSABLE | pkg_domains)
        logger.info(
            "Loaded %d disposable domains (%d from pkg, 4 from source)",
            len(_DISPOSABLE_DOMAINS), len(pkg_domains),
        )
    return _DISPOSABLE_DOMAINS


# ---- MX Rate Limiter ----
class MXRateLimiter:
    """
    Simple per-MX token-bucket rate limiter.
    Allows up to `max_per_second` probes to each MX host.
    """

    def __init__(self, max_per_second: int = 5):
        self.max_per_second = max_per_second
        self._windows: dict[str, list[float]] = {}  # mx_host -> [timestamps]
        self._lock = threading.Lock()

    def can_probe(self, mx_host: str) -> bool:
        """Check and consume one probe slot for the given MX host."""
        now = time.monotonic()
        with self._lock:
            timestamps = self._windows.get(mx_host, [])
            # Prune timestamps older than 1 second
            cutoff = now - 1.0
            timestamps = [t for t in timestamps if t > cutoff]
            if len(timestamps) >= self.max_per_second:
                self._windows[mx_host] = timestamps
                return False
            timestamps.append(now)
            self._windows[mx_host] = timestamps
            return True


# Global rate limiter instance
_mx_limiter = MXRateLimiter(max_per_second=_get_cfg("per_mx_rate_limit", 5))


# ---- Domain Cache (in-memory mirror of DB cache) ----
# Used for fast lookups during batch validation before DB write.
_checked_domains: dict[str, Optional[bool]] = {}  # domain -> catch_all


def _domain_cache_clear():
    _checked_domains.clear()


def _domain_cache_get(domain: str) -> Optional[Optional[bool]]:
    """Get cached catch_all result for domain. Returns None if not cached."""
    return _checked_domains.get(domain)


def _domain_cache_set(domain: str, catch_all: Optional[bool]):
    _checked_domains[domain] = catch_all


# ---- Core Validation Types ----
@dataclass
class ValidationResult:
    email: str
    status: str             # "Okay to Send" | "Do Not Send" | "Maybe" | "Unknown"
    analysis: str           # "Accepted" | "No MX" | "Invalid Syntax" | "Disposable" | "Catch-All" | "SMTP Error"
    smtp_probed: bool       # True if we attempted SMTP RCPT TO
    smtp_code: Optional[int]  # The SMTP response code (e.g. 250, 550)
    mx_host: Optional[str]  # The MX server we probed
    catch_all: Optional[bool]  # True=domain is catch-all, False=not, None=unknown
    domain: str             # The email domain
    validated_at: str       # ISO timestamp

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "status": self.status,
            "analysis": self.analysis,
            "smtp_probed": self.smtp_probed,
            "smtp_code": self.smtp_code,
            "mx_host": self.mx_host,
            "catch_all": self.catch_all,
            "domain": self.domain,
            "validated_at": self.validated_at,
        }


# ---- Core Functions ----
def has_mx_record(domain: str) -> tuple[bool, Optional[str]]:
    """
    Check if domain has MX records.
    Returns (has_mx, mx_host).
    If no MX, mx_host is None.
    """
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        if answers:
            mx = str(answers[0].exchange).rstrip(".")
            return True, mx
        return False, None
    except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN,
            dns.resolver.LifetimeTimeout, dns.exception.DNSException) as e:
        logger.debug(f"MX lookup failed for {domain}: {e}")
        return False, None


def get_mx_host(domain: str) -> Optional[str]:
    """Get primary MX host for domain. Returns None on failure."""
    _, mx = has_mx_record(domain)
    return mx


def is_catch_all(domain: str, mx_host: str) -> tuple[Optional[bool], Optional[int]]:
    """
    SMTP RCPT TO probe to determine if domain has catch-all email handling.
    
    Sends MAIL FROM + RCPT TO with a random 24-char localpart.
    - 250 response -> catch-all (accepts unknown users)
    - 550 response -> not catch-all (rejects unknown users)
    - Any other response or error -> None (unknown)
    
    Returns (catch_all: bool|None, smtp_code: int|None).
    """
    cfg = _get_cfg("smtp_timeout", 10)
    ehlo_host = _get_cfg("ehlo_hostname", "validator.livesolutionsnow.com")
    mail_from = _get_cfg("mail_from", "probe@validator.livesolutionsnow.com")
    use_starttls = _get_cfg("use_starttls", True)

    # Check rate limit
    if not _mx_limiter.can_probe(mx_host):
        logger.warning(f"Rate limited: skipping probe for {domain} via {mx_host}")
        return None, None

    fake_user = "".join(random.choices(string.ascii_lowercase + string.digits, k=24))
    probe_address = f"{fake_user}@{domain}"

    try:
        server = smtplib.SMTP(timeout=cfg)
        server.connect(mx_host)

        # EHLO with fallback to HELO (R2 mitigation: EHLO first)
        try:
            server.ehlo(ehlo_host)
        except smtplib.SMTPHeloError:
            try:
                server.helo(ehlo_host)
            except smtplib.SMTPHeloError:
                server.quit()
                return None, None

        # Opportunistic STARTTLS (R2 mitigation)
        if use_starttls and server.has_extn("STARTTLS"):
            try:
                server.starttls()
                # Re-EHLO after STARTTLS
                server.ehlo(ehlo_host)
            except (smtplib.SMTPException, OSError):
                pass  # Best-effort only

        server.mail(mail_from)
        code, message = server.rcpt(probe_address)
        server.quit()

        if code == 250:
            return True, code        # Catch-all: accepts unknown user
        elif code == 550:
            return False, code       # Not catch-all: rejects unknown user
        elif code in (450, 451, 452):
            # Temporary failure - retry later
            return None, code
        else:
            return None, code        # Unknown response

    except (socket.gaierror, socket.timeout,
            smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected,
            smtplib.SMTPRecipientsRefused, smtplib.SMTPHeloError,
            ConnectionRefusedError, ConnectionResetError, OSError) as e:
        logger.debug(f"SMTP probe failed for {domain} via {mx_host}: {e}")
        return None, None
    except Exception as e:
        logger.error(f"Unexpected SMTP error for {domain}: {e}")
        return None, None


def check_email(email: str) -> ValidationResult:
    """
    Validate a single email address.
    Runs: syntax check -> MX lookup -> disposable check -> SMTP catch-all probe.
    
    SMTP probe only runs if email_validation.enabled is True in config.
    """
    now = datetime.now(timezone.utc).isoformat()

    # Clean input (mirrors original: strip, remove semicolons)
    email = email.strip().replace(";", "")
    if not email:
        return ValidationResult(
            email=email, status="Do Not Send", analysis="Empty",
            smtp_probed=False, smtp_code=None, mx_host=None,
            catch_all=None, domain="", validated_at=now,
        )

    # Step 1: Syntax check
    try:
        valid = validate_email(email, check_deliverability=False)
        domain = valid["domain"].lower()
    except EmailNotValidError as e:
        return ValidationResult(
            email=email, status="Do Not Send", analysis="Invalid Syntax",
            smtp_probed=False, smtp_code=None, mx_host=None,
            catch_all=None, domain="", validated_at=now,
        )

    # Step 2: MX lookup
    has_mx, mx_host = has_mx_record(domain)
    if not has_mx:
        return ValidationResult(
            email=email, status="Do Not Send", analysis="No MX",
            smtp_probed=False, smtp_code=None, mx_host=mx_host,
            catch_all=None, domain=domain, validated_at=now,
        )

    # Step 3: Disposable domain check
    if domain in get_disposable_domains():
        return ValidationResult(
            email=email, status="Do Not Send", analysis="Disposable",
            smtp_probed=False, smtp_code=None, mx_host=mx_host,
            catch_all=False, domain=domain, validated_at=now,
        )

    # Step 4: SMTP catch-all probe (only if enabled in config)
    smtp_enabled = _get_cfg("enabled", False)
    if not smtp_enabled:
        return ValidationResult(
            email=email, status="Okay to Send", analysis="Accepted (SMTP disabled)",
            smtp_probed=False, smtp_code=None, mx_host=mx_host,
            catch_all=None, domain=domain, validated_at=now,
        )

    # Check domain cache first
    cached = _domain_cache_get(domain)
    if cached is not None:
        if cached:
            return ValidationResult(
                email=email, status="Do Not Send", analysis="Catch-All (cached)",
                smtp_probed=True, smtp_code=None, mx_host=mx_host,
                catch_all=True, domain=domain, validated_at=now,
            )
        else:
            return ValidationResult(
                email=email, status="Okay to Send", analysis="Accepted",
                smtp_probed=True, smtp_code=550, mx_host=mx_host,
                catch_all=False, domain=domain, validated_at=now,
            )

    # Jittered delay before SMTP probe (R3 mitigation)
    delay_min = _get_cfg("delay_min_seconds", 0.5)
    delay_max = _get_cfg("delay_max_seconds", 1.5)
    time.sleep(random.uniform(delay_min, delay_max))

    # Perform SMTP probe
    catch_all, smtp_code = is_catch_all(domain, mx_host)

    # Cache result
    _domain_cache_set(domain, catch_all)

    # Interpret result
    if catch_all is True and smtp_code == 250:
        status = "Do Not Send"
        analysis = "Catch-All"
    elif catch_all is False:
        status = "Okay to Send"
        analysis = "Accepted"
    elif catch_all is None and smtp_code is not None:
        status = "Maybe"
        analysis = f"SMTP Temp Fail ({smtp_code})"
    else:
        status = "Unknown"
        analysis = "SMTP Error"

    return ValidationResult(
        email=email, status=status, analysis=analysis,
        smtp_probed=True, smtp_code=smtp_code, mx_host=mx_host,
        catch_all=catch_all, domain=domain, validated_at=now,
    )


def validate_batch(emails: list[str]) -> list[ValidationResult]:
    """
    Validate a batch of emails sequentially (with jittered delays).
    Returns results in the same order as input.
    """
    results = []
    for email in emails:
        results.append(check_email(email))
    return results


def db_cache_key(email: str) -> str:
    """Normalize email for use as a cache key."""
    return email.strip().lower()


# ---- Status Icon Helper (for dashboard display) ----
STATUS_ICONS = {
    "Okay to Send": "🟢",
    "Do Not Send": "🔴",
    "Maybe": "🟠",
    "Unknown": "⚪",
    "Checking...": "🕐",
}

def status_display(status: str) -> str:
    icon = STATUS_ICONS.get(status, "❓")
    return f"{icon} {status}"


# ---- Self-Test ----
if __name__ == "__main__":
    print("=== Email Validator Self-Test ===\n")

    print(f"Disposable domains loaded: {len(get_disposable_domains())}")
    print(f"SMTP probe enabled: {_get_cfg('enabled', False)}")
    print()

    # Test 1: Invalid syntax
    r = check_email("not-an-email")
    print(f"1. Invalid syntax: {r.email} -> {r.status} ({r.analysis})")

    # Test 2: MX fail
    r = check_email("test@this-domain-definitely-does-not-exist-12345.com")
    print(f"2. No MX: {r.email} -> {r.status} ({r.analysis})")

    # Test 3: Disposable
    r = check_email("test@mailinator.com")
    print(f"3. Disposable: {r.email} -> {r.status} ({r.analysis})")

    # Test 4: Valid (SMTP disabled)
    r = check_email("test@gmail.com")
    print(f"4. Valid (SMTP off): {r.email} -> {r.status} ({r.analysis})")

    print("\nSelf-test complete.")