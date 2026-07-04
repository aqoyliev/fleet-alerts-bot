from data import config
from utils.db_api import db


async def get_groups_for_event(event_type: str, vehicle_number: str | None = None) -> list[int]:
    """Returns telegram_group_ids that should receive this event.

    Two kinds of group qualify:
      • the main group (config.MAIN_GROUP_ID) — receives every unit's alerts, and
      • the driver group whose vehicle_number matches this event's unit.
    Either way the group's optional group_event_types filter still applies: a group with
    no rows there receives every type; otherwise only the types listed for it.
    """
    rows = await db.fetch(
        """
        SELECT g.telegram_group_id
        FROM alert_groups g
        WHERE (
                  ($2::text   IS NOT NULL AND g.vehicle_number = $2)
               OR ($3::bigint IS NOT NULL AND g.telegram_group_id = $3)
              )
          AND (
                  NOT EXISTS (SELECT 1 FROM group_event_types WHERE group_id = g.id)
               OR EXISTS (SELECT 1 FROM group_event_types WHERE group_id = g.id AND event_type = $1)
              )
        """,
        event_type, vehicle_number, config.MAIN_GROUP_ID,
    )
    return [r["telegram_group_id"] for r in rows]


async def get_all_groups() -> list[int]:
    """Returns the groups that should receive the all-fleet daily report: the main /
    catch-all groups (vehicle_number IS NULL). Driver groups are per-unit and are
    intentionally left out of the company-wide digest."""
    rows = await db.fetch(
        "SELECT telegram_group_id FROM alert_groups WHERE vehicle_number IS NULL"
    )
    return [r["telegram_group_id"] for r in rows]


async def register_group(telegram_group_id: int, title: str | None,
                         vehicle_number: str | None) -> None:
    """Insert (or update) a group registration. Called when the bot is added to a group;
    vehicle_number is the parsed unit for driver groups, or NULL for the main group."""
    await db.execute(
        """
        INSERT INTO alert_groups (telegram_group_id, title, vehicle_number)
        VALUES ($1, $2, $3)
        ON CONFLICT (telegram_group_id) DO UPDATE
            SET title = EXCLUDED.title,
                vehicle_number = EXCLUDED.vehicle_number
        """,
        telegram_group_id, title, vehicle_number,
    )


async def ensure_main_group() -> None:
    """Guarantee config.MAIN_GROUP_ID exists as a (NULL-vehicle) row so it receives all
    alerts and works with /report even if the bot was never re-added after configuring it."""
    if not config.MAIN_GROUP_ID:
        return
    await db.execute(
        """
        INSERT INTO alert_groups (telegram_group_id, vehicle_number)
        VALUES ($1, NULL)
        ON CONFLICT (telegram_group_id) DO NOTHING
        """,
        config.MAIN_GROUP_ID,
    )


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
