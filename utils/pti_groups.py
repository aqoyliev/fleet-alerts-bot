"""Truck unit -> driver Telegram group, live from pti-checker-bot's own database.

Each company's pti-checker-bot keeps the groups it serves (`groups`) and the Telegram
members it counts as that truck's drivers (`group_drivers`; staff are in `non_drivers`).
Groups change daily — driver swaps, hometime, new groups — so a company with a
PTI_DATABASE_URL_<SLUG> (data.config.pti_database_url) is routed only from here, never
from the one-off driver_groups import.

Read-only: one small pool per company (created on first use), every query inside a
READ ONLY transaction. A company's usable groups and their drivers are fetched in one
query and cached for CACHE_TTL. When a refresh fails, the last good snapshot is used
while it is younger than STALE_MAX; after that the lookup fails closed
(pti_unavailable). A unit that matches more than one usable group is ambiguous and
also fails closed.

A usable group: is_active AND setup_complete AND NOT post_blocked, with a unit number.
"""
import asyncio
import logging
import time
from dataclasses import dataclass

import asyncpg

from data import config
from utils.unit_key import unit_candidates

logger = logging.getLogger(__name__)

REASON_NO_PTI_DB = "no_pti_db"
REASON_UNAVAILABLE = "pti_unavailable"
REASON_NO_GROUP = "no_group"
REASON_AMBIGUOUS = "ambiguous"

CACHE_TTL = 60.0         # a snapshot is refreshed after this
STALE_MAX = 600.0        # a failed refresh may fall back to a snapshot this old, no older
RETRY_AFTER_FAILURE = 15.0  # no new attempt this soon after a failed one
COMMAND_TIMEOUT = 5.0    # per query / connection acquire
FETCH_TIMEOUT = 10.0     # the whole refresh, connecting included

# One row per usable group, its drivers' Telegram names aggregated (staff left out).
USABLE_GROUPS_SQL = """
    SELECT g.group_id,
           g.unit_number::text AS unit_number,
           COALESCE(g.title::text, '') AS title,
           COALESCE(array_agg(d.name::text ORDER BY d.id) FILTER (WHERE d.name IS NOT NULL),
                    ARRAY[]::text[]) AS driver_names
    FROM groups g
    LEFT JOIN group_drivers d
           ON d.group_id = g.group_id
          AND NOT EXISTS (SELECT 1 FROM non_drivers n WHERE n.user_id = d.user_id)
    WHERE COALESCE(g.is_active, FALSE)
      AND COALESCE(g.setup_complete, FALSE)
      AND NOT COALESCE(g.post_blocked, FALSE)
      AND btrim(COALESCE(g.unit_number::text, '')) <> ''
    GROUP BY g.group_id, g.unit_number, g.title
"""


@dataclass(frozen=True)
class PtiGroup:
    group_id: int
    title: str
    unit_number: str
    driver_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class PtiLookup:
    """A routing answer: `group` when exactly one usable group is the truck's, else
    `reason` (REASON_*). `matches` lists the groups an ambiguous unit hit; `age` is how
    old the snapshot answering was, in seconds (None when there was none)."""
    group: PtiGroup | None = None
    reason: str | None = None
    matches: tuple[PtiGroup, ...] = ()
    age: float | None = None

    @property
    def ok(self) -> bool:
        return self.group is not None


@dataclass(frozen=True)
class _Snapshot:
    fetched_at: float
    groups: tuple[PtiGroup, ...]


_pools: dict[str, asyncpg.Pool] = {}
_snapshots: dict[str, _Snapshot] = {}
_failed_at: dict[str, float] = {}
_locks: dict[str, asyncio.Lock] = {}


def _key(company_slug: str | None) -> str:
    return (company_slug or "").strip().lower()


def is_configured(company_slug: str | None) -> bool:
    """Whether the company routes from its pti-checker-bot database."""
    return bool(config.pti_database_url(company_slug))


