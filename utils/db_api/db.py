import logging
from pathlib import Path

import asyncpg
from data import config

logger = logging.getLogger(__name__)

pool: asyncpg.Pool | None = None

# Idempotent, additive migrations applied on every startup so existing databases pick
# up new columns/indexes without a manual SQL step (Railway deployments don't run
# schemas.sql by hand). Fresh databases get everything from schemas.sql first; these
# ALTERs are the no-ops that upgrade older ones.
_MIGRATIONS = [
    "ALTER TABLE alert_groups ADD COLUMN IF NOT EXISTS title VARCHAR(255)",
    "ALTER TABLE alert_groups ADD COLUMN IF NOT EXISTS vehicle_number VARCHAR(50)",
    "ALTER TABLE alert_groups ADD COLUMN IF NOT EXISTS enabled BOOLEAN DEFAULT TRUE",
    # What marks the all-fleet group, so that a NULL unit can mean "registered but
    # not yet assigned" instead. Left nullable, and backfilled from the rule it
    # replaces: every existing NULL-unit row IS a main group, because under the old
    # code nothing else could be one. The WHERE is what makes that safe to re-run —
    # it can only ever match rows written before the column existed, since every
    # INSERT since then sets the flag explicitly.
    "ALTER TABLE alert_groups ADD COLUMN IF NOT EXISTS is_main BOOLEAN",
    "UPDATE alert_groups SET is_main = (vehicle_number IS NULL) WHERE is_main IS NULL",
    # Set when the bot is removed from a chat, cleared when it is added back. Hides
    # the group from the panel without throwing away its unit and filter.
    "ALTER TABLE alert_groups ADD COLUMN IF NOT EXISTS left_at TIMESTAMPTZ",
    "CREATE UNIQUE INDEX IF NOT EXISTS alert_groups_tgid ON alert_groups (telegram_group_id)",
    "CREATE INDEX IF NOT EXISTS alert_groups_vehicle ON alert_groups (vehicle_number)",
    # Crash DMs are on for every admin unless they turn them off. A column defaulting to
    # TRUE is what makes that work on both sides: existing rows get TRUE when it is added,
    # new admins get TRUE for free, and an admin who opts out stays opted out — seeding
    # subscription rows instead would silently re-enable them on the next startup.
    "ALTER TABLE admins ADD COLUMN IF NOT EXISTS crash_dm BOOLEAN NOT NULL DEFAULT TRUE",
    # The PTI reminder claims each morning before it posts, so a restart on top of
    # the slot neither loses the album nor sends it twice.
    "CREATE TABLE IF NOT EXISTS pti_reminder_runs (sent_on DATE PRIMARY KEY, group_count INT NOT NULL DEFAULT 0, created_at TIMESTAMPTZ DEFAULT NOW())",
    # Uploaded once, then re-sent by file_id — see utils/db_api/pti_posters.py.
    "CREATE TABLE IF NOT EXISTS pti_poster_cache (fingerprint TEXT PRIMARY KEY, file_ids TEXT[] NOT NULL, created_at TIMESTAMPTZ DEFAULT NOW())",
]


async def init_pool():
    global pool
    pool = await asyncpg.create_pool(config.DATABASE_URL)


async def run_migrations():
    """Apply schemas.sql (all CREATE ... IF NOT EXISTS) then additive column/index
    migrations. Every statement is idempotent; failures are logged, not fatal, so a
    startup is never blocked by a migration that a manual step already covered."""
    schema_sql = Path(__file__).with_name("schemas.sql").read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        try:
            await conn.execute(schema_sql)
        except Exception as e:
            logger.warning(f"schema apply failed (continuing): {e}")
        for stmt in _MIGRATIONS:
            try:
                await conn.execute(stmt)
            except Exception as e:
                logger.warning(f"migration failed ({stmt!r}): {e}")


async def close_pool():
    global pool
    if pool:
        await pool.close()
        pool = None


async def fetch(query: str, *args):
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)


async def fetchrow(query: str, *args):
    async with pool.acquire() as conn:
        return await conn.fetchrow(query, *args)


async def fetchval(query: str, *args):
    async with pool.acquire() as conn:
        return await conn.fetchval(query, *args)


async def execute(query: str, *args):
    async with pool.acquire() as conn:
        return await conn.execute(query, *args)
