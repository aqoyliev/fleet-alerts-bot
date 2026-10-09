"""Tests for a group that is registered but has no unit yet.

The bot used not to record a group at all unless it could work out which truck it
belonged to. That was defensible when a NULL unit meant "the all-fleet group" — writing
a row for an unidentified chat would have started sending it every truck's alerts — but
it also meant the group never appeared in the admin panel, which is the one screen built
for choosing its unit. You could only fix it from inside the chat, with /setunit.

So "which group is the all-fleet one" moved out of the NULL and into is_main, and the
third state became representable. These pin down both halves:

  • a group the bot was just added to is listed, and
  • listing it changed nothing about what it receives, which is nothing.

The messages the chat and the admins get are deliberately untouched.
"""
import pytest

import handlers.groups.group_events as ge
from data import config
from utils.db_api import db as dbmod
from utils.db_api import groups as grp
from utils.webapp import api


# ── a harness for "the bot was just added to a group" ───────────────────────────

class _Chat:
    def __init__(self, chat_id, title, type="supergroup", description=""):
        self.id = chat_id
        self.title = title
        self.type = type
        self.description = description


class _Member:
    def __init__(self, status):
        self.status = status


def _update(chat):
    class _Update:
        pass
    _Update.chat = chat
    _Update.old_chat_member = _Member("left")
    _Update.new_chat_member = _Member("member")
    return _Update


@pytest.fixture
def joined(monkeypatch):
    """Runs on_bot_chat_member_update with everything outside it recorded."""
    seen = {"unassigned": [], "registered": [], "said": [], "notified": []}

    async def _unassigned(chat_id, title):
        seen["unassigned"].append((chat_id, title))

    async def _register(chat_id, title, unit, is_main=False):
        seen["registered"].append((chat_id, title, unit, is_main))

    async def _say(chat_id, text):
        seen["said"].append((chat_id, text))

    async def _notify_parse(chat, title, description):
        seen["notified"].append(("parse_failure", chat.id))

    async def _notify_unknown(chat, title, unit, suggestions):
        seen["notified"].append(("unknown_unit", chat.id))

    async def _notify_registered(chat, title, unit, by=None):
        seen["notified"].append(("registered", chat.id))

    async def _get_chat(chat_id):
        return _Chat(chat_id, "")

    async def _suggest(unit):
        return []

    monkeypatch.setattr(ge, "register_unassigned_group", _unassigned)
    monkeypatch.setattr(ge, "register_group", _register)
    monkeypatch.setattr(ge, "_say", _say)
    monkeypatch.setattr(ge, "_notify_admins_parse_failure", _notify_parse)
    monkeypatch.setattr(ge, "_notify_admins_unknown_unit", _notify_unknown)
    monkeypatch.setattr(ge, "_notify_admins_group_registered", _notify_registered)
    monkeypatch.setattr(ge, "suggest_units_any", _suggest)
    monkeypatch.setattr(ge.bot, "get_chat", _get_chat)
    monkeypatch.setattr(config, "MAIN_GROUP_ID", None)
    monkeypatch.setattr(config, "CRASH_GROUP_ID", None)
    return seen


# ── the group is listed, whichever way identifying it failed ───────────────────

async def test_a_title_with_no_unit_in_it_still_registers(joined, monkeypatch):
    """The ordinary case for a fleet whose trucks are called "G8PZ-7X5-FF2": nothing in
    the chat name is a unit number, and the parser was never going to find one."""
    await ge.on_bot_chat_member_update(_update(_Chat(-100555, "test 1234 ALI / ABDI")))

    assert joined["unassigned"] == [(-100555, "test 1234 ALI / ABDI")]
    assert joined["registered"] == [], "it has no unit — nothing to register it against"


async def test_a_unit_the_roster_does_not_have_still_registers(joined, monkeypatch):
    async def _resolve(vehicle):
        return "missing", vehicle

    monkeypatch.setattr(ge, "resolve_unit", _resolve)

    await ge.on_bot_chat_member_update(_update(_Chat(-100555, "UNIT 9999 somebody")))

    assert joined["unassigned"] == [(-100555, "UNIT 9999 somebody")]
    assert joined["registered"] == []


async def test_a_roster_that_could_not_be_read_still_registers(joined, monkeypatch):
    """Samsara being down is the most temporary of the three failures, and the one where
    leaving no trace was hardest to recover from — nobody re-adds a bot to find out."""
    async def _resolve(vehicle):
        return "unavailable", vehicle

    monkeypatch.setattr(ge, "resolve_unit", _resolve)

    await ge.on_bot_chat_member_update(_update(_Chat(-100555, "UNIT 571 driver")))

    assert joined["unassigned"] == [(-100555, "UNIT 571 driver")]
    assert joined["registered"] == []


