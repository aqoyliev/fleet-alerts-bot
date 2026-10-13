# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The Telegram alerts bot for a company that has **one alerts group** (branch `single-group`). It receives vehicle safety webhooks from **Samsara** and/or **Motive**, stores each violation in PostgreSQL, and posts an alert to the company's one Telegram group and to the admins who asked for DMs. It sends a daily violations report at midnight ET and serves an admin **Mini App** from the same process.

**One branch, several companies.** Each company is its own Railway project with its own service, database and bot token, all deploying this branch. Nothing in the code names a company: what differs between them is that service's variables and, for the ones that came from an older build, what is in its database.

| | HAULAGE FREIGHT (`hf`) | MZ CARGO (`mzcargo`) | JTR (`jtr`) |
|---|---|---|---|
| Railway project / service | `hf` / `web` | `mz-cargo` / `gallant-hope` | `jtr` / `web` |
| Providers | Samsara | Samsara + Motive | Motive |
| `EXTRA_EVENT_TYPES` | — | — | the two Motive camera obstructions, `obstructed_camera`, `seat_belt_violation`, `near_miss`, `drowsiness` |
| `SPEEDING_MIN_SEVERITY` | — (`medium`) | — (`medium`) | `high` |
| Crash to the group | yes | yes | **no** — DMs only (its group filter has crash off) |
| Database came from | the multi-company schema | the multi-company schema | the old single-group template |
| Company name from | its old `companies` row | its old `companies` row | `COMPANY_NAME` |

A push to `single-group` deploys **all of them**. A change meant for one company goes behind a variable, never behind an `if` on the company.

### What follows from the providers (nothing to configure)

`data/config.py` reads which providers a deployment has keys for (`SAMSARA_ENABLED`, `MOTIVE_ENABLED`) and derives two things:

- **Wording** (`EVENT_WORDING`). Samsara says "Harsh Brake" and "Rolling Stop"; Motive says "Hard Brake" and "Stop Sign Violation". Reports and the panel use Motive's names on a fleet with any Motive in it, Samsara's otherwise. An **alert card** always uses the names of the provider that reported that event (`event_title(type, source)`).
- **The provider line** (`SHOW_PROVIDER`). A card ends with `via Samsara` / `via Motive` only where there are two providers.

### How this branch relates to the others

`single-group` is `single-company` with the per-truck driver groups taken out. It replaced three branches — `hf`, `mz-cargo` and an older `single-group` template — and is a descendant of all of them. What it deliberately does differently from `single-company` — keep these when merging from it:

| | `single-company` | `single-group` |
|---|---|---|
| Groups | one driver group per truck, registered when the bot is added, paired by `/setunit` | **one alerts group**, a single row; adding the bot to a chat registers nothing |
| Crash alerts | DMs + a dedicated `CRASH_GROUP_ID`, never the other groups | the alerts group (if its filter lists crash) + DMs |
| Mute | `/disable` · `/enable` by anyone in a driver group; a kicked group is muted automatically | **the panel's switch only** — no group commands, nothing is ever muted automatically, nobody is DMed when the bot joins or leaves a chat |
| Super admins | nobody can deactivate, demote or remove one | the **maintainer** (`ADMINS`) can do all three to any other super admin |
| Event types | one fixed list | a core list + per-deployment `EXTRA_EVENT_TYPES`; the rest are recognised and dropped |
| Alert card | ruled into blocks | plain rows, title + blank line (`_format_event`) |
| Speeding threshold | `high` | `medium` unless `SPEEDING_MIN_SEVERITY` says otherwise |
| Critical Motive `hard_brake` | promoted to a crash and held for confirmation | **stays a hard brake** (`_get_event_type`) — Motive files crashes as `crash` |
| Motive crash | card and clip in one message, after the clip wait | **card as soon as it is confirmed, clip as a follow-up**; a withdrawn one is sent at once as a hard brake |
| Reports | group-only `/report`, 3+/day rule in the download | `/report today\|yesterday` and `/top [N]` in the group **and** admin DMs; every event listed |
| PTI reminder, unit rosters | yes | removed (they exist for driver groups) |

