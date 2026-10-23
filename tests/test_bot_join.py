"""Tests for getting the driver bot into a driver group through the userbot
(utils/bot_join.py, utils/userbot.py). Telegram (Telethon and the driver bot), the
driver_bot_join_requests table and the clock are all fakes (tests/userbot_fakes.py)."""
import asyncio
import types

import pytest
from aiogram.utils.exceptions import CantInitiateConversation
from telethon import errors
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest

import userbot_fakes as fx
from data import config
from utils import bot_join, bot_membership, userbot

SG, BG = fx.SUPERGROUP, fx.BASIC_GROUP
_LEFT = bot_membership.Membership("left", bot_membership.REASON_NOT_IN_GROUP)


class _DriverBot:
    """The driver bot: its own membership per chat, its username, its DMs."""
    id = 777

    def __init__(self):
        self.status = {}         # chat id -> status (default "left")
        self.member_calls = []
        self.dms = []            # (chat id, text)
        self.dm_fail = []        # exceptions raised by the next send_message calls

    async def get_chat_member(self, chat_id, user_id):
        self.member_calls.append(chat_id)
        return types.SimpleNamespace(status=self.status.get(chat_id, "left"))

    async def get_me(self):
        return types.SimpleNamespace(username=fx.BOT_USERNAME, id=self.id)

    async def send_message(self, chat_id, text, parse_mode=None, disable_web_page_preview=None):
        if self.dm_fail:
            raise self.dm_fail.pop(0)
        self.dms.append((chat_id, text))


@pytest.fixture
def ub(monkeypatch):
    ns = fx.install(monkeypatch)
    monkeypatch.setattr(config, "DRIVER_ALERTS_DRY_RUN", False, raising=False)
    ns.bot = _DriverBot()
    yield ns
    userbot._reset_state()
    bot_join._reset_state()


def _bot_joins_on_add(ub, group_id=SG):
    def on_add(request):
        if isinstance(request, (InviteToChannelRequest, AddChatUserRequest)):
            ub.bot.status[group_id] = "member"
    ub.world.on_add = on_add


async def _join(ub, group_id=SG, unit="4042", driver_name="John Smith", dry=False,
                company_slug="jrd"):
    return await bot_membership.request_bot_join(ub.bot, company_slug, unit, group_id, _LEFT,
                                                 driver_name=driver_name, dry_run=dry)


def _admin_text(reason, group_id=SG, unit="4042"):
    return (f"Could not add @{fx.BOT_USERNAME} to the group for JRD unit {unit} "
            f"(group {group_id}): {reason}. Please add it manually.")


# ── wording ──────────────────────────────────────────────────────────────────────

def test_hos_message_shape():
    assert bot_join.hos_message("JRD", "4042", "John Smith") == \
        "Can you please add me to this group?\nJRD\n4042 - John Smith"
    assert bot_join.hos_message("JRD", "4042", None) == "Can you please add me to this group?\nJRD\n4042"
    assert bot_join.hos_message("JRD", "4042", "  ") == "Can you please add me to this group?\nJRD\n4042"


def test_pick_hos_is_the_first(monkeypatch):
    assert bot_join.pick_hos(["anna", "bek"], SG) == "anna"
    assert bot_join.pick_hos([], SG) is None


def test_hos_usernames_from_env(monkeypatch):
    monkeypatch.setenv("HOS_USERNAMES_JRD", " @anna , bek,, @ ")
    assert config.hos_usernames("jrd") == ["anna", "bek"]
    monkeypatch.setenv("HOS_USERNAMES_ACME_CO", "@x")
    assert config.hos_usernames("acme-co") == ["x"]
    monkeypatch.delenv("HOS_USERNAMES_JRD")
    assert config.hos_usernames("jrd") == [] and config.hos_usernames("") == []


async def test_company_name_falls_back_to_the_slug(monkeypatch):
    async def none(slug):
        return None
    monkeypatch.setattr(bot_join, "get_company_name", none)
    assert await bot_join.company_display_name("jrd") == "JRD"

    async def boom(slug):
        raise OSError("db down")
    monkeypatch.setattr(bot_join, "get_company_name", boom)
    assert await bot_join.company_display_name("jrd") == "JRD"


