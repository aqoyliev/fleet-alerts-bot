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
    "CREATE UNIQUE INDEX IF NOT EXISTS alert_groups_tgid ON alert_groups (telegram_group_id)",
    "CREATE INDEX IF NOT EXISTS alert_groups_vehicle ON alert_groups (vehicle_number)",
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
