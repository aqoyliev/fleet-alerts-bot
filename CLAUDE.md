# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Telegram bot that receives vehicle safety webhooks from **Motive** and **Samsara** fleet APIs, stores violations in PostgreSQL, and distributes alerts to configured Telegram groups and admin DMs. It also generates a daily violations report at midnight ET.

## Running the bot

```bash
pip install -r requirements.txt
python app.py
```

The bot starts an aiohttp webhook server on port 8080 alongside the Telegram polling loop.

**Python environment note (Windows):** Python is inside a pipenv virtualenv. Either activate it first (`C:\Users\user\.virtualenvs\motive_alerts_bot-c-vbPPhO\Scripts\Activate.ps1`) or use the full path: `C:\Users\user\.virtualenvs\motive_alerts_bot-c-vbPPhO\Scripts\python.exe`.

**Run tests:**
```powershell
python -m pytest tests/ -v
```

**Manual webhook formatting test** (no live bot needed):
```bash
python test_webhook.py
```

## Architecture

### Data flow

```
POST /webhook/motive or /webhook/samsara
  → HMAC verification (if secret configured)
  → deduplication (in-memory 5-min TTL on event_id)
  → normalize to internal event dict
  → [Samsara only] background poll for harsh_event details + video URL
  → format message (HTML, emojis, ET timezone)
  → download media from cloud storage
  → save_violation() → violations table
  → send to company groups + subscribed admin DMs
```

Webhook always returns 200 immediately; `_handle_event()` runs in `asyncio.create_task()` so it never blocks.

### Key files

- `utils/webhook_handler.py` — all event processing: HMAC, dedup, parsing, formatting, Telegram send with retries
- `utils/daily_report.py` — midnight ET scheduled report, groups violations by event type
- `utils/db_api/violations.py` — save and query violations
- `utils/db_api/companies.py` — route events to the right Telegram groups
- `utils/db_api/admins.py` — admin permissions and DM subscriptions
- `utils/db_api/db.py` — asyncpg pool wrapper (`fetch`, `fetchrow`, `execute`)
- `data/config.py` — all env var loading
- `loader.py` — Bot + Dispatcher init, optional Redis FSM

### Multi-company routing

Events are routed based on `company_slug`. `company_groups` maps a company to Telegram groups; `group_event_types` filters which event types each group receives (empty = all types). Admins can be scoped to specific companies or be super-admins with full access.

### Motive vs. Samsara differences

Motive sends complete event data in the webhook payload. Samsara webhooks are thin notifications — the handler must make a follow-up API call to `GET /fleet/harsh-events/{id}` to get speed, location, and video URL. This polling happens in a background task with 20s retry intervals.

## Database

Schema is in `utils/db_api/schemas.sql`. No migrations framework — changes require manual SQL.

Key tables:
- `violations` — all processed events; `event_id` is the unique idempotency key
- `companies` / `company_groups` / `group_event_types` — routing config
- `admins` / `admin_companies` / `admin_subscriptions` — admin access + DM preferences

All queries use asyncpg `$1, $2` placeholders (never f-strings). Times stored as UTC `timestamptz`, displayed in ET.

## Vehicle number normalization

`_clean_vehicle()` in `webhook_handler.py` strips leading `unit`/`UNIT`/`Unit #`/`unit:` etc. prefixes using `^unit[\s:#-]+` (case-insensitive) before saving to DB. Existing records without this fix can be cleaned with:

```sql
UPDATE violations
SET vehicle_number = TRIM(REGEXP_REPLACE(vehicle_number, '^unit[\s:#-]+', '', 'i'))
WHERE vehicle_number ~* '^unit[\s:#-]+';
```

## Environment variables

| Var | Purpose |
|-----|---------|
| `BOT_TOKEN` | Telegram bot token |
| `ADMINS` | Super-admin Telegram IDs (space/comma separated) |
| `DATABASE_URL` | PostgreSQL connection string |
| `COMPANY_SLUG` | Default company slug |
| `GROUP_CHAT_ID` | Default fallback Telegram group ID |
| `SAMSARA_API_KEY` | Samsara REST API key (optional) |
| `MOTIVE_WEBHOOK_SECRET` | HMAC secret for Motive (empty = skip verification) |
| `SAMSARA_WEBHOOK_SECRET` | HMAC secret for Samsara (empty = skip verification) |
| `REDIS_URL` | Redis for FSM storage (optional; falls back to in-memory) |
| `ip` | Server IP used in webhook URL construction |