# ── and nothing the chat or the admins see has changed ─────────────────────────

async def test_the_group_is_still_told_it_needs_a_unit(joined):
    await ge.on_bot_chat_member_update(_update(_Chat(-100555, "test 1234 ALI / ABDI")))

    assert len(joined["said"]) == 1
    chat_id, text = joined["said"][0]
    assert chat_id == -100555
    assert "/setunit" in text


async def test_the_admins_are_still_told_it_failed(joined):
    await ge.on_bot_chat_member_update(_update(_Chat(-100555, "test 1234 ALI / ABDI")))

    assert joined["notified"] == [("parse_failure", -100555)]


async def test_an_unknown_unit_still_gets_its_did_you_mean(joined, monkeypatch):
    async def _resolve(vehicle):
        return "missing", vehicle

    monkeypatch.setattr(ge, "resolve_unit", _resolve)

    await ge.on_bot_chat_member_update(_update(_Chat(-100555, "UNIT 9999 somebody")))

    assert joined["notified"] == [("unknown_unit", -100555)]
    assert len(joined["said"]) == 1


# ── the paths that already worked still work ───────────────────────────────────

async def test_a_resolved_unit_registers_as_a_driver_group(joined, monkeypatch):
    async def _resolve(vehicle):
        return "ok", "unit571"

    monkeypatch.setattr(ge, "resolve_unit", _resolve)

    await ge.on_bot_chat_member_update(_update(_Chat(-100555, "UNIT 571 driver")))

    assert joined["registered"] == [(-100555, "UNIT 571 driver", "unit571", False)]
    assert joined["unassigned"] == []


async def test_the_main_group_registers_with_the_flag(joined, monkeypatch):
    """is_main is now carried explicitly. Were it inferred from the NULL unit again, every
    unassigned group above would become a second all-fleet group."""
    monkeypatch.setattr(config, "MAIN_GROUP_ID", -100999)

    await ge.on_bot_chat_member_update(_update(_Chat(-100999, "Dispatch office")))

    assert joined["registered"] == [(-100999, "Dispatch office", None, True)]


async def test_the_crash_group_is_still_not_registered(joined, monkeypatch):
    """Its routing comes from the config; a row would only add ways for it to be wrong."""
    monkeypatch.setattr(config, "CRASH_GROUP_ID", -100777)

    await ge.on_bot_chat_member_update(_update(_Chat(-100777, "Crash alerts")))

    assert joined["registered"] == [] and joined["unassigned"] == []


# ── what the queries now ask for ───────────────────────────────────────────────

@pytest.fixture
def sql(monkeypatch):
    """Records the SQL the group helpers run, with no database behind it."""
    seen = {"fetch": [], "execute": []}

    async def _fetch(query, *args):
        seen["fetch"].append((" ".join(query.split()), args))
        return []

    async def _execute(query, *args):
        seen["execute"].append((" ".join(query.split()), args))

    monkeypatch.setattr(grp.db, "fetch", _fetch)
    monkeypatch.setattr(grp.db, "execute", _execute)
    return seen


async def test_routing_asks_for_the_flag_not_for_a_missing_unit(sql):
    """The whole change in one assertion: were the catch-all branch still
    "vehicle_number IS NULL", every group registered above would receive every truck's
    alerts the moment it was listed."""
    await grp.get_groups_for_event("speeding", "unit571")

    query = sql["fetch"][0][0]
    assert "COALESCE(g.is_main, g.vehicle_number IS NULL)" in query
    assert "OR g.vehicle_number IS NULL" not in query


async def test_the_daily_digest_asks_for_the_flag_too(sql):
    await grp.get_all_groups()

    query = sql["fetch"][0][0]
    assert "COALESCE(is_main, vehicle_number IS NULL)" in query
    assert "WHERE vehicle_number IS NULL" not in query


async def test_an_unassigned_group_is_still_a_driver_group(sql):
    """An unassigned group receives no alerts -- there is no unit to say which truck they
    would be about. The PTI album is not an alert: it is the same walk-around for every
    truck, and the drivers in a group dispatch has not paired yet are exactly the ones
    going out without it. So this one asks only that the group is not the office."""
    await grp.get_driver_groups()

    query = sql["fetch"][0][0]
    assert "NOT COALESCE(is_main, vehicle_number IS NULL)" in query
    assert "vehicle_number IS NOT NULL" not in query


