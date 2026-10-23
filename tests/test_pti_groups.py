"""Tests for the live pti-checker-bot routing (utils/pti_groups.py) and the driver bot's
membership check (utils/bot_membership.py). Routing decides whose group an alert goes
to, so these pin the unit matching, the fail-closed cases, the cache and its staleness
limit, and that every query runs read-only. No Postgres and no Telegram: the pool,
connection and bot are fakes."""
import types

import pytest
from aiogram.utils.exceptions import (
    BadRequest,
    BotKicked,
    ChatNotFound,
    MigrateToChat,
    NetworkError,
    RetryAfter,
    Unauthorized,
)

import utils.bot_membership as bm
import utils.pti_groups as pg
from data import config

URL = "postgresql://pti:pw@pti.example/jrd"


def _g(group_id, unit, title="", drivers=()):
    return pg.PtiGroup(group_id=group_id, title=title, unit_number=unit, driver_names=tuple(drivers))


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(pg, "_pools", {})
    monkeypatch.setattr(pg, "_snapshots", {})
    monkeypatch.setattr(pg, "_failed_at", {})
    monkeypatch.setattr(pg, "_locks", {})
    monkeypatch.setattr(bm, "_cache", {})
    monkeypatch.delenv("PTI_DATABASE_URL_JRD", raising=False)


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(pg.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(bm.time, "monotonic", lambda: now[0])
    return now


@pytest.fixture
def pti(monkeypatch):
    """PTI_DATABASE_URL_JRD set; fetch_usable_groups answers from `rec["groups"]` or
    raises `rec["error"]`, counting calls."""
    monkeypatch.setenv("PTI_DATABASE_URL_JRD", URL)
    rec = {"groups": (), "error": None, "calls": 0}

    async def fetch(company_slug):
        rec["calls"] += 1
        if rec["error"] is not None:
            raise rec["error"]
        return tuple(rec["groups"])

    monkeypatch.setattr(pg, "fetch_usable_groups", fetch)
    return rec


# ── configuration ────────────────────────────────────────────────────────────────

def test_database_url_env_name_per_company(monkeypatch):
    monkeypatch.setenv("PTI_DATABASE_URL_JRD", URL)
    monkeypatch.setenv("PTI_DATABASE_URL_ACME_CO", "postgresql://acme")
    assert config.pti_database_url("jrd") == URL
    assert config.pti_database_url(" JRD ") == URL
    assert config.pti_database_url("acme-co") == "postgresql://acme"
    assert config.pti_database_url("other") == "" and config.pti_database_url("") == ""
    assert pg.is_configured("jrd") and not pg.is_configured("other")


async def test_no_database_is_no_pti_db(monkeypatch):
    async def never(_slug):
        pytest.fail("must not query")
    monkeypatch.setattr(pg, "fetch_usable_groups", never)
    r = await pg.find_group("jrd", "1304")
    assert not r.ok and r.reason == pg.REASON_NO_PTI_DB


# ── unit matching ────────────────────────────────────────────────────────────────

def test_exact_unit_matches_one_group():
    groups = [_g(-1, "1304"), _g(-2, "1305"), _g(-3, "T-120")]
    assert pg.match_unit(groups, "1304").group.group_id == -1
    assert pg.match_unit(groups, "t120").group.group_id == -3       # normalized on both sides
    assert pg.match_unit(groups, "<1304 >").group.group_id == -1


def test_suffix_fallback_on_both_sides():
    assert pg.match_unit([_g(-1, "4063")], "4063   O/O").group.group_id == -1   # vehicle suffix
    assert pg.match_unit([_g(-1, "4063 O/O")], "4063").group.group_id == -1     # pti suffix
    assert pg.match_unit([_g(-1, "4063")], "O/O 4063").reason == pg.REASON_NO_GROUP
    assert pg.match_unit([_g(-1, "40630")], "4063").reason == pg.REASON_NO_GROUP


def test_no_group_and_blank_vehicle():
    assert pg.match_unit([_g(-1, "1304")], "9999").reason == pg.REASON_NO_GROUP
    assert pg.match_unit([_g(-1, "1304")], "").reason == pg.REASON_NO_GROUP
    assert pg.match_unit([_g(-1, "1304")], "—").reason == pg.REASON_NO_GROUP
    assert pg.match_unit([], "1304").reason == pg.REASON_NO_GROUP


def test_more_than_one_usable_group_is_ambiguous():
    r = pg.match_unit([_g(-1, "1304"), _g(-2, "1304 ")], "1304")
    assert not r.ok and r.reason == pg.REASON_AMBIGUOUS
    assert {g.group_id for g in r.matches} == {-1, -2}
    # Even when one of them is the exact spelling: a doubt is never resolved by guessing.
    r = pg.match_unit([_g(-1, "4063"), _g(-2, "4063 O/O")], "4063 O/O")
    assert r.reason == pg.REASON_AMBIGUOUS


def test_the_same_group_listed_twice_is_one_group():
    r = pg.match_unit([_g(-1, "1304"), _g(-1, "1304")], "1304")
    assert r.ok and r.group.group_id == -1


# ── snapshot cache ───────────────────────────────────────────────────────────────

async def test_snapshot_is_cached_for_a_minute(pti, clock):
    pti["groups"] = [_g(-1, "1304")]
    assert (await pg.find_group("jrd", "1304")).group.group_id == -1
    clock[0] += 59
    pti["groups"] = [_g(-2, "1304")]          # a driver swap lands in the pti DB
    assert (await pg.find_group("JRD", "1304")).group.group_id == -1
    assert pti["calls"] == 1
    clock[0] += 2
    assert (await pg.find_group("jrd", "1304")).group.group_id == -2
    assert pti["calls"] == 2


async def test_failed_refresh_uses_a_snapshot_younger_than_ten_minutes(pti, clock, caplog):
    pti["groups"] = [_g(-1, "1304")]
    await pg.find_group("jrd", "1304")
    clock[0] += 300
    pti["error"] = OSError("connection refused")
    with caplog.at_level("WARNING", logger=pg.logger.name):
        r = await pg.find_group("jrd", "1304")
    assert r.ok and r.group.group_id == -1 and r.age == pytest.approx(300)
    assert any("snapshot" in m.getMessage() for m in caplog.records)


async def test_failed_refresh_with_an_old_snapshot_fails_closed(pti, clock):
    pti["groups"] = [_g(-1, "1304")]
    await pg.find_group("jrd", "1304")
    clock[0] += 601
    pti["error"] = OSError("connection refused")
    r = await pg.find_group("jrd", "1304")
    assert not r.ok and r.reason == pg.REASON_UNAVAILABLE


async def test_failure_without_any_snapshot_fails_closed(pti, clock):
    pti["error"] = TimeoutError()
    r = await pg.find_group("jrd", "1304")
    assert r.reason == pg.REASON_UNAVAILABLE


async def test_no_retry_right_after_a_failure(pti, clock):
    pti["error"] = OSError("down")
    await pg.find_group("jrd", "1304")
    clock[0] += 5
    await pg.find_group("jrd", "1304")
    assert pti["calls"] == 1
    clock[0] += pg.RETRY_AFTER_FAILURE
    pti["error"], pti["groups"] = None, [_g(-1, "1304")]
    assert (await pg.find_group("jrd", "1304")).ok and pti["calls"] == 2


async def test_find_group_never_raises(pti, monkeypatch):
    def boom(*_a):
        raise RuntimeError("bug")
    monkeypatch.setattr(pg, "match_unit", boom)
    pti["groups"] = [_g(-1, "1304")]
    assert (await pg.find_group("jrd", "1304")).reason == pg.REASON_UNAVAILABLE


# ── the query: one go, read-only ─────────────────────────────────────────────────

class _FakeTx:
    def __init__(self, conn, readonly):
        self.conn, self.readonly = conn, readonly

    async def __aenter__(self):
        self.conn.log.append(("begin", self.readonly))

    async def __aexit__(self, *exc):
        self.conn.log.append(("end", self.readonly))


class _FakeConn:
    def __init__(self, rows):
        self.rows, self.log = rows, []

    def transaction(self, readonly=False, **_kw):
        return _FakeTx(self, readonly)

    async def fetch(self, query, *args, timeout=None):
        assert self.log and self.log[-1] == ("begin", True), "query outside a read-only transaction"
        self.log.append(("fetch", query, timeout))
        return self.rows


class _FakePool:
    def __init__(self, conn):
        self.conn, self.closed = conn, False

    def acquire(self, timeout=None):
        pool = self

        class _Acq:
            async def __aenter__(self):
                return pool.conn

            async def __aexit__(self, *exc):
                return False
        return _Acq()

    async def close(self):
        self.closed = True


async def test_fetch_runs_one_query_in_a_read_only_transaction(monkeypatch):
    monkeypatch.setenv("PTI_DATABASE_URL_JRD", URL)
    conn = _FakeConn([
        {"group_id": -1001, "unit_number": " 1304 ", "title": "1304 SMITH, JOHN / LEE, ANN",
         "driver_names": ["John Smith", "  ", None, " Ann "]},
        {"group_id": -1002, "unit_number": "1305", "title": None, "driver_names": None},
    ])
    created = []

    async def create_pool(url):
        created.append(url)
        return _FakePool(conn)

    monkeypatch.setattr(pg, "_create_pool", create_pool)
    groups = await pg.fetch_usable_groups("jrd")
    await pg.fetch_usable_groups("jrd")
    assert created == [URL]                                       # one lazy pool per company
    assert groups[0] == _g(-1001, "1304", "1304 SMITH, JOHN / LEE, ANN", ("John Smith", "Ann"))
    assert groups[1] == _g(-1002, "1305")
    fetches = [e for e in conn.log if e[0] == "fetch"]
    assert len(fetches) == 2 and fetches[0][2] == pg.COMMAND_TIMEOUT
    assert conn.log[:3] == [("begin", True), fetches[0], ("end", True)]

    pool = pg._pools["jrd"]
    await pg.close()
    assert pool.closed and pg._pools == {}


def test_query_selects_only_usable_groups_and_leaves_staff_out():
    sql = " ".join(pg.USABLE_GROUPS_SQL.split())
    assert "COALESCE(g.is_active, FALSE)" in sql
    assert "COALESCE(g.setup_complete, FALSE)" in sql
    assert "NOT COALESCE(g.post_blocked, FALSE)" in sql
    assert "btrim(COALESCE(g.unit_number::text, '')) <> ''" in sql
    assert "LEFT JOIN group_drivers d" in sql and "non_drivers" in sql
    for word in ("INSERT", "UPDATE", "DELETE"):
        assert word not in sql.upper()


async def test_fetch_without_a_database_raises(monkeypatch):
    with pytest.raises(LookupError):
        await pg.fetch_usable_groups("jrd")


async def test_pool_is_small(monkeypatch):
    seen = {}

    async def create_pool(url, **kw):
        seen.update(kw, url=url)
        return "pool"

    monkeypatch.setattr(pg.asyncpg, "create_pool", create_pool)
    assert await pg._create_pool(URL) == "pool"
    assert seen["min_size"] <= 1 and seen["max_size"] == 2
    assert seen["command_timeout"] == pg.COMMAND_TIMEOUT


# ── driver bot membership ────────────────────────────────────────────────────────

class _Bot:
    id = 777

    def __init__(self, answer="member"):
        self.answer, self.calls = answer, []

    async def get_chat_member(self, chat_id, user_id):
        self.calls.append((chat_id, user_id))
        if isinstance(self.answer, Exception):
            raise self.answer
        if isinstance(self.answer, str):
            return types.SimpleNamespace(status=self.answer)
        return self.answer


@pytest.mark.parametrize("status", ["member", "administrator", "creator"])
async def test_member_or_admin_can_post(status):
    bot = _Bot(status)
    m = await bm.check(bot, -100)
    assert m.can_post and m.status == status and bot.calls == [(-100, 777)]


@pytest.mark.parametrize("answer,reason", [
    (types.SimpleNamespace(status="restricted", is_member=True, can_send_messages=True), None),
    (types.SimpleNamespace(status="restricted", is_member=True, can_send_messages=False),
     bm.REASON_CANNOT_POST),
    (types.SimpleNamespace(status="restricted", is_member=False, can_send_messages=True),
     bm.REASON_NOT_IN_GROUP),
    ("left", bm.REASON_NOT_IN_GROUP),
    ("kicked", bm.REASON_NOT_IN_GROUP),
    ("weird", bm.REASON_CHECK_FAILED),
])
async def test_restricted_left_and_kicked(answer, reason):
    m = await bm.check(_Bot(answer), -100)
    assert m.reason == reason and m.can_post is (reason is None)


@pytest.mark.parametrize("error,reason,cached", [
    (ChatNotFound("Bad Request: chat not found"), bm.REASON_NOT_IN_GROUP, True),
    (BotKicked("Forbidden: bot was kicked from the group chat"), bm.REASON_NOT_IN_GROUP, True),
    (Unauthorized("Forbidden: bot is not a member of the supergroup chat"), bm.REASON_NOT_IN_GROUP, True),
    (BadRequest("Bad Request: participant_id_invalid"), bm.REASON_NOT_IN_GROUP, True),
    (Unauthorized("Unauthorized"), bm.REASON_CHECK_FAILED, False),        # a bad token
    (NetworkError("Connection reset"), bm.REASON_CHECK_FAILED, False),
    (RetryAfter(5), bm.REASON_CHECK_FAILED, False),
    (RuntimeError("boom"), bm.REASON_CHECK_FAILED, False),
])
async def test_errors_fail_closed_and_only_definitive_answers_are_cached(error, reason, cached):
    bot = _Bot(error)
    m = await bm.check(bot, -100)
    assert m.reason == reason and not m.can_post and m.detail
    await bm.check(bot, -100)
    assert len(bot.calls) == (1 if cached else 2)


async def test_migrated_group_is_reported_with_its_new_id():
    e = MigrateToChat("Group migrated to supergroup")
    e.migrate_to_chat_id = -1009
    m = await bm.check(_Bot(e), -100)
    assert m.reason == bm.REASON_MIGRATED and m.migrate_to == -1009 and m.status == "migrated"


async def test_membership_is_cached_ten_minutes_per_chat(clock):
    bot = _Bot("member")
    await bm.check(bot, -100)
    clock[0] += 599
    await bm.check(bot, -100)
    await bm.check(bot, -200)
    assert bot.calls == [(-100, 777), (-200, 777)]
    clock[0] += 2
    bot.answer = "left"
    assert (await bm.check(bot, -100)).reason == bm.REASON_NOT_IN_GROUP
    bm.invalidate(-100)
    bot.answer = "member"
    assert (await bm.check(bot, -100)).can_post
    assert len(bot.calls) == 4


async def test_join_hook_does_nothing_yet():
    m = bm.Membership("left", bm.REASON_NOT_IN_GROUP)
    assert await bm.request_bot_join("jrd", "1304", -100, m) is False