Files that are this branch's own and should win a conflict: `utils/db_api/groups.py`, `utils/db_api/legacy.py`, `handlers/groups/group_events.py`, `handlers/users/report.py`, `utils/daily_report.py`, `data/event_catalog.py`, `data/config.py`, the Group screen in `utils/webapp/static/app.js`, and in `utils/webhook_handler.py`: `EVENT_TYPE_MAP`, `_get_event_type`, `_format_event` and the crash branch around `_wait_for_motive_clip` in `_handle_event`.

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

On this machine this virtualenv has the right dependencies: `C:\Users\ALFA\.virtualenvs\hf-alerts-bot-96Gq4aZS\Scripts\python.exe`.

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
      · a type this deployment does not alert on ends the poll and drops the event
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
- `utils/db_api/legacy.py` — carrying over a database an older build made (see Database)
- `utils/db_api/admins.py` — admins, hidden maintainer accounts, DM subscriptions
- `utils/db_api/violations.py` — save + every report/panel query
- `utils/db_api/crash_confirmations.py` — the durable record of each held Motive crash
- `utils/daily_report.py` — midnight ET report; `handlers/users/report.py` — `/report`, `/top`
- `handlers/groups/group_events.py` — `/help` in the group; logs the bot joining/leaving a chat (nothing else)
- `handlers/users/support.py` — ✉️ Contact Support relay to the maintainer
- `utils/webapp/` — the admin Mini App (`api.py` endpoints, `auth.py` initData check, `static/` frontend)
- `data/config.py` — all env var loading; `data/event_catalog.py` — every event type, which of them this deployment alerts on, what each is called, and the group filter's rows

### The alerts group

A singleton row in `alert_group` (`id` pinned to 1). Seeded once by `ensure_group()`: from the old schema's `company_groups` if this database has one, else from `GROUP_CHAT_ID`. After that the row is authoritative — it is rewritten when Telegram migrates the chat to a supergroup, and the env var is never read again.

`alert_group_event_types` is the group's allowlist; **no rows = every type**. Admins edit it, and the mute, from the panel's Group screen. Nothing in Telegram mutes the group and nothing mutes it automatically.

### Event types

`EVENT_TYPES` in `data/event_catalog.py` is every type the code recognises. A deployment acts on the **core** list plus whichever `OPTIONAL_EVENT_TYPES` its `EXTRA_EVENT_TYPES` variable names (`ALERTED_TYPES`); anything else is recognised and dropped — not alerted, not stored, not in a report. That is a different thing from the group's filter, which only decides what reaches the group. A name in `EXTRA_EVENT_TYPES` that matches nothing is ignored with a warning at startup; the line `app.py::_log_alert_rules` logs on every boot says which company this is, its providers, its speeding threshold and its type list — check it after a deploy.

### Motive

Live at MZ CARGO and JTR; inert at HF, where `MOTIVE_WEBHOOK_SECRET` is blank and the endpoint refuses whatever is posted to it.

**Crash confirmation.** Motive fires its crash webhook the instant the detector trips, before its own review runs, then *withdraws* a rejected detection from `/v2/driver_performance_events` — it disappears rather than being reclassified. Measured 2026-07-29..08-01, 59 of 62 crash webhooks were gone afterwards. Presence in that API is therefore the only reliable classifier. A Motive `crash` waits `_CRASH_CONFIRM_DELAY` (180s) and then:

- **still listed** → alerted as a crash
- **withdrawn** → `_type_override='hard_brake'`, sent at once as a hard brake (no clip wait: the listing the clip would come from is gone)
- **lookup failed / no `MOTIVE_API_KEY`** → **fails open**: sent as a crash with `⚠️ Unconfirmed` on the card

A row is written to `motive_crash_confirmations` before the wait and its verdict filled in after; `resume_pending_crash_confirmations()` picks up anything still undecided at startup, so a deploy landing inside the wait does not lose the alert. Bookkeeping failures are swallowed (`_note_crash`) — losing the row is acceptable, losing the alert is not.

