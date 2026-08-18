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
import string
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


# ---- Domain cache hydration from DB (Phase 1 hardening, 2026-07-26) ----
# On first use of check_email() within a process, pre-load catch_all results
# from the persistent email_validation_cache so we don't re-probe domains we
# already know about. Lazy (not at module load) to avoid a DB hit at import time
# and avoid any circular-import risk (lf_db does not import this module).
_domain_cache_hydrated: bool = False


def _domain_cache_hydrate_from_db() -> None:
    """
    Pre-populate the in-memory `_checked_domains` cache from the DB
    `email_validation_cache` table. Only domains with a non-NULL `catch_all`
    value are loaded, deduplicated by domain (the DB is keyed by email; one
    domain may have many rows). Safe to call multiple times — it replaces the
    in-memory cache with a fresh snapshot.
    """
    global _domain_cache_hydrated
    try:
        # Lazy import to avoid circular import at module load time.
        from lf_db import get_db

        conn = get_db()
        try:
            cur = conn.cursor()
            rows = cur.execute(
                """
                SELECT domain, catch_all
                FROM email_validation_cache
                WHERE catch_all IS NOT NULL
                """,
            ).fetchall()
        finally:
            conn.close()

        # Deduplicate by domain: prefer an explicit True (catch-all) if any
        # row for the domain reports True, else False. This mirrors the
        # semantics of probe 1 (a single 250 => domain is catch-all).
        by_domain: dict[str, bool] = {}
        for r in rows:
            d = r["domain"]
            ca = bool(r["catch_all"])
            if d not in by_domain or ca:
                by_domain[d] = ca
        _checked_domains.clear()
        _checked_domains.update(by_domain)
        _domain_cache_hydrated = True
    except Exception:
        # Hydration is a perf optimization, not a correctness invariant.
        # Swallow errors so a missing/unreadable DB never breaks validation.
        _domain_cache_hydrated = True


# ---- Core Validation Types ----
@dataclass
class ValidationResult:
    email: str
    status: str             # "Okay to Send" | "Do Not Send" | "Maybe" | "Unknown"
    analysis: str           # "Accepted" | "No MX" | "Invalid Syntax" | "Disposable" | "Catch-All" | "SMTP Error" | "Not Found" | "Soft Fail"
    smtp_probed: bool       # True if we attempted SMTP RCPT TO
    smtp_code: Optional[int]  # The SMTP response code (e.g. 250, 550)
    mx_host: Optional[str]  # The MX server we probed
    catch_all: Optional[bool]  # True=domain is catch-all, False=not, None=unknown
    domain: str             # The email domain
    validated_at: str       # ISO timestamp
    sender_ehlo: str = ""   # EHLO hostname used in SMTP probe (added R4)
    sender_mail_from: str = ""  # MAIL FROM address used in SMTP probe (added R4)
    # Phase 1 / CONTRACTS.md section 1 — extended validation fields (added 2026-07-24)
    validation_confidence: float = 0.0   # 0.0..1.0
    validation_method: str = ""          # 'smtp_live' | 'smtp_cached' | 'manual_override'
    validation_checked_at: str = ""      # ISO 8601 (alias of validated_at but written separately)
    validation_mx_host: str = ""         # duplicate of mx_host for the contracts field
    validation_response: str = ""        # SMTP code + text
    validation_latency_ms: int = 0       # total wall time of the probe

    def to_dict(self) -> dict:
        d = {
            "email": self.email,
            "status": self.status,
            "analysis": self.analysis,
            "smtp_probed": self.smtp_probed,
            "smtp_code": self.smtp_code,
            "mx_host": self.mx_host,
            "catch_all": self.catch_all,
            "domain": self.domain,
            "validated_at": self.validated_at,
            # Phase 1 fields
            "validation_confidence": self.validation_confidence,
            "validation_method": self.validation_method,
            "validation_checked_at": self.validation_checked_at or self.validated_at,
            "validation_mx_host": self.validation_mx_host or (self.mx_host or ""),
            "validation_response": self.validation_response,
            "validation_latency_ms": self.validation_latency_ms,
        }
        if self.sender_ehlo:
            d["sender_ehlo"] = self.sender_ehlo
        if self.sender_mail_from:
            d["sender_mail_from"] = self.sender_mail_from
        return d


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


# ---- Sender Identity Rotation (added R4) ----
_sender_rotation_counter: dict[str, int] = {}
_sender_rotation_lock = threading.Lock()


