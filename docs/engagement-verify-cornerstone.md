# Engagement Verification — Cornerstone Workflow Runbook

> **Purpose:** Prove a company's email pattern by sending ONE cold email to a
> low-level "cornerstone" contact, watching the tracking pixel / bounce status,
> then auto-applying the proven pattern to every sibling contact and pushing
> the validated leads to **EspoCRM** (default) or, with `USE_SALESFORCE=1`,
> to Salesforce — all from OWUI chat or cron.
>
> **Audience:** Business Development operators using the Live Solutions
> lead-finder + lead-seeker stack.
>
> **Status:** Production. Cron watcher installed (every 2h). Graph delivery
> poller installed (every 5 min). Real sends verified (Gmail inbox, SPF/DKIM/
> DMARC pass). Bounce diagnosis complete.

---

## 1. The big picture

```
lead-finder (lf.db)                lead-seeker (track.db)            EspoCRM (default)
  companies ──┐                       sends ──┐                       Lead
  contacts ───┤   cornerstone send ──► pixel ──┤   auto-push ──►      Account
  engagement_ ┤                       NDR  ───┤
  verify_queue┘                       opens ───┘
  engagement_company_runs
```

1. **Pick** one low-level contact per unproven company (the "cornerstone").
2. **Send** a short cold-intro email with a tracking pixel to ≤3 pattern
   candidates for that contact (hub = `lead-finder-verify`).
3. **Wait** 24–72h. A cron watcher polls `track.db` every 2h; a Graph delivery
   poller runs every 5 min to capture bounces (NDR) and opens.
4. **On proof** (delivered/opened → "Okay to Send"), the proven pattern is
   auto-applied to every sibling contact at the company.
5. **Auto-push** the validated contacts + the company to Salesforce (Account +
   Leads). A separate `--confirm` step (or `ENGAGEMENT_VERIFY_AUTOPUSH_SF=1`)
   actually pushes; the watcher never auto-confirms SF by default.

---

## 2. Key files

| File | Role |
|---|---|
| `lead-finder/scripts/engagement_verify.py` | The CLI. Picker, enqueuer, sender, checker, pattern-apply, SF push, watcher. All safety gates live here. |
| `lead-finder/lf_db.py` | Schema. `engagement_company_runs` table + cornerstone/SF columns on `contacts`/`companies`. |
| `lead-finder/lf_email_patterns.py` | `generate_candidates()`, `derive_email()`, `extract_domain_from_website()`. |
| `lead-seeker/live_solutions_outreach.py` | `send_email()` — MS Graph sendMail + tracker registration + InternetMessageId capture. |
| `lead-seeker/track.db` | `sends` table: token, opened_at, bounce_status, delivery_status, internet_message_id. |
| `lead-seeker/track_server.py` | Pixel endpoint + register endpoint. Allowlist `TRACKER_ALLOWED_HUBS` must include `lead-finder-verify`. |
| `lead-seeker/poll_graph_delivery.py` | Conditional poller. NDR inbox scan + per-token Sent Items lookup. Cron every 5 min. |
| `ai-stack/templates/email/live-solutions-cornerstone-verify.html` | The cold-intro template. Short, imagination-first, meeting-forward. |
| `ai-stack/scripts/ops/b7_salesforce_module.py` | `create_sf_lead()` — used by the auto-push (server-to-server, no user context). |

---

## 3. The OWUI chat interface (turnkey)

The `lf_email_pipeline` OWUI workspace tool now accepts **engagement-verify
actions** in addition to the legacy discover/derive/validate actions. Call it
from chat:

| Chat request | `action` | Extra params |
|---|---|---|
| "Show me all active engagement runs" | `ev_status_all` | — |
| "What's the status of company 641?" | `ev_status` | `company_id=641` |
| "Pick a cornerstone for company 641" | `ev_pick` | `company_id=641` |
| "Stage a cornerstone batch for company 641" | `ev_stage` | `company_id=641` |
| "Send the staged cornerstone batch" | `ev_send` | `plan_file` (path) |
| "Check results for company 641" | `ev_check` | `company_id=641` |
| "Apply the proven pattern for company 641" | `ev_apply` | `company_id=641` |
| "Push company 641 to EspoCRM (dry-run)" | `ev_push_espo` | `company_id=641` |
| "Push company 641 to EspoCRM (real)" | `ev_push_espo` | `company_id=641`, `confirm=True` |
| "Push company 641 to Salesforce (LEGACY dry-run)" | `ev_push_sf` | `company_id=641` (requires `USE_SALESFORCE=1`) |
| "Run the watcher once" | `ev_watchdog` | — |

