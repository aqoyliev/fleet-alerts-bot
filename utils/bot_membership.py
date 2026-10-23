"""Can the driver-alerts bot post in a group? Read-only: one getChatMember for itself.

Checked before every driver-group send and in DRY RUN, so a group the bot was never
added to (or was removed from, or muted in) is skipped and logged instead of failing at
send time. A definitive answer is cached for CACHE_TTL per chat; a failed check is not
cached and fails closed (bot_check_failed).

request_bot_join is called when the bot is not in the group: utils/bot_join.py has the
userbot add it (or asks HOS / the admin). It is off unless USERBOT_ENABLED.
"""
import logging
import time
from dataclasses import dataclass

from aiogram.utils.exceptions import (
    BadRequest,
    BotKicked,
    ChatNotFound,
    GroupDeactivated,
    MigrateToChat,
    Unauthorized,
)

logger = logging.getLogger(__name__)

CACHE_TTL = 600.0

REASON_NOT_IN_GROUP = "bot_not_in_group"
REASON_CANNOT_POST = "bot_cannot_post"
REASON_MIGRATED = "group_migrated"
REASON_CHECK_FAILED = "bot_check_failed"

_POSTING = {"creator", "administrator", "member"}


@dataclass(frozen=True)
class Membership:
    """status: Telegram's ("creator", "administrator", "member", "restricted", "left",
    "kicked"), or "not_found" (chat unknown to the bot), "migrated", "error".
    reason is None exactly when the bot can post."""
    status: str
    reason: str | None = None
    detail: str = ""
    migrate_to: int | None = None

    @property
    def can_post(self) -> bool:
        return self.reason is None


# (bot id, chat id) -> (monotonic time, Membership)
_cache: dict[tuple[int, int], tuple[float, Membership]] = {}


def classify(member) -> Membership:
    """A getChatMember answer for the bot itself -> Membership."""
    status = str(getattr(member, "status", "") or "").lower()
    if status in _POSTING:
        return Membership(status)
    if status == "restricted":
        if getattr(member, "is_member", True) is False:
            return Membership(status, REASON_NOT_IN_GROUP, "restricted, not a member")
        if not getattr(member, "can_send_messages", False):
            return Membership(status, REASON_CANNOT_POST, "restricted without can_send_messages")
        return Membership(status)
    if status in ("left", "kicked"):
        return Membership(status, REASON_NOT_IN_GROUP)
    return Membership(status or "error", REASON_CHECK_FAILED, f"unexpected status {status!r}")


def from_error(e: Exception) -> tuple[Membership, bool]:
    """(Membership, cacheable) for a getChatMember failure."""
    msg = str(e).lower()
    detail = f"{type(e).__name__}: {e}"
    if isinstance(e, MigrateToChat):
        return Membership("migrated", REASON_MIGRATED, detail, e.migrate_to_chat_id), False
    if isinstance(e, BotKicked):
        return Membership("kicked", REASON_NOT_IN_GROUP, detail), True
    if isinstance(e, (ChatNotFound, GroupDeactivated)):
        return Membership("not_found", REASON_NOT_IN_GROUP, detail), True
    if isinstance(e, Unauthorized) and ("not a member" in msg or "kicked" in msg
                                        or "chat was deleted" in msg):
        return Membership("left", REASON_NOT_IN_GROUP, detail), True
    if isinstance(e, BadRequest) and ("not found" in msg or "participant_id_invalid" in msg):
        return Membership("not_found", REASON_NOT_IN_GROUP, detail), True
    # Network, flood control, a bad token: nothing known about the group.
    return Membership("error", REASON_CHECK_FAILED, detail), False


async def check(bot, chat_id: int, *, use_cache: bool = True) -> Membership:
    """The driver bot's membership in chat_id. Never raises."""
    key = (int(getattr(bot, "id", 0) or 0), int(chat_id))
    now = time.monotonic()
    if use_cache:
        cached = _cache.get(key)
        if cached and now - cached[0] < CACHE_TTL:
            return cached[1]
    try:
        member = await bot.get_chat_member(chat_id, bot.id)
    except Exception as e:
        result, cacheable = from_error(e)
    else:
        result, cacheable = classify(member), True
    if cacheable:
        _cache[key] = (now, result)
    else:
        _cache.pop(key, None)
    return result


def invalidate(chat_id: int) -> None:
    """Forget what is known about chat_id (e.g. after a send there failed)."""
    for key in [k for k in _cache if k[1] == int(chat_id)]:
        _cache.pop(key, None)


async def request_bot_join(bot, company_slug: str, unit: str | None, group_id: int,
                           membership: Membership, *, driver_name: str | None = None,
                           dry_run: bool = False) -> bool:
    """The driver bot is not in group_id (membership.reason == REASON_NOT_IN_GROUP): have
    the userbot add it, or ask HOS / the admin (utils/bot_join.py). True only when the
    bot can post there now — the caller then sends as usual. In DRY RUN nothing is
    added or sent, only logged. Never raises."""
    try:
        from utils import bot_join  # lazy: pulls in Telethon
    except Exception as e:
        logger.error(f"[driver-bot] {company_slug}/{unit}: driver bot not usable in group "
                     f"{group_id} ({membership.reason}) and the userbot module failed to load: {e}")
        return False
    return await bot_join.request_join(bot, company_slug, unit, group_id,
                                       driver_name=driver_name, dry_run=dry_run)
