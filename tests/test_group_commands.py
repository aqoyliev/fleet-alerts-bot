"""What the bot does in a group chat: the alerts group, and every other one.

Only one group means anything on this branch, and it cannot be made to by anything typed
or done in Telegram — adding the bot to a chat registers nothing. So the tests here are
mostly about refusals: a chat that is not the alerts group is not answered, somebody who
is not a bot admin cannot silence the fleet, and the bot being put into a chat never
changes where alerts go.
"""
import pytest

from data import config
import handlers.groups.group_events as ge
import handlers.users.report as report

THE_GROUP = -1003786362177
OTHER_GROUP = -100555
ADMIN = 42
STRANGER = 99


class _User:
    def __init__(self, user_id, full_name="Dana Dispatcher"):
        self.id = user_id
        self.full_name = full_name


class _Chat:
    def __init__(self, chat_id, chat_type="supergroup", title="HF Safety"):
        self.id = chat_id
        self.type = chat_type
        self.title = title


class _Message:
    def __init__(self, chat_id, user_id=STRANGER, args="", chat_type="supergroup"):
        self.chat = _Chat(chat_id, chat_type)
        self.from_user = _User(user_id)
        self._args = args
        self.replies: list[str] = []
        self.answers: list[str] = []

    def get_args(self):
        return self._args

    async def reply(self, text, **kwargs):
        self.replies.append(text)

    async def answer(self, text, **kwargs):
        self.answers.append(text)


class _Member:
    def __init__(self, status):
        self.status = status


class _Update:
    def __init__(self, chat_id, old, new, chat_type="supergroup"):
        self.chat = _Chat(chat_id, chat_type)
        self.old_chat_member = _Member(old)
        self.new_chat_member = _Member(new)


class _Bot:
    def __init__(self, unreachable=()):
        self.sent: list[tuple[int, str]] = []
        self.unreachable = set(unreachable)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.unreachable:
            raise RuntimeError("bot was blocked by the user")
        self.sent.append((chat_id, text))


@pytest.fixture
def world(monkeypatch):
    """The alerts group is THE_GROUP, ADMIN is a bot admin, and the maintainer is id 1."""
    state = {"enabled_writes": [], "bot": _Bot()}

    async def _is_the_group(chat_id):
        return chat_id == THE_GROUP

    async def _is_admin(telegram_id):
        return telegram_id == ADMIN

    async def _set_enabled(enabled):
        state["enabled_writes"].append(enabled)

    async def _all_admins():
        return [{"telegram_id": ADMIN, "is_active": True},
                {"telegram_id": 43, "is_active": False}]

    for module in (ge, report):
        monkeypatch.setattr(module, "is_the_group", _is_the_group)
        monkeypatch.setattr(module, "is_admin", _is_admin)
    monkeypatch.setattr(ge, "set_group_enabled", _set_enabled)
    monkeypatch.setattr(ge, "get_all_admins", _all_admins)
    monkeypatch.setattr(ge, "bot", state["bot"])
    monkeypatch.setattr(config, "HIDDEN_ADMIN_IDS", {1})
    return state


# ── muting is a bot admin's decision ───────────────────────────────────────────

async def test_a_bot_admin_can_pause_the_group(world):
    message = _Message(THE_GROUP, user_id=ADMIN)
    await ge.cmd_disable(message)

    assert world["enabled_writes"] == [False]
    assert "paused" in message.replies[0]


async def test_somebody_else_in_the_group_cannot(world):
    """On single-company anyone in a driver's group may mute it: that silences one
    truck's chat for the people in it. Here it would silence the whole fleet."""
    message = _Message(THE_GROUP, user_id=STRANGER)
    await ge.cmd_disable(message)

    assert world["enabled_writes"] == []
    assert "Only the bot's admins" in message.replies[0]


async def test_nor_can_they_turn_it_back_on(world):
    message = _Message(THE_GROUP, user_id=STRANGER)
    await ge.cmd_enable(message)

    assert world["enabled_writes"] == []


async def test_a_bot_admin_can_resume_the_group(world):
    message = _Message(THE_GROUP, user_id=ADMIN)
    await ge.cmd_enable(message)

    assert world["enabled_writes"] == [True]
    assert "resumed" in message.replies[0]


async def test_pausing_tells_every_active_admin_and_the_maintainer_who_did_it(world):
    """A muted group is a fleet nobody is being warned about. That must not be something
    one person knows."""
    await ge.cmd_disable(_Message(THE_GROUP, user_id=ADMIN))

    recipients = [chat_id for chat_id, _ in world["bot"].sent]
    assert recipients == [1, ADMIN]          # the maintainer and the active admin
    assert 43 not in recipients              # a deactivated admin is not told
    assert all("Dana Dispatcher" in text for _, text in world["bot"].sent)


async def test_one_unreachable_admin_does_not_cost_the_others_the_notice(world):
    world["bot"].unreachable = {1}
    await ge.cmd_disable(_Message(THE_GROUP, user_id=ADMIN))

    assert [chat_id for chat_id, _ in world["bot"].sent] == [ADMIN]


@pytest.mark.parametrize("command", ["cmd_disable", "cmd_enable"])
async def test_the_mute_commands_do_nothing_in_any_other_group(world, command):
    """Even for a bot admin. The command says "this group", and this is not the group."""
    message = _Message(OTHER_GROUP, user_id=ADMIN)
    await getattr(ge, command)(message)

    assert world["enabled_writes"] == []
    assert message.replies == []


