"""Truck unit -> driver Telegram group, for the driver-alerts bot.

Units are matched on utils.unit_key.normalize_unit: Motive/Samsara vehicle names and
the imported groups list spell the same truck differently, so the table is keyed by
the normalized form and keeps the imported spelling only for display.
"""
import asyncio
import logging

from utils.db_api import db
from utils.unit_key import normalize_unit, unit_candidates

logger = logging.getLogger(__name__)

# The same statements as the "Driver notifications" block of schemas.sql. That file is
# applied by hand, so a deploy that forgot to run it would otherwise fail every driver
# notification on a missing table/column. Each statement is idempotent; they run once
# per process, the first time anything here needs them.
_DRIVER_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS driver_groups (
        company_slug      VARCHAR(50)  NOT NULL,
        unit_key          VARCHAR(50)  NOT NULL,
        unit_number       VARCHAR(50),
        telegram_group_id BIGINT       NOT NULL,
        is_active         BOOLEAN      DEFAULT TRUE,
        created_at        TIMESTAMPTZ  DEFAULT NOW(),
        PRIMARY KEY (company_slug, unit_key)
    )
    """,
    "CREATE INDEX IF NOT EXISTS driver_groups_tgid ON driver_groups (telegram_group_id)",
    "ALTER TABLE violations ADD COLUMN IF NOT EXISTS driver_ref    VARCHAR(100)",
    "ALTER TABLE violations ADD COLUMN IF NOT EXISTS driver_name   VARCHAR(255)",
    "ALTER TABLE violations ADD COLUMN IF NOT EXISTS driver_source VARCHAR(30)",
    "ALTER TABLE violations ADD COLUMN IF NOT EXISTS on_duty       BOOLEAN",
    "CREATE INDEX IF NOT EXISTS violations_driver_ref ON violations (company_slug, driver_ref)",
    """
    CREATE TABLE IF NOT EXISTS driver_notifications (
        event_id       BIGINT       PRIMARY KEY,
        company_slug   VARCHAR(50)  NOT NULL,
        vehicle_number VARCHAR(50),
        driver_ref     VARCHAR(100),
        ordinal        INT,
        sent_group     BOOLEAN      NOT NULL DEFAULT FALSE,
        sent_email     BOOLEAN      NOT NULL DEFAULT FALSE,
        created_at     TIMESTAMPTZ  DEFAULT NOW()
    )
    """,
    "ALTER TABLE violations ADD COLUMN IF NOT EXISTS coaching_state VARCHAR(30)",
    "ALTER TABLE driver_notifications ADD COLUMN IF NOT EXISTS coaching_state VARCHAR(30)",
    "ALTER TABLE driver_notifications ADD COLUMN IF NOT EXISTS skip_reason    VARCHAR(50)",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS quickmanage_carrier_id VARCHAR(100)",
    "ALTER TABLE companies ADD COLUMN IF NOT EXISTS vitality_company_key   VARCHAR(255)",
]

_schema_ready = False
_schema_lock: asyncio.Lock | None = None


async def ensure_driver_schema() -> None:
    """Apply the driver tables/columns once per process. Raises if the database
    rejects them; the next call tries again."""
    global _schema_ready, _schema_lock
    if _schema_ready:
        return
    if _schema_lock is None:
        _schema_lock = asyncio.Lock()
    async with _schema_lock:
        if _schema_ready:
            return
        for statement in _DRIVER_SCHEMA:
            await db.execute(statement)
        _schema_ready = True
        logger.info("Driver notification schema ensured")


async def get_driver_group(company_slug: str, vehicle_number: str | None) -> int | None:
    """The active driver group for this truck, or None if none is mapped."""
    # Full key first, then the bare unit before a suffix like "O/O" (unit_candidates).
    for key in unit_candidates(vehicle_number):
        group_id = await db.fetchval(
            """
            SELECT telegram_group_id FROM driver_groups
            WHERE company_slug = $1 AND unit_key = $2 AND COALESCE(is_active, TRUE)
            """,
            company_slug, key,
        )
        if group_id is not None:
            return group_id
    return None


async def upsert_driver_group(company_slug: str, unit_number: str, telegram_group_id: int) -> None:
    """Map a truck to its driver group (insert or replace). Re-importing a mapping also
    re-activates a group that had been deactivated. Raises ValueError for a unit that
    normalizes to nothing, since it could never be matched."""
    key = normalize_unit(unit_number)
    if not key:
        raise ValueError(f"unit number {unit_number!r} has no letters or digits")
    await ensure_driver_schema()
    await db.execute(
        """
        INSERT INTO driver_groups (company_slug, unit_key, unit_number, telegram_group_id, is_active)
        VALUES ($1, $2, $3, $4, TRUE)
        ON CONFLICT (company_slug, unit_key) DO UPDATE
            SET unit_number       = EXCLUDED.unit_number,
                telegram_group_id = EXCLUDED.telegram_group_id,
                is_active         = TRUE
        """,
        company_slug, key, (unit_number or "").strip()[:50], int(telegram_group_id),
    )


async def migrate_driver_group(old_id: int, new_id: int) -> None:
    """Telegram upgraded a group to a supergroup, which changes its chat id."""
    await db.execute(
        "UPDATE driver_groups SET telegram_group_id = $1 WHERE telegram_group_id = $2",
        new_id, old_id,
    )


async def deactivate_driver_group(group_id: int) -> None:
    """Stop sending to a group the driver bot can no longer post in. The mapping is
    kept; re-importing it (upsert_driver_group) turns it back on."""
    await db.execute(
        "UPDATE driver_groups SET is_active = FALSE WHERE telegram_group_id = $1",
        group_id,
    )
