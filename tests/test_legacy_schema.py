"""Booting on a database that still has the old multi-company schema.

That is MZ CARGO's database: `companies`, the alerts group as a `company_groups` row
with its filter in `group_event_types`, and a NOT NULL `company_slug` on every violation
and every Motive crash confirmation. The deploy that brings this build in runs against it
with nobody doing anything by hand, so whatever has to change changes at startup — and it
must not touch anything the previous build reads, because rolling back is how a bad
deploy gets fixed.

The database here is a scripted fake keyed on what each query is looking for. What is
being tested is which questions legacy.py asks and what it does with the answers; the
SQL itself is checked for the properties that matter and otherwise left to Postgres
(and to the run against a real one that goes with each change to this module).
"""
import pytest

from data import config
from utils.db_api import legacy

MZ = {"id": 1, "slug": "mzcargo", "name": "MZ Cargo Inc"}


class _Db:
    """A database described by what exists in it."""

    def __init__(self, tables=(), columns=(), companies=(), groups=(), filters=None,
                 foreign_rows=False):
        self.tables = set(tables)
        self.columns = set(columns)            # {(table, column)}
        self.companies = list(companies)       # rows of companies, in id order
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
            # What Postgres's format() does with %I and %L for plain names.
            template = q.split("'")[1]
            out = template.replace("%I", args[0])
            return out.replace("%L", f"'{args[1]}'") if len(args) > 1 else out
        if q.startswith("SELECT EXISTS"):
            self.foreign_check_slug = args[0]
            return self.foreign_rows
        raise AssertionError(f"unexpected fetchval: {q}")

    async def fetchrow(self, query, *args):
        q = " ".join(query.split())
        self.queries.append((q, args))
        if "FROM companies WHERE slug = $1" in q:
            return next((c for c in self.companies if c["slug"] == args[0]), None)
        raise AssertionError(f"unexpected fetchrow: {q}")

    async def fetch(self, query, *args):
        q = " ".join(query.split())
        self.queries.append((q, args))
        if "FROM companies" in q:
            assert "LIMIT 2" in q
            return self.companies[:2]
        if "FROM company_groups" in q:
            return self.groups
        if "FROM group_event_types" in q:
            return [{"event_type": t} for t in self.filters.get(args[0], [])]
        raise AssertionError(f"unexpected fetch: {q}")

    async def execute(self, query, *args):
        self.executed.append(" ".join(query.split()))


@pytest.fixture
def use(monkeypatch):
    monkeypatch.setattr(config, "COMPANY_SLUG", "mzcargo")
    monkeypatch.setattr(config, "COMPANY_NAME", "MZ CARGO INC")
    monkeypatch.setattr(config, "COMPANY_NAME_FROM_ENV", False)

    def _install(db):
        monkeypatch.setattr(legacy, "db", db)
        return db
    return _install


OLD_TABLES = ("companies", "company_groups", "group_event_types")


# ── which company this database belongs to ─────────────────────────────────────

async def test_the_company_is_the_row_with_this_deployments_slug(use):
    use(_Db(tables=OLD_TABLES, companies=[{"id": 4, "slug": "other", "name": "Other"}, MZ]))

    assert await legacy.legacy_company() == {**MZ, "by_slug": True}


async def test_a_database_of_one_company_is_that_companys_whatever_the_slug_says(use):
    """The old build read the company from the webhook's path when the path named one,
    so on such a deployment COMPANY_SLUG was never consulted and nothing ever held it to
    the table. MZ has been spelled mzcargo, mz_cargo and mz-cargo in three places."""
    use(_Db(tables=OLD_TABLES, companies=[{"id": 1, "slug": "mz_cargo", "name": "MZ Cargo"}]))

    found = await legacy.legacy_company()

    assert found["slug"] == "mz_cargo"
    assert found["by_slug"] is False


async def test_several_companies_and_no_match_is_nobody(use):
    """Two candidates and nothing to choose between them by. Guessing here would route
    one company's alerts to another's group."""
    use(_Db(tables=OLD_TABLES, companies=[{"id": 1, "slug": "a", "name": "A"},
                                          {"id": 2, "slug": "b", "name": "B"}]))

    assert await legacy.legacy_company() is None


