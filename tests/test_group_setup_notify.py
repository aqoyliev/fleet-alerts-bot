"""Tests for admin notifications when a group's setup finishes.

The failure paths (_notify_admins_parse_failure, _notify_admins_unknown_unit) already DM
the admins when a group *fails* to register. Success was the missing half — a group could
go from silent to fully wired up with nobody but that one chat finding out. These pin
down the fix: every active admin is told, from both the places a group gets set up
(auto-detected on join, and /setunit).
"""
import handlers.groups.group_events as ge
from data import config


class _Chat:
    def __init__(self, chat_id, title, type="supergroup", description=""):
        self.id = chat_id
        self.title = title
        self.type = type
        self.description = description


class _Bot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))


def _admins(rows):
    async def _get_all():
        return rows
    return _get_all


# ── _notify_admins_group_registered ──────────────────────────────────────────────

async def test_every_active_admin_is_notified(monkeypatch):
    bot = _Bot()
    monkeypatch.setattr(ge, "bot", bot)
    monkeypatch.setattr(config, "ADMINS", [])
    monkeypatch.setattr(ge, "get_all_admins", _admins([
        {"telegram_id": 111, "is_active": True},
        {"telegram_id": 222, "is_active": True},
        {"telegram_id": 333, "is_active": False},  # deactivated — must not be paged
    ]))

    await ge._notify_admins_group_registered(_Chat(-100555, "unit571"), "unit571", "unit571")

    notified = {chat_id for chat_id, _ in bot.sent}
    assert notified == {111, 222}
    assert all("unit571" in text and "-100555" in text for _, text in bot.sent)


async def test_bootstrap_admins_from_config_are_included(monkeypatch):
    """The bootstrap ADMINS ids (config, not the admins table) must be reachable too —
    same convention as the existing failure-path notifications."""
    bot = _Bot()
    monkeypatch.setattr(ge, "bot", bot)
    monkeypatch.setattr(config, "ADMINS", ["999"])
    monkeypatch.setattr(ge, "get_all_admins", _admins([]))

    await ge._notify_admins_group_registered(_Chat(-100555, "unit571"), "unit571", "unit571")

    assert {chat_id for chat_id, _ in bot.sent} == {999}


async def test_a_failed_send_does_not_block_the_rest(monkeypatch):
    """One admin blocking the bot must not cost the others their notification — the
    same shape as _notify_admins_unknown_unit's per-recipient try/except."""
    class _FlakyBot(_Bot):
        async def send_message(self, chat_id, text, parse_mode=None):
            if chat_id == 111:
                raise RuntimeError("blocked")
            await super().send_message(chat_id, text, parse_mode=parse_mode)

    bot = _FlakyBot()
    monkeypatch.setattr(ge, "bot", bot)
    monkeypatch.setattr(config, "ADMINS", [])
    monkeypatch.setattr(ge, "get_all_admins", _admins([
        {"telegram_id": 111, "is_active": True},
        {"telegram_id": 222, "is_active": True},
    ]))

    await ge._notify_admins_group_registered(_Chat(-100555, "unit571"), "unit571", "unit571")

    assert {chat_id for chat_id, _ in bot.sent} == {222}


async def test_message_credits_whoever_ran_setunit(monkeypatch):
    bot = _Bot()
    monkeypatch.setattr(ge, "bot", bot)
    monkeypatch.setattr(config, "ADMINS", ["1"])
    monkeypatch.setattr(ge, "get_all_admins", _admins([]))

    await ge._notify_admins_group_registered(_Chat(-100555, "unit571"), "unit571", "unit571",
                                              by="Dispatcher Dave")

    assert "Dispatcher Dave" in bot.sent[0][1]


async def test_auto_detected_setup_says_so_instead_of_naming_anyone(monkeypatch):
    bot = _Bot()
    monkeypatch.setattr(ge, "bot", bot)
    monkeypatch.setattr(config, "ADMINS", ["1"])
    monkeypatch.setattr(ge, "get_all_admins", _admins([]))

    await ge._notify_admins_group_registered(_Chat(-100555, "unit571"), "unit571", "unit571")

    assert "auto-detected" in bot.sent[0][1]


# ── the call sites actually fire it ──────────────────────────────────────────────

async def test_setunit_success_notifies_admins(monkeypatch):
    """cmd_setunit's happy path must call the new notifier with the actor's name."""
    calls = []

    async def _fake_notify(chat, title, unit, by=None):
        calls.append((chat.id, title, unit, by))

    async def _fake_resolve(unit):
        return "ok", "unit571"

    async def _fake_register(chat_id, title, unit):
        pass

    monkeypatch.setattr(ge, "_notify_admins_group_registered", _fake_notify)
    monkeypatch.setattr(ge, "resolve_unit", _fake_resolve)
    monkeypatch.setattr(ge, "register_group", _fake_register)

    class _User:
        id = 42
        full_name = "Dispatcher Dave"

    class _Message:
        chat = _Chat(-100555, "unit571")
        from_user = _User()

        def get_args(self):
            return "571"

        async def reply(self, *a, **k):
            pass

    await ge.cmd_setunit(_Message())

    assert calls == [(-100555, "unit571", "unit571", "Dispatcher Dave")]


async def test_join_auto_registration_notifies_admins(monkeypatch):
    """on_bot_chat_member_update's happy path (unit auto-detected from the title) must
    call the new notifier with no actor — it wasn't a person who set it up."""
    calls = []

    async def _fake_notify(chat, title, unit, by=None):
        calls.append((chat.id, title, unit, by))

    async def _fake_resolve(vehicle):
        return "ok", "unit571"

    async def _fake_register(chat_id, title, unit):
        pass

    async def _fake_get_chat(chat_id):
        return _Chat(chat_id, "unit571 driver group")

    async def _fake_say(chat_id, text):
        pass

    monkeypatch.setattr(ge, "_notify_admins_group_registered", _fake_notify)
    monkeypatch.setattr(ge, "resolve_unit", _fake_resolve)
    monkeypatch.setattr(ge, "register_group", _fake_register)
    monkeypatch.setattr(ge.bot, "get_chat", _fake_get_chat)
    monkeypatch.setattr(ge, "_say", _fake_say)
    monkeypatch.setattr(config, "MAIN_GROUP_ID", None)
    monkeypatch.setattr(config, "CRASH_GROUP_ID", None)

    class _Member:
        def __init__(self, status):
            self.status = status

    class _Update:
        chat = _Chat(-100555, "unit571 driver group")
        old_chat_member = _Member("left")
        new_chat_member = _Member("member")

    await ge.on_bot_chat_member_update(_Update())

    assert calls == [(-100555, "unit571 driver group", "unit571", None)]
