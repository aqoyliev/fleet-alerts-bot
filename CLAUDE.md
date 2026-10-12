# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The Telegram alerts bot for **HAULAGE FREIGHT LLC** (branch `hf`). It receives vehicle safety webhooks from **Samsara**, stores each violation in PostgreSQL, and posts an alert to the company's **one** Telegram group and to the admins who asked for DMs. It sends a daily violations report at midnight ET and serves an admin **Mini App** from the same process.

## How this branch relates to the others

`hf` follows `single-company`. It was re-based onto it by a merge (`Merge single-company into hf: adopt it as the base`), so later work on `single-company` comes in with an ordinary `git merge origin/single-company`.

What `hf` deliberately does differently — keep these when resolving a merge:

| | `single-company` | `hf` |
|---|---|---|
| Groups | one driver group per truck, registered when the bot is added, paired by `/setunit` | **one alerts group**, a single row; adding the bot to a chat registers nothing |
| Crash alerts | DMs + a dedicated `CRASH_GROUP_ID`, never the other groups | the alerts group (if its filter lists crash) + DMs |
| Mute | anyone in a driver group; a kicked group is muted automatically | **bot admins only**; nothing is ever muted automatically |
| Event types | adds tailgating, camera obstruction, near miss, … | HF's shorter list (`EVENT_TYPE_MAP`); the rest are recognised and dropped |
| Alert card | ruled into blocks | plain rows, title + blank line (`_format_event`) |
| Speeding threshold | `high` | `medium` |
| Reports | group-only `/report`, 3+/day rule in the download | `/report today\|yesterday` and `/top [N]` in the group **and** admin DMs; every event listed |
| PTI reminder, unit rosters | yes | removed (they exist for driver groups) |

Files that are HF's own and should win a conflict: `utils/db_api/groups.py`, `utils/db_api/legacy.py`, `handlers/groups/group_events.py`, `handlers/users/report.py`, `utils/daily_report.py`, `data/event_catalog.py`, and the Group screen in `utils/webapp/static/app.js`.

## Running the bot

```bash
pip install -r requirements.txt
python app.py
```

The bot starts an aiohttp server (webhooks + admin panel) on `$PORT` (default 8080) alongside the Telegram polling loop.

**Python environment (Windows):** a pipenv virtualenv, `C:\Users\ALFA\.virtualenvs\hf-alerts-bot-96Gq4aZS\Scripts\python.exe`.

**Run tests** (needs `pip install -r requirements-dev.txt` — pytest-asyncio is what makes the async tests run):
```powershell
python -m pytest -q
```

## Architecture

### Data flow

```
POST /webhook/samsara            (also /webhook/samsara/hf — the old build's URL, still answered)
  → HMAC + timestamp check       (no secret configured = delivery REFUSED, unless ALLOW_UNSIGNED_WEBHOOKS)
  → in-memory dedup              (5-min TTL on eventId)
  → normalize to internal event dict
  → harsh events: background poll of the legacy harsh_event API for type, location, video
      · first typed response: violation saved; a crash is alerted at once ("Video pending…")
      · a type not in EVENT_TYPE_MAP ends the poll and drops the event
      · an empty record after the whole poll: DM-only, marked "Possible error"
  → speeding: speed / limit / location looked up from the Samsara API
  → save_violation()             (UNIQUE event_id decides: already stored = no second alert)
  → the alerts group (if not muted and the type is on its filter) + subscribed admin DMs
```

The webhook always answers 200 immediately; `_handle_event()` runs as a background task.

### Key files

- `utils/webhook_handler.py` — all event processing: auth, dedup, parsing, the alert card, Telegram send with retries, webhook routes
- `utils/db_api/groups.py` — the one alerts group: seeding, mute, event filter, routing (`get_alert_target`)
- `utils/db_api/legacy.py` — reading the old multi-company schema on first boot (see Database)
- `utils/db_api/admins.py` — admins, hidden maintainer accounts, DM subscriptions
- `utils/db_api/violations.py` — save + every report/panel query
- `utils/daily_report.py` — midnight ET report; `handlers/users/report.py` — `/report`, `/top`
- `handlers/groups/group_events.py` — `/help`, `/disable`, `/enable` in the group; removal notices
- `handlers/users/support.py` — ✉️ Contact Support relay to the maintainer
- `utils/webapp/` — the admin Mini App (`api.py` endpoints, `auth.py` initData check, `static/` frontend)
- `utils/samsara/client.py` — speeding enrichment
- `data/config.py` — all env var loading; `data/event_catalog.py` — the filterable event types

### The alerts group

A singleton row in `alert_group` (`id` pinned to 1). Seeded once by `ensure_group()`: from the old schema's `company_groups` if this database has one, else from `GROUP_CHAT_ID`. After that the row is authoritative — it is rewritten when Telegram migrates the chat to a supergroup, and the env var is never read again.

`alert_group_event_types` is the group's allowlist; **no rows = every type**. Admins edit it, and the mute, from the panel's Group screen.

### Admins and the hidden maintainer

`ADMINS` (env) are the **maintainer's** ids, not the company's staff. They are permanent super admins and are hidden from the panel: other admins cannot see, deactivate or remove them (`is_maintainer` / `visible_admins` in `utils/db_api/admins.py`). The company's own admins are added and removed in the panel's Admins screen.

Crash DMs are on by default for every admin (`admins.crash_dm`); every other type is opt-in under ⚙️ Settings.

### Motive

HF is a Samsara fleet. The Motive webhook, crash confirmation and clip fetching are kept as they are on `single-company` and are inert with `MOTIVE_WEBHOOK_SECRET` blank.

## Database

Schema is in `utils/db_api/schemas.sql` and is applied on every boot (`run_migrations`) — all `CREATE … IF NOT EXISTS`, then the additive statements in `db.py::_MIGRATIONS`. No migrations framework.

Key tables: `violations` (`event_id` is the idempotency key), `alert_group` / `alert_group_event_types`, `users` / `admins` / `admin_subscriptions`, `motive_crash_confirmations`, `support_relays`.

**HF's production database predates this schema.** It still holds `companies`, `company_groups`, `group_event_types`, `admin_companies`, and `violations.company_slug`. On startup:

- `adopt_legacy_violations()` drops NOT NULL on `violations.company_slug` and gives it the company's slug as its default (new inserts don't supply it);
- `ensure_group()` copies the group's chat id and event filter out of the old tables, once.

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
| `GROUP_CHAT_ID` | Seeds the alerts group on a fresh database only (optional) |
| `COMPANY_SLUG` / `COMPANY_NAME` | Default to `hf` / `HAULAGE FREIGHT LLC` |
| `SPEEDING_MIN_SEVERITY` | Default `medium` |
| `WEBAPP_URL` | Panel base URL; blank on Railway (read from `RAILWAY_PUBLIC_DOMAIN`) |
| `ALLOW_UNSIGNED_WEBHOOKS` | `true` accepts unsigned deliveries — bring-up / emergency only |
| `MOTIVE_WEBHOOK_SECRET` / `MOTIVE_API_KEY` | Unused for HF — leave blank |
| `PORT` | Server port (Railway sets it) |