def _pick_sender_domain() -> str:
    """Pick a sender domain from the rotation pool."""
    domains = _get_cfg("sender_domains", [])
    if not domains:
        # Fallback to ehlo_hostname domain
        ehlo = _get_cfg("ehlo_hostname", "validator.livesolutionsnow.com")
        return ehlo
    with _sender_rotation_lock:
        # Pick least-used domain for even distribution
        min_count = min(_sender_rotation_counter.get(d, 0) for d in domains)
        candidates = [d for d in domains if _sender_rotation_counter.get(d, 0) == min_count]
        chosen = random.choice(candidates)
        _sender_rotation_counter[chosen] = _sender_rotation_counter.get(chosen, 0) + 1
    return chosen


def _get_rotated_identity() -> tuple[str, str]:
    """
    Return (ehlo_hostname, mail_from) for SMTP probe.
    If rotate_sender_identity is False, use the fixed config values.
    If True, rotate based on sender_domains pool.
    """
    if not _get_cfg("rotate_sender_identity", False):
        ehlo = _get_cfg("ehlo_hostname", "validator.livesolutionsnow.com")
        mail_from = _get_cfg("mail_from", "probe@validator.livesolutionsnow.com")
        return ehlo, mail_from

    domain = _pick_sender_domain()
    # EHLO must be a valid FQDN; use the domain itself
    ehlo = domain
    mail_from = f"probe@{domain}"
    return ehlo, mail_from


def _smtp_probe_rcpt(mx_host: str, ehlo_host: str, mail_from: str,
                     rcpt_to: str, timeout: int, use_starttls: bool):
    """
    Open an SMTP connection to mx_host and execute MAIL FROM + RCPT TO for rcpt_to.
    Returns (smtp_code, smtp_message, latency_ms). Any exception or timeout returns (None, None, latency).

    This is the low-level primitive. Both is_catch_all() and verify_specific_address()
    use it. It performs one connection attempt only — retry/backoff is the caller's
    concern (so that the higher-level functions can decide whether to retry).
    """
    start = time.monotonic()
    server = None
    try:
        server = smtplib.SMTP(mx_host, timeout=timeout)

        # EHLO with fallback to HELO (R2 mitigation: EHLO first)
        try:
            server.ehlo(ehlo_host)
        except smtplib.SMTPHeloError:
            try:
                server.helo(ehlo_host)
            except smtplib.SMTPHeloError:
                return None, None, int((time.monotonic() - start) * 1000)

        # Opportunistic STARTTLS (R2 mitigation)
        if use_starttls and server.has_extn("STARTTLS"):
            try:
                server.starttls()
                # Re-EHLO after STARTTLS
                server.ehlo(ehlo_host)
            except (smtplib.SMTPException, OSError):
                pass  # Best-effort only

        server.mail(mail_from)
        code, message = server.rcpt(rcpt_to)
        return code, message, int((time.monotonic() - start) * 1000)

    except (socket.gaierror, socket.timeout,
            smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected,
            smtplib.SMTPRecipientsRefused, smtplib.SMTPHeloError,
            smtplib.SMTPSenderRefused, smtplib.SMTPDataError,
            smtplib.SMTPNotSupportedError, smtplib.SMTPException,
            ConnectionRefusedError, ConnectionResetError, OSError) as e:
        logger.debug(f"SMTP probe failed for {rcpt_to} via {mx_host}: {e}")
        return None, None, int((time.monotonic() - start) * 1000)
    except Exception as e:
        logger.error(f"Unexpected SMTP error for {rcpt_to}: {e}")
        return None, None, int((time.monotonic() - start) * 1000)
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