async def test_a_fresh_database_has_no_legacy_company(use):
    db = use(_Db())

    assert await legacy.legacy_company() is None
    assert not any("FROM companies" in q for q, _ in db.queries)


# ── the company's name ─────────────────────────────────────────────────────────

async def test_the_name_on_the_reports_stays_the_one_the_old_schema_held(use):
    """The deployment has never had a COMPANY_NAME variable: the old build read the name
    from the table. Without this an upgrade renames the company on every report."""
    use(_Db(tables=OLD_TABLES, companies=[MZ]))

    await legacy.adopt_legacy_company()

    assert config.COMPANY_NAME == "MZ Cargo Inc"


async def test_a_name_set_in_the_environment_overrules_the_table(use, monkeypatch):
    monkeypatch.setattr(config, "COMPANY_NAME", "Set By Hand")
    monkeypatch.setattr(config, "COMPANY_NAME_FROM_ENV", True)
    use(_Db(tables=OLD_TABLES, companies=[MZ]))

    await legacy.adopt_legacy_company()

    assert config.COMPANY_NAME == "Set By Hand"


async def test_a_blank_name_in_the_table_leaves_the_default_alone(use):
    use(_Db(tables=OLD_TABLES, companies=[{**MZ, "name": "  "}]))

    await legacy.adopt_legacy_company()

    assert config.COMPANY_NAME == "MZ CARGO INC"


async def test_a_slug_that_matches_nothing_is_said_out_loud(use, caplog):
    use(_Db(tables=OLD_TABLES, companies=[{"id": 1, "slug": "mz_cargo", "name": "MZ Cargo"}]))

    with caplog.at_level("WARNING"):
        await legacy.adopt_legacy_company()

    assert "COMPANY_SLUG is 'mzcargo'" in caplog.text
    assert "'mz_cargo'" in caplog.text


async def test_a_matching_slug_is_not_remarked_on(use, caplog):
    use(_Db(tables=OLD_TABLES, companies=[MZ]))

    with caplog.at_level("WARNING"):
        await legacy.adopt_legacy_company()

    assert caplog.text == ""


async def test_reading_the_name_never_stops_the_boot(use, caplog):
    class _Broken(_Db):
        async def fetchrow(self, query, *args):
            raise RuntimeError("connection reset")
    use(_Broken(tables=OLD_TABLES))

    with caplog.at_level("ERROR"):
        await legacy.adopt_legacy_company()   # must not raise

    assert "connection reset" in caplog.text
    assert config.COMPANY_NAME == "MZ CARGO INC"


# ── violations: a table this build could not otherwise write to ────────────────

async def test_the_old_company_column_stops_refusing_inserts(use):
    """NOT NULL with no default, and save_violation no longer supplies it. Left alone,
    every insert fails, _persist_once shrugs and alerts anyway, and the only symptom is
    that the reports are empty from the day of the deploy."""
    db = use(_Db(tables=OLD_TABLES, companies=[MZ],
                 columns={("violations", "company_slug")}))

    await legacy.adopt_legacy_violations()

    assert db.executed == [
        "ALTER TABLE violations ALTER COLUMN company_slug DROP NOT NULL",
        "ALTER TABLE violations ALTER COLUMN company_slug SET DEFAULT 'mzcargo'",
    ]


async def test_new_rows_are_labelled_the_way_the_old_ones_are(use):
    """A default, not just nullable: after a rollback the previous build filters its
    reports by company_slug, and rows left NULL would vanish from them. And the slug the
    TABLE uses, not the variable's, when the two disagree — the old build will go on
    asking for the one its rows have always had."""
    db = use(_Db(tables=OLD_TABLES,
                 companies=[{"id": 1, "slug": "mz_cargo", "name": "MZ Cargo"}],
                 columns={("violations", "company_slug")}))

    await legacy.adopt_legacy_violations()

    assert db.executed[-1].endswith("SET DEFAULT 'mz_cargo'")
    assert db.foreign_check_slug == "mz_cargo"


