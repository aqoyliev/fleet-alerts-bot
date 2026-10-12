"""Tests for a group the bot is no longer in.

Being thrown out of a chat used to leave the row exactly where it was. The group went on
being listed in the admin panel — eventually as "muted", once an alert had tried and
failed to reach it — offering a unit picker and an event filter for a conversation the
bot could not see. The muted count on the dashboard counted it too.

So a departure is now recorded, and the panel asks only for the groups the bot is still
in. The row itself stays: someone who removes the bot for an afternoon and adds it back
should not have to pick the truck and rebuild the filter, and that is exactly what a
delete would have cost them.
"""
import pytest

import handlers.groups.group_events as ge
import utils.webhook_handler as wh
from data import config
from utils.db_api import groups as grp


class _Chat:
    def __init__(self, chat_id, title, type="supergroup"):
        self.id = chat_id
        self.title = title
        self.type = type


class _Member:
    def __init__(self, status):
        self.status = status


def _left(chat, old="member", new="kicked"):
    class _Update:
        pass
    _Update.chat = chat
    _Update.old_chat_member = _Member(old)
    _Update.new_chat_member = _Member(new)
    return _Update


@pytest.fixture
def departures(monkeypatch):
    seen = []

    async def _mark(chat_id):
        seen.append(chat_id)

    monkeypatch.setattr(ge, "mark_group_left", _mark)
    monkeypatch.setattr(config, "MAIN_GROUP_ID", None)
    monkeypatch.setattr(config, "CRASH_GROUP_ID", None)
    return seen


# ── the departure is recorded ──────────────────────────────────────────────────

@pytest.mark.parametrize("status", ["left", "kicked"])
async def test_the_bot_leaving_a_group_is_recorded(departures, status):
    """Removed by an admin, or the whole group deleted — both arrive the same way."""
    await ge.on_bot_chat_member_update(_left(_Chat(-100555, "UNIT 571"), new=status))

    assert departures == [-100555]


async def test_a_blocked_dm_is_not_a_group_departure(departures):
    """The same update fires when a person blocks the bot. There is no row to retire, and
    every branch here would be treating a private chat as a group."""
    await ge.on_bot_chat_member_update(_left(_Chat(555, None, type="private")))

    assert departures == []


async def test_a_demotion_is_not_a_departure(departures):
    """administrator → member means the bot lost its admin rights, not the chat."""
    await ge.on_bot_chat_member_update(
        _left(_Chat(-100555, "UNIT 571"), old="administrator", new="member"))

    assert departures == []


# ── what it writes, and what the panel then asks for ───────────────────────────

@pytest.fixture
def sql(monkeypatch):
    seen = {"fetch": [], "execute": []}

    async def _fetch(query, *args):
        seen["fetch"].append((" ".join(query.split()), args))
        return []

    async def _execute(query, *args):
        seen["execute"].append((" ".join(query.split()), args))

    monkeypatch.setattr(grp.db, "fetch", _fetch)
    monkeypatch.setattr(grp.db, "execute", _execute)
    return seen


async def test_a_departure_mutes_as_well_as_hides(sql):
    """Two flags because they answer two questions. Routing reads enabled, the panel reads
    left_at, and a chat the bot has been thrown out of is both unpostable and
    unconfigurable."""
    await grp.mark_group_left(-100555)

    query, args = sql["execute"][0]
    assert "SET left_at = NOW()" in query
    assert "enabled = FALSE" in query
    assert args == (-100555,)


async def test_a_departure_keeps_the_row(sql):
    """A delete would take the unit and the event filter with it, and re-adding the bot
    would be a fresh setup rather than a resumption."""
    await grp.mark_group_left(-100555)

    assert sql["execute"][0][0].startswith("UPDATE alert_groups")
    assert "DELETE" not in sql["execute"][0][0]


async def test_the_panel_only_lists_groups_the_bot_is_in(sql):
    await grp.get_groups_overview()

    assert "WHERE g.left_at IS NULL" in sql["fetch"][0][0]


# ── and adding the bot back undoes all of it ───────────────────────────────────

async def test_re_adding_restores_a_configured_group(sql):
    await grp.register_group(-100555, "UNIT 571", "unit571")

    update = sql["execute"][0][0].split("DO UPDATE", 1)[1]
    assert "left_at = NULL" in update
    assert "enabled = TRUE" in update


async def test_re_adding_restores_a_group_whose_title_stopped_parsing(sql):
    """This is the path a returning group usually takes — the parser rarely reads a real
    chat name. It has to clear the departure without touching the unit someone picked in
    the panel before the bot was removed."""
    await grp.register_unassigned_group(-100555, "test 1234 ALI / ABDI")

    update = sql["execute"][0][0].split("DO UPDATE", 1)[1]
    assert "left_at = NULL" in update
    assert "vehicle_number" not in update


# ── the backstop, for a departure nobody saw ───────────────────────────────────

async def test_being_kicked_mid_alert_retires_the_group_too(monkeypatch):
    """Polling starts with skip_updates, so a group the bot was removed from while it was
    redeploying never reports it. The next alert addressed there is the only other place
    the truth shows up."""
    from aiogram.utils.exceptions import BotKicked

    seen = []

    async def _mark(chat_id):
        seen.append(chat_id)

    async def _no_sleep(_s):
        return None

    class _Bot:
        async def send_message(self, chat_id, text, parse_mode=None,
                               disable_web_page_preview=None):
            raise BotKicked("bot was kicked from the group chat")

    monkeypatch.setattr(wh, "mark_group_left", _mark)
    monkeypatch.setattr(wh.asyncio, "sleep", _no_sleep)

    await wh._send_with_retry(_Bot(), -100555, "alert")

    assert seen == [-100555]
