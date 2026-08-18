# Session 3 / t4.1 — End-to-End Test Results

**Run started:** 2026-07-27T01:37:25.272550+00:00
**Run finished:** 2026-07-27T01:37:36.498643+00:00
**Server:** http://localhost:8798

## Summary

- Total profiles tested: **20**
- Passed: **20**
- Failed: **0**

## Post-test DB state

- contacts total: **199**
- contacts with `linkedin_slug` set: **33** (was 0 before test)
- contact_experience rows: **33** (was 0 before test)
- pending_pushes rows: **119** (was 0 before test)

### Validation status distribution

| Status | Count |
|---|---|
| `None` | 162 |
| `Do Not Send` | 32 |
| `Maybe` | 3 |
| `Okay to Send` | 2 |

## Per-profile results

| # | Test ID | Action | Label | HTTP | Pass | Time |
|---|---|---|---|---|---|---|
| 1 | `U01` | `update_existing` | update_existing: Austal USA #1633 | 200 | ✓ | 0.04s |
| 2 | `U02` | `update_existing` | update_existing: Boeing Company #2019 | 200 | ✓ | 0.02s |
| 3 | `U03` | `update_existing` | update_existing: Boeing Company #2020 | 200 | ✓ | 0.02s |
| 4 | `U04` | `update_existing` | update_existing: Honeywell Aerospace #1482 | 200 | ✓ | 0.02s |
| 5 | `U05` | `update_existing` | update_existing: Honeywell Aerospace #1485 | 200 | ✓ | 0.02s |
| 6 | `U06` | `update_existing` | update_existing: Honeywell Aerospace #1486 | 200 | ✓ | 0.02s |
| 7 | `U07` | `update_existing` | update_existing: Honeywell Aerospace #1487 | 200 | ✓ | 0.02s |
| 8 | `U08` | `update_existing` | update_existing: Honeywell Aerospace #1488 | 200 | ✓ | 0.02s |
| 9 | `U09` | `update_existing` | update_existing: Honeywell Aerospace #1489 | 200 | ✓ | 0.02s |
| 10 | `U10` | `update_existing` | update_existing: NASSCO #1608 | 200 | ✓ | 0.02s |
| 11 | `N11` | `new_contact_existing_company` | new_contact_existing_company: Austal USA | 200 | ✓ | 0.02s |
| 12 | `N12` | `new_contact_existing_company` | new_contact_existing_company: SpaceX | 200 | ✓ | 0.03s |
| 13 | `N13` | `new_contact_existing_company` | new_contact_existing_company: Honeywell Aerospace | 200 | ✓ | 0.02s |
| 14 | `C14` | `new_company_new_contact` | new_company_new_contact: TransAstra Corp | 200 | ✓ | 0.01s |
| 15 | `C15` | `new_company_new_contact` | new_company_new_contact: Rocket Lab Ltd | 200 | ✓ | 0.01s |
| 16 | `C16` | `new_company_new_contact` | new_company_new_contact: Kairos Aerospace Inc | 200 | ✓ | 0.29s |
| 17 | `A17` | `update_existing` | auto-match by linkedin_slug: e2e-danad-14 | 200 | ✓ | 5.29s |
| 18 | `A18` | `update_existing` | auto-match by linkedin_slug: e2e-edmunde-15 | 200 | ✓ | 5.31s |
| 19 | `D19` | `discard` | discard: Greg Gagarin | 200 | ✓ | 0.01s |
| 20 | `D20` | `discard` | discard: Helena Hubble | 200 | ✓ | 0.01s |

## Per-profile detail

### U01 — update_existing: Austal USA #1633

- HTTP: `200` (elapsed 0.04s)
- Response: `{"ok": true, "contact_id": 1633, "company_id": 812, "matched_action": "update_existing", "smtp_validation_status": "Do Not Send", "pending_push_id": 149}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=update_existing |
| `smtp_status` | ✓ | smtp_validation_status='Do Not Send' |
| `contact_id_present` | ✓ | contact_id=1633 |
| `contact_in_db` | ✓ | contact #1633 row found |
| `same_contact_id` | ✓ | contact #1633 matched |
| `same_company_id` | ✓ | company_id=812 matched |
| `linkedin_slug_set` | ✓ | linkedin_slug='craig-perciavalle' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #149 committed_at set |

### U02 — update_existing: Boeing Company #2019

- HTTP: `200` (elapsed 0.02s)
- Response: `{"ok": true, "contact_id": 2019, "company_id": 611, "matched_action": "update_existing", "smtp_validation_status": "Do Not Send", "pending_push_id": 150}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=update_existing |
| `smtp_status` | ✓ | smtp_validation_status='Do Not Send' |
| `contact_id_present` | ✓ | contact_id=2019 |
| `contact_in_db` | ✓ | contact #2019 row found |
| `same_contact_id` | ✓ | contact #2019 matched |
| `same_company_id` | ✓ | company_id=611 matched |
| `linkedin_slug_set` | ✓ | linkedin_slug='jane-m-87245479' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #150 committed_at set |