def is_catch_all(domain: str, mx_host: str) -> tuple[Optional[bool], Optional[int], str, str, int]:
    """
    SMTP RCPT TO probe to determine if domain has catch-all email handling.

    Sends MAIL FROM + RCPT TO with a random 24-char localpart.
    - 250 response -> catch-all (accepts unknown users)
    - 550 response -> not catch-all (rejects unknown users)
    - Any other response or error -> None (unknown)

    Returns (catch_all, smtp_code, sender_ehlo, sender_mail_from, latency_ms).
    """
    cfg = _get_cfg("smtp_timeout", 10)
    ehlo_host, mail_from = _get_rotated_identity()
    use_starttls = _get_cfg("use_starttls", True)

    # Check rate limit
    if not _mx_limiter.can_probe(mx_host):
        logger.warning(f"Rate limited: skipping probe for {domain} via {mx_host}")
        return None, None, ehlo_host, mail_from, 0

    fake_user = "".join(random.choices(string.ascii_lowercase + string.digits, k=24))
    probe_address = f"{fake_user}@{domain}"

    code, _message, latency = _smtp_probe_rcpt(
        mx_host=mx_host, ehlo_host=ehlo_host, mail_from=mail_from,
        rcpt_to=probe_address, timeout=cfg, use_starttls=use_starttls,
    )

    if code == 250:
        return True, code, ehlo_host, mail_from, latency       # Catch-all
    elif code == 550:
        return False, code, ehlo_host, mail_from, latency      # Not catch-all
    elif code in (450, 451, 452):
        return None, code, ehlo_host, mail_from, latency       # Temporary failure
    else:
        return None, code, ehlo_host, mail_from, latency       # Unknown response


def verify_specific_address(email: str, mx_host: str, ehlo_host: str, mail_from: str
                            ) -> tuple[Optional[int], str, int]:
    """
    Phase 1 / t1.0 — Second-probe SMTP RCPT TO for the ACTUAL local-part.
    Uses the same connection primitive as is_catch_all() but with the real address.
    Retries once on transport failure (per t1.7 robustness requirement).

    Returns (smtp_code, response_message, latency_ms).
      - 250: mailbox exists
      - 550/553: mailbox does not exist
      - 450/451/452: soft (greylist / rate limit)
      - None: connection/transport failure
    """
    cfg = _get_cfg("smtp_timeout", 10)
    use_starttls = _get_cfg("use_starttls", True)

    # First attempt
    code, message, latency = _smtp_probe_rcpt(
        mx_host=mx_host, ehlo_host=ehlo_host, mail_from=mail_from,
        rcpt_to=email, timeout=cfg, use_starttls=use_starttls,
    )
    if code is not None:
        return code, message or "", latency

    # Per t1.7: retry once on transport failure with 2s backoff
    logger.debug(f"verify_specific_address first attempt failed for {email}, retrying in 2s")
    time.sleep(2.0)
    code2, message2, latency2 = _smtp_probe_rcpt(
        mx_host=mx_host, ehlo_host=ehlo_host, mail_from=mail_from,
        rcpt_to=email, timeout=cfg, use_starttls=use_starttls,
    )
    return code2, message2 or "", (latency + latency2)


