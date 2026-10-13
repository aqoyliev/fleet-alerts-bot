# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The Telegram alerts bot for **MZ CARGO INC** (branch `mz-cargo`). It receives vehicle safety webhooks from **Samsara** and **Motive** — MZ runs both — stores each violation in PostgreSQL, and posts an alert to the company's **one** Telegram group and to the admins who asked for DMs. It sends a daily violations report at midnight ET and serves an admin **Mini App** from the same process.

## How this branch relates to the others

`mz-cargo` follows `hf`, which follows `single-company`. It was re-based onto hf by a merge (`Merge hf into mz-cargo: adopt it as the base`), so later work comes in with an ordinary `git merge origin/hf`. Merge `hf`, not `single-company` directly: hf is where single-company's per-truck driver groups have already been taken back out.

Everything hf does differently from single-company (one alerts group, crash goes to the group, mute only from the panel, the maintainer above the company's super admins, `/report` and `/top`, no PTI or unit rosters) holds here unchanged — see hf's `CLAUDE.md` for that table. What `mz-cargo` deliberately does differently **from hf** — keep these when resolving a merge:

| | `hf` | `mz-cargo` |
|---|---|---|
| Providers | Samsara only; the Motive path is carried and inert | **Samsara and Motive, both live** |
| Critical Motive `hard_brake` | promoted to a crash and held for confirmation | **stays a hard brake** (`_get_event_type`) — Motive files crashes as `crash` |
| Motive crash | card and clip in one message, after the clip wait | **card as soon as it is confirmed, clip as a follow-up** — like a Samsara crash |
| Withdrawn Motive crash | waits out the clip probes, finds nothing | **sent at once as a hard brake** (`_motive_clip_is_coming`) |
| Alert card | `🚛 Vehicle:` label, company name under a crash title, no provider | unit on its own unlabelled row, **no company name**, signed `via Samsara` / `via Motive` |
| Titles | HARSH BRAKE, ROLLING STOP | **HARD BRAKE, STOP SIGN VIOLATION** (cards, reports and panel chips) |
| Old-schema adoption | slug must match the `companies` row | a database of one company is that company's **whatever the slug says**; the company's **name** and the **crash-confirmation table** are carried over too |

Files that are MZ's own and should win a conflict: `utils/db_api/legacy.py`, `tests/test_legacy_schema.py`, `tests/test_event_card.py`, and in `utils/webhook_handler.py` the three places above (`EVENT_TYPE_MAP` titles, `_get_event_type`, `_format_event`) plus the crash branch around `_wait_for_motive_clip` in `_handle_event`. Everything else should take hf's side.

## Running the bot

```bash
pip install -r requirements.txt
python app.py
```

The bot starts an aiohttp server (webhooks + admin panel) on `$PORT` (default 8080) alongside the Telegram polling loop.

**Run tests** (needs `pip install -r requirements-dev.txt` — pytest-asyncio is what makes the async tests run):
```powershell
python -m pytest -q
```

On this machine the hf checkout's virtualenv has the right dependencies: `C:\Users\ALFA\.virtualenvs\hf-alerts-bot-96Gq4aZS\Scripts\python.exe`.

## Architecture

### Data flow

```
POST /webhook/samsara   or   POST /webhook/motive
     (the old build's forms — /webhook, /webhook/<slug>, /webhook/<provider>/<slug> — are still answered)
  → HMAC check                   (Samsara: SHA256 over v1:<ts>:<body> + timestamp freshness;
                                  Motive: SHA1 over the body, X-KT-Webhook-Signature;
                                  no secret configured = delivery REFUSED, unless ALLOW_UNSIGNED_WEBHOOKS)
  → in-memory dedup              (5-min TTL on the event id)
  → normalize to internal event dict
  → Samsara harsh events: background poll of the legacy harsh_event API for type, location, video
      · first typed response: violation saved; a crash is alerted at once ("Video pending…")
      · a type not in EVENT_TYPE_MAP ends the poll and drops the event
      · an empty record after the whole poll: DM-only, marked "Possible error"
  → Samsara speeding: speed / limit / location looked up from the Samsara API
  → Motive crash: held 180s, then confirmed against Motive's API (see below)
  → save_violation()             (UNIQUE event_id decides: already stored = no second alert)
  → Motive clip not ready yet: wait for it (probes at 2, 5, 10 min) — except a crash
  → the alerts group (if not muted and the type is on its filter) + subscribed admin DMs
```

The webhook always answers 200 immediately; `_handle_event()` runs as a background task.

### Key files

- `utils/webhook_handler.py` — all event processing: auth, dedup, parsing, the alert card, Telegram send with retries, webhook routes
- `utils/motive/__init__.py` — Motive's `driver_performance_events` lookup (crash confirmation, clip fetch)
- `utils/samsara/client.py` — speeding enrichment
- `utils/db_api/groups.py` — the one alerts group: seeding, mute, event filter, routing (`get_alert_target`)
- `utils/db_api/legacy.py` — reading the old multi-company schema on first boot (see Database)
- `utils/db_api/admins.py` — admins, hidden maintainer accounts, DM subscriptions
- `utils/db_api/violations.py` — save + every report/panel query
- `utils/db_api/crash_confirmations.py` — the durable record of each held Motive crash
- `utils/daily_report.py` — midnight ET report; `handlers/users/report.py` — `/report`, `/top`
- `handlers/groups/group_events.py` — `/help` in the group; logs the bot joining/leaving a chat (nothing else)
- `handlers/users/support.py` — ✉️ Contact Support relay to the maintainer
- `utils/webapp/` — the admin Mini App (`api.py` endpoints, `auth.py` initData check, `static/` frontend)
- `data/config.py` — all env var loading; `data/event_catalog.py` — the filterable event types

### The alerts group

A singleton row in `alert_group` (`id` pinned to 1). Seeded once by `ensure_group()`: from the old schema's `company_groups` if this database has one, else from `GROUP_CHAT_ID`. After that the row is authoritative — it is rewritten when Telegram migrates the chat to a supergroup, and the env var is never read again.

`alert_group_event_types` is the group's allowlist; **no rows = every type**. Admins edit it, and the mute, from the panel's Group screen. Nothing in Telegram mutes the group and nothing mutes it automatically.

### Motive

MZ CARGO is the one deployment in this family where Motive's deliveries are live, so this path is not hypothetical here the way it is on hf.

**Crash confirmation.** Motive fires its crash webhook the instant the detector trips, before its own review runs, then *withdraws* a rejected detection from `/v2/driver_performance_events` — it disappears rather than being reclassified. Measured 2026-07-29..08-01, 59 of 62 crash webhooks were gone afterwards. Presence in that API is therefore the only reliable classifier. A Motive `crash` waits `_CRASH_CONFIRM_DELAY` (180s) and then:

- **still listed** → alerted as a crash
- **withdrawn** → `_type_override='hard_brake'`, sent at once as a hard brake (no clip wait: the listing the clip would come from is gone)
- **lookup failed / no `MOTIVE_API_KEY`** → **fails open**: sent as a crash with `⚠️ Unconfirmed` on the card

A row is written to `motive_crash_confirmations` before the wait and its verdict filled in after; `resume_pending_crash_confirmations()` picks up anything still undecided at startup, so a deploy landing inside the wait does not lose the alert. Bookkeeping failures are swallowed (`_note_crash`) — losing the row is acceptable, losing the alert is not.

**Clips.** The webhook arrives before the dashcam clip is transcoded (`camera_media.available` true, every URL null). Motive re-delivers when it is ready, under the same id, which the durable dedup refuses — so the clip is fetched from the API instead, and the alert **waits for it** (probes at 120 / 300 / 600 s): one message with card and footage. A crash is the exception on this branch: its card goes out as soon as it is confirmed, with "Video pending…", and the clip (or "No video available for this crash.") follows. A restart during a clip wait loses that alert, or for a crash, its clip.

**A hard brake is a hard brake.** hf and single-company read a critical-severity Motive `hard_brake` as a crash; this branch does not, and `tests/test_motive_crash.py` holds it to that.

### Admins and the hidden maintainer

`ADMINS` (env) are the **maintainer's** ids, not the company's staff — on this deployment, Akbar alone. They are permanent super admins and are hidden from the panel: other admins cannot see, deactivate or remove them (`is_maintainer` / `visible_admins` in `utils/db_api/admins.py`). The company's own admins are added and removed in the panel's Admins screen.

The maintainer is also above the company's super admins. A super admin manages regular admins only and cannot touch another super admin; the maintainer can deactivate, demote (`demote_from_super`) and remove any of them (`update_admin` / `delete_admin_route` in `utils/webapp/api.py`). Refusals shown to a company super admin never mention that such an account exists.

Crash DMs are on by default for every admin (`admins.crash_dm`); every other type is opt-in under ⚙️ Settings.

## Database

Schema is in `utils/db_api/schemas.sql` and is applied on every boot (`run_migrations`) — all `CREATE … IF NOT EXISTS`, then the additive statements in `db.py::_MIGRATIONS`. No migrations framework.

Key tables: `violations` (`event_id` is the idempotency key), `alert_group` / `alert_group_event_types`, `users` / `admins` / `admin_subscriptions`, `motive_crash_confirmations`, `support_relays`.

**MZ's production database predates this schema**, and was built by hand from the old `schemas.sql` (the old build never applied it itself). It still holds `companies`, `company_groups`, `group_event_types`, `admin_companies`, and a NOT NULL `company_slug` on `violations` and on `motive_crash_confirmations`. On startup:

- `adopt_legacy_violations()` and `adopt_legacy_crash_confirmations()` drop NOT NULL on `company_slug` and give it the company's slug as its default (new inserts don't supply it);
- `adopt_legacy_company()` takes the display name from the `companies` row unless `COMPANY_NAME` is set;
- `ensure_group()` copies the group's chat id and event filter out of the old tables, once.

