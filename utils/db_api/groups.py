"""The one Telegram group this deployment posts its alerts to.

There is no table of groups on this branch and nothing is registered by adding the bot
to a chat: the group is a single row, seeded once, and every vehicle's alerts go to it.
What an admin can change about it is whether it is muted and which event types it
receives — both from the panel, the first also by command in the group itself.
"""
import logging

from data import config
from data.event_catalog import complete_rows
from utils.db_api import db
from utils.db_api.legacy import legacy_group

logger = logging.getLogger(__name__)


async def ensure_group() -> None:
    """Seed the singleton alert_group row on first boot. A no-op on every boot after.

    The chat id comes from the old schema's routing table when this database has one,
    and from config.GROUP_CHAT_ID otherwise. In that order, because the old table is
    what tracked the group: it was rewritten whenever Telegram upgraded the chat to a
    supergroup, while the env var was never required and is 0 on the deployment this was
    written for. The group's event filter comes across with it.

    "Already seeded" is asked of the table rather than left to ON CONFLICT alone, so
    that the legacy lookup — several queries against tables that may not exist — runs
    once in the life of the database instead of on every start. The insert still names
    the conflict, for the two containers of a rolling deploy that both get here.

    With no source at all nothing is written, and the gap is logged as an error: alerts
    still reach subscribed admin DMs, and the panel says no group is configured.
    """
    if await db.fetchval("SELECT 1 FROM alert_group"):
        return

    chat_id, event_types = config.GROUP_CHAT_ID, set()
    try:
        legacy = await legacy_group()
    except Exception as e:
        logger.error(f"Could not read the old routing tables: {e}", exc_info=True)
        legacy = None
    if legacy:
        chat_id = legacy["telegram_group_id"]
        event_types = complete_rows(legacy["event_types"])
        if legacy["others"]:
            logger.warning(
                f"The old schema routed this company to more than one group. Alerts now "
                f"go to {chat_id} only; no longer posted to: {legacy['others']}"
            )

    if not chat_id:
        logger.error(
            "No alerts group is configured: the database has no group to carry over and "
            "GROUP_CHAT_ID is not set. Alerts will reach admin DMs only."
        )
        return

    inserted = await db.fetchval(
        "INSERT INTO alert_group (id, telegram_group_id) VALUES (1, $1) "
        "ON CONFLICT (id) DO NOTHING RETURNING id",
        chat_id,
    )
    if inserted is None:
        return  # another container seeded it between the check above and here
    await set_group_event_types(event_types)
    source = "the old schema" if legacy else "GROUP_CHAT_ID"
    scope = f"{len(event_types)} event types" if event_types else "every event type"
    logger.info(f"Alerts group set to {chat_id} from {source}, receiving {scope}")


async def get_alert_target(event_type: str | None = None) -> int | None:
    """The chat id an alert of this type goes to, or None if the group should not get it.

    None covers all three reasons the same way, because the caller does the same thing
    for each — it sends to the admin DMs alone: no group is configured, the group is
    muted, or its event filter does not list this type.

    Called without a type it answers for things that are not alerts about an event —
    the daily report — where only the mute applies. The filter says which ALERTS the
    chat wants; a digest of yesterday is not one of them.
    """
    return await db.fetchval(
        """
        SELECT g.telegram_group_id
        FROM alert_group g
        WHERE g.enabled
          AND (
                  $1::text IS NULL
               OR NOT EXISTS (SELECT 1 FROM alert_group_event_types)
               OR EXISTS (SELECT 1 FROM alert_group_event_types WHERE event_type = $1)
              )
        """,
        event_type,
    )


async def get_group_status() -> dict | None:
    """{"telegram_group_id", "enabled", "event_types"} for the panel's Group screen, or
    None if no group has been configured. An empty event_types list is the "every type"
    state, not "no types"."""
    row = await db.fetchrow(
        """
        SELECT g.telegram_group_id, g.enabled,
               ARRAY(SELECT t.event_type FROM alert_group_event_types t
                     ORDER BY t.event_type) AS event_types
        FROM alert_group g
        """
    )
    return dict(row) if row else None


async def get_group_chat_id() -> int | None:
    """The chat id of the one group, muted or not — for recognising it (as /report and
    /top do) as opposed to deciding whether to alert it."""
    return await db.fetchval("SELECT telegram_group_id FROM alert_group")


async def is_the_group(telegram_group_id: int) -> bool:
    """True if this chat is the group this deployment posts to.

    Compared against the row, never config.GROUP_CHAT_ID: the row is what follows the
    chat through a supergroup migration, and on an upgraded deployment the env var was
    never set at all.
    """
    return telegram_group_id == await get_group_chat_id()


async def set_group_enabled(enabled: bool) -> None:
    """Mute (enabled=False) or unmute (enabled=True) the group's alerts."""
    await db.execute("UPDATE alert_group SET enabled = $1", enabled)


async def migrate_group(old_id: int, new_id: int) -> None:
    """Point the alert group at its new chat id after Telegram upgrades it to a
    supergroup."""
    await db.execute(
        "UPDATE alert_group SET telegram_group_id = $1 WHERE telegram_group_id = $2",
        new_id, old_id,
    )


async def get_group_event_types() -> list[str]:
    """The group's event-type allowlist. Empty list = every type."""
    rows = await db.fetch(
        "SELECT event_type FROM alert_group_event_types ORDER BY event_type"
    )
    return [r["event_type"] for r in rows]


async def set_group_event_types(event_types: set[str]) -> None:
    """Replace the group's event-type allowlist. An empty set clears the filter, which
    (per get_alert_target) means the group receives every type.

    In one transaction: between the delete and the inserts the table is empty, and empty
    means "every type" — an alert routed in that gap would reach the group for a type an
    admin had just finished turning off.
    """
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM alert_group_event_types")
            if event_types:
                await conn.executemany(
                    "INSERT INTO alert_group_event_types (event_type) VALUES ($1) "
                    "ON CONFLICT DO NOTHING",
                    [(t,) for t in sorted(event_types)],
                )
