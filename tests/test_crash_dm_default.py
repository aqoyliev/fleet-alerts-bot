"""Tests for crash DMs being on by default.

Every other event type is opt-in: an admin with no admin_subscriptions rows gets nothing.
Crash can't work that way — it is never delivered to a group, so a DM is the only place a
crash is ever reported, and an admin who never opened Settings would hear about a
collision from nobody. It is stored as admins.crash_dm DEFAULT TRUE, and these pin down
that the default is on, that turning it off sticks, and that nothing else changed.
"""
import pytest

from utils.db_api import admins as adm
from utils.db_api import db as dbmod


@pytest.fixture
def spy(monkeypatch):
    """Records SQL and returns canned rows, so no database is involved."""
    calls = {"fetch": [], "fetchval": [], "execute": []}
    result = {"fetch": [], "fetchval": True}

    async def _fetch(sql, *args):
        calls["fetch"].append((" ".join(sql.split()), args))
        return result["fetch"]

    async def _fetchval(sql, *args):
        calls["fetchval"].append((" ".join(sql.split()), args))
        return result["fetchval"]

    async def _execute(sql, *args):
        calls["execute"].append((" ".join(sql.split()), args))

    monkeypatch.setattr(adm.db, "fetch", _fetch)
    monkeypatch.setattr(adm.db, "fetchval", _fetchval)
    monkeypatch.setattr(adm.db, "execute", _execute)
    calls["_result"] = result
    return calls


# ── who receives a crash ───────────────────────────────────────────────────────

async def test_crash_goes_to_every_active_admin(spy):
    spy["_result"]["fetch"] = [{"telegram_id": 111}, {"telegram_id": 222}]
    assert await adm.get_subscribed_admins("crash") == [111, 222]

    sql, args = spy["fetch"][0]
    # Read off the admins table, NOT admin_subscriptions — that is what makes it default-on.
    assert "FROM admins" in sql
    assert "admin_subscriptions" not in sql
    assert "is_active = TRUE" in sql
    assert "COALESCE(crash_dm, TRUE)" in sql
    assert args == ()


async def test_an_admin_who_opted_out_is_excluded_by_the_query(spy):
    """The exclusion is the crash_dm predicate above; with it false for a row, that row
    simply isn't returned."""
    spy["_result"]["fetch"] = [{"telegram_id": 111}]
    assert await adm.get_subscribed_admins("crash") == [111]
    assert "COALESCE(crash_dm, TRUE)" in spy["fetch"][0][0]


async def test_every_other_type_is_still_opt_in(spy):
    spy["_result"]["fetch"] = [{"telegram_id": 111}]
    assert await adm.get_subscribed_admins("speeding") == [111]

    sql, args = spy["fetch"][0]
    assert "admin_subscriptions" in sql
    assert "sub.event_type = 'all'" in sql
    assert args == ("speeding",)


# ── what Settings shows ────────────────────────────────────────────────────────

async def test_crash_shows_as_subscribed_by_default(spy):
    """The notifications keyboard ticks a type when it appears in this list, so crash has
    to appear there for the ✅ to be right — even though it isn't stored as a row."""
    spy["_result"]["fetch"] = []
    spy["_result"]["fetchval"] = True
    assert await adm.get_admin_subscriptions(7) == ["crash"]


async def test_crash_is_absent_once_turned_off(spy):
    spy["_result"]["fetch"] = []
    spy["_result"]["fetchval"] = False
    assert await adm.get_admin_subscriptions(7) == []


async def test_crash_is_not_listed_twice(spy):
    """A leftover 'crash' row from before the column existed must not double up."""
    spy["_result"]["fetch"] = [{"event_type": "crash"}]
    spy["_result"]["fetchval"] = True
    assert await adm.get_admin_subscriptions(7) == ["crash"]


async def test_other_subscriptions_are_returned_alongside(spy):
    spy["_result"]["fetch"] = [{"event_type": "speeding"}]
    spy["_result"]["fetchval"] = True
    assert await adm.get_admin_subscriptions(7) == ["speeding", "crash"]


# ── toggling ───────────────────────────────────────────────────────────────────

async def test_toggling_crash_flips_the_column_and_nothing_else(spy):
    await adm.toggle_subscription(7, "crash")

    assert len(spy["execute"]) == 1
    sql, args = spy["execute"][0]
    assert sql == ("UPDATE admins SET crash_dm = NOT COALESCE(crash_dm, TRUE) "
                   "WHERE telegram_id = $1")
    assert args == (7,)
    # No admin_subscriptions row is written, or the two stores could disagree.
    assert all("admin_subscriptions" not in s for s, _ in spy["execute"])
    assert spy["fetchval"] == []  # never looks up the admin id — it isn't needed


async def test_toggling_another_type_still_uses_the_subscription_table(spy):
    spy["_result"]["fetchval"] = 5  # admin id lookup, then "does the row exist" → truthy
    await adm.toggle_subscription(7, "speeding")
    assert any("admin_subscriptions" in s for s, _ in spy["execute"])


# ── the migration that turns it on for existing deployments ────────────────────

def test_the_column_is_added_by_an_idempotent_migration():
    """Railway never runs schemas.sql by hand, so an existing database only gets the
    column from this list — and it has to default to TRUE, or every current admin would
    start out opted out."""
    stmts = [" ".join(s.split()) for s in dbmod._MIGRATIONS]
    assert any(
        "ALTER TABLE admins ADD COLUMN IF NOT EXISTS crash_dm BOOLEAN NOT NULL DEFAULT TRUE" == s
        for s in stmts
    )