# ── being added and removed ────────────────────────────────────────────────────

async def test_being_added_to_some_chat_changes_nothing_and_tells_nobody(world):
    """No registration, no pairing prompt, no DM. The log line carrying the chat id is
    the whole effect."""
    await ge.on_bot_chat_member_update(_Update(OTHER_GROUP, "left", "member"))

    assert world["bot"].sent == []
    assert world["enabled_writes"] == []


async def test_being_removed_from_the_alerts_group_is_reported_to_the_admins(world):
    await ge.on_bot_chat_member_update(_Update(THE_GROUP, "member", "kicked"))

    assert [chat_id for chat_id, _ in world["bot"].sent] == [1, ADMIN]
    assert "removed from the alerts group" in world["bot"].sent[0][1]


async def test_removal_does_not_mute_the_group(world):
    """The mute is an admin's switch and only an admin's. If leaving muted the group,
    coming back would have to un-mute it — and that update is skipped whenever the bot
    is re-added while it is restarting, which would leave the group silent for good."""
    await ge.on_bot_chat_member_update(_Update(THE_GROUP, "administrator", "left"))

    assert world["enabled_writes"] == []


async def test_coming_back_is_reported_and_does_not_override_a_deliberate_mute(world):
    await ge.on_bot_chat_member_update(_Update(THE_GROUP, "kicked", "member"))

    assert "back in the alerts group" in world["bot"].sent[0][1]
    assert world["enabled_writes"] == []


async def test_a_promotion_within_the_group_is_not_an_arrival(world):
    await ge.on_bot_chat_member_update(_Update(THE_GROUP, "member", "administrator"))

    assert world["bot"].sent == []


async def test_a_private_chat_blocking_the_bot_is_not_a_group_event(world):
    await ge.on_bot_chat_member_update(_Update(STRANGER, "member", "kicked", "private"))

    assert world["bot"].sent == []


# ── /help in a group ───────────────────────────────────────────────────────────

async def test_help_in_the_alerts_group_explains_the_group(world):
    message = _Message(THE_GROUP, user_id=STRANGER)
    await ge.cmd_group_help(message)

    assert "/report" in message.replies[0] and "/top" in message.replies[0]
    # The switch is not advertised to somebody who cannot use it.
    assert "/disable" not in message.replies[0]


async def test_help_tells_a_bot_admin_about_the_switch(world):
    message = _Message(THE_GROUP, user_id=ADMIN)
    await ge.cmd_group_help(message)

    assert "/disable" in message.replies[0]


async def test_help_is_silent_in_any_other_group(world):
    message = _Message(OTHER_GROUP, user_id=ADMIN)
    await ge.cmd_group_help(message)

    assert message.replies == []


# ── /report and /top ───────────────────────────────────────────────────────────

@pytest.fixture
def reports(world, monkeypatch):
    async def _by_type(since, until):
        return [{"event_type": "speeding", "vehicle_number": "1196", "total": 3}]

    async def _top(since, until=None, limit=10, **_k):
        world["limit"] = limit
        return [{"vehicle_number": "1196", "total": 3}]

    monkeypatch.setattr(report, "get_violations_by_type", _by_type)
    monkeypatch.setattr(report, "get_top_violators", _top)
    return world


async def test_anyone_in_the_alerts_group_can_ask_for_the_report(reports):
    message = _Message(THE_GROUP, user_id=STRANGER, args="today")
    await report.cmd_report(message)

    assert "Daily Violations Report" in message.answers[0]
    assert "Unit 1196 — 3" in message.answers[0]


async def test_another_group_gets_no_report_and_no_usage_hint(reports):
    """The bot can be added to any chat. That chat must not be able to read this
    fleet's violations — or learn that there is a command for it."""
    for args in ("today", ""):
        message = _Message(OTHER_GROUP, user_id=ADMIN, args=args)
        await report.cmd_report(message)
        assert message.answers == []


async def test_an_admin_can_ask_in_a_dm_and_a_stranger_cannot(reports):
    admin = _Message(ADMIN, user_id=ADMIN, args="yesterday", chat_type="private")
    await report.cmd_report(admin)
    assert "Daily Violations Report" in admin.answers[0]

    stranger = _Message(STRANGER, user_id=STRANGER, args="yesterday", chat_type="private")
    await report.cmd_report(stranger)
    assert stranger.answers == []


async def test_report_without_a_period_says_how_to_ask(reports):
    message = _Message(THE_GROUP, args="")
    await report.cmd_report(message)

    assert message.answers == ["Usage: /report today  or  /report yesterday"]


@pytest.mark.parametrize("args, limit", [("", 10), ("20", 20), ("500", 50), ("0", 1),
                                         ("abc", 10)])
async def test_top_takes_a_count_within_bounds(reports, args, limit):
    message = _Message(THE_GROUP, args=args)
    await report.cmd_top(message)

    assert reports["limit"] == limit
    assert f"Top {limit} Violators" in message.answers[0]


async def test_top_is_silent_in_another_group(reports):
    message = _Message(OTHER_GROUP, user_id=ADMIN)
    await report.cmd_top(message)

    assert message.answers == []