async def test_without_a_companies_table_the_variable_is_the_label(use):
    db = use(_Db(columns={("violations", "company_slug")}))

    await legacy.adopt_legacy_violations()

    assert db.executed[-1].endswith("SET DEFAULT 'mzcargo'")


async def test_the_slug_reaches_the_ddl_as_a_quoted_literal_not_by_string_formatting(use):
    """DDL takes no bind parameters, so the statement has to be assembled — and it is
    assembled by Postgres's own format(), with the table and the slug bound, not by
    Python."""
    db = use(_Db(tables=OLD_TABLES, companies=[MZ],
                 columns={("violations", "company_slug")}))

    await legacy.adopt_legacy_violations()

    built = [(q, a) for q, a in db.queries if q.startswith("SELECT format(")]
    assert [a for _, a in built] == [("violations",), ("violations", "mzcargo")]
    for query, _ in built:
        assert "%I" in query and "mzcargo" not in query
    assert "%L" in built[1][0]
    assert "$1::text" in built[1][0] and "$2::text" in built[1][0]


async def test_a_fresh_database_is_not_touched(use):
    """The new schema has no company_slug at all. Nothing to relax, nothing to run."""
    db = use(_Db())

    await legacy.adopt_legacy_violations()
    await legacy.adopt_legacy_crash_confirmations()

    assert db.executed == []


async def test_rows_belonging_to_another_company_are_reported_at_boot(use, caplog):
    """Nothing here filters by company any more. If the table was ever shared, every
    report would be counting somebody else's trucks — which has to be said, since it
    would never show up as an error."""
    use(_Db(tables=OLD_TABLES, companies=[MZ],
            columns={("violations", "company_slug")}, foreign_rows=True))

    with caplog.at_level("ERROR"):
        await legacy.adopt_legacy_violations()

    assert "other than 'mzcargo'" in caplog.text


async def test_a_table_holding_only_this_company_is_not_complained_about(use, caplog):
    use(_Db(tables=OLD_TABLES, companies=[MZ],
            columns={("violations", "company_slug")}, foreign_rows=False))

    with caplog.at_level("ERROR"):
        await legacy.adopt_legacy_violations()

    assert caplog.text == ""


# ── crash confirmations: the same column, and a failure nobody would see ───────

async def test_a_held_crash_can_still_be_recorded_on_the_old_table(use):
    """record_pending no longer names a company, and the old table requires one. The
    insert would fail inside _note_crash, which swallows it by design — so the crash
    alert would still go out and the only thing lost would be the row that lets a crash
    held mid-confirmation survive a restart. MZ is the fleet that has those."""
    db = use(_Db(tables=OLD_TABLES, companies=[MZ],
                 columns={("motive_crash_confirmations", "company_slug")}))

    await legacy.adopt_legacy_crash_confirmations()

    assert db.executed == [
        "ALTER TABLE motive_crash_confirmations ALTER COLUMN company_slug DROP NOT NULL",
        "ALTER TABLE motive_crash_confirmations ALTER COLUMN company_slug "
        "SET DEFAULT 'mzcargo'",
    ]


async def test_a_database_that_never_had_the_crash_table_is_left_alone(use):
    """The old build did not create it — it was added by hand, and maybe never was. The
    new schema then creates it without the column, and there is nothing to adopt."""
    db = use(_Db(tables=OLD_TABLES, companies=[MZ],
                 columns={("violations", "company_slug")}))

    await legacy.adopt_legacy_crash_confirmations()

    assert db.executed == []


# ── the group and its filter ───────────────────────────────────────────────────

async def test_a_fresh_database_has_no_legacy_group(use):
    db = use(_Db())

    assert await legacy.legacy_group() is None
    # And it found that out without selecting from a table that isn't there.
    assert not any("FROM company_groups" in q for q, _ in db.queries)


