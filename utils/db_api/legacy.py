"""Carrying a database that an older build created over to this one.

Every company on this branch got here from somewhere else, and its database shows it.

HF's and MZ CARGO's ran the multi-company bot's schema: a `companies` table, the alerts
group as a `company_groups` row with its event filter in `group_event_types`, and a
`company_slug` on every violation and on every Motive crash confirmation. None of that
was ever configured in the environment — the group's chat id lives only in the database
— so a deploy that simply started on the new tables would come up with months of history
it could no longer write next to and no idea where its alerts go.

JTR's was created by the single-group template this branch replaces. Its tables are
already the new ones, less the group's event filter — and that template kept crash
alerts out of the group in code, which this build does with the filter. So the one thing
to carry over there is a rule (adopt_single_group_template).

Everything here reads the old tables and leaves them as they are. That is deliberate:
the previous build keeps working against this database, so rolling a bad deploy back is
a redeploy and not a restore.

On a database none of this applies to, every function here finds nothing and does
nothing.
"""
import logging

from data import config
from data.event_catalog import EVENT_TYPES
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


async def legacy_company() -> dict | None:
    """This deployment's row in the old `companies` table, or None.

    Returns {"id", "slug", "name", "by_slug"}. The row is the one whose slug is
    COMPANY_SLUG when there is one. When there is not, and the table holds exactly one
    company, that company is it (`by_slug` False).

    The second rule is not a convenience. The old build took the company from the
    webhook's own path whenever the path named one (/webhook/motive/<slug>) and only
    fell back to COMPANY_SLUG when it did not, so on a deployment whose providers were
    given such a URL the variable was never consulted and nothing ever held it to the
    table. This company has been spelled three ways across its checkouts. A database of
    one company belongs to that company whatever the variable says, and reading it that
    way is what stands between a mismatch and a group that silently stops being found.

    With several companies and none matching there is no telling them apart, and the
    answer is None.
    """
    if not await _has_table("companies"):
        return None
    row = await db.fetchrow(
        "SELECT id, slug, name FROM companies WHERE slug = $1", config.COMPANY_SLUG
    )
    if row:
        return {**dict(row), "by_slug": True}
    rows = await db.fetch("SELECT id, slug, name FROM companies ORDER BY id LIMIT 2")
    if len(rows) == 1:
        return {**dict(rows[0]), "by_slug": False}
    return None


async def _row_slug() -> str:
    """The slug the old build's rows carry for this company — the one new rows should
    keep carrying, so that both builds go on seeing the same history."""
    company = await legacy_company()
    return company["slug"] if company else config.COMPANY_SLUG


async def adopt_legacy_company() -> None:
    """Take the company's display name from the old schema, if the environment gave none.

    Reports and the panel head every page with config.COMPANY_NAME. The old build read
    that name from the `companies` row, and the deployment has never had a variable for
    it, so without this an upgrade would quietly rename the company on every report to
    whatever this file's default happens to say.

    Also where a COMPANY_SLUG that matches nothing in the table is said out loud, once.
    Never raises: a name is not worth a boot.
    """
    try:
        company = await legacy_company()
    except Exception as e:
        logger.error(f"Could not read the old companies table: {e}", exc_info=True)
        return
    if not company:
        return
    if not company["by_slug"]:
        logger.warning(
            f"COMPANY_SLUG is '{config.COMPANY_SLUG}' but the only company in this "
            f"database is '{company['slug']}'. Treating that one as this deployment's "
            f"company; set COMPANY_SLUG={company['slug']} to make the two agree."
        )
    name = (company.get("name") or "").strip()
    if name and not config.COMPANY_NAME_FROM_ENV:
        config.COMPANY_NAME = name
        logger.info(f"Company name '{name}' taken from the old schema")


async def _adopt_slug_column(table: str, slug: str) -> bool:
    """Make `table`.company_slug optional and default it to `slug`. False if the table
    has no such column (or does not exist), which is every database but an upgraded one.

    A default rather than merely nullable, so new rows are still labelled the way the
    old ones are — which is what keeps them visible to the previous build after a
    rollback. Safe on every boot: both ALTERs are no-ops once applied.
    """
    if not await _has_column(table, "company_slug"):
        return False
    # DDL takes no bind parameters, so both statements are built by format() on the
    # server: %I quotes the table as an identifier and %L the slug as a literal.
    for template, args in (
        ("ALTER TABLE %I ALTER COLUMN company_slug DROP NOT NULL", (table,)),
        ("ALTER TABLE %I ALTER COLUMN company_slug SET DEFAULT %L", (table, slug)),
    ):
        placeholders = ", ".join(f"${i}::text" for i in range(1, len(args) + 1))
        statement = await db.fetchval(f"SELECT format('{template}', {placeholders})", *args)
        await db.execute(statement)
    return True


