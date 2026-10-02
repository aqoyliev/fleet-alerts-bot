from data import config
from data.event_catalog import next_event_filter
from utils.db_api import db


async def get_groups_for_event(event_type: str, vehicle_number: str | None = None) -> list[int]:
    """Returns telegram_group_ids that should receive this event.

    Two kinds of group qualify:
      • the all-fleet main group — receives every unit's alerts, and
      • the driver group whose vehicle_number matches this event's unit.
    Either way the group's optional group_event_types filter still applies: a group with
    no rows there receives every type; otherwise only the types listed for it.

    The main group is recognized by its is_main flag, not only by matching
    config.MAIN_GROUP_ID: when Telegram upgrades a group to a supergroup its chat id
    changes, _migrate_group writes the new id into this table, and the id in .env is
    then the old one — which used to leave the main group silently receiving nothing.
    The config id stays in the test as a belt-and-braces match for the row
    ensure_main_group creates.

    The flag replaced "vehicle_number IS NULL" so that a NULL unit could start meaning
    something else: a group registered on join but not yet pointed at a truck (see
    register_unassigned_group). Under the old rule such a row was indistinguishable from
    the all-fleet group and would have received every unit's alerts, which is why the
    join handler used not to register one at all — and why a group the bot had just been
    added to was invisible in the admin panel that exists to configure it.

    is_main is read through a COALESCE onto the old rule so that a row written by the
    previous code — by the outgoing container during a rolling deploy, before the
    backfill — still routes the way it always did.
    """
    rows = await db.fetch(
        """
        SELECT g.telegram_group_id
        FROM alert_groups g
        WHERE COALESCE(g.enabled, TRUE)
          AND (
                  ($2::text   IS NOT NULL AND g.vehicle_number = $2)
               OR COALESCE(g.is_main, g.vehicle_number IS NULL)
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
    main / catch-all groups. Driver groups are per-unit and are intentionally left out of
    the company-wide digest — and so is a group that is registered but has no unit yet,
    which is why this asks for is_main rather than for a NULL unit."""
    rows = await db.fetch(
        "SELECT telegram_group_id FROM alert_groups "
        "WHERE COALESCE(is_main, vehicle_number IS NULL) AND COALESCE(enabled, TRUE)"
    )
    return [r["telegram_group_id"] for r in rows]


async def get_driver_groups() -> list[int]:
    """Returns the enabled groups bound to a unit — the drivers' own chats, and the exact
    complement of get_all_groups.

    This is who a pre-trip reminder is for: the person about to walk around the truck.
    The dispatcher/office group is deliberately not included — it would get the same
    poster every morning and learn nothing from it.

    A group's event-type filter is not consulted. That filter answers "which ALERTS does
    this chat want", and a reminder is not an alert about anything that happened; the one
    switch that does apply is `enabled`, because a muted group means "stop posting here".
    """
    rows = await db.fetch(
        "SELECT telegram_group_id FROM alert_groups "
        "WHERE vehicle_number IS NOT NULL AND COALESCE(enabled, TRUE) "
        "ORDER BY telegram_group_id"
    )
    return [r["telegram_group_id"] for r in rows]


async def register_group(telegram_group_id: int, title: str | None,
                         vehicle_number: str | None, is_main: bool = False) -> None:
    """Insert (or update) a configured group. Called with the parsed unit for a driver
    group, or with is_main=True and no unit for the all-fleet group.

    Re-registering clears any mute. A group that went silent because the bot was kicked
    is muted automatically (see _drop_unreachable), and nobody would think to unmute it
    by hand afterwards — putting the bot back in the group is the intent signal, so it
    is what turns the alerts back on."""
    await db.execute(
        """
        INSERT INTO alert_groups (telegram_group_id, title, vehicle_number, is_main)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (telegram_group_id) DO UPDATE
            SET title = EXCLUDED.title,
                vehicle_number = EXCLUDED.vehicle_number,
                is_main = EXCLUDED.is_main,
                enabled = TRUE,
                left_at = NULL
        """,
        telegram_group_id, title, vehicle_number, is_main,
    )


async def register_unassigned_group(telegram_group_id: int, title: str | None) -> None:
    """Record a group the bot has just been added to but could not point at a truck.

    This is what puts it in the admin panel. Nothing routes to it — it is neither a unit's
    group nor the all-fleet one — so until someone picks its unit it receives exactly what
    it received before this existed, which is nothing. What changes is that it is now
    visible in the one screen built for choosing that unit, instead of being inferable
    only from a message in the chat and a DM to the admins.

    vehicle_number and is_main are deliberately left alone on conflict. The bot being
    re-added to an already-configured group runs through here whenever the title has
    stopped parsing, and overwriting the unit there would quietly unconfigure a working
    group — the one outcome worse than not registering it at all.
    """
    await db.execute(
        """
        INSERT INTO alert_groups (telegram_group_id, title, vehicle_number, is_main)
        VALUES ($1, $2, NULL, FALSE)
        ON CONFLICT (telegram_group_id) DO UPDATE
            SET title = EXCLUDED.title,
                enabled = TRUE,
                left_at = NULL
        """,
        telegram_group_id, title,
    )


async def get_group(telegram_group_id: int) -> dict | None:
    """Return the full registration row for a group, or None if it isn't registered.

    left_at is part of the row because "registered" and "listed in the panel" are two
    different questions: the row of a group the bot was thrown out of survives so that
    putting the bot back restores its unit (see mark_group_left), and a caller deciding
    whether that group still needs attaching has to be able to tell the two apart.
    """
    row = await db.fetchrow(
        """
        SELECT id, telegram_group_id, title, vehicle_number, left_at,
               COALESCE(enabled, TRUE) AS enabled
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


async def mark_group_left(telegram_group_id: int) -> None:
    """Record that the bot is no longer in this chat: hide it from the panel and stop
    anything being addressed to it.

    Not a delete. The row holds the group's unit and its event filter, and someone who
    removes the bot for an afternoon and puts it back should not have to set those again —
    register_group and register_unassigned_group both clear left_at, so re-adding the bot
    restores the group exactly as it was.

    enabled is set alongside because that is the flag every routing query already reads;
    left_at is what the panel reads. One says "do not post here", the other says "do not
    offer this to configure", and a chat the bot has been thrown out of is both.
    """
    await db.execute(
        "UPDATE alert_groups SET left_at = NOW(), enabled = FALSE "
        "WHERE telegram_group_id = $1",
        telegram_group_id,
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
    group is pinned first because it behaves unlike every other row, and the groups with
    no unit yet come next: they are the only rows in this list that are not doing anything
    and the only ones the reader has to act on.
    """
    rows = await db.fetch(
        r"""
        SELECT g.id, g.telegram_group_id, g.title, g.vehicle_number,
               COALESCE(g.is_main, g.vehicle_number IS NULL) AS is_main,
               COALESCE(g.enabled, TRUE) AS enabled, g.created_at,
               ARRAY(SELECT t.event_type FROM group_event_types t
                     WHERE t.group_id = g.id ORDER BY t.event_type) AS event_types
        FROM alert_groups g
        WHERE g.left_at IS NULL
        ORDER BY COALESCE(g.is_main, g.vehicle_number IS NULL) DESC,
                 (g.vehicle_number IS NULL) DESC,
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
    """Guarantee config.MAIN_GROUP_ID exists as a main-group row so it receives all alerts
    and works with /report even if the bot was never re-added after configuring it.

    The conflict branch sets the flag rather than doing nothing: a row that predates
    is_main, or one a previous boot wrote as an ordinary group, is exactly the case where
    the setting has been right all along and the routing silently has not.
    """
    if not config.MAIN_GROUP_ID:
        return
    await db.execute(
        """
        INSERT INTO alert_groups (telegram_group_id, vehicle_number, is_main)
        VALUES ($1, NULL, TRUE)
        ON CONFLICT (telegram_group_id) DO UPDATE SET is_main = TRUE
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
