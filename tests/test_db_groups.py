"""The singleton alert_group row: where it comes from, and what it answers.

Seeded exactly once. On HF's database — upgraded from the multi-company schema — the
chat id is in the old company_groups table and nowhere else, because GROUP_CHAT_ID was
never a required setting. If the seed looked only at the environment, the first boot on
this build would come up with no group at all and every alert would go to DMs alone,
with nothing having failed. So the old table is read first, the env var second, and the
case with neither is loud.
"""
import pytest

from data import config
from utils.db_api import groups

LEGACY = -1003786362177
FROM_ENV = -100555


@pytest.fixture
def seed(monkeypatch):
    """ensure_group with the database scripted: whether a row already exists, what the
    old schema holds, and a record of what was written."""
    state = {"exists": None, "legacy": None, "inserted": [], "filter": [],
             "legacy_calls": 0, "insert_returns": 1}

    async def _fetchval(query, *args):
        q = " ".join(query.split())
        if q == "SELECT 1 FROM alert_group":
            return state["exists"]
        if q.startswith("INSERT INTO alert_group"):
            state["inserted"].append((q, args))
            return state["insert_returns"]
        raise AssertionError(f"unexpected query: {q}")

    async def _legacy_group():
        state["legacy_calls"] += 1
        if isinstance(state["legacy"], Exception):
            raise state["legacy"]
        return state["legacy"]

    async def _set_filter(event_types):
        state["filter"].append(set(event_types))

    monkeypatch.setattr(groups.db, "fetchval", _fetchval)
    monkeypatch.setattr(groups, "legacy_group", _legacy_group)
    monkeypatch.setattr(groups, "set_group_event_types", _set_filter)
    monkeypatch.setattr(config, "GROUP_CHAT_ID", None)
    return state


# ── where the group comes from ─────────────────────────────────────────────────

async def test_the_group_is_carried_over_from_the_old_schema(seed):
    seed["legacy"] = {"telegram_group_id": LEGACY, "event_types": [], "others": []}

    await groups.ensure_group()

    query, args = seed["inserted"][0]
    assert "INSERT INTO alert_group (id, telegram_group_id) VALUES (1, $1)" in query
    assert "ON CONFLICT (id) DO NOTHING" in query
    assert args == (LEGACY,)


async def test_the_old_schema_wins_over_the_env_var(seed, monkeypatch):
    """The old table is what followed the chat through a supergroup migration; the env
    var, where anyone set it at all, is whatever the id was on the day they did."""
    monkeypatch.setattr(config, "GROUP_CHAT_ID", FROM_ENV)
    seed["legacy"] = {"telegram_group_id": LEGACY, "event_types": [], "others": []}

    await groups.ensure_group()

    assert seed["inserted"][0][1] == (LEGACY,)


async def test_a_fresh_database_is_seeded_from_the_env_var(seed, monkeypatch):
    monkeypatch.setattr(config, "GROUP_CHAT_ID", FROM_ENV)

    await groups.ensure_group()

    assert seed["inserted"][0][1] == (FROM_ENV,)
    assert seed["filter"] == [set()]      # no filter to carry: every type


async def test_the_event_filter_comes_across_with_the_group(seed):
    seed["legacy"] = {"telegram_group_id": LEGACY, "others": [],
                      "event_types": ["speeding", "crash", "harsh_turn"]}

    await groups.ensure_group()

    # harsh_turn brings hard_cornering with it: they are one chip in the panel.
    assert seed["filter"] == [{"speeding", "crash", "harsh_turn", "hard_cornering"}]


async def test_an_unreadable_old_schema_falls_back_to_the_env_var(seed, monkeypatch):
    """The old tables are somebody else's shape. A surprise in them must not be what
    stops the bot from booting."""
    monkeypatch.setattr(config, "GROUP_CHAT_ID", FROM_ENV)
    seed["legacy"] = RuntimeError("relation \"companies\" does not exist")

    await groups.ensure_group()

    assert seed["inserted"][0][1] == (FROM_ENV,)


async def test_with_no_source_nothing_is_written_and_it_is_said_loudly(seed, caplog):
    with caplog.at_level("ERROR"):
        await groups.ensure_group()

    assert seed["inserted"] == []
    assert "No alerts group is configured" in caplog.text
    assert "GROUP_CHAT_ID" in caplog.text


async def test_further_groups_in_the_old_schema_are_named_not_silently_dropped(seed, caplog):
    seed["legacy"] = {"telegram_group_id": LEGACY, "event_types": [],
                      "others": [-100777, -100888]}

    with caplog.at_level("WARNING"):
        await groups.ensure_group()

    assert seed["inserted"][0][1] == (LEGACY,)
    assert "-100777" in caplog.text and "-100888" in caplog.text


# ── and it is seeded once ──────────────────────────────────────────────────────

async def test_an_already_seeded_group_is_left_completely_alone(seed, monkeypatch):
    """Every boot after the first. Not the env var, not the old table: the row is the
    authority, and it may have moved on from both — a supergroup migration rewrites it,
    and an admin may have changed its filter in the panel."""
    monkeypatch.setattr(config, "GROUP_CHAT_ID", FROM_ENV)
    seed["exists"] = 1
    seed["legacy"] = {"telegram_group_id": LEGACY, "event_types": ["speeding"], "others": []}

    await groups.ensure_group()

    assert seed["inserted"] == []
    assert seed["filter"] == []
    assert seed["legacy_calls"] == 0      # the old tables are not even read


