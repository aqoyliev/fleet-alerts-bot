"""Booting on a database that still has the old multi-company schema.

That is HF's database: `companies`, the alerts group as a `company_groups` row with its
filter in `group_event_types`, and a NOT NULL `company_slug` on every violation. The
deploy that brings this build in runs against it with nobody doing anything by hand, so
whatever has to change changes at startup — and it must not touch anything the previous
build reads, because rolling back is how a bad deploy gets fixed.

The database here is a scripted fake keyed on what each query is looking for. What is
being tested is which questions legacy.py asks and what it does with the answers; the
SQL itself is checked for the properties that matter and otherwise left to Postgres.
"""
import pytest

from data import config
from utils.db_api import legacy


class _Db:
    """A database described by what exists in it."""

    def __init__(self, tables=(), columns=(), groups=(), filters=None, foreign_rows=False):
        self.tables = set(tables)
        self.columns = set(columns)            # {(table, column)}
        self.groups = list(groups)             # rows of company_groups, already ordered
        self.filters = filters or {}           # {company_groups.id: [event_type, ...]}
        self.foreign_rows = foreign_rows
        self.executed: list[str] = []
        self.queries: list[tuple[str, tuple]] = []

    async def fetchval(self, query, *args):
        q = " ".join(query.split())
        self.queries.append((q, args))
        # The column check first: it names its table through to_regclass too.
        if "FROM pg_attribute" in q:
            return 1 if (args[0], args[1]) in self.columns else None
        if "to_regclass" in q:
            return args[0] in self.tables
        if q.startswith("SELECT format("):
            # What Postgres's format('%L') does with a plain slug.
            return f"ALTER TABLE violations ALTER COLUMN company_slug SET DEFAULT '{args[0]}'"
        if q.startswith("SELECT EXISTS"):
            return self.foreign_rows
        raise AssertionError(f"unexpected fetchval: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        self.queries.append((q, args))
        if "FROM company_groups" in q:
            return self.groups
        if "FROM group_event_types" in q:
            return [{"event_type": t} for t in self.filters.get(args[0], [])]
        raise AssertionError(f"unexpected fetch: {q}")

    async def execute(self, query, *args):
        self.executed.append(" ".join(query.split()))


@pytest.fixture
def use(monkeypatch):
    monkeypatch.setattr(config, "COMPANY_SLUG", "hf")

    def _install(db):
        monkeypatch.setattr(legacy, "db", db)
        return db
    return _install


OLD_TABLES = ("companies", "company_groups", "group_event_types")


# ── violations: a table this build could not otherwise write to ────────────────

async def test_the_old_company_column_stops_refusing_inserts(use):
    """NOT NULL with no default, and save_violation no longer supplies it. Left alone,
    every insert fails, _persist_once shrugs and alerts anyway, and the only symptom is
    that the reports are empty from the day of the deploy."""
    db = use(_Db(columns={("violations", "company_slug")}))

    await legacy.adopt_legacy_violations()

    assert db.executed == [
        "ALTER TABLE violations ALTER COLUMN company_slug DROP NOT NULL",
        "ALTER TABLE violations ALTER COLUMN company_slug SET DEFAULT 'hf'",
    ]


async def test_new_rows_are_still_labelled_with_the_company(use):
    """A default, not just nullable: after a rollback the previous build filters its
    reports by company_slug, and rows left NULL would vanish from them."""
    db = use(_Db(columns={("violations", "company_slug")}))

    await legacy.adopt_legacy_violations()

    assert db.executed[-1].endswith("SET DEFAULT 'hf'")


async def test_the_slug_reaches_the_ddl_as_a_quoted_literal_not_by_string_formatting(use):
    """DDL takes no bind parameters, so the statement has to be assembled — and it is
    assembled by Postgres's own format('%L'), with the slug bound, not by Python."""
    db = use(_Db(columns={("violations", "company_slug")}))

    await legacy.adopt_legacy_violations()

    query, args = next((q, a) for q, a in db.queries if q.startswith("SELECT format("))
    assert "%L" in query
    assert args == ("hf",)


async def test_a_fresh_database_is_not_touched(use):
    """The new schema has no company_slug at all. Nothing to relax, nothing to run."""
    db = use(_Db())

    await legacy.adopt_legacy_violations()

    assert db.executed == []


async def test_rows_belonging_to_another_company_are_reported_at_boot(use, caplog):
    """Nothing here filters by company any more. If the table was ever shared, every
    report would be counting somebody else's trucks — which has to be said, since it
    would never show up as an error."""
    use(_Db(columns={("violations", "company_slug")}, foreign_rows=True))

    with caplog.at_level("ERROR"):
        await legacy.adopt_legacy_violations()

    assert "other than 'hf'" in caplog.text


async def test_a_table_holding_only_this_company_is_not_complained_about(use, caplog):
    use(_Db(columns={("violations", "company_slug")}, foreign_rows=False))

    with caplog.at_level("ERROR"):
        await legacy.adopt_legacy_violations()

    assert caplog.text == ""


# ── the group and its filter ───────────────────────────────────────────────────

async def test_a_fresh_database_has_no_legacy_group(use):
    db = use(_Db())

    assert await legacy.legacy_group() is None
    # And it found that out without selecting from a table that isn't there.
    assert not any("FROM company_groups" in q for q, _ in db.queries)


async def test_the_group_and_its_filter_are_read_from_the_old_tables(use):
    use(_Db(tables=OLD_TABLES,
            groups=[{"id": 1, "telegram_group_id": -1003786362177}],
            filters={1: ["speeding", "crash", "hard_brake"]}))

    assert await legacy.legacy_group() == {
        "telegram_group_id": -1003786362177,
        "event_types": ["speeding", "crash", "hard_brake"],
        "others": [],
    }


async def test_the_lookup_is_for_this_company_or_a_catch_all_row(use):
    db = use(_Db(tables=OLD_TABLES, groups=[{"id": 1, "telegram_group_id": -100}]))

    await legacy.legacy_group()

    query, args = next((q, a) for q, a in db.queries if "FROM company_groups" in q)
    assert "c.slug = $1 OR cg.company_id IS NULL" in query
    # The company's own row first, then the oldest: the one seed.sql labelled 'main'.
    assert "ORDER BY (cg.company_id IS NULL), cg.id" in query
    assert args == ("hf",)


async def test_a_group_with_no_filter_rows_carries_the_all_types_state(use):
    use(_Db(tables=OLD_TABLES, groups=[{"id": 1, "telegram_group_id": -100}]))

    assert (await legacy.legacy_group())["event_types"] == []


async def test_the_filter_read_is_the_first_groups_own(use):
    use(_Db(tables=OLD_TABLES,
            groups=[{"id": 7, "telegram_group_id": -100}, {"id": 9, "telegram_group_id": -200}],
            filters={7: ["speeding"], 9: ["crash"]}))

    found = await legacy.legacy_group()

    assert found["telegram_group_id"] == -100
    assert found["event_types"] == ["speeding"]
    assert found["others"] == [-200]


async def test_the_same_chat_listed_twice_is_not_a_second_group(use):
    """The old table has no unique key on the chat id, and seed.sql run twice puts the
    same group in it twice. That must not be reported as a group being dropped."""
    use(_Db(tables=OLD_TABLES,
            groups=[{"id": 1, "telegram_group_id": -100}, {"id": 2, "telegram_group_id": -100}]))

    assert (await legacy.legacy_group())["others"] == []


async def test_an_old_schema_with_no_group_for_this_company_has_nothing_to_carry(use):
    use(_Db(tables=OLD_TABLES, groups=[]))

    assert await legacy.legacy_group() is None


async def test_nothing_in_the_old_tables_is_ever_written(use):
    """The whole of the rollback story. Reading is all this module does to them."""
    db = use(_Db(tables=OLD_TABLES, columns={("violations", "company_slug")},
                 groups=[{"id": 1, "telegram_group_id": -100}], filters={1: ["speeding"]}))

    await legacy.legacy_group()
    await legacy.adopt_legacy_violations()

    for statement in db.executed:
        assert statement.startswith("ALTER TABLE violations ALTER COLUMN company_slug")
    for name in ("companies", "company_groups", "group_event_types", "admin_companies"):
        assert not any(name in s for s in db.executed)