def check_email(email: str) -> ValidationResult:
    """
    Validate a single email address.
    Runs: syntax check -> MX lookup -> disposable check -> SMTP 2-probe.

    SMTP 2-probe design (Phase 1 / t1.0):
      1. Random-local-part RCPT TO (catch-all detection).
         - 250 -> catch-all -> Do Not Send (catch_all).
         - 550 -> not catch-all. Proceed to step 2.
         - Soft (450/451/452) or error -> Maybe (risky).
      2. (Only if step 1 returned 550) RCPT TO with the ACTUAL local-part.
         - 250 -> mailbox exists -> Okay to Send.
         - 550/553 -> mailbox does not exist -> Do Not Send.
         - Soft -> Maybe.
         - None -> Maybe (could not reach server, even after retry).

    SMTP probe only runs if email_validation.enabled is True in config.
    """
    # Phase 1 hardening: hydrate the in-memory domain cache from the DB on
    # first use so we skip re-probing domains whose catch_all is already known.
    if not _domain_cache_hydrated:
        _domain_cache_hydrate_from_db()

    now = datetime.now(timezone.utc).isoformat()
    overall_start = time.monotonic()

    # Clean input (mirrors original: strip, remove semicolons)
    email = email.strip().replace(";", "")
    if not email:
        return ValidationResult(
            email=email, status="Do Not Send", analysis="Empty",
            smtp_probed=False, smtp_code=None, mx_host=None,
            catch_all=None, domain="", validated_at=now,
            validation_confidence=0.95, validation_method="smtp_live",
            validation_checked_at=now,
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
            validation_confidence=0.95, validation_method="smtp_live",
            validation_checked_at=now,
        )

    # Step 2: MX lookup
    has_mx, mx_host = has_mx_record(domain)
    if not has_mx:
        return ValidationResult(
            email=email, status="Do Not Send", analysis="No MX",
            smtp_probed=False, smtp_code=None, mx_host=mx_host,
            catch_all=None, domain=domain, validated_at=now,
            validation_confidence=0.95, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
        )

    # Step 3: Disposable domain check
    if domain in get_disposable_domains():
        return ValidationResult(
            email=email, status="Do Not Send", analysis="Disposable",
            smtp_probed=False, smtp_code=None, mx_host=mx_host,
            catch_all=False, domain=domain, validated_at=now,
            validation_confidence=0.99, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
        )

    # Step 4: SMTP catch-all probe (only if enabled in config)
    smtp_enabled = _get_cfg("enabled", False)
    if not smtp_enabled:
        return ValidationResult(
            email=email, status="Okay to Send", analysis="Accepted (SMTP disabled)",
            smtp_probed=False, smtp_code=None, mx_host=mx_host,
            catch_all=None, domain=domain, validated_at=now,
            sender_ehlo="", sender_mail_from="",
            validation_confidence=0.4, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
        )

    # Check domain cache first
    cached = _domain_cache_get(domain)
    if cached is not None:
        if cached:
            return ValidationResult(
                email=email, status="Do Not Send", analysis="Catch-All (cached)",
                smtp_probed=True, smtp_code=None, mx_host=mx_host,
                catch_all=True, domain=domain, validated_at=now,
                sender_ehlo="", sender_mail_from="",
                validation_confidence=0.4, validation_method="smtp_cached",
                validation_checked_at=now, validation_mx_host=mx_host or "",
            )
        else:
            # domain not catch-all (cached) — still need to verify specific local-part
            # (we don't cache specific addresses; fall through to specific probe)
            pass

    # Jittered delay before SMTP probe (R3 mitigation)
    delay_min = _get_cfg("delay_min_seconds", 0.5)
    delay_max = _get_cfg("delay_max_seconds", 1.5)
    time.sleep(random.uniform(delay_min, delay_max))

    # 2-probe SMTP flow ──────────────────────────────────────────────────
    # Probe 1: random local-part (catch-all detection)
    catch_all, code1, sender_ehlo, sender_mail_from, lat1 = is_catch_all(domain, mx_host)
    _domain_cache_set(domain, catch_all)

    if catch_all is True and code1 == 250:
        # Domain is catch-all — do not bother probing the specific address;
        # the server accepts anything, so "validity" of the specific address
        # is unknown and the safe default is Do Not Send.
        return ValidationResult(
            email=email, status="Do Not Send", analysis="Catch-All",
            smtp_probed=True, smtp_code=code1, mx_host=mx_host,
            catch_all=True, domain=domain, validated_at=now,
            sender_ehlo=sender_ehlo, sender_mail_from=sender_mail_from,
            validation_confidence=0.4, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
            validation_response=f"{code1} catch-all detected (random local-part accepted)",
            validation_latency_ms=lat1,
        )

    if catch_all is None and code1 is not None and code1 in (450, 451, 452):
        # Soft fail on probe 1 — risky
        return ValidationResult(
            email=email, status="Maybe", analysis=f"SMTP Temp Fail ({code1})",
            smtp_probed=True, smtp_code=code1, mx_host=mx_host,
            catch_all=None, domain=domain, validated_at=now,
            sender_ehlo=sender_ehlo, sender_mail_from=sender_mail_from,
            validation_confidence=0.3, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
            validation_response=f"{code1} (greylist/quarantine)",
            validation_latency_ms=lat1,
        )

    if catch_all is None and code1 is None:
        # Transport/connection failure on probe 1, even before any specific probe.
        return ValidationResult(
            email=email, status="Maybe", analysis="SMTP Error",
            smtp_probed=True, smtp_code=None, mx_host=mx_host,
            catch_all=None, domain=domain, validated_at=now,
            sender_ehlo=sender_ehlo, sender_mail_from=sender_mail_from,
            validation_confidence=0.5, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
            validation_response="connection failed",
            validation_latency_ms=lat1,
        )

    # Probe 1 returned 550 (not catch-all). Probe the ACTUAL local-part.
    # If the cached result said "not catch-all", we still want to verify the
    # specific address — but we can skip a redundant probe 1 by trusting the cache.
    if catch_all is False and code1 == 550:
        # Use sender_ehlo / sender_mail_from from the first probe so we don't
        # rotate identity mid-validation.
        ehlo2 = sender_ehlo or _get_cfg("ehlo_hostname", "validator.livesolutionsnow.com")
        mail_from2 = sender_mail_from or _get_cfg("mail_from", "probe@validator.livesolutionsnow.com")
    else:
        # Cached "not catch-all" — fall back to current rotated identity
        ehlo2, mail_from2 = _get_rotated_identity()

    # Per t1.7: graceful per-probe timeout is enforced by the underlying socket timeout
    # (smtp_timeout, default 10s). The verify function already retries once.
    code2, msg2, lat2 = verify_specific_address(
        email=email, mx_host=mx_host, ehlo_host=ehlo2, mail_from=mail_from2,
    )
    total_latency = int((time.monotonic() - overall_start) * 1000)

    if code2 == 250:
        return ValidationResult(
            email=email, status="Okay to Send", analysis="Accepted",
            smtp_probed=True, smtp_code=code2, mx_host=mx_host,
            catch_all=False, domain=domain, validated_at=now,
            sender_ehlo=ehlo2, sender_mail_from=mail_from2,
            validation_confidence=0.9, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
            validation_response=f"{code2} {msg2}" if msg2 else f"{code2}",
            validation_latency_ms=total_latency,
        )

    if code2 in (550, 553):
        return ValidationResult(
            email=email, status="Do Not Send", analysis="Not Found",
            smtp_probed=True, smtp_code=code2, mx_host=mx_host,
            catch_all=False, domain=domain, validated_at=now,
            sender_ehlo=ehlo2, sender_mail_from=mail_from2,
            validation_confidence=0.95, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
            validation_response=f"{code2} {msg2}" if msg2 else f"{code2}",
            validation_latency_ms=total_latency,
        )

    if code2 in (450, 451, 452):
        return ValidationResult(
            email=email, status="Maybe", analysis=f"SMTP Temp Fail ({code2})",
            smtp_probed=True, smtp_code=code2, mx_host=mx_host,
            catch_all=False, domain=domain, validated_at=now,
            sender_ehlo=ehlo2, sender_mail_from=mail_from2,
            validation_confidence=0.3, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
            validation_response=f"{code2} (greylist/quarantine)",
            validation_latency_ms=total_latency,
        )

    # None — couldn't reach server even after retry
    return ValidationResult(
        email=email, status="Maybe", analysis="SMTP Error",
        smtp_probed=True, smtp_code=None, mx_host=mx_host,
        catch_all=False, domain=domain, validated_at=now,
        sender_ehlo=ehlo2, sender_mail_from=mail_from2,
        validation_confidence=0.5, validation_method="smtp_live",
        validation_checked_at=now, validation_mx_host=mx_host or "",
        validation_response="connection failed after retry",
        validation_latency_ms=total_latency,
    )