### U03 — update_existing: Boeing Company #2020

- HTTP: `200` (elapsed 0.02s)
- Response: `{"ok": true, "contact_id": 2020, "company_id": 611, "matched_action": "update_existing", "smtp_validation_status": "Do Not Send", "pending_push_id": 151}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=update_existing |
| `smtp_status` | ✓ | smtp_validation_status='Do Not Send' |
| `contact_id_present` | ✓ | contact_id=2020 |
| `contact_in_db` | ✓ | contact #2020 row found |
| `same_contact_id` | ✓ | contact #2020 matched |
| `same_company_id` | ✓ | company_id=611 matched |
| `linkedin_slug_set` | ✓ | linkedin_slug='jay-malave-69a33748' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #151 committed_at set |

### U04 — update_existing: Honeywell Aerospace #1482

- HTTP: `200` (elapsed 0.02s)
- Response: `{"ok": true, "contact_id": 1482, "company_id": 591, "matched_action": "update_existing", "smtp_validation_status": "Do Not Send", "pending_push_id": 152}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=update_existing |
| `smtp_status` | ✓ | smtp_validation_status='Do Not Send' |
| `contact_id_present` | ✓ | contact_id=1482 |
| `contact_in_db` | ✓ | contact #1482 row found |
| `same_contact_id` | ✓ | contact #1482 matched |
| `same_company_id` | ✓ | company_id=591 matched |
| `linkedin_slug_set` | ✓ | linkedin_slug='george-armenta-1a58607a' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #152 committed_at set |

### U05 — update_existing: Honeywell Aerospace #1485

- HTTP: `200` (elapsed 0.02s)
- Response: `{"ok": true, "contact_id": 1485, "company_id": 591, "matched_action": "update_existing", "smtp_validation_status": "Do Not Send", "pending_push_id": 153}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=update_existing |
| `smtp_status` | ✓ | smtp_validation_status='Do Not Send' |
| `contact_id_present` | ✓ | contact_id=1485 |
| `contact_in_db` | ✓ | contact #1485 row found |
| `same_contact_id` | ✓ | contact #1485 matched |
| `same_company_id` | ✓ | company_id=591 matched |
| `linkedin_slug_set` | ✓ | linkedin_slug='paul-vidano-b6414a111' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #153 committed_at set |

### U06 — update_existing: Honeywell Aerospace #1486

- HTTP: `200` (elapsed 0.02s)
- Response: `{"ok": true, "contact_id": 1486, "company_id": 591, "matched_action": "update_existing", "smtp_validation_status": "Do Not Send", "pending_push_id": 154}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=update_existing |
| `smtp_status` | ✓ | smtp_validation_status='Do Not Send' |
| `contact_id_present` | ✓ | contact_id=1486 |
| `contact_in_db` | ✓ | contact #1486 row found |
| `same_contact_id` | ✓ | contact #1486 matched |
| `same_company_id` | ✓ | company_id=591 matched |
| `linkedin_slug_set` | ✓ | linkedin_slug='cheryl-dimaria-5229bb8' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #154 committed_at set |

### U07 — update_existing: Honeywell Aerospace #1487

