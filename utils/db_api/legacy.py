"""Carrying a database from the old multi-company schema over to this one.

HF's deployment ran the multi-company bot's schema before this branch took single-company
as its base: a `companies` table, the alerts group as a `company_groups` row with its
event filter in `group_event_types`, and a `company_slug` on every violation. None of
that was ever configured in the environment — the group's chat id lives only in the
database — so a deploy that simply started on the new tables would come up with months
of history it could no longer write next to and no idea where its alerts go.

Everything here reads the old tables and leaves them as they are. That is deliberate:
the previous build keeps working against this database, so rolling a bad deploy back is
a redeploy and not a restore.

On a database that never had the old schema, every function here finds nothing and does
nothing.
"""
import logging

from data import config
from utils.db_api import db

logger = logging.getLogger(__name__)


async def _has_table(name: str) -> bool:
    return bool(await db.fetchval("SELECT to_regclass($1::text) IS NOT NULL", name))


async def _has_column(table: str, column: str) -> bool:
    """Asked of pg_attribute, with both parameters cast, rather than of
    information_schema: its columns are domain types (sql_identifier), and a bare $1
    compared against one leaves asyncpg to work out how to encode a str for it."""
    return bool(await db.fetchval(
        "SELECT 1 FROM pg_attribute "
        "WHERE attrelid = to_regclass($1::text) AND attname = $2::text "
        "AND attnum > 0 AND NOT attisdropped",
        table, column,
    ))


async def adopt_legacy_violations() -> None:
    """Let this build insert into a `violations` table that still has `company_slug`.

    The old column is NOT NULL with no default, and save_violation no longer supplies
    it, so every insert would be refused. _persist_once treats a failed insert as "alert
    anyway", which means nothing would look wrong: the alerts would keep arriving while
    the table quietly stopped growing and every report came back empty.

    The column gets this company's slug as its default rather than merely becoming
    nullable, so new rows are still labelled the way the old ones are — which is what
    keeps them visible to the previous build's reports after a rollback.

    Safe to run on every boot: both ALTERs are no-ops once applied.
    """
    if not await _has_column("violations", "company_slug"):
        return
    await db.execute("ALTER TABLE violations ALTER COLUMN company_slug DROP NOT NULL")
    # A default is DDL and takes no bind parameters, so the statement is built by
    # format('%L'), which quotes the slug as a literal on the server.
    statement = await db.fetchval(
        "SELECT format('ALTER TABLE violations ALTER COLUMN company_slug SET DEFAULT %L', "
        "$1::text)",
        config.COMPANY_SLUG,
    )
    await db.execute(statement)

    # Nothing on this branch filters by company any more, because one deployment is one
    # company. If this table was ever shared, that assumption is wrong and every report
    # and the whole panel would be counting somebody else's trucks — say so at boot
    # rather than leave it to be noticed in the numbers.
    shared = await db.fetchval(
        "SELECT EXISTS (SELECT 1 FROM violations "
        "WHERE company_slug IS NOT NULL AND company_slug <> $1)",
        config.COMPANY_SLUG,
    )
    if shared:
        logger.error(
            f"The violations table holds rows for companies other than "
            f"'{config.COMPANY_SLUG}'. This build reports on the whole table, so those "
            f"rows WILL appear in this company's reports and panel. Give this deployment "
            f"a database of its own."
        )


async def legacy_group() -> dict | None:
    """The alerts group the old schema was sending to, with its event filter.

    Returns {"telegram_group_id", "event_types", "others"} or None when there is no old
    routing table (a fresh database) or it holds no group for this company. `others` are
    further groups the old schema had for it: this build posts to one chat, so they are
    reported to the caller rather than silently dropped.

    A company's own row wins over a catch-all one (company_id IS NULL), and the oldest
    wins among equals — the row seed.sql created as 'main'.
    """
    if not await _has_table("company_groups") or not await _has_table("companies"):
        return None
    rows = await db.fetch(
        """
        SELECT cg.id, cg.telegram_group_id
        FROM company_groups cg
        LEFT JOIN companies c ON c.id = cg.company_id
        WHERE c.slug = $1 OR cg.company_id IS NULL
        ORDER BY (cg.company_id IS NULL), cg.id
        """,
        config.COMPANY_SLUG,
    )
    if not rows:
        return None

    event_types: list[str] = []
    if await _has_table("group_event_types"):
        filter_rows = await db.fetch(
            "SELECT event_type FROM group_event_types WHERE group_id = $1", rows[0]["id"]
        )
        event_types = [r["event_type"] for r in filter_rows]

    chat_id = rows[0]["telegram_group_id"]
    return {
        "telegram_group_id": chat_id,
        "event_types": event_types,
        # A set, less the chosen one: the old table has no unique key on the chat id, so
        # the same group can be in it twice, and that is not a second group.
        "others": sorted({r["telegram_group_id"] for r in rows[1:]} - {chat_id}),
    }