def check_email_pattern_proof(email: str, timeout: int = 5) -> ValidationResult:
    """
    Fast SMTP validation for pattern proofing.

    Runs the same 2-probe logic as check_email() but optimized for speed when
    many candidates must be tested:
      - configurable shorter socket timeout (default 5s)
      - no retry on transport failure
      - minimal fixed delay before probing
      - no SMTP-level retry/backoff

    This is intentionally separate from check_email() so that the final
    validation path keeps its conservative timeouts, jitter, and retry behavior.
    """
    now = datetime.now(timezone.utc).isoformat()
    overall_start = time.monotonic()

    email = email.strip().replace(";", "")
    if not email:
        return ValidationResult(
            email=email, status="Do Not Send", analysis="Empty",
            smtp_probed=False, smtp_code=None, mx_host=None,
            catch_all=None, domain="", validated_at=now,
            validation_confidence=0.95, validation_method="smtp_live",
            validation_checked_at=now,
        )

    # Syntax check
    try:
        valid = validate_email(email, check_deliverability=False)
        domain = valid["domain"].lower()
    except EmailNotValidError as e:
        return ValidationResult(
            email=email, status="Do Not Send", analysis="Invalid Syntax",
            smtp_probed=False, smtp_code=None, mx_host=None,
            catch_all=None, domain="", validated_at=now,
            validation_confidence=0.95, validation_method="smtp_live",
            validation_checked_at=now,
        )

    # MX lookup
    has_mx, mx_host = has_mx_record(domain)
    if not has_mx:
        return ValidationResult(
            email=email, status="Do Not Send", analysis="No MX",
            smtp_probed=False, smtp_code=None, mx_host=mx_host,
            catch_all=None, domain=domain, validated_at=now,
            validation_confidence=0.95, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
        )

    # Disposable check
    if domain in get_disposable_domains():
        return ValidationResult(
            email=email, status="Do Not Send", analysis="Disposable",
            smtp_probed=False, smtp_code=None, mx_host=mx_host,
            catch_all=False, domain=domain, validated_at=now,
            validation_confidence=0.99, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
        )

    if not _get_cfg("enabled", False):
        return ValidationResult(
            email=email, status="Okay to Send", analysis="Accepted (SMTP disabled)",
            smtp_probed=False, smtp_code=None, mx_host=mx_host,
            catch_all=None, domain=domain, validated_at=now,
            validation_confidence=0.4, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
        )

    # Minimal fixed delay instead of full jittered sleep.
    time.sleep(0.05)

    ehlo_host, mail_from = _get_rotated_identity()
    use_starttls = _get_cfg("use_starttls", True)

    # Probe 1: catch-all detection
    code1, _msg1, lat1 = _smtp_probe_rcpt(
        mx_host=mx_host, ehlo_host=ehlo_host, mail_from=mail_from,
        rcpt_to=f"{''.join(random.choices(string.ascii_lowercase + string.digits, k=24))}@{domain}",
        timeout=timeout, use_starttls=use_starttls,
    )

    if code1 == 250:
        return ValidationResult(
            email=email, status="Do Not Send", analysis="Catch-All",
            smtp_probed=True, smtp_code=code1, mx_host=mx_host,
            catch_all=True, domain=domain, validated_at=now,
            validation_confidence=0.5, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
            validation_response=f"{code1} catch-all detected (pattern proof)",
            validation_latency_ms=lat1,
        )

    if code1 in (450, 451, 452):
        return ValidationResult(
            email=email, status="Maybe", analysis=f"SMTP Temp Fail ({code1})",
            smtp_probed=True, smtp_code=code1, mx_host=mx_host,
            catch_all=None, domain=domain, validated_at=now,
            validation_confidence=0.3, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
            validation_response=f"{code1} (greylist/quarantine)",
            validation_latency_ms=lat1,
        )

    if code1 is None:
        return ValidationResult(
            email=email, status="Maybe", analysis="SMTP Error",
            smtp_probed=True, smtp_code=None, mx_host=mx_host,
            catch_all=None, domain=domain, validated_at=now,
            validation_confidence=0.5, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
            validation_response="connection failed",
            validation_latency_ms=lat1,
        )

    # Probe 1 returned 550 (not catch-all). Probe the real address — no retry.
    code2, msg2, lat2 = _smtp_probe_rcpt(
        mx_host=mx_host, ehlo_host=ehlo_host, mail_from=mail_from,
        rcpt_to=email, timeout=timeout, use_starttls=use_starttls,
    )
    total_latency = int((time.monotonic() - overall_start) * 1000)

    if code2 == 250:
        return ValidationResult(
            email=email, status="Okay to Send", analysis="Accepted",
            smtp_probed=True, smtp_code=code2, mx_host=mx_host,
            catch_all=False, domain=domain, validated_at=now,
            validation_confidence=0.9, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
            validation_response=f"{code2} {msg2}" if msg2 else f"{code2}",
            validation_latency_ms=total_latency,
        )

    if code2 in (550, 553):
        return ValidationResult(
            email=email, status="Do Not Send", analysis="Not Found",
            smtp_probed=True, smtp_code=code2, mx_host=mx_host,
            catch_all=False, domain=domain, validated_at=now,
            validation_confidence=0.95, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
            validation_response=f"{code2} {msg2}" if msg2 else f"{code2}",
            validation_latency_ms=total_latency,
        )

    if code2 in (450, 451, 452):
        return ValidationResult(
            email=email, status="Maybe", analysis=f"SMTP Temp Fail ({code2})",
            smtp_probed=True, smtp_code=code2, mx_host=mx_host,
            catch_all=False, domain=domain, validated_at=now,
            validation_confidence=0.3, validation_method="smtp_live",
            validation_checked_at=now, validation_mx_host=mx_host or "",
            validation_response=f"{code2} (greylist/quarantine)",
            validation_latency_ms=total_latency,
        )

    return ValidationResult(
        email=email, status="Maybe", analysis="SMTP Error",
        smtp_probed=True, smtp_code=None, mx_host=mx_host,
        catch_all=False, domain=domain, validated_at=now,
        validation_confidence=0.5, validation_method="smtp_live",
        validation_checked_at=now, validation_mx_host=mx_host or "",
        validation_response="connection failed after retry",
        validation_latency_ms=total_latency,
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