- HTTP: `200` (elapsed 0.02s)
- Response: `{"ok": true, "contact_id": 1487, "company_id": 591, "matched_action": "update_existing", "smtp_validation_status": "Do Not Send", "pending_push_id": 155}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=update_existing |
| `smtp_status` | ✓ | smtp_validation_status='Do Not Send' |
| `contact_id_present` | ✓ | contact_id=1487 |
| `contact_in_db` | ✓ | contact #1487 row found |
| `same_contact_id` | ✓ | contact #1487 matched |
| `same_company_id` | ✓ | company_id=591 matched |
| `linkedin_slug_set` | ✓ | linkedin_slug='gregory-bopp-7b88b649' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #155 committed_at set |

### U08 — update_existing: Honeywell Aerospace #1488

- HTTP: `200` (elapsed 0.02s)
- Response: `{"ok": true, "contact_id": 1488, "company_id": 591, "matched_action": "update_existing", "smtp_validation_status": "Do Not Send", "pending_push_id": 156}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=update_existing |
| `smtp_status` | ✓ | smtp_validation_status='Do Not Send' |
| `contact_id_present` | ✓ | contact_id=1488 |
| `contact_in_db` | ✓ | contact #1488 row found |
| `same_contact_id` | ✓ | contact #1488 matched |
| `same_company_id` | ✓ | company_id=591 matched |
| `linkedin_slug_set` | ✓ | linkedin_slug='patrick-n-young-a0833672' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #156 committed_at set |

### U09 — update_existing: Honeywell Aerospace #1489

- HTTP: `200` (elapsed 0.02s)
- Response: `{"ok": true, "contact_id": 1489, "company_id": 591, "matched_action": "update_existing", "smtp_validation_status": "Do Not Send", "pending_push_id": 157}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=update_existing |
| `smtp_status` | ✓ | smtp_validation_status='Do Not Send' |
| `contact_id_present` | ✓ | contact_id=1489 |
| `contact_in_db` | ✓ | contact #1489 row found |
| `same_contact_id` | ✓ | contact #1489 matched |
| `same_company_id` | ✓ | company_id=591 matched |
| `linkedin_slug_set` | ✓ | linkedin_slug='bob-norazni-7085b7a4' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #157 committed_at set |

### U10 — update_existing: NASSCO #1608

- HTTP: `200` (elapsed 0.02s)
- Response: `{"ok": true, "contact_id": 1608, "company_id": 807, "matched_action": "update_existing", "smtp_validation_status": "Do Not Send", "pending_push_id": 158}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=update_existing |
| `smtp_status` | ✓ | smtp_validation_status='Do Not Send' |
| `contact_id_present` | ✓ | contact_id=1608 |
| `contact_in_db` | ✓ | contact #1608 row found |
| `same_contact_id` | ✓ | contact #1608 matched |
| `same_company_id` | ✓ | company_id=807 matched |
| `linkedin_slug_set` | ✓ | linkedin_slug='steve-davison-89a4bb3b' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #158 committed_at set |

### N11 — new_contact_existing_company: Austal USA

- HTTP: `200` (elapsed 0.02s)
- Response: `{"ok": true, "contact_id": 2055, "company_id": 812, "matched_action": "new_contact_existing_company", "smtp_validation_status": "Do Not Send", "pending_push_id": 159}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=new_contact_existing_company |
| `smtp_status` | ✓ | smtp_validation_status='Do Not Send' |
| `contact_id_present` | ✓ | contact_id=2055 |
| `contact_in_db` | ✓ | contact #2055 row found |
| `same_company_id` | ✓ | company_id=812 matched |
| `linkedin_slug_set` | ✓ | linkedin_slug='e2e-austal-usa-aldrin-11' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #159 committed_at set |

### N12 — new_contact_existing_company: SpaceX

- HTTP: `200` (elapsed 0.03s)
- Response: `{"ok": true, "contact_id": 2056, "company_id": 614, "matched_action": "new_contact_existing_company", "smtp_validation_status": "Do Not Send", "pending_push_id": 160}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=new_contact_existing_company |
| `smtp_status` | ✓ | smtp_validation_status='Do Not Send' |
| `contact_id_present` | ✓ | contact_id=2056 |
| `contact_in_db` | ✓ | contact #2056 row found |
| `same_company_id` | ✓ | company_id=614 matched |
| `linkedin_slug_set` | ✓ | linkedin_slug='e2e-spacex-buzz-12' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #160 committed_at set |

### N13 — new_contact_existing_company: Honeywell Aerospace

