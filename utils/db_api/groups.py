from data import config
from utils.db_api import db


async def ensure_group() -> None:
    """Seed the singleton alert_group row from config.GROUP_CHAT_ID on first boot.

    ON CONFLICT (id) rather than (telegram_group_id): the row's id is pinned to 1, so
    this is a no-op on every boot after the first — including after migrate_group()
    has moved telegram_group_id away from what's still in the env. Keying the conflict
    on telegram_group_id instead would re-insert a dead duplicate row for the old id
    every time the env and the DB disagree after a migration.
    """
    await db.execute(
        "INSERT INTO alert_group (id, telegram_group_id) VALUES (1, $1) "
        "ON CONFLICT (id) DO NOTHING",
        config.GROUP_CHAT_ID,
    )


async def get_alert_target() -> int | None:
    """The chat id every alert goes to, or None if the group is muted."""
    row = await db.fetchrow("SELECT telegram_group_id FROM alert_group WHERE enabled")
    return row["telegram_group_id"] if row else None


async def get_group_status() -> dict | None:
    """{"telegram_group_id", "enabled"} for the panel's group card, or None if the
    singleton row hasn't been seeded yet (ensure_group hasn't run)."""
    row = await db.fetchrow("SELECT telegram_group_id, enabled FROM alert_group")
    return dict(row) if row else None


async def get_group_chat_id() -> int | None:
    """The chat id of the one group, muted or not — for identifying it (e.g. /report,
    /top) as opposed to deciding whether to alert it."""
    row = await db.fetchrow("SELECT telegram_group_id FROM alert_group")
    return row["telegram_group_id"] if row else None


async def is_the_group(telegram_group_id: int) -> bool:
    """True if this chat is the one group this deployment is configured for."""
    return telegram_group_id == await get_group_chat_id()


async def set_group_enabled(telegram_group_id: int, enabled: bool) -> None:
    """Mute (enabled=False) or unmute (enabled=True) the group's alerts."""
    await db.execute(
        "UPDATE alert_group SET enabled = $2 WHERE telegram_group_id = $1",
        telegram_group_id, enabled,
    )


async def migrate_group(old_id: int, new_id: int) -> None:
    """Point the alert group at its new chat id after Telegram upgrades it to a
    supergroup."""
    await db.execute(
        "UPDATE alert_group SET telegram_group_id = $1 WHERE telegram_group_id = $2",
        new_id, old_id,
    )
