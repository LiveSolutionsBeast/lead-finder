# Session 3 / t4.2 — SMTP Validation Status Lifecycle

**Run started:** 2026-07-26T22:20:28.739454+00:00
**Run finished:** 2026-07-26T22:20:38.326947+00:00
**Server:** http://localhost:8798

## Summary

- Total cases tested: **3**
- Passed: **3**
- Failed: **0**

## Cases

| # | Test ID | Label | Pass | Time |
|---|---|---|---|---|
| 1 | `T42-1` | plugin push with no email → smtp_validation_status=NULL | ✓ | 0.27s |
| 2 | `T42-2` | plugin push WITH email ('rick@greenfieldpaper.com') → validator picks it up | ✓ | 7.47s |
| 3 | `T42-3` | existing contact #2042 updated with new email → validator picks it up | ✓ | 1.84s |

## Per-case detail

### T42-1 — plugin push with no email → smtp_validation_status=NULL

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | contact_id=2050 |
| `smtp_status_reasonable` | ✓ | smtp_validation_status=None |
| `email_empty` | ✓ | email=None |

### T42-2 — plugin push WITH email ('rick@greenfieldpaper.com') → validator picks it up

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | contact_id=2051 |
| `email_persisted` | ✓ | email='rick@greenfieldpaper.com' |
| `smtp_status_after_push` | ✓ | smtp_validation_status='Do Not Send' |
| `validation_method_after_push` | ✓ | validation_method='smtp_live' |
| `validator_wrote` | ✓ | smtp_validation_status='Do Not Send' |
| `validator_method_set` | ✓ | validation_method='smtp_live' |
| `validator_confidence_set` | ✓ | validation_confidence=0.95 |
| `validator_checked_at_set` | ✓ | validation_checked_at='2026-07-26T22:20:34.227382+00:00' |
| `validator_latency_set` | ✓ | validation_latency_ms=2243 |

### T42-3 — existing contact #2042 updated with new email → validator picks it up

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | contact_id=2042 |
| `same_contact_id` | ✓ | expected 2042 got 2042 |
| `email_updated` | ✓ | email='test-t42-222028@example.com' |
| `smtp_status_after_update` | ✓ | smtp_validation_status='Maybe' |
| `validator_wrote` | ✓ | smtp_validation_status='Maybe' |
| `validator_method_set` | ✓ | validation_method='smtp_live' |

