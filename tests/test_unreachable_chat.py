"""Tests for delivery to chats the bot can no longer post to.

Production hit this when the bot was kicked from a driver group: BotKicked subclasses
TelegramAPIError, so the media loop retried it three times, the text-only fallback did
not catch it at all, and the exception escaped _send_with_retry — aborting the whole
broadcast, so every recipient after the dead group silently lost the alert.
"""
import pytest
from aiogram.utils.exceptions import BotKicked, ChatNotFound, NetworkError

import utils.webhook_handler as wh


class _Bot:
    """Fake bot whose sends raise a scripted exception (or record the call)."""

    def __init__(self, exc=None, fail_media_only=False):
        self.exc = exc
        self.fail_media_only = fail_media_only
        self.photo_calls: list[int] = []
        self.message_calls: list[int] = []

    async def send_photo(self, chat_id, media, caption=None, parse_mode=None):
        self.photo_calls.append(chat_id)
        if self.exc:
            raise self.exc

    async def send_message(self, chat_id, text, parse_mode=None, disable_web_page_preview=None):
        self.message_calls.append(chat_id)
        if self.exc and not self.fail_media_only:
            raise self.exc


@pytest.fixture
def muted(monkeypatch):
    """Records which groups got muted, standing in for the DB write."""
    calls: list[tuple[int, bool]] = []

    async def _set(gid, enabled):
        calls.append((gid, enabled))

    monkeypatch.setattr(wh, "set_group_enabled", _set)
    monkeypatch.setattr(wh.asyncio, "sleep", _no_sleep)
    return calls


async def _no_sleep(_seconds):
    return None


# ── permanent errors are not retried ───────────────────────────────────────────

async def test_kicked_group_is_not_retried_and_gets_muted(muted):
    bot = _Bot(exc=BotKicked("bot was kicked from the group chat"))
    await wh._send_with_retry(bot, -100123, "alert", media=[b"jpg"], is_video=False)

    assert bot.photo_calls == [-100123]      # one attempt, not three
    assert bot.message_calls == []           # no pointless text fallback
    assert muted == [(-100123, False)]       # group muted so it stops being targeted


async def test_chat_not_found_is_not_retried_but_is_left_unmuted(muted):
    """Nothing would ever un-mute a group silenced by ChatNotFound — there is no
    re-add event to recover from it — so the send is skipped without muting."""
    bot = _Bot(exc=ChatNotFound("chat not found"))
    await wh._send_with_retry(bot, -100777, "alert")

    assert bot.message_calls == [-100777]   # one attempt, not three
    assert muted == []


async def test_blocked_dm_is_skipped_without_touching_groups(muted):
    """A DM (positive id) is never muted — the admin keeps access and can unblock."""
    bot = _Bot(exc=BotKicked("blocked"))
    await wh._send_with_retry(bot, 555, "alert")

    assert bot.message_calls == [555]
    assert muted == []


async def test_transient_error_still_retries(muted):
    bot = _Bot(exc=NetworkError("boom"))
    await wh._send_with_retry(bot, 555, "alert", retries=3)

    assert bot.message_calls == [555, 555, 555]   # transient errors keep their retries
    assert muted == []


# ── one dead chat must not silence the rest ────────────────────────────────────

async def test_send_all_continues_past_a_failing_chat(monkeypatch):
    delivered: list[int] = []

    async def _send(_bot, chat_id, *_a, **_k):
        if chat_id == -100999:
            raise BotKicked("bot was kicked from the group chat")
        delivered.append(chat_id)

    monkeypatch.setattr(wh, "_send_with_retry", _send)
    await wh._send_all(None, [-100999, -100111, 555], "alert")

    # The dead group used to abort the loop, costing everyone behind it their alert.
    assert delivered == [-100111, 555]


# ── re-adding the bot restores a muted group ───────────────────────────────────

async def test_set_group_enabled_updates_by_chat_id(monkeypatch):
    """The auto-mute is only safe because re-adding the bot undoes it (see
    on_bot_chat_member_update in handlers/groups/group_events.py) — which depends on
    this write actually targeting the row by telegram_group_id."""
    import utils.db_api.groups as groups
    captured = {}

    async def _execute(query, *args):
        captured["query"] = query
        captured["args"] = args

    monkeypatch.setattr(groups.db, "execute", _execute)
    await groups.set_group_enabled(-100123, True)

    q = " ".join(captured["query"].split())
    assert "UPDATE alert_group SET enabled = $2 WHERE telegram_group_id = $1" in q
    assert captured["args"] == (-100123, True)
