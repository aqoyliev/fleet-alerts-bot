"""What the bot does in a group chat: the alerts group, and every other one.

Only one group means anything on this branch, and it cannot be made to by anything typed
or done in Telegram — adding the bot to a chat registers nothing. Nor is the group
controlled from inside it: there is no /disable or /enable, and nobody is messaged when
the bot is added to or removed from a chat. So the tests here are mostly about things
not happening — a chat that is not the alerts group is not answered, and the bot coming
and going changes nothing and tells nobody.
"""
import pytest

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


@pytest.fixture
def world(monkeypatch):
    """The alerts group is THE_GROUP and ADMIN is a bot admin. Every write to the group
    row and every message the bot could send is recorded, because the claim in most of
    these tests is that there are none."""
    state = {"db_writes": [], "sent": []}

    async def _is_the_group(chat_id):
        return chat_id == THE_GROUP

    async def _is_admin(telegram_id):
        return telegram_id == ADMIN

    async def _execute(query, *args):
        state["db_writes"].append(" ".join(query.split()))

    class _Bot:
        async def send_message(self, chat_id, text, **kwargs):
            state["sent"].append((chat_id, text))

    import loader
    import utils.db_api.groups as groups

    monkeypatch.setattr(ge, "is_the_group", _is_the_group)
    monkeypatch.setattr(report, "is_the_group", _is_the_group)
    monkeypatch.setattr(report, "is_admin", _is_admin)
    monkeypatch.setattr(groups.db, "execute", _execute)
    monkeypatch.setattr(loader, "bot", _Bot())
    return state


# ── the group is not controlled from the group ─────────────────────────────────

@pytest.mark.parametrize("command", ["disable", "enable", "mute", "unmute", "setunit",
                                     "events", "removegroup"])
def test_no_group_command_changes_what_the_group_receives(command):
    """single-company has /disable and /enable in every driver group, and /setunit,
    /events and /removegroup beside them. None of them exists here: the alerts group's
    mute and its event filter are in the admin panel and nowhere else, so a merge that
    brought one of these handlers back would be handing the switch to a chat."""
    from loader import dp

    registered = set()
    for handler in dp.message_handlers.handlers:
        for flt in handler.filters or []:
            registered.update(getattr(flt.filter, "commands", None) or [])

    assert command not in registered
    # And the commands that are meant to be there, are — so this is not passing only
    # because the handlers could not be read.
    assert {"help", "report", "top"} <= registered


def test_the_group_module_has_no_way_to_mute():
    assert not hasattr(ge, "cmd_disable")
    assert not hasattr(ge, "cmd_enable")
    assert not hasattr(ge, "set_group_enabled")


# ── being added and removed ────────────────────────────────────────────────────

@pytest.mark.parametrize("chat_id", [THE_GROUP, OTHER_GROUP])
@pytest.mark.parametrize("old, new", [("left", "member"), ("kicked", "administrator"),
                                      ("member", "kicked"), ("administrator", "left")])
async def test_the_bot_coming_or_going_changes_nothing_and_tells_nobody(world, chat_id,
                                                                        old, new):
    """Not for the alerts group either. No registration, no mute, no DM to the admins:
    the log line with the chat's id is the whole effect. A group the bot was removed
    from is tried again on the next alert, so putting it back is the entire fix."""
    await ge.on_bot_chat_member_update(_Update(chat_id, old, new))

    assert world["db_writes"] == []
    assert world["sent"] == []


async def test_the_chat_id_is_logged_so_a_group_can_be_configured(world, caplog):
    with caplog.at_level("INFO"):
        await ge.on_bot_chat_member_update(_Update(OTHER_GROUP, "left", "member"))

    assert f"id={OTHER_GROUP}" in caplog.text
    assert "added to" in caplog.text


async def test_a_private_chat_blocking_the_bot_is_not_a_group_event(world, caplog):
    with caplog.at_level("INFO"):
        await ge.on_bot_chat_member_update(_Update(STRANGER, "member", "kicked", "private"))

    assert caplog.text == ""


# ── /help in a group ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("user_id", [STRANGER, ADMIN])
async def test_help_in_the_alerts_group_explains_the_group(world, user_id):
    """The same text for everyone: there is nothing an admin can do here that anybody
    else in the chat cannot."""
    message = _Message(THE_GROUP, user_id=user_id)
    await ge.cmd_group_help(message)

    assert "/report" in message.replies[0] and "/top" in message.replies[0]
    assert "/disable" not in message.replies[0] and "/enable" not in message.replies[0]


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