- HTTP: `200` (elapsed 0.02s)
- Response: `{"ok": true, "contact_id": 2057, "company_id": 591, "matched_action": "new_contact_existing_company", "smtp_validation_status": "Do Not Send", "pending_push_id": 161}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=new_contact_existing_company |
| `smtp_status` | ✓ | smtp_validation_status='Do Not Send' |
| `contact_id_present` | ✓ | contact_id=2057 |
| `contact_in_db` | ✓ | contact #2057 row found |
| `same_company_id` | ✓ | company_id=591 matched |
| `linkedin_slug_set` | ✓ | linkedin_slug='e2e-honeywell-aerospace-carla-13' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #161 committed_at set |

### C14 — new_company_new_contact: TransAstra Corp

- HTTP: `200` (elapsed 0.01s)
- Response: `{"ok": true, "contact_id": 2058, "company_id": 1565, "matched_action": "new_company_new_contact", "smtp_validation_status": null, "pending_push_id": 162}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=new_company_new_contact |
| `smtp_status` | ✓ | smtp_validation_status=None |
| `contact_id_present` | ✓ | contact_id=2058 |
| `contact_in_db` | ✓ | contact #2058 row found |
| `linkedin_slug_set` | ✓ | linkedin_slug='e2e-danad-14' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #162 committed_at set |

### C15 — new_company_new_contact: Rocket Lab Ltd

- HTTP: `200` (elapsed 0.01s)
- Response: `{"ok": true, "contact_id": 2059, "company_id": 1566, "matched_action": "new_company_new_contact", "smtp_validation_status": null, "pending_push_id": 163}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=new_company_new_contact |
| `smtp_status` | ✓ | smtp_validation_status=None |
| `contact_id_present` | ✓ | contact_id=2059 |
| `contact_in_db` | ✓ | contact #2059 row found |
| `linkedin_slug_set` | ✓ | linkedin_slug='e2e-edmunde-15' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #163 committed_at set |

### C16 — new_company_new_contact: Kairos Aerospace Inc

- HTTP: `200` (elapsed 0.29s)
- Response: `{"ok": true, "contact_id": 2060, "company_id": 1567, "matched_action": "new_company_new_contact", "smtp_validation_status": null, "pending_push_id": 164}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=new_company_new_contact |
| `smtp_status` | ✓ | smtp_validation_status=None |
| `contact_id_present` | ✓ | contact_id=2060 |
| `contact_in_db` | ✓ | contact #2060 row found |
| `linkedin_slug_set` | ✓ | linkedin_slug='e2e-fionaf-16' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #164 committed_at set |

### A17 — auto-match by linkedin_slug: e2e-danad-14

- HTTP: `200` (elapsed 5.29s)
- Response: `{"ok": true, "contact_id": 2058, "company_id": 1565, "matched_action": "update_existing", "smtp_validation_status": null, "pending_push_id": 165}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=update_existing |
| `smtp_status` | ✓ | smtp_validation_status=None |
| `contact_id_present` | ✓ | contact_id=2058 |
| `contact_in_db` | ✓ | contact #2058 row found |
| `linkedin_slug_set` | ✓ | linkedin_slug='e2e-danad-14' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #165 committed_at set |

### A18 — auto-match by linkedin_slug: e2e-edmunde-15

- HTTP: `200` (elapsed 5.31s)
- Response: `{"ok": true, "contact_id": 2059, "company_id": 1566, "matched_action": "update_existing", "smtp_validation_status": null, "pending_push_id": 166}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=update_existing |
| `smtp_status` | ✓ | smtp_validation_status=None |
| `contact_id_present` | ✓ | contact_id=2059 |
| `contact_in_db` | ✓ | contact #2059 row found |
| `linkedin_slug_set` | ✓ | linkedin_slug='e2e-edmunde-15' |
| `source_verified` | ✓ | source_linkedin_verified=1 |
| `experience_count` | ✓ | 1 experience rows |
| `audit_committed` | ✓ | pending_pushes #166 committed_at set |

### D19 — discard: Greg Gagarin

- HTTP: `200` (elapsed 0.01s)
- Response: `{"ok": true, "contact_id": null, "company_id": null, "matched_action": "discard", "smtp_validation_status": null}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=discard |
| `smtp_status` | ✓ | smtp_validation_status=None |
| `discard_no_ids` | ✓ | contact_id=None, company_id=None |
| `discard_audit` | ✓ | pending_pushes row #167 |

### D20 — discard: Helena Hubble

- HTTP: `200` (elapsed 0.01s)
- Response: `{"ok": true, "contact_id": null, "company_id": null, "matched_action": "discard", "smtp_validation_status": null}`

| Assertion | Pass | Detail |
|---|---|---|
| `http_200` | ✓ | 200 OK |
| `ok_true` | ✓ | ok=true |
| `matched_action` | ✓ | action=discard |
| `smtp_status` | ✓ | smtp_validation_status=None |
| `discard_no_ids` | ✓ | contact_id=None, company_id=None |
| `discard_audit` | ✓ | pending_pushes row #168 |