**Clips.** The webhook arrives before the dashcam clip is transcoded (`camera_media.available` true, every URL null). Motive re-delivers when it is ready, under the same id, which the durable dedup refuses — so the clip is fetched from the API instead, and the alert **waits for it** (probes at 120 / 300 / 600 s): one message with card and footage. A crash is the exception: its card goes out as soon as it is confirmed, with "Video pending…", and the clip (or "No video available for this crash.") follows. A restart during a clip wait loses that alert, or for a crash, its clip.

**A hard brake is a hard brake.** single-company reads a critical-severity Motive `hard_brake` as a crash; this branch does not, and `tests/test_motive_crash.py` holds it to that.

### Admins and the hidden maintainer

`ADMINS` (env) are the **maintainer's** ids, not the company's staff — on every deployment, Akbar alone. They are permanent super admins and are hidden from the panel: other admins cannot see, deactivate or remove them (`is_maintainer` / `visible_admins` in `utils/db_api/admins.py`). The company's own admins are added and removed in the panel's Admins screen.

The maintainer is also above the company's super admins. A super admin manages regular admins only and cannot touch another super admin; the maintainer can deactivate, demote (`demote_from_super`) and remove any of them (`update_admin` / `delete_admin_route` in `utils/webapp/api.py`). Refusals shown to a company super admin never mention that such an account exists.

Crash DMs are on by default for every admin (`admins.crash_dm`); every other type is opt-in under ⚙️ Settings.

## Database

Schema is in `utils/db_api/schemas.sql` and is applied on every boot (`run_migrations`) — all `CREATE … IF NOT EXISTS`, then the additive statements in `db.py::_MIGRATIONS`. No migrations framework.

Key tables: `violations` (`event_id` is the idempotency key), `alert_group` / `alert_group_event_types`, `users` / `admins` / `admin_subscriptions`, `motive_crash_confirmations`, `support_relays`.

**Every production database predates this schema**, and `utils/db_api/legacy.py` is what carries each one over at startup. On a database none of it applies to, every function there finds nothing and does nothing.

*HF's and MZ CARGO's* ran the multi-company schema. They still hold `companies`, `company_groups`, `group_event_types`, `admin_companies`, and a NOT NULL `company_slug` on `violations` and on `motive_crash_confirmations`. On startup:

- `adopt_legacy_violations()` and `adopt_legacy_crash_confirmations()` drop NOT NULL on `company_slug` and give it the company's slug as its default (new inserts don't supply it);
- `adopt_legacy_company()` takes the display name from the `companies` row unless `COMPANY_NAME` is set;
- `ensure_group()` copies the group's chat id and event filter out of the old tables, once.

Which `companies` row is this deployment's is `legacy_company()`'s call: the one with `COMPANY_SLUG`, or — when none has it — the only one there is. The old build took the company from the webhook path when the path named one, so the variable was never held to the table; the slug has been spelled `mzcargo`, `mz_cargo` and `mz-cargo` in different places.

The old tables are only ever **read** — never altered or dropped — so the previous build still runs against this database if a deploy is rolled back. Don't "clean them up".

*JTR's* was made by the old single-group template: already the new tables, less `alert_group_event_types`. That template kept crashes out of the group in code; here the group's filter decides, and an empty filter means every type. So `adopt_single_group_template()` — which runs **before** `schemas.sql`, because the missing table is the mark it looks for — creates the filter listing every type except crash, in one transaction. After that it is an admin's to change on the Group screen.

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
| `COMPANY_SLUG` | Short id (report filenames, legacy lookup). Default `fleet` — set it |
| `COMPANY_NAME` | Unset = taken from the old schema if there is one, else the slug in capitals |
| `SPEEDING_MIN_SEVERITY` | `low` / `medium` / `high` / `critical`. Default `medium` |
| `EXTRA_EVENT_TYPES` | Optional event types this company alerts on, comma separated. Default none |
| `WEBAPP_URL` | Panel base URL; blank on Railway (read from `RAILWAY_PUBLIC_DOMAIN`) |
| `ALLOW_UNSIGNED_WEBHOOKS` | `true` accepts unsigned deliveries — bring-up / emergency only |
| `PORT` | Server port (Railway sets it) |

`ip`, which the old build required, is no longer read.