async def test_the_group_and_its_filter_are_read_from_the_old_tables(use):
    use(_Db(tables=OLD_TABLES, companies=[MZ],
            groups=[{"id": 1, "telegram_group_id": -1003879261617}],
            filters={1: ["speeding", "crash", "hard_brake"]}))

    assert await legacy.legacy_group() == {
        "telegram_group_id": -1003879261617,
        "event_types": ["speeding", "crash", "hard_brake"],
        "others": [],
    }


async def test_the_lookup_is_for_this_company_or_a_catch_all_row(use):
    db = use(_Db(tables=OLD_TABLES, companies=[{"id": 6, "slug": "x", "name": "X"},
                                               {**MZ, "id": 9}],
                 groups=[{"id": 1, "telegram_group_id": -100}]))

    await legacy.legacy_group()

    query, args = next((q, a) for q, a in db.queries if "FROM company_groups" in q)
    assert "cg.company_id = $1::int OR cg.company_id IS NULL" in query
    # The company's own row first, then the oldest: the one seed.sql labelled 'main'.
    assert "ORDER BY (cg.company_id IS NULL), cg.id" in query
    assert args == (9,)


async def test_the_group_is_found_under_a_slug_the_variable_does_not_have(use):
    """The case legacy_company's second rule exists for. Looked up by COMPANY_SLUG alone
    this group is not found, ensure_group falls through to GROUP_CHAT_ID — a variable
    the old build only ever read in a test script — and the alerts move to whatever chat
    that happens to name."""
    db = use(_Db(tables=OLD_TABLES,
                 companies=[{"id": 3, "slug": "mz-cargo", "name": "MZ Cargo"}],
                 groups=[{"id": 1, "telegram_group_id": -100777}]))

    found = await legacy.legacy_group()

    assert found["telegram_group_id"] == -100777
    _, args = next((q, a) for q, a in db.queries if "FROM company_groups" in q)
    assert args == (3,)


async def test_with_no_company_identified_only_catch_all_rows_are_asked_for(use):
    db = use(_Db(tables=OLD_TABLES, companies=[{"id": 1, "slug": "a", "name": "A"},
                                               {"id": 2, "slug": "b", "name": "B"}],
                 groups=[]))

    assert await legacy.legacy_group() is None
    _, args = next((q, a) for q, a in db.queries if "FROM company_groups" in q)
    # NULL never equals a company_id, so this leaves the IS NULL half of the WHERE.
    assert args == (None,)


async def test_a_group_with_no_filter_rows_carries_the_all_types_state(use):
    use(_Db(tables=OLD_TABLES, companies=[MZ],
            groups=[{"id": 1, "telegram_group_id": -100}]))

    assert (await legacy.legacy_group())["event_types"] == []


async def test_the_filter_read_is_the_first_groups_own(use):
    use(_Db(tables=OLD_TABLES, companies=[MZ],
            groups=[{"id": 7, "telegram_group_id": -100}, {"id": 9, "telegram_group_id": -200}],
            filters={7: ["speeding"], 9: ["crash"]}))

    found = await legacy.legacy_group()

    assert found["telegram_group_id"] == -100
    assert found["event_types"] == ["speeding"]
    assert found["others"] == [-200]


async def test_the_same_chat_listed_twice_is_not_a_second_group(use):
    """The old table has no unique key on the chat id, and seed.sql run twice puts the
    same group in it twice. That must not be reported as a group being dropped."""
    use(_Db(tables=OLD_TABLES, companies=[MZ],
            groups=[{"id": 1, "telegram_group_id": -100}, {"id": 2, "telegram_group_id": -100}]))

    assert (await legacy.legacy_group())["others"] == []


async def test_an_old_schema_with_no_group_for_this_company_has_nothing_to_carry(use):
    use(_Db(tables=OLD_TABLES, companies=[MZ], groups=[]))

    assert await legacy.legacy_group() is None


