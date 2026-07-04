from utils.db_api import db


async def get_groups_for_event(event_type: str) -> list[int]:
    """Returns telegram_group_ids that should receive this event type.

    A group with no group_event_types rows receives every type; otherwise it only
    receives the types explicitly listed for it.
    """
    rows = await db.fetch(
        """
        SELECT g.telegram_group_id
        FROM alert_groups g
        WHERE NOT EXISTS (
                  SELECT 1 FROM group_event_types WHERE group_id = g.id
              )
           OR EXISTS (
                  SELECT 1 FROM group_event_types
                  WHERE group_id = g.id AND event_type = $1
              )
        """,
        event_type,
    )
    return [r["telegram_group_id"] for r in rows]


async def get_all_groups() -> list[int]:
    """Returns every configured alert group's telegram_group_id (used for daily reports)."""
    rows = await db.fetch("SELECT telegram_group_id FROM alert_groups")
    return [r["telegram_group_id"] for r in rows]


async def group_exists(telegram_group_id: int) -> bool:
    """True if this Telegram group is registered to receive the company's alerts."""
    val = await db.fetchval(
        "SELECT 1 FROM alert_groups WHERE telegram_group_id = $1", telegram_group_id
    )
    return val is not None


async def get_group_event_types(telegram_group_id: int) -> list[str]:
    """Returns the list of event types configured for a group. Empty list = all types allowed."""
    rows = await db.fetch(
        """
        SELECT get.event_type
        FROM group_event_types get
        JOIN alert_groups g ON g.id = get.group_id
        WHERE g.telegram_group_id = $1
        ORDER BY get.event_type
        """,
        telegram_group_id,
    )
    return [r["event_type"] for r in rows]


async def migrate_group(old_id: int, new_id: int) -> None:
    """Point an alert group at its new chat id after Telegram upgrades it to a supergroup."""
    await db.execute(
        "UPDATE alert_groups SET telegram_group_id = $1 WHERE telegram_group_id = $2",
        new_id, old_id,
    )