async def _create_pool(url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(
        url, min_size=0, max_size=2,
        command_timeout=COMMAND_TIMEOUT, timeout=COMMAND_TIMEOUT,
        max_inactive_connection_lifetime=120.0,
        server_settings={"application_name": "nexus-driver-alerts"},
    )


async def _get_pool(company_slug: str, url: str) -> asyncpg.Pool:
    key = _key(company_slug)
    pool = _pools.get(key)
    if pool is None:
        pool = await _create_pool(url)
        _pools[key] = pool
    return pool


def _row_to_group(row) -> PtiGroup:
    names = tuple(n.strip() for n in (row["driver_names"] or []) if n and n.strip())
    return PtiGroup(group_id=int(row["group_id"]), title=(row["title"] or "").strip(),
                    unit_number=(row["unit_number"] or "").strip(), driver_names=names)


async def fetch_usable_groups(company_slug: str) -> tuple[PtiGroup, ...]:
    """The company's usable groups, straight from its pti database (no cache).
    Raises LookupError when no database is configured, or the database's error."""
    url = config.pti_database_url(company_slug)
    if not url:
        raise LookupError(f"no {config.PTI_DATABASE_URL_PREFIX}<SLUG> for {company_slug!r}")
    pool = await _get_pool(company_slug, url)
    async with pool.acquire(timeout=COMMAND_TIMEOUT) as conn:
        async with conn.transaction(readonly=True):
            rows = await conn.fetch(USABLE_GROUPS_SQL, timeout=COMMAND_TIMEOUT)
    return tuple(_row_to_group(r) for r in rows)


async def _snapshot(company_slug: str) -> _Snapshot | None:
    """A snapshot young enough to route from, refreshed when due; None = fail closed."""
    key = _key(company_slug)
    snap = _snapshots.get(key)
    if snap is not None and time.monotonic() - snap.fetched_at < CACHE_TTL:
        return snap
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        snap = _snapshots.get(key)
        if snap is not None and now - snap.fetched_at < CACHE_TTL:
            return snap
        usable_stale = snap if snap is not None and now - snap.fetched_at < STALE_MAX else None
        failed = _failed_at.get(key)
        if failed is not None and now - failed < RETRY_AFTER_FAILURE:
            return usable_stale
        try:
            groups = await asyncio.wait_for(fetch_usable_groups(company_slug), FETCH_TIMEOUT)
        except Exception as e:
            _failed_at[key] = time.monotonic()
            if usable_stale is not None:
                logger.warning(f"[pti] {key}: groups refresh failed ({type(e).__name__}: {e}) — "
                               f"routing from the snapshot of {now - usable_stale.fetched_at:.0f}s ago")
            else:
                logger.error(f"[pti] {key}: groups unavailable ({type(e).__name__}: {e}) and no "
                             f"snapshot younger than {STALE_MAX:.0f}s — failing closed")
            return usable_stale
        _failed_at.pop(key, None)
        if not groups:
            logger.warning(f"[pti] {key}: the pti database has no usable groups")
        elif snap is None or len(snap.groups) != len(groups):
            logger.info(f"[pti] {key}: {len(groups)} usable groups")
        snap = _Snapshot(time.monotonic(), groups)
        _snapshots[key] = snap
        return snap


def match_unit(groups, vehicle: str | None) -> PtiLookup:
    """The one usable group whose unit is this truck's. Both sides go through
    unit_candidates (the full normalized unit, and the leading token before a suffix
    like "O/O"); any overlap is a match. More than one distinct group -> ambiguous."""
    wanted = set(unit_candidates(vehicle))
    if not wanted:
        return PtiLookup(reason=REASON_NO_GROUP)
    hits: dict[int, PtiGroup] = {}
    for g in groups:
        if wanted & set(unit_candidates(g.unit_number)):
            hits.setdefault(g.group_id, g)
    if not hits:
        return PtiLookup(reason=REASON_NO_GROUP)
    if len(hits) > 1:
        return PtiLookup(reason=REASON_AMBIGUOUS, matches=tuple(hits.values()))
    return PtiLookup(group=next(iter(hits.values())))


async def find_group(company_slug: str, vehicle: str | None) -> PtiLookup:
    """The truck's driver group per the company's pti database. Never raises."""
    try:
        if not is_configured(company_slug):
            return PtiLookup(reason=REASON_NO_PTI_DB)
        snap = await _snapshot(company_slug)
        if snap is None:
            return PtiLookup(reason=REASON_UNAVAILABLE)
        result = match_unit(snap.groups, vehicle)
        age = time.monotonic() - snap.fetched_at
        return PtiLookup(group=result.group, reason=result.reason, matches=result.matches, age=age)
    except Exception as e:
        logger.error(f"[pti] Lookup failed for {company_slug}/{vehicle}: {e}", exc_info=True)
        return PtiLookup(reason=REASON_UNAVAILABLE)


async def close() -> None:
    """Close every pool (service shutdown)."""
    pools = list(_pools.items())
    _pools.clear()
    for key, pool in pools:
        try:
            await pool.close()
        except Exception as e:
            logger.warning(f"[pti] Could not close the {key} pool: {e}")