async def test_nothing_in_the_old_tables_is_ever_written(use):
    """The whole of the rollback story. Reading is all this module does to them; the
    two tables it alters are ones both builds write to, and only to relax a column."""
    db = use(_Db(tables=OLD_TABLES, companies=[MZ],
                 columns={("violations", "company_slug"),
                          ("motive_crash_confirmations", "company_slug")},
                 groups=[{"id": 1, "telegram_group_id": -100}], filters={1: ["speeding"]}))

    await legacy.adopt_legacy_company()
    await legacy.legacy_group()
    await legacy.adopt_legacy_violations()
    await legacy.adopt_legacy_crash_confirmations()

    assert len(db.executed) == 4
    for statement in db.executed:
        assert (statement.startswith("ALTER TABLE violations ALTER COLUMN company_slug")
                or statement.startswith(
                    "ALTER TABLE motive_crash_confirmations ALTER COLUMN company_slug"))
    for name in ("companies", "company_groups", "group_event_types", "admin_companies"):
        assert not any(name in s for s in db.executed)


# ── a database the single-group template made ──────────────────────────────────

class _Conn:
    def __init__(self, log, fail_on=None):
        self.log, self.fail_on = log, fail_on

    def transaction(self):
        return self

    async def __aenter__(self):
        self.log.append("BEGIN")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.log.append("ROLLBACK" if exc_type else "COMMIT")
        return False

    async def execute(self, query, *args):
        self.log.append(" ".join(query.split()))

    async def executemany(self, query, rows):
        if self.fail_on == "insert":
            raise RuntimeError("connection lost")
        self.log.append((" ".join(query.split()), [r[0] for r in rows]))


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return self

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *exc):
        return False


def _template_db(tables, fail_on=None):
    db = _Db(tables=tables)
    db.log = []
    db.pool = _Pool(_Conn(db.log, fail_on))
    return db


async def test_the_templates_database_keeps_crashes_out_of_the_group(use):
    """JTR's. That build never posted a crash to the group, as a rule in its code; here
    the group's filter decides, and no filter at all means every type."""
    db = use(_template_db({"alert_group", "violations", "admins"}))

    assert await legacy.adopt_single_group_template() is True

    create, (insert, types) = db.log[1], db.log[2]
    assert create.startswith("CREATE TABLE alert_group_event_types")
    assert insert.startswith("INSERT INTO alert_group_event_types")
    assert "crash" not in types
    assert {"speeding", "hard_brake", "cell_phone", "harsh_event"} <= set(types)


async def test_the_carried_filter_does_not_depend_on_what_is_alerted_today(use, monkeypatch):
    """It is written once. EXTRA_EVENT_TYPES may be set after that boot, and a type
    added then must not find itself filtered out of the group by a list made earlier."""
    monkeypatch.setattr(config, "EXTRA_EVENT_TYPES", frozenset())
    db = use(_template_db({"alert_group"}))

    await legacy.adopt_single_group_template()

    types = set(db.log[2][1])
    assert {"near_miss", "road_facing_cam_obstruction", "seat_belt_violation",
            "drowsiness", "tailgating"} <= types


async def test_the_table_and_its_rows_are_written_as_one(use):
    """A filter table that exists and is empty means "every type", for good: the
    template's mark is gone and nothing would ever fill it. So the two are one
    transaction, and a failure between them leaves the database as it was."""
    db = use(_template_db({"alert_group"}, fail_on="insert"))

    with pytest.raises(RuntimeError):
        await legacy.adopt_single_group_template()

    assert db.log[0] == "BEGIN"
    assert db.log[-1] == "ROLLBACK"


@pytest.mark.parametrize("tables", [
    (),                                                   # fresh
    ("alert_group", "alert_group_event_types"),           # already this build's
    OLD_TABLES,                                           # the multi-company schema
    OLD_TABLES + ("alert_group", "alert_group_event_types"),   # that, already upgraded
])
async def test_no_other_database_is_taken_for_the_templates(use, tables):
    """A group that receives crashes today — HF's, MZ CARGO's, a new company's — must
    not lose them to a rule that was never its own."""
    db = use(_template_db(set(tables)))

    assert await legacy.adopt_single_group_template() is False
    assert db.log == []
