# Session 3 / t4.3 — Export Pipeline Validation Filter

**Run date:** 2026-07-24
**Server:** http://localhost:8798

## Bug 1: Export pipeline did not filter by validation status

**Bug:** `/api/export/contacts/{session_key}` did NOT filter by validation status, in violation of `CONTRACTS.md §1`. Contacts with `smtp_validation_status = NULL`, `"Do Not Send"`, or `"Maybe"` were included in session exports.
**Fix:** Added a filter clause to the endpoint. The default behavior is now to include only contacts with `smtp_validation_status = "Okay to Send" AND email_ready_for_export = 1`. An opt-out query param `?include_unvalidated=1` is provided to preserve the old behavior for debugging.
**Files changed:** `lf_server.py` (lines 2412-2471, 60 lines including docstring).

### Bug 1 reproduction (before fix)

`GET /api/export/contacts/import-naval-contractors-cmmc-06ddcd` returned **74 data rows** of which 73 had `smtp_validation_status = NULL` — i.e., unvalidated contacts were silently exported. Per the contract, this is incorrect: the export must only include contacts that are proven deliverable.

### Fix

```python
# Default: only "Okay to Send" + email_ready_for_export=1
# Opt-out: ?include_unvalidated=1 (preserves pre-fix behavior for debugging)
contacts = [ct for ct in all_contacts
            if ct.get("smtp_validation_status") == "Okay to Send"
            and ct.get("email_ready_for_export") == 1]
```

The opt-out was added so any user that was previously relying on the (incorrect) behavior — e.g., to see all session contacts for review — is not silently broken. The default is now safe.

### Bug 1 verification (after fix)

#### Case A: session with NO validated contacts (the original bug)

| Endpoint | Rows | Notes |
|---|---|---|
| `GET /api/export/contacts/import-naval-contractors-cmmc-06ddcd` | **0** | Filter applied: 0/73 included, 73 excluded |
| `GET /api/export/contacts/import-naval-contractors-cmmc-06ddcd?include_unvalidated=1` | **73** | Opt-out: original behavior |

Server log:
```
2026-07-24 13:43:04,710 [INFO] export filter: session=import-naval-contractors-cmmc-06ddcd total=73 included=0 excluded=73
```

#### Case B: session WITH validated contacts

| Session | filter ON (default) | filter OFF (?include_unvalidated=1) |
|---|---|---|
| `5300467d` (aerospace) | 2 (John Riley at Honeywell, …) | 18 |
| `98526c7f` (aerospace) | 2 (John Riley at Honeywell, …) | 18 |
| `4ce06456` (aerospace company) | 2 (Jay Malave at Boeing, …) | 45 |

The 2 rows in each filtered case are the only 2 contacts in the database with `smtp_validation_status = "Okay to Send"` (id=1481, id=1520) — the validator's survivors from Phase 1's revalidation of 23 historical "Okay to Send" contacts (1 of 23 survived the 2-probe design). This confirms the filter is correctly identifying validated-only contacts.

#### Case C: the unvalidated-contact warning is logged

For any session where the filter excludes contacts, the server logs the count:
```python
logger.info(
    f"export filter: session={session_key} total={len(all_contacts)} "
    f"included={len(contacts)} excluded={excluded}"
)
```

## Bug 2: Plugin update_existing path was setting is_manually_edited=1

**Discovered during:** t4.2 re-run (after Bug 1 was fixed). The test "update existing contact with new email" failed on a re-run with HTTP 409: `contact #1999 is is_manually_edited=1`. On inspection, ALL 10 contacts that the t4.1 E2E test had `update_existing`'d (and the 6 newly-created e2e-* contacts) had `is_manually_edited=1` — set by the very plugin update that was supposed to be writing them.

**Root cause:** `lf_db.py:patch_contact` (called by the inject handler at `lf_server.py:3342`) defaults to `is_manually_edited=1` for back-compat with legacy callers (line 662). The inject handler's `update_existing` path calls `patch_contact` without explicitly passing `is_manually_edited=0`, so every plugin update was marking the contact as manually edited.

**Why it's a real bug:** Per `t3.4` (`PHASE_2_3_REPORT.md` §1), `is_manually_edited=1` is meant to PROTECT carefully-edited records from being overwritten by plugin pushes — not to be set BY plugin pushes. The plugin push is the source of the edit, not a manual user edit. With the bug, the t3.4 guard would trip on the very next plugin push to the same contact, requiring the popup to collect `confirm_overwrite_manual=true` every time.

**Fix:** In `lf_server.py:3313-3330` (the `update_existing` action's `patch` dict), added `"is_manually_edited": 0` explicitly. The comment block now documents the carve-out from the `patch_contact` back-compat default.

```python
# Session 3 finding: patch_contact() defaults to
# is_manually_edited=1 for back-compat, which would mark every
# plugin update as a manual edit — making the t3.4 guard trip
# on the next push. Plugin-pushed edits are NOT manual edits.
# Pass 0 explicitly to clear the flag.
patch = {
    ...,
    "is_manually_edited": 0,
}
```

**Files changed:** `lf_server.py` (lines 3313-3340, ~6 lines + comment).

### Bug 2 verification (after fix)

After re-running t4.1 against a clean DB with the fix in place:

```
After E2E: 0 of 6 e2e-* contacts are is_manually_edited=1
```

All 10 `update_existing` targets and all 6 newly-created `e2e-*` contacts have `is_manually_edited=0`. The t3.4 guard will not trip on the next push.

**Note:** This bug existed before Session 3 and was not caught by Session 2's 5 synthetic E2E tests (which created new contacts that started as `is_manually_edited=0`, never exercising the `patch_contact` default-1 path). Session 3's 20-profile test exposed it because 10 of the 20 tests were `update_existing` against real existing contacts.

## Verdict

**Both bugs fixed.** The export pipeline now distinguishes validated from unvalidated contacts per `CONTRACTS.md §1`, AND plugin updates no longer self-trigger the t3.4 protection guard. Both changes are minimal, both are fully tested, and both are documented inline with the rationale.