# ── the kill switch ──────────────────────────────────────────────────────────────

async def test_userbot_disabled_skips_everything(ub, monkeypatch, caplog):
    monkeypatch.setattr(config, "USERBOT_ENABLED", False, raising=False)
    with caplog.at_level("INFO", logger=bot_join.logger.name):
        assert await _join(ub) is False
    assert ub.world.clients == [] and ub.bot.member_calls == [] and ub.store.rows == []
    assert ub.bot.dms == []
    assert "USERBOT_ENABLED=false" in caplog.text


async def test_userbot_not_configured_skips(ub, monkeypatch):
    monkeypatch.setattr(config, "USERBOT_SESSION", "", raising=False)
    assert await _join(ub) is False
    assert ub.world.clients == [] and ub.store.rows == [] and ub.bot.dms == []


# ── userbot can add ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("entity,rights", [
    (fx.channel(), fx.perms(creator=True)),
    (fx.channel(), fx.perms(admin=True, invite_users=True)),
    (fx.channel(members_may_invite=True), fx.perms()),
])
async def test_userbot_adds_the_bot_to_a_supergroup(ub, entity, rights):
    ub.world.dialogs[SG] = entity
    ub.world.perms[SG] = rights
    _bot_joins_on_add(ub)
    assert await _join(ub) is True
    [req] = ub.world.requests
    assert isinstance(req, InviteToChannelRequest) and req.users == [f"input:{fx.BOT_USERNAME}"]
    assert 2.0 in ub.sleeps                          # waited before re-checking
    assert ub.store.actions() == [("add", "added")]
    assert ub.bot.dms == []


async def test_userbot_adds_the_bot_to_a_basic_group(ub):
    ub.world.dialogs[BG] = fx.basic_chat()
    ub.world.perms[BG] = fx.perms(admin=True, invite_users=True)   # as Telethon says for a basic-group admin
    _bot_joins_on_add(ub, BG)
    assert await _join(ub, BG) is True
    [req] = ub.world.requests
    assert isinstance(req, AddChatUserRequest) and req.chat_id == 4567 and req.fwd_limit == 0


async def test_already_a_participant_counts_as_added(ub, monkeypatch):
    ub.world.dialogs[SG] = fx.channel()
    ub.world.perms[SG] = fx.perms(creator=True)
    ub.world.fail["call"] = [errors.UserAlreadyParticipantError(request=None)]

    async def joined_meanwhile(seconds):     # the re-check after the 2 s sees it in
        ub.bot.status[SG] = "member"
    monkeypatch.setattr(bot_join, "_sleep", joined_meanwhile)
    assert await _join(ub) is True
    assert ub.store.actions() == [("add", "added")]


async def test_add_that_did_not_take_tells_the_admin(ub):
    ub.world.dialogs[SG] = fx.channel()
    ub.world.perms[SG] = fx.perms(creator=True)
    assert await _join(ub) is False                    # bot still "left" after the add
    assert ub.bot.dms == [(fx.ADMIN_ID, _admin_text("the add went through but the bot is left"))]
    assert ub.store.actions() == [("add", "not_in_after_add"), ("admin_message", "sent")]


async def test_add_error_tells_the_admin(ub):
    ub.world.dialogs[SG] = fx.channel()
    ub.world.perms[SG] = fx.perms(creator=True)
    ub.world.fail["call"] = [errors.ChatAdminRequiredError(request=None)]
    assert await _join(ub) is False
    [(chat, text)] = ub.bot.dms
    assert chat == fx.ADMIN_ID and text.startswith(
        f"Could not add @{fx.BOT_USERNAME} to the group for JRD unit 4042 (group {SG}): "
        f"ChatAdminRequiredError")
    assert text.endswith("Please add it manually.")
    assert ub.store.actions() == [("add", "add_failed"), ("admin_message", "sent")]


