import os

import asyncpg
from data import config

pool: asyncpg.Pool | None = None
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schemas.sql")


async def init_pool():
    global pool
    pool = await asyncpg.create_pool(config.DATABASE_URL)
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        async with pool.acquire() as conn:
            await conn.execute(f.read())


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