Which `companies` row is this deployment's is `legacy_company()`'s call: the one with `COMPANY_SLUG`, or — when none has it — the only one there is. The old build took the company from the webhook path when the path named one, so the variable was never held to the table; the slug has been spelled `mzcargo`, `mz_cargo` and `mz-cargo` in different places.

The old tables are only ever **read** — never altered or dropped — so the previous build still runs against this database if a deploy is rolled back. Don't "clean them up".

All queries use asyncpg `$1, $2` placeholders (never f-strings for values). Times are stored as UTC `timestamptz` and displayed in ET.

## Vehicle number normalization

`_clean_vehicle()` in `webhook_handler.py` strips a leading `unit` / `Unit #` / `unit:` label with `^unit[\s:#-]+` (case-insensitive) before the row is saved, and the alert card shows the same cleaned value. Older rows can be cleaned with:

```sql
UPDATE violations
SET vehicle_number = TRIM(REGEXP_REPLACE(vehicle_number, '^unit[\s:#-]+', '', 'i'))
WHERE vehicle_number ~* '^unit[\s:#-]+';
```

## Environment variables

| Var | Purpose |
|-----|---------|
| `BOT_TOKEN` | Telegram bot token |
| `ADMINS` | The maintainer's Telegram IDs (comma separated) — hidden, permanent super admins |
| `DATABASE_URL` | PostgreSQL connection string |
| `SAMSARA_API_KEY` | Samsara REST API key |
| `SAMSARA_WEBHOOK_SECRET` | HMAC secret for Samsara — **blank means deliveries are refused** |
| `MOTIVE_WEBHOOK_SECRET` | HMAC secret for Motive — **blank means deliveries are refused** |
| `MOTIVE_API_KEY` | Motive REST API key — crash confirmation and clips. Blank = every crash "Unconfirmed", no Motive video |
| `GROUP_CHAT_ID` | Seeds the alerts group on a fresh database only; ignored on an upgraded one |
| `COMPANY_SLUG` | Defaults to `mzcargo` |
| `COMPANY_NAME` | Unset = taken from the old schema, else `MZ CARGO INC` |
| `SPEEDING_MIN_SEVERITY` | Default `medium` |
| `WEBAPP_URL` | Panel base URL; blank on Railway (read from `RAILWAY_PUBLIC_DOMAIN`) |
| `ALLOW_UNSIGNED_WEBHOOKS` | `true` accepts unsigned deliveries — bring-up / emergency only |
| `PORT` | Server port (Railway sets it) |

`ip`, which the old build required, is no longer read.
