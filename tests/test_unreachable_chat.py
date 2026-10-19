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
def retired(monkeypatch):
    """Records which groups were marked as left, standing in for the DB write."""
    calls: list[int] = []

    async def _mark(gid):
        calls.append(gid)

    monkeypatch.setattr(wh, "mark_group_left", _mark)
    monkeypatch.setattr(wh.asyncio, "sleep", _no_sleep)
    return calls


async def _no_sleep(_seconds):
    return None


# ── permanent errors are not retried ───────────────────────────────────────────

async def test_kicked_group_is_not_retried_and_is_retired(retired):
    bot = _Bot(exc=BotKicked("bot was kicked from the group chat"))
    await wh._send_with_retry(bot, -100123, "alert", media=[b"jpg"], is_video=False)

    assert bot.photo_calls == [-100123]      # one attempt, not three
    assert bot.message_calls == []           # no pointless text fallback
    assert retired == [-100123]              # out of routing and out of the panel


async def test_chat_not_found_is_not_retried_but_is_left_alone(retired):
    """Nothing would ever bring back a group retired on ChatNotFound — there is no re-add
    event to recover from it — so the send is skipped and the row is left as it is."""
    bot = _Bot(exc=ChatNotFound("chat not found"))
    await wh._send_with_retry(bot, -100777, "alert")

    assert bot.message_calls == [-100777]   # one attempt, not three
    assert retired == []


async def test_blocked_dm_is_skipped_without_touching_groups(retired):
    """A DM (positive id) is never retired — the admin keeps access and can unblock."""
    bot = _Bot(exc=BotKicked("blocked"))
    await wh._send_with_retry(bot, 555, "alert")

    assert bot.message_calls == [555]
    assert retired == []


async def test_transient_error_still_retries(retired):
    bot = _Bot(exc=NetworkError("boom"))
    await wh._send_with_retry(bot, 555, "alert", retries=3)

    assert bot.message_calls == [555, 555, 555]   # transient errors keep their retries
    assert retired == []


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


# ── re-adding the bot restores a retired group ─────────────────────────────────

async def test_register_group_clears_the_mute_and_the_departure(monkeypatch):
    """Retiring a group is only safe because re-registration undoes it: nobody unmutes a
    group by hand, so putting the bot back has to set enabled back to TRUE — and clear
    left_at, or the group would stay invisible in the panel it has to be configured in."""
    import utils.db_api.groups as groups
    captured = {}

    async def _execute(query, *args):
        captured["query"] = query

    monkeypatch.setattr(groups.db, "execute", _execute)
    await groups.register_group(-100123, "UNIT 571", "571")

    q = " ".join(captured["query"].split())
    assert "ON CONFLICT (telegram_group_id) DO UPDATE" in q
    assert "enabled = TRUE" in q
    assert "left_at = NULL" in q


# ── media goes by URL, and is downloaded only when Telegram cannot take it ─────

class _UrlBot:
    """Refuses URL sources the way Telegram does for a clip it cannot fetch."""

    def __init__(self, refuse_urls=True):
        self.refuse_urls = refuse_urls
        self.sent: list = []

    async def send_video(self, chat_id, media, caption=None, parse_mode=None):
        if isinstance(media, str) and self.refuse_urls:
            from aiogram.utils.exceptions import BadRequest
            raise BadRequest("wrong file identifier/HTTP URL specified")
        self.sent.append((chat_id, media))

    async def send_message(self, chat_id, text, parse_mode=None, disable_web_page_preview=None):
        self.sent.append((chat_id, "TEXT"))


async def test_a_clip_telegram_can_fetch_is_never_downloaded(retired, monkeypatch):
    async def _boom(url):
        raise AssertionError("downloaded a clip Telegram could fetch itself")

    monkeypatch.setattr(wh, "_download", _boom)
    bot = _UrlBot(refuse_urls=False)
    await wh._send_all(bot, [1, 2], "alert", ["https://x/c.mp4"], True)

    assert bot.sent == [(1, "https://x/c.mp4"), (2, "https://x/c.mp4")]


async def test_a_clip_telegram_refuses_is_downloaded_once_for_every_chat(retired, monkeypatch):
    downloads = []

    async def _download(url):
        downloads.append(url)
        return b"clip"

    monkeypatch.setattr(wh, "_download", _download)
    bot = _UrlBot()
    await wh._send_all(bot, [1, 2, 3], "alert", ["https://x/c.mp4"], True)

    assert downloads == ["https://x/c.mp4"]
    assert [chat for chat, _ in bot.sent] == [1, 2, 3]
    assert all(src != "TEXT" and not isinstance(src, str) for _, src in bot.sent)


async def test_a_clip_that_cannot_be_fetched_or_downloaded_still_sends_the_text(retired, monkeypatch):
    async def _download(url):
        return None

    monkeypatch.setattr(wh, "_download", _download)
    bot = _UrlBot()
    await wh._send_with_retry(bot, 1, "alert", media=["https://x/c.mp4"], is_video=True)

    assert bot.sent == [(1, "TEXT")]