async def test_losing_the_race_to_another_container_does_not_reset_the_filter(seed):
    """Two containers overlap in a rolling deploy and both find the table empty. The one
    whose INSERT conflicts must stop there: writing the filter anyway would be a second
    seed, on top of whatever has happened to the row since the first."""
    seed["legacy"] = {"telegram_group_id": LEGACY, "event_types": ["speeding"], "others": []}
    seed["insert_returns"] = None         # ON CONFLICT DO NOTHING returned no row

    await groups.ensure_group()

    assert len(seed["inserted"]) == 1
    assert seed["filter"] == []


# ── what the row answers ───────────────────────────────────────────────────────

@pytest.fixture
def spy(monkeypatch):
    calls = {"fetchval": [], "execute": []}
    answer = {"fetchval": None}

    async def _fetchval(query, *args):
        calls["fetchval"].append((" ".join(query.split()), args))
        return answer["fetchval"]

    async def _execute(query, *args):
        calls["execute"].append((" ".join(query.split()), args))

    monkeypatch.setattr(groups.db, "fetchval", _fetchval)
    monkeypatch.setattr(groups.db, "execute", _execute)
    calls["answer"] = answer
    return calls


async def test_an_alert_is_routed_by_mute_and_by_the_filter(spy):
    spy["answer"]["fetchval"] = LEGACY

    assert await groups.get_alert_target("speeding") == LEGACY

    query, args = spy["fetchval"][0]
    assert "WHERE g.enabled" in query
    # No filter rows means every type; otherwise the type has to be listed.
    assert "NOT EXISTS (SELECT 1 FROM alert_group_event_types)" in query
    assert "EXISTS (SELECT 1 FROM alert_group_event_types WHERE event_type = $1)" in query
    assert args == ("speeding",)


async def test_the_daily_report_is_routed_by_mute_alone(spy):
    """Asked without a type. The filter says which alerts the chat wants, and the report
    is not an alert — the NULL short-circuits it."""
    spy["answer"]["fetchval"] = LEGACY

    assert await groups.get_alert_target() == LEGACY

    query, args = spy["fetchval"][0]
    assert "$1::text IS NULL" in query
    assert args == (None,)


async def test_no_group_or_a_muted_one_answers_none(spy):
    assert await groups.get_alert_target("speeding") is None


async def test_is_the_group_follows_the_row_not_the_env(spy, monkeypatch):
    """The row can drift from GROUP_CHAT_ID after a migration — and on HF's deployment
    the env var was never set at all."""
    spy["answer"]["fetchval"] = -1009999
    monkeypatch.setattr(config, "GROUP_CHAT_ID", -100111)

    assert await groups.is_the_group(-1009999) is True
    assert await groups.is_the_group(-100111) is False


async def test_no_chat_is_the_group_before_one_is_configured(spy):
    assert await groups.is_the_group(-100111) is False


async def test_migrate_group_repoints_the_row_by_its_current_chat_id(spy):
    await groups.migrate_group(-100111, -1001000000111)

    query, args = spy["execute"][0]
    assert "UPDATE alert_group SET telegram_group_id = $1 WHERE telegram_group_id = $2" in query
    assert args == (-1001000000111, -100111)


async def test_muting_writes_the_one_row(spy):
    await groups.set_group_enabled(False)

    assert spy["execute"] == [("UPDATE alert_group SET enabled = $1", (False,))]


# ── replacing the filter ───────────────────────────────────────────────────────

class _Conn:
    def __init__(self, log):
        self.log = log

    def transaction(self):
        log = self.log

        class _Tx:
            async def __aenter__(self):
                log.append("BEGIN")

            async def __aexit__(self, exc_type, *_a):
                log.append("ROLLBACK" if exc_type else "COMMIT")
                return False
        return _Tx()

    async def execute(self, query, *args):
        self.log.append(" ".join(query.split()))

    async def executemany(self, query, rows):
        self.log.append((" ".join(query.split()), list(rows)))


class _Pool:
    def __init__(self, log):
        self.conn = _Conn(log)

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *_a):
                return False
        return _Ctx()


async def test_the_filter_is_replaced_inside_one_transaction(monkeypatch):
    """Between the DELETE and the INSERTs the table is empty, and empty means "every
    type". An alert routed in that gap would reach the group for a type an admin had
    just switched off — so there must be no gap anyone else can see."""
    log = []
    monkeypatch.setattr(groups.db, "pool", _Pool(log))

    await groups.set_group_event_types({"speeding", "crash"})

    assert log[0] == "BEGIN"
    assert log[1] == "DELETE FROM alert_group_event_types"
    insert, rows = log[2]
    assert insert.startswith("INSERT INTO alert_group_event_types (event_type) VALUES ($1)")
    assert rows == [("crash",), ("speeding",)]
    assert log[3] == "COMMIT"


async def test_clearing_the_filter_deletes_and_inserts_nothing(monkeypatch):
    log = []
    monkeypatch.setattr(groups.db, "pool", _Pool(log))

    await groups.set_group_event_types(set())

    assert log == ["BEGIN", "DELETE FROM alert_group_event_types", "COMMIT"]