async def test_adds_are_spaced_30s_apart(ub):
    other = SG - 1
    for gid in (SG, other):
        ub.world.dialogs[gid] = fx.channel(gid)
        ub.world.perms[gid] = fx.perms(creator=True)

    order = iter([SG, other])

    def on_add(request):
        ub.bot.status[next(order)] = "member"
    ub.world.on_add = on_add
    assert await _join(ub, SG) is True
    ub.sleeps.clear()
    ub.clock[0] += 5                                   # 2 s re-check + 5 s since the add
    assert await _join(ub, other, unit="4043") is True
    assert ub.sleeps[0] == pytest.approx(30 - 7)       # topped up to 30 s
    assert len(ub.world.requests) == 2


# ── userbot cannot add ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("gid,entity,rights,reason", [
    (SG, fx.channel(), fx.perms(), "the userbot has no right to add members"),
    (SG, fx.channel(), fx.perms(admin=True, invite_users=False), "the userbot has no right to add members"),
    (BG, fx.basic_chat(migrated=True), fx.perms(creator=True),
     "basic group migrated/deactivated: pti has a stale id"),
    (BG, fx.basic_chat(deactivated=True), fx.perms(creator=True),
     "basic group migrated/deactivated: pti has a stale id"),
])
async def test_userbot_without_the_right_tells_the_admin(ub, gid, entity, rights, reason):
    ub.world.dialogs[gid] = entity
    ub.world.perms[gid] = rights
    assert await _join(ub, gid) is False
    assert ub.world.requests == []
    assert ub.bot.dms == [(fx.ADMIN_ID, _admin_text(reason, gid))]
    assert ub.store.actions() == [("admin_message", "sent")]


async def test_a_telegram_error_reading_the_rights_tells_the_admin(ub):
    ub.world.dialogs[SG] = fx.channel()
    ub.world.perms[SG] = errors.ChannelPrivateError(request=None)
    assert await _join(ub) is False
    [(_, text)] = ub.bot.dms
    assert "could not read the userbot's rights (ChannelPrivateError" in text


async def test_admin_dm_needs_start_once(ub):
    ub.world.dialogs[SG] = fx.channel()
    ub.bot.dm_fail = [CantInitiateConversation("Forbidden: bot can't initiate conversation with a user")]
    assert await _join(ub) is False
    assert ub.world.sent == [(fx.BOT_USERNAME, "/start")]      # from the userbot, then retried
    assert len(ub.bot.dms) == 1 and ub.store.actions() == [("admin_message", "sent")]

    # Never twice per process: a later failure is only logged.
    ub.world.dialogs[SG - 1] = fx.channel(SG - 1)
    ub.bot.dm_fail = [CantInitiateConversation("Forbidden: bot can't initiate conversation with a user")]
    assert await _join(ub, SG - 1) is False
    assert ub.world.sent == [(fx.BOT_USERNAME, "/start")]
    assert ub.store.actions()[-1] == ("admin_message", "failed")


async def test_admin_id_comes_from_get_me_when_unset(ub, monkeypatch):
    monkeypatch.setattr(config, "USERBOT_USER_ID", "", raising=False)
    for gid in (SG, SG - 1):
        ub.world.dialogs[gid] = fx.channel(gid)
    await _join(ub, SG)
    await _join(ub, SG - 1)
    assert [c for c, _ in ub.bot.dms] == [fx.ADMIN_ID, fx.ADMIN_ID]
    assert ub.world.get_me_calls == 1


# ── userbot not in the group ─────────────────────────────────────────────────────

async def test_not_a_member_asks_the_first_hos(ub, monkeypatch):
    monkeypatch.setenv("HOS_USERNAMES_JRD", "@hos_anna, @hos_bek")
    assert await _join(ub) is False
    assert ub.world.sent == [("hos_anna", "Can you please add me to this group?\nJRD\n4042 - John Smith")]
    assert ub.bot.dms == [] and ub.world.requests == []
    assert ub.store.actions() == [("hos_message", "sent")]
    assert ub.store.rows[0]["detail"] == "@hos_anna" and ub.store.rows[0]["unit_number"] == "4042"


async def test_hos_message_without_a_driver_name(ub, monkeypatch):
    monkeypatch.setenv("HOS_USERNAMES_JRD", "hos_anna")
    await _join(ub, driver_name=None)
    assert ub.world.sent == [("hos_anna", "Can you please add me to this group?\nJRD\n4042")]