All `ev_*` actions shell out to `engagement_verify.py` and return the JSON
output as a Markdown-formatted summary. **Safety gates are inherited from the
CLI**: dry-run by default, `confirm=True` required for real SF pushes, and
`ENGAGEMENT_VERIFY_AUTOSEND=1` required for auto-sends.

---

## 4. CLI quick reference

```bash
cd /home/anthonyturgman/lead-finder

# Status
python3 scripts/engagement_verify.py --status-all
python3 scripts/engagement_verify.py --status --company-id 641

# Pick + stage (dry-run safe)
python3 scripts/engagement_verify.py --pick-cornerstone --company-id 641 --dry-run
python3 scripts/engagement_verify.py --stage-cornerstone --company-id 641 -o /tmp/c641.json

# Send after review (approves automatically with --send)
python3 scripts/engagement_verify.py --send /tmp/c641.json --throttle 60.0

# Check results after 24h
python3 scripts/engagement_verify.py --check-company --company-id 641 --wait-hours 24

# Apply proven pattern to siblings (writes lf.db only)
python3 scripts/engagement_verify.py --apply-proven --company-id 641

# EspoCRM push (dry-run first, then confirm) — DEFAULT destination
python3 scripts/engagement_verify.py --push-espo --company-id 641 --dry-run
python3 scripts/engagement_verify.py --push-espo --company-id 641 --confirm

# LEGACY: Salesforce push (only when USE_SALESFORCE=1 in .env — DO NOT USE)
# python3 scripts/engagement_verify.py --push-sf --company-id 641 --dry-run
# python3 scripts/engagement_verify.py --push-sf --company-id 641 --confirm

# Watcher (cron uses --once; dry-run by default)
python3 scripts/engagement_verify.py --watchdog --once
```

---

## 5. Safety gates (read this before enabling auto-send)

| Gate | Default | How to enable |
|---|---|---|
| Dry-run | ON everywhere | `dry_run=False` + `confirm=True` for real Espo push |
| Auto-send (cornerstone) | OFF | `ENGAGEMENT_VERIFY_AUTOSEND=1` in `~/.env` |
| Auto-push to EspoCRM | OFF | `ENGAGEMENT_VERIFY_AUTOPUSH_ESPO=1` in `~/.env` (watcher still dry-runs Espo unless this is set; default behavior) |
| Per-company cap | 3 proof emails / 7 days | Hard-coded in `enqueue_cornerstone_verification` |
| Per-batch cap | 20 emails | Hard-coded in `send_approved_batch` |
| Title-rank floor | Senior titles blocked | `SENIOR_TITLE_BLOCKLIST` (CEO, VP, Director, President, Founder, Owner, Partner, Chairman) |
| Cornerstone cooldown | 96h | `companies.cornerstone_locked_until` |
| Tracker reachability | Required | Batch aborts if `register_send_to_tracker` fails |
| Throttle | 60s | `--throttle 60.0` (was 8s; raised after deliverability work). The cornerstone-send code floor was inadvertently left at 8s — fixed 2026-08-12 (engagement_verify.py:1091, 1190, 1979) to match this runbook. |

**To go live with auto-send:**
1. Run 2–3 successful dry-run cycles (`--watchdog --once`).
2. Review the staged plans and sent emails.
3. Add `ENGAGEMENT_VERIFY_AUTOSEND=1` to `~/.env`.
4. The watcher will then send cornerstone proof emails automatically.
5. Add `ENGAGEMENT_VERIFY_AUTOPUSH_ESPO=1` only after you've reviewed the
   pattern-apply results and are confident the proven patterns are correct
   (Espo is the default destination as of 2026-08-11).

---

## 6. The state machine (`engagement_company_runs.state`)

```
queued ──send──► sent ──proof──► proof_succeeded ──sf push──► sf_pushed
                   │                                          │
                   └──bounce──► proof_failed ───────────────► sf_failed
```

