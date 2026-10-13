"""Tests for delivery to chats the bot can no longer post to.

Production hit this on single-company when the bot was kicked from a driver group:
BotKicked subclasses TelegramAPIError, so the media loop retried it three times, the
text-only fallback did not catch it at all, and the exception escaped _send_with_retry —
aborting the whole broadcast, so every recipient after the dead group silently lost the
alert.

The fix is carried over whole, with one difference: single-company also mutes the dead
group, and this branch must not. See test_a_kicked_group_is_not_muted.
"""
import pytest
from aiogram.utils.exceptions import BotKicked, ChatNotFound, NetworkError

import utils.db_api.groups as groups
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
def writes(monkeypatch):
    """Every statement the group layer sends to the database while a send fails.

    The claim under test is that there are none, so the spy sits on the database itself
    rather than on one named function: a mute added back under any name still lands here.
    """
    calls: list[str] = []

    async def _execute(query, *args):
        calls.append(" ".join(query.split()))

    monkeypatch.setattr(groups.db, "execute", _execute)
    monkeypatch.setattr(wh.asyncio, "sleep", _no_sleep)
    return calls


async def _no_sleep(_seconds):
    return None


# ── permanent errors are not retried ───────────────────────────────────────────

async def test_kicked_group_is_not_retried(writes):
    bot = _Bot(exc=BotKicked("bot was kicked from the group chat"))
    await wh._send_with_retry(bot, -100123, "alert", media=[b"jpg"], is_video=False)

    assert bot.photo_calls == [-100123]      # one attempt, not three
    assert bot.message_calls == []           # no pointless text fallback


async def test_a_kicked_group_is_not_muted(writes):
    """There is one group, and a mute is lifted only by an admin.

    single-company mutes a group it is kicked from and relies on the my_chat_member
    update to un-mute it when the bot is put back. That update is skipped whenever the
    bot is re-added while it is restarting, which would leave the fleet's only group
    silent with nothing on screen to explain why. So nothing is recorded: the group is
    tried again on the next alert, and the first one after the bot returns gets through.
    """
    bot = _Bot(exc=BotKicked("bot was kicked from the group chat"))
    await wh._send_with_retry(bot, -100123, "alert")

    assert writes == []


async def test_chat_not_found_is_not_retried_and_changes_nothing(writes):
    bot = _Bot(exc=ChatNotFound("chat not found"))
    await wh._send_with_retry(bot, -100777, "alert")

    assert bot.message_calls == [-100777]   # one attempt, not three
    assert writes == []


async def test_blocked_dm_is_skipped(writes):
    """An admin who blocked the bot keeps their access and can unblock at any time."""
    bot = _Bot(exc=BotKicked("blocked"))
    await wh._send_with_retry(bot, 555, "alert")

    assert bot.message_calls == [555]
    assert writes == []


async def test_transient_error_still_retries(writes):
    bot = _Bot(exc=NetworkError("boom"))
    await wh._send_with_retry(bot, 555, "alert", retries=3)

    assert bot.message_calls == [555, 555, 555]   # transient errors keep their retries


# ── one dead chat must not silence the rest ────────────────────────────────────

async def test_send_all_continues_past_a_failing_chat(monkeypatch):
    delivered: list[int] = []

    async def _send(_bot, chat_id, *_a, **_k):
        if chat_id == -100999:
            raise BotKicked("bot was kicked from the group chat")
        delivered.append(chat_id)

    monkeypatch.setattr(wh, "_send_with_retry", _send)
    await wh._send_all(None, [-100999, 555, 556], "alert")

    # The dead group used to abort the loop, costing every admin behind it their alert.
    assert delivered == [555, 556]