async def test_hos_message_uses_the_company_name(ub, monkeypatch):
    monkeypatch.setenv("HOS_USERNAMES_JRD", "hos_anna")

    async def name(slug):
        return "JRD Logistics"
    monkeypatch.setattr(bot_join, "get_company_name", name)
    await _join(ub)
    assert ub.world.sent[0][1].split("\n")[1] == "JRD Logistics"


@pytest.mark.parametrize("entity", [fx.basic_chat(), fx.channel()])
async def test_left_or_forbidden_dialogs_count_as_not_a_member(ub, monkeypatch, entity):
    from telethon import types as tl
    monkeypatch.setenv("HOS_USERNAMES_JRD", "hos_anna")
    entity.left = True
    ub.world.dialogs[SG] = entity
    await _join(ub)
    assert ub.world.sent and ub.world.permission_reads == 0
    ub.world.sent.clear()
    ub.store.rows.clear()
    ub.world.dialogs[SG] = tl.ChannelForbidden(id=1, access_hash=1, title="x")
    ub.clock[0] += userbot.DIALOGS_TTL + 1             # the dialog list is re-read
    await _join(ub)
    assert ub.world.sent


async def test_user_not_participant_counts_as_not_a_member(ub, monkeypatch):
    monkeypatch.setenv("HOS_USERNAMES_JRD", "hos_anna")
    ub.world.dialogs[SG] = fx.channel()
    ub.world.perms[SG] = errors.UserNotParticipantError(request=None)
    await _join(ub)
    assert [p for p, _ in ub.world.sent] == ["hos_anna"]


async def test_no_hos_configured_tells_the_admin(ub, caplog):
    with caplog.at_level("INFO", logger=bot_join.logger.name):
        assert await _join(ub) is False
    assert "no HOS configured (HOS_USERNAMES_JRD)" in caplog.text
    assert ub.world.sent == []
    assert ub.bot.dms == [(fx.ADMIN_ID, _admin_text(
        "the userbot is not in this group and no HOS is configured (HOS_USERNAMES_JRD)"))]
    assert ub.store.actions() == [("admin_message", "sent")]


async def test_failed_hos_message_tells_the_admin(ub, monkeypatch):
    monkeypatch.setenv("HOS_USERNAMES_JRD", "hos_anna")
    ub.world.fail["send"] = [errors.UsernameNotOccupiedError(request=None)]
    await _join(ub)
    assert ub.store.actions() == [("hos_message", "failed"), ("admin_message", "sent")]
    [(_, text)] = ub.bot.dms
    assert "messaging HOS @hos_anna failed (UsernameNotOccupiedError" in text


async def test_dialogs_are_reread_for_a_group_the_userbot_was_just_added_to(ub, monkeypatch):
    monkeypatch.setenv("HOS_USERNAMES_JRD", "hos_anna")
    await _join(ub)                                     # not a member: HOS asked
    assert ub.world.dialog_reads == 1
    ub.world.dialogs[SG] = fx.channel()                 # HOS added the userbot
    ub.world.perms[SG] = fx.perms(creator=True)
    _bot_joins_on_add(ub)
    ub.clock[0] += 30
    assert await _join(ub) is False                     # dialogs read < 60 s ago: still unknown
    ub.clock[0] += 31
    assert await _join(ub) is True                      # re-read: now it adds
    assert ub.world.dialog_reads == 2
    assert ub.store.actions() == [("hos_message", "sent"), ("add", "added")]


# ── de-duplication ───────────────────────────────────────────────────────────────

async def test_hos_is_asked_only_once_per_group_ever(ub, monkeypatch):
    monkeypatch.setenv("HOS_USERNAMES_JRD", "hos_anna")
    results = await asyncio.gather(*[_join(ub) for _ in range(5)])
    assert results == [False] * 5
    assert len(ub.world.sent) == 1 and ub.store.actions() == [("hos_message", "sent")]
    ub.clock[0] += 30 * 24 * 3600                       # a month later: still not again
    await _join(ub)
    assert len(ub.world.sent) == 1 and ub.bot.dms == []
    assert ub.store.actions() == [("hos_message", "sent")]