async def adopt_legacy_violations() -> None:
    """Let this build insert into a `violations` table that still has `company_slug`.

    The old column is NOT NULL with no default, and save_violation no longer supplies
    it, so every insert would be refused. _persist_once treats a failed insert as "alert
    anyway", which means nothing would look wrong: the alerts would keep arriving while
    the table quietly stopped growing and every report came back empty.
    """
    slug = await _row_slug()
    if not await _adopt_slug_column("violations", slug):
        return

    # Nothing on this branch filters by company any more, because one deployment is one
    # company. If this table was ever shared, that assumption is wrong and every report
    # and the whole panel would be counting somebody else's trucks — say so at boot
    # rather than leave it to be noticed in the numbers.
    shared = await db.fetchval(
        "SELECT EXISTS (SELECT 1 FROM violations "
        "WHERE company_slug IS NOT NULL AND company_slug <> $1)",
        slug,
    )
    if shared:
        logger.error(
            f"The violations table holds rows for companies other than '{slug}'. This "
            f"build reports on the whole table, so those rows WILL appear in this "
            f"company's reports and panel. Give this deployment a database of its own."
        )


async def adopt_legacy_crash_confirmations() -> None:
    """The same, for `motive_crash_confirmations`.

    The old build stamped each held crash with its company; record_pending no longer
    does. Left alone, that insert fails on an upgraded table — and it fails quietly,
    because _note_crash swallows it so that a bookkeeping error can never cost a crash
    alert. The alert would still go out. What would be gone is the row that lets a crash
    held mid-confirmation survive a restart, and nobody would find that out until the
    deploy that lost one.
    """
    await _adopt_slug_column("motive_crash_confirmations", await _row_slug())


async def legacy_group() -> dict | None:
    """The alerts group the old schema was sending to, with its event filter.

    Returns {"telegram_group_id", "event_types", "others"} or None when there is no old
    routing table (a fresh database) or it holds no group for this company. `others` are
    further groups the old schema had for it: this build posts to one chat, so they are
    reported to the caller rather than silently dropped.

    A company's own row wins over a catch-all one (company_id IS NULL), and the oldest
    wins among equals — the row seed.sql created as 'main'. Which rows are the company's
    own is legacy_company()'s call; with no company identified only the catch-all rows
    are left.
    """
    if not await _has_table("company_groups") or not await _has_table("companies"):
        return None
    company = await legacy_company()
    rows = await db.fetch(
        """
        SELECT cg.id, cg.telegram_group_id
        FROM company_groups cg
        WHERE cg.company_id = $1::int OR cg.company_id IS NULL
        ORDER BY (cg.company_id IS NULL), cg.id
        """,
        company["id"] if company else None,
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


async def adopt_single_group_template() -> bool:
    """Keep crash alerts out of the group on a database the old single-group template made.

    That template sent a crash to the admins' DMs and never to the group, as a rule in
    its code. Here a crash is routed like every other type and the group's filter decides
    — and an empty filter means "every type". Started on such a database unprepared, this
    build would begin posting crashes into a group that has never had one, with nobody
    having decided that it should.

    So the rule is carried over as what it now is: a filter listing every type except
    crashes. From then on it is an admin's to change, on the panel's Group screen, like
    the rest of the filter.

    Every type the build knows, and not only the ones this deployment alerts on today:
    this runs once, on the first boot, and which optional types the deployment takes is a
    variable that may be set — or corrected — after it. A filter written from that
    variable would go on keeping out of the group whatever was added to it later.

    The template's mark is an `alert_group` table with no `alert_group_event_types`
    beside it. That is why this has to run BEFORE schemas.sql is applied, which creates
    the filter table on any database that lacks it. The table and its rows are written
    in one transaction, so there is no moment — and no crash of this process — that
    leaves the table present and empty, which would read as "every type" for good. Two
    containers of a rolling deploy cannot both do it: the second one's CREATE fails.

    Returns True if it carried the rule over. A database from any other origin has
    either both tables or neither, and is left alone.
    """
    if not await _has_table("alert_group") or await _has_table("alert_group_event_types"):
        return False
    event_types = sorted(set(EVENT_TYPES) - {"crash"})
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            # The same definition schemas.sql gives it; that file's IF NOT EXISTS then
            # finds it already there.
            await conn.execute(
                "CREATE TABLE alert_group_event_types (event_type VARCHAR(50) PRIMARY KEY)"
            )
            await conn.executemany(
                "INSERT INTO alert_group_event_types (event_type) VALUES ($1)",
                [(t,) for t in event_types],
            )
    logger.info(
        f"This database is from the single-group template: crash alerts stay out of the "
        f"group, as they did there. The group's filter now lists every other event "
        f"type; an admin can change it on the Group screen."
    )
    return True