async def test_registering_unassigned_leaves_an_existing_unit_alone(sql):
    """The bot being re-added to a working group runs through this whenever its title has
    stopped parsing. Overwriting the unit there would unconfigure it silently."""
    await grp.register_unassigned_group(-100555, "test 1234")

    query, args = sql["execute"][0]
    assert "ON CONFLICT (telegram_group_id) DO UPDATE" in query
    update = query.split("DO UPDATE", 1)[1]
    assert "vehicle_number" not in update
    assert "is_main" not in update
    assert "title = EXCLUDED.title" in update
    assert args == (-100555, "test 1234")


async def test_a_new_unassigned_row_is_explicitly_not_the_main_group(sql):
    await grp.register_unassigned_group(-100555, "test 1234")

    insert = sql["execute"][0][0].split("ON CONFLICT", 1)[0]
    assert "VALUES ($1, $2, NULL, FALSE)" in insert


async def test_ensure_main_group_repairs_a_row_that_predates_the_flag(sql, monkeypatch):
    """A main group registered by the old code has is_main NULL. DO NOTHING would have
    left the setting correct and the routing quietly wrong."""
    monkeypatch.setattr(config, "MAIN_GROUP_ID", -100999)

    await grp.ensure_main_group()

    query, args = sql["execute"][0]
    assert "DO UPDATE SET is_main = TRUE" in query
    assert args == (-100999,)


# ── the backfill ───────────────────────────────────────────────────────────────

def test_the_backfill_cannot_promote_a_later_unassigned_group():
    """It runs on every boot. Without the WHERE it would, on the second deploy, mark every
    group that is merely waiting for its unit as the all-fleet group — and that group
    would then receive the entire fleet's alerts."""
    backfill = [m for m in dbmod._MIGRATIONS
                if isinstance(m, str) and m.startswith("UPDATE alert_groups SET is_main")]
    assert len(backfill) == 1
    assert "WHERE is_main IS NULL" in backfill[0]


def test_the_column_is_added_before_it_is_backfilled():
    stmts = [m for m in dbmod._MIGRATIONS if isinstance(m, str) and "is_main" in m]
    assert stmts[0].startswith("ALTER TABLE alert_groups ADD COLUMN IF NOT EXISTS is_main")
    # Nullable on purpose: NULL means "written before this existed" and every read falls
    # back to the old rule for it.
    assert "NOT NULL" not in stmts[0] and "DEFAULT" not in stmts[0]


# ── how the panel sees it ──────────────────────────────────────────────────────

async def _groups_payload(monkeypatch, rows, counts):
    async def _overview():
        return rows

    async def _counts(_since):
        return counts

    captured = {}

    def _ok(payload):
        captured["payload"] = payload
        return payload

    monkeypatch.setattr(api, "get_groups_overview", _overview)
    monkeypatch.setattr(api, "get_counts_by_vehicle", _counts)
    monkeypatch.setattr(api, "_ok", _ok)
    # require_admin wraps the handler; call the function it guards directly.
    await api.groups.__wrapped__(object())
    return captured["payload"]


async def test_the_panel_offers_the_unit_picker_for_an_unassigned_group(monkeypatch):
    """is_main false is what makes the picker appear — the screen hides it for the
    all-fleet group, which is the row an inferred flag would have made this look like."""
    payload = await _groups_payload(monkeypatch, [
        {"id": 1, "telegram_group_id": -100555, "title": "test 1234 ALI / ABDI",
         "vehicle_number": None, "is_main": False, "enabled": True,
         "created_at": None, "event_types": []},
    ], {"unit571": 9})

    assert payload[0]["is_main"] is False


async def test_an_unassigned_group_is_not_shown_the_fleets_alert_count(monkeypatch):
    """It has received none of them. The old expression fell through to the fleet-wide
    total for any row without a unit."""
    payload = await _groups_payload(monkeypatch, [
        {"id": 1, "telegram_group_id": -100555, "title": "test 1234",
         "vehicle_number": None, "is_main": False, "enabled": True,
         "created_at": None, "event_types": []},
    ], {"unit571": 9, "unit572": 4})

    assert payload[0]["alerts_7d"] == 0


async def test_the_main_group_still_shows_the_whole_fleet(monkeypatch):
    payload = await _groups_payload(monkeypatch, [
        {"id": 1, "telegram_group_id": -100999, "title": "Dispatch",
         "vehicle_number": None, "is_main": True, "enabled": True,
         "created_at": None, "event_types": []},
    ], {"unit571": 9, "unit572": 4})

    assert payload[0]["alerts_7d"] == 13