async def test_one_admin_message_per_24h(ub):
    ub.world.dialogs[SG] = fx.channel()
    for _ in range(3):
        await _join(ub)
    assert len(ub.bot.dms) == 1


async def test_one_add_per_24h_and_no_notice_after_a_notice(ub):
    ub.world.dialogs[SG] = fx.channel()
    ub.world.perms[SG] = fx.perms(creator=True)
    ub.world.fail["call"] = [errors.ChatAdminRequiredError(request=None)]
    await _join(ub)
    await _join(ub)
    assert len(ub.world.requests) == 0 and len(ub.bot.dms) == 1
    assert ub.store.actions() == [("add", "add_failed"), ("admin_message", "sent")]
    ub.clock[0] += 24 * 3600 + 1
    _bot_joins_on_add(ub)
    assert await _join(ub) is True                     # a day later it tries again


async def test_alerts_waiting_behind_an_add_see_the_bot_in(ub):
    ub.world.dialogs[SG] = fx.channel()
    ub.world.perms[SG] = fx.perms(creator=True)
    _bot_joins_on_add(ub)
    assert await asyncio.gather(_join(ub), _join(ub), _join(ub)) == [True, True, True]
    assert len(ub.world.requests) == 1 and ub.store.actions() == [("add", "added")]


async def test_unreadable_history_does_nothing(ub, monkeypatch):
    monkeypatch.setenv("HOS_USERNAMES_JRD", "hos_anna")
    ub.store.fail_reads = OSError("db down")
    assert await _join(ub) is False
    assert ub.world.sent == [] and ub.bot.dms == [] and ub.world.clients == []


async def test_bot_already_back_in_needs_nothing(ub):
    ub.bot.status[SG] = "member"
    assert await _join(ub) is True
    assert ub.world.clients == [] and ub.store.rows == []


async def test_dedup_is_per_group(ub, monkeypatch):
    monkeypatch.setenv("HOS_USERNAMES_JRD", "hos_anna")
    await _join(ub, SG)
    await _join(ub, SG - 1, unit="4043")
    assert [t.split("\n")[2] for _, t in ub.world.sent] == ["4042 - John Smith", "4043 - John Smith"]


# ── DRY RUN ──────────────────────────────────────────────────────────────────────

async def test_dry_run_only_logs_with_unit_and_group(ub, monkeypatch, caplog):
    monkeypatch.setenv("HOS_USERNAMES_JRD", "hos_anna")
    other, third = SG - 1, SG - 2
    ub.world.dialogs[SG] = fx.channel()
    ub.world.perms[SG] = fx.perms(creator=True)
    ub.world.dialogs[third] = fx.channel(third)       # in, but no right
    with caplog.at_level("INFO", logger=bot_join.logger.name):
        assert await _join(ub, SG, dry=True) is False
        assert await _join(ub, other, unit="4043", dry=True) is False
        assert await _join(ub, third, unit="4044", dry=True) is False
        await _join(ub, other, unit="4043", dry=True)  # de-dup kept in memory
    assert ub.world.requests == [] and ub.world.sent == [] and ub.bot.dms == []
    assert ub.store.rows == []
    assert ub.world.dialog_reads >= 1 and ub.world.permission_reads == 2   # read-only checks ran
    text = caplog.text
    assert f"DRY RUN jrd/4042 group {SG}: would add @{fx.BOT_USERNAME} (unit 4042, group {SG})" in text
    assert f"jrd/4043 group {other}: would message HOS @hos_anna" in text
    assert f"jrd/4044 group {third}: would notify admin: " in text
    assert text.count("would message HOS") == 1


async def test_dry_run_never_sends_start(ub, caplog):
    ub.world.dialogs[SG] = fx.channel()
    with caplog.at_level("INFO", logger=bot_join.logger.name):
        await _join(ub, dry=True)
    assert ub.world.sent == [] and ub.bot.dms == [] and "would notify admin" in caplog.text


# ── flood waits and a broken session ─────────────────────────────────────────────