- `queued` — batch staged, not sent yet.
- `sent` — emails sent via Graph, waiting for proof (24–72h).
- `proof_succeeded` — at least one candidate delivered/opened; pattern promoted.
- `proof_failed` — all candidates bounced; company locked 24h.
- `sf_pushed` — LEGACY. Account + Leads created in Salesforce (only reachable when `USE_SALESFORCE=1`).
- `sf_failed` — SF push errored; check `last_error`.

Use `--status --company-id N` to see the current state + queue counts.

---

## 7. Deliverability findings (baked in)

These were learned the hard way during the real-send testing phase:

- **SPF / DKIM / DMARC all pass** on external sends. Graph sendMail DKIM-signs
  outbound mail. Same-domain internal sends omit DKIM by design (Microsoft).
- **DMARC record** is `v=DMARC1; p=none; rua=mailto:dmarc@fbl.optin.com; pct=100;`
  (monitor mode while DKIM stabilizes). Was previously malformed (two `v=DMARC1`
  tags).
- **Sender reputation is clean** — Barracuda Central, Spamhaus ZEN, URIBL,
  SORBS, SpamCop all clear. The `127.255.255.254` from Spamhaus was a
  public-resolver block (1.1.1.1/8.8.8.8), not a real listing.
- **Gmail deliverability confirmed** — test email to asturgman@gmail.com landed
  clean in the inbox.
- **Barracuda mail servers reject content-based spam** even when SPF/DKIM/DMARC
  pass (Cosmetic Group USA test). This is a content filter, not an auth failure.
  The cornerstone template was shortened to reduce this risk.
- **Throttle is 60s** for cornerstone sends (was 8s). Higher-stakes than bulk.
- **Token mismatch bug fixed** — `send_email` generates its own tracker token;
  the queue now stores it in `tracker_token` (backfilled after send).
  `check_results` uses `tracker_token` (falls back to `token`).
- **`lead-finder-verify` hub** must be in `TRACKER_ALLOWED_HUBS` on the track
  server systemd env, or pixels return 404.

---

## 8. Cron jobs

```cron
# Engagement watchdog — polls track.db, applies proven patterns, stages SF push
0 */2 * * * cd /home/anthonyturgman/lead-finder && /usr/bin/python3 scripts/engagement_verify.py --watchdog --once >> logs/engagement_watchdog.log 2>&1

# Graph delivery poller — captures NDRs + opens from track.db
*/5 * * * * cd /home/anthonyturgman/lead-seeker && /usr/bin/python3 poll_graph_delivery.py --hub lead-finder-verify --wait 60 >> logs/poll_graph_delivery.log 2>&1
```

The watchdog is dry-run by default. To enable auto-send + auto-push, set the
env vars in `~/.env` and restart the cron environment (or reboot).

---

## 9. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Pixel returns 404 | `lead-finder-verify` not in `TRACKER_ALLOWED_HUBS` | `sudo systemctl edit ls-track-server.service`, add to env, `sudo systemctl restart ls-track-server` |
| All candidates bounce "user not found" (550) | Contact doesn't exist at that domain | The lead was hallucinated. Remove the company and move on. |
| All candidates bounce "spam content rejected" | Barracuda content filter | Shorten the template; this is not an auth issue. |
| `check_results` shows pending forever | Token mismatch | Verify `tracker_token` column is populated in `engagement_verify_queue`. |
| Watcher doesn't send | `ENGAGEMENT_VERIFY_AUTOSEND` not set | Add to `~/.env`, restart cron. |
| SF push dry-run works but confirm fails | SF auth expired | Check `b7_salesforce_module` logs; refresh token. |
| Duplicate cornerstone for one company | Race condition | `idx_companies_cornerstone` unique index prevents this; second pick is a no-op. |

---

## 10. Roadmap (next phase)

The user's stated next step: **leverage the existing leads.livesolutionsnow.com
platform** to run this workflow and deprecate the manual CLI / OWUI methods
once the platform path is complete. The engagement-verify logic in
`scripts/engagement_verify.py` is the reference implementation; the platform
will wrap the same `lf.db` + `track.db` + Graph + SF calls behind a web UI.

Until then, the OWUI `lf_email_pipeline` tool (with the new `ev_*` actions) is
the turnkey chat interface.