from data import config
from data.event_catalog import next_event_filter
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
        WHERE COALESCE(g.enabled, TRUE)
          AND (
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
    """Returns the groups that should receive the all-fleet daily report: the enabled
    main / catch-all groups (vehicle_number IS NULL). Driver groups are per-unit and are
    intentionally left out of the company-wide digest."""
    rows = await db.fetch(
        "SELECT telegram_group_id FROM alert_groups "
        "WHERE vehicle_number IS NULL AND COALESCE(enabled, TRUE)"
    )
    return [r["telegram_group_id"] for r in rows]


async def register_group(telegram_group_id: int, title: str | None,
                         vehicle_number: str | None) -> None:
    """Insert (or update) a group registration. Called when the bot is added to a group;
    vehicle_number is the parsed unit for driver groups, or NULL for the main group.

    Re-registering clears any mute. A group that went silent because the bot was kicked
    is muted automatically (see _drop_unreachable), and nobody would think to unmute it
    by hand afterwards — putting the bot back in the group is the intent signal, so it
    is what turns the alerts back on."""
    await db.execute(
        """
        INSERT INTO alert_groups (telegram_group_id, title, vehicle_number)
        VALUES ($1, $2, $3)
        ON CONFLICT (telegram_group_id) DO UPDATE
            SET title = EXCLUDED.title,
                vehicle_number = EXCLUDED.vehicle_number,
                enabled = TRUE
        """,
        telegram_group_id, title, vehicle_number,
    )


async def get_group(telegram_group_id: int) -> dict | None:
    """Return the full registration row for a group, or None if it isn't registered."""
    row = await db.fetchrow(
        """
        SELECT id, telegram_group_id, title, vehicle_number, COALESCE(enabled, TRUE) AS enabled
        FROM alert_groups WHERE telegram_group_id = $1
        """,
        telegram_group_id,
    )
    return dict(row) if row else None


async def set_group_enabled(telegram_group_id: int, enabled: bool) -> None:
    """Mute (enabled=False) or unmute (enabled=True) a group's alerts."""
    await db.execute(
        "UPDATE alert_groups SET enabled = $2 WHERE telegram_group_id = $1",
        telegram_group_id, enabled,
    )


async def remove_group(telegram_group_id: int) -> None:
    """Unregister a group (its group_event_types rows cascade away). The bot stays in
    the chat; the group simply stops receiving alerts until re-registered."""
    await db.execute(
        "DELETE FROM alert_groups WHERE telegram_group_id = $1", telegram_group_id
    )


async def set_group_unit(telegram_group_id: int, vehicle_number: str) -> None:
    """Repoint an already-registered group at a different unit, leaving its mute alone.

    register_group is the wrong call for this even though it would write the same column.
    It treats every write as a (re-)registration and therefore clears the mute — correct
    when the bot is added back to a group that went silent, wrong when a dispatcher is
    only fixing a typo in the unit of a group they muted this morning. Reusing it would
    turn an unrelated edit into an un-mute, and nobody would connect the two events.
    """
    await db.execute(
        "UPDATE alert_groups SET vehicle_number = $2 WHERE telegram_group_id = $1",
        telegram_group_id, vehicle_number,
    )


async def get_groups_overview() -> list[dict]:
    """Every registered group with the fields the admin panel lists, filter folded in.

    get_all_groups is not this: it returns only the telegram ids of the catch-all groups
    that receive the daily digest. The panel needs one row per group, and it needs them in
    ONE query — calling get_group_event_types per group turns opening the Groups tab on a
    40-truck fleet into 41 round trips.

    The ordering is digit-aware deliberately. This fleet's roster spells units "unit571"
    and "unit2007", and a plain VARCHAR sort files unit2007 before unit571, so a
    dispatcher scanning the list for a truck cannot find it where they expect. The main
    group (NULL unit) is pinned first because it behaves unlike every other row.
    """
    rows = await db.fetch(
        r"""
        SELECT g.id, g.telegram_group_id, g.title, g.vehicle_number,
               COALESCE(g.enabled, TRUE) AS enabled, g.created_at,
               ARRAY(SELECT t.event_type FROM group_event_types t
                     WHERE t.group_id = g.id ORDER BY t.event_type) AS event_types
        FROM alert_groups g
        ORDER BY (g.vehicle_number IS NULL) DESC,
                 NULLIF(regexp_replace(COALESCE(g.vehicle_number, ''), '\D', '', 'g'),
                        '')::bigint NULLS LAST,
                 g.vehicle_number
        """
    )
    return [dict(r) for r in rows]


async def set_group_event_types(telegram_group_id: int, event_types: set[str]) -> None:
    """Replace a group's event-type allowlist. An empty set clears the filter, which
    (per get_groups_for_event) means the group receives every type."""
    group_id = await db.fetchval(
        "SELECT id FROM alert_groups WHERE telegram_group_id = $1", telegram_group_id
    )
    if group_id is None:
        return
    await db.execute("DELETE FROM group_event_types WHERE group_id = $1", group_id)
    for event_type in event_types:
        await db.execute(
            "INSERT INTO group_event_types (group_id, event_type) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING",
            group_id, event_type,
        )


async def toggle_group_event_type(telegram_group_id: int, event_type: str) -> list[str]:
    """Toggle one event type in a group's filter and persist. Returns the new allowlist
    (empty = all types), which the caller uses to redraw the toggle keyboard."""
    current = set(await get_group_event_types(telegram_group_id))
    updated = next_event_filter(current, event_type)
    await set_group_event_types(telegram_group_id, updated)
    return sorted(updated)


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