async def test_flood_wait_on_add_falls_back_to_the_admin_and_pauses_the_userbot(ub, monkeypatch):
    monkeypatch.setenv("HOS_USERNAMES_JRD", "hos_anna")
    ub.world.dialogs[SG] = fx.channel()
    ub.world.perms[SG] = fx.perms(creator=True)
    ub.world.fail["call"] = [errors.FloodWaitError(request=None, capture=600)]
    assert await _join(ub) is False
    assert ub.store.actions() == [("add", "flood_wait"), ("admin_message", "sent")]
    assert "FloodWait 600s" in ub.bot.dms[0][1]
    assert userbot.flood_remaining() == pytest.approx(600, abs=3)

    # During the wait: no userbot use at all; one admin notice for the outage, not per group.
    reads = ub.world.dialog_reads
    await _join(ub, SG - 1, unit="4043")
    await _join(ub, SG - 2, unit="4044")
    assert ub.world.dialog_reads == reads and ub.world.sent == []
    assert len(ub.bot.dms) == 2 and "the userbot is unavailable" in ub.bot.dms[1][1]
    assert [a for a, _ in ub.store.actions()] == ["add", "admin_message"]   # nothing per group

    ub.clock[0] += 601                                  # wait over: back to normal
    await _join(ub, SG - 1, unit="4043")
    assert ub.world.sent == [("hos_anna", "Can you please add me to this group?\nJRD\n4043 - John Smith")]


async def test_flood_wait_while_inspecting(ub):
    ub.world.dialogs[SG] = fx.channel()
    ub.world.perms[SG] = errors.FloodWaitError(request=None, capture=60)
    assert await _join(ub) is False
    assert ub.store.rows == [] and "the userbot is unavailable" in ub.bot.dms[0][1]


async def test_revoked_session_disables_the_userbot(ub, caplog):
    ub.world.fail["connect"] = [errors.AuthKeyDuplicatedError(request=None)]
    with caplog.at_level("INFO"):
        await _join(ub)
        await _join(ub, SG - 1)
    assert len(ub.world.clients) == 1                   # never tried again
    assert "AUTH_KEY_DUPLICATED" in ub.bot.dms[0][1] and len(ub.bot.dms) == 1
    assert fx.SESSION not in caplog.text


async def test_unauthorized_session(ub):
    ub.world.authorized = False
    await _join(ub)
    assert "not authorized" in ub.bot.dms[0][1]
    assert ub.world.disconnects == 1


# ── the client: lazy, one per process, serialized, idle disconnect ───────────────

async def test_client_is_lazy_single_and_disconnects_when_idle(ub, monkeypatch, caplog):
    monkeypatch.setenv("HOS_USERNAMES_JRD", "hos_anna")
    assert ub.world.clients == []                       # nothing until needed
    with caplog.at_level("DEBUG"):
        await _join(ub, SG)
        await _join(ub, SG - 1)
    assert len(ub.world.clients) == 1 and ub.world.connects == 1
    ub.clock[0] += 299
    assert await userbot.disconnect_if_idle() is False
    ub.clock[0] += 2
    assert await userbot.disconnect_if_idle() is True
    assert ub.world.disconnects == 1
    await _join(ub, SG - 2)
    assert len(ub.world.clients) == 2                   # reconnects on demand
    assert fx.SESSION not in caplog.text


async def test_userbot_actions_never_overlap(ub, monkeypatch):
    monkeypatch.setenv("HOS_USERNAMES_JRD", "hos_anna")
    await asyncio.gather(*[userbot.send_message("x", str(i)) for i in range(5)],
                         *[userbot.inspect_group(SG - i) for i in range(5)])
    assert ub.world.max_active == 1 and len(ub.world.clients) == 1


async def test_close_disconnects(ub):
    await userbot.send_message("x", "hi")
    await userbot.close()
    assert ub.world.disconnects == 1


def test_can_add_rules():
    assert userbot.can_add(fx.channel(), fx.perms(creator=True))
    assert userbot.can_add(fx.channel(), fx.perms(admin=True, invite_users=True))
    assert not userbot.can_add(fx.channel(), fx.perms(admin=True, invite_users=False))
    assert userbot.can_add(fx.channel(members_may_invite=True), fx.perms())
    assert not userbot.can_add(fx.channel(), fx.perms())
    assert not userbot.can_add(types.SimpleNamespace(), fx.perms())
