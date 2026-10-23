"""The userbot: a Telegram user account (Telethon) that adds the driver bot to groups.

The driver bot cannot join a group by itself; a member with the right to add members
has to add it. The userbot is in many of the trucks' driver groups, so when an alert's
group lacks the driver bot, utils/bot_join.py has it add the bot. This module only
manages the Telegram client and does the individual userbot actions.

Session safety. The account is logged in through a StringSession (USERBOT_SESSION).
Telegram may revoke an authorization used from two places at once
(AUTH_KEY_DUPLICATED), so: ONE client per process, created and connected lazily on
first use, used by one action at a time (a lock), and disconnected after IDLE_DISCONNECT
seconds without use. Do not run scripts/add_driver_bot.py (or anything else with the
same session) while the driver-alerts service has USERBOT_ENABLED=true. A revoked or
unauthorized session disables the userbot for the life of the process. The session
string is never logged.

Rate limits. Adds are spaced at least ADD_SPACING seconds apart. A FloodWaitError
stops all userbot use until the wait has passed (UserbotFlood is raised meanwhile).

Groups are found among the userbot's dialogs: Telethon's dialog ids are the same
"marked" ids the Bot API uses (-100... for supergroups, -id for basic groups), and the
dialogs carry the access hashes a StringSession does not store.
"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.channels import InviteToChannelRequest
from telethon.tl.functions.messages import AddChatUserRequest
from telethon.tl.types import Channel, ChannelForbidden, Chat, ChatForbidden

from data import config

logger = logging.getLogger(__name__)

IDLE_DISCONNECT = 300.0     # disconnect after this long unused
IDLE_CHECK = 30.0           # how often the idle watcher looks
ADD_SPACING = 30.0          # at least this long between two adds
DIALOGS_TTL = 600.0         # the dialog list is re-read after this
DIALOGS_MISS_REFRESH = 60.0  # a group missing from it re-reads it at most this often

# inspect_group answers
CAN_ADD = "can_add"
CANNOT_ADD = "cannot_add"
NOT_MEMBER = "not_member"

_now = time.monotonic        # tests patch these
_sleep = asyncio.sleep


class UserbotUnavailable(Exception):
    """The userbot cannot be used right now (not configured, session revoked, cannot
    connect, flood wait). str() is a reason fit for a log line or the admin."""


class UserbotFlood(UserbotUnavailable):
    def __init__(self, seconds: float):
        self.seconds = max(0, int(seconds))
        super().__init__(f"Telegram rate limit on the userbot, {self.seconds}s left")


@dataclass(frozen=True)
class GroupAccess:
    status: str          # CAN_ADD | CANNOT_ADD | NOT_MEMBER
    reason: str = ""     # why not, for CANNOT_ADD


# ── process state ────────────────────────────────────────────────────────────────

_client = None
_lock: asyncio.Lock | None = None
_last_used = 0.0
_idle_task: asyncio.Task | None = None
_flood_until = 0.0
_dead: str | None = None          # why the session is unusable for good
_last_add_at: float | None = None
_me_id: int | None = None
_dialogs: dict[int, object] | None = None
_dialogs_at = 0.0


def _reset_state() -> None:
    """Forget everything (tests). Does not disconnect."""
    global _client, _lock, _last_used, _idle_task, _flood_until, _dead, _last_add_at
    global _me_id, _dialogs, _dialogs_at
    _client, _lock, _last_used, _idle_task = None, None, 0.0, None
    _flood_until, _dead, _last_add_at, _me_id = 0.0, None, None, None
    _dialogs, _dialogs_at = None, 0.0


def is_enabled() -> bool:
    """USERBOT_ENABLED, the kill switch (default off)."""
    return bool(config.USERBOT_ENABLED)


def _api_id() -> int | None:
    try:
        return int(str(config.USERBOT_API_ID).strip())
    except (TypeError, ValueError):
        return None


def is_configured() -> bool:
    """Session, API id and API hash are all set."""
    return bool(config.USERBOT_SESSION and _api_id() and config.USERBOT_API_HASH)


def flood_remaining() -> float:
    return max(0.0, _flood_until - _now())


def unusable_reason() -> str | None:
    """Why the userbot cannot be used now without trying, or None."""
    if _dead:
        return _dead
    if not is_configured():
        return "USERBOT_SESSION / USERBOT_API_ID / USERBOT_API_HASH not set"
    left = flood_remaining()
    if left > 0:
        return str(UserbotFlood(left))
    return None


def make_client(session: str, api_id: int, api_hash: str) -> TelegramClient:
    """A Telethon client on a StringSession (also used by scripts/add_driver_bot.py)."""
    return TelegramClient(StringSession(session), int(api_id), api_hash)


def _make_client():
    return make_client(config.USERBOT_SESSION, _api_id(), config.USERBOT_API_HASH)


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _note_flood(seconds) -> UserbotFlood:
    global _flood_until
    exc = UserbotFlood(seconds)
    _flood_until = max(_flood_until, _now() + exc.seconds)
    logger.warning(f"[userbot] FloodWait {exc.seconds}s — not using the userbot until it passes")
    return exc


async def _drop_client() -> None:
    global _client
    client, _client = _client, None
    if client is not None:
        try:
            await client.disconnect()
        except Exception as e:
            logger.warning(f"[userbot] Disconnect failed: {type(e).__name__}")


async def _kill(reason: str) -> UserbotUnavailable:
    global _dead
    _dead = reason
    logger.error(f"[userbot] {reason} — userbot disabled until the service restarts")
    await _drop_client()
    return UserbotUnavailable(reason)


async def _connect():
    client = _make_client()
    try:
        await client.connect()
        authorized = await client.is_user_authorized()
    except errors.AuthKeyDuplicatedError:
        raise await _kill("session revoked by Telegram (AUTH_KEY_DUPLICATED: used from two "
                          "places at once) — generate a new USERBOT_SESSION")
    except Exception as e:
        try:
            await client.disconnect()
        except Exception:
            pass
        # The type only: a session parse error could echo part of the session.
        raise UserbotUnavailable(f"could not connect ({type(e).__name__})")
    if not authorized:
        try:
            await client.disconnect()
        except Exception:
            pass
        raise await _kill("USERBOT_SESSION is not authorized")
    logger.info("[userbot] Connected")
    return client


def _start_idle_watch() -> None:
    global _idle_task
    if _idle_task is None or _idle_task.done():
        _idle_task = asyncio.get_running_loop().create_task(_idle_watch())


async def _idle_watch() -> None:
    while _client is not None:
        await asyncio.sleep(IDLE_CHECK)
        await disconnect_if_idle()


async def disconnect_if_idle() -> bool:
    """Disconnect when unused for IDLE_DISCONNECT. True if it did."""
    async with _get_lock():
        if _client is None or _now() - _last_used < IDLE_DISCONNECT:
            return False
        await _drop_client()
        logger.info(f"[userbot] Disconnected after {IDLE_DISCONNECT:.0f}s idle")
        return True


@asynccontextmanager
async def _session():
    """Exclusive use of the one client, connected. Raises UserbotUnavailable (or
    UserbotFlood) instead of yielding when it cannot be used; a FloodWaitError or a
    revoked session inside the block is turned into the same."""
    global _client, _last_used
    _raise_if_unusable()
    async with _get_lock():
        _raise_if_unusable()             # may have changed while waiting
        if _client is None or not _client.is_connected():
            await _drop_client()
            _client = await _connect()
            _start_idle_watch()
        try:
            yield _client
        except errors.FloodWaitError as e:
            raise _note_flood(e.seconds) from None
        except errors.AuthKeyDuplicatedError:
            raise await _kill("session revoked by Telegram (AUTH_KEY_DUPLICATED)") from None
        except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
                errors.UserDeactivatedError, errors.UserDeactivatedBanError) as e:
            raise await _kill(f"session no longer valid ({type(e).__name__})") from None
        finally:
            _last_used = _now()


def _raise_if_unusable() -> None:
    if not _dead and flood_remaining() > 0:
        raise UserbotFlood(flood_remaining())
    reason = unusable_reason()
    if reason:
        raise UserbotUnavailable(reason)


# ── pure helpers (shared with scripts/add_driver_bot.py) ─────────────────────────

def can_add(entity, perms) -> bool:
    """Whether the userbot may add members: creator, admin with invite_users, or the
    chat lets every member invite (default_banned_rights.invite_users is False)."""
    if perms.is_creator or (perms.is_admin and perms.invite_users):
        return True
    banned = getattr(entity, "default_banned_rights", None)
    return banned is not None and not banned.invite_users


def stale_basic_group(entity) -> bool:
    """A basic group that was upgraded to a supergroup or deactivated: nothing can be
    added to it any more (pti still has its old id)."""
    return isinstance(entity, Chat) and bool(entity.deactivated or entity.migrated_to)


async def add_member(client, entity, user) -> None:
    """Add user (an input entity) to a supergroup or a basic group."""
    if isinstance(entity, Channel):
        await client(InviteToChannelRequest(entity, [user]))
    else:
        await client(AddChatUserRequest(chat_id=entity.id, user_id=user, fwd_limit=0))


async def group_dialogs(client) -> dict[int, object]:
    """The userbot's groups: marked chat id -> entity."""
    return {d.id: d.entity for d in await client.get_dialogs(limit=None) if d.is_group}


# ── actions ──────────────────────────────────────────────────────────────────────

async def _dialog_entity(client, group_id: int):
    global _dialogs, _dialogs_at
    now = _now()
    stale = _dialogs is None or now - _dialogs_at >= DIALOGS_TTL
    missing = _dialogs is not None and group_id not in _dialogs \
        and now - _dialogs_at >= DIALOGS_MISS_REFRESH
    if stale or missing:
        _dialogs = await group_dialogs(client)
        _dialogs_at = _now()
        logger.info(f"[userbot] {len(_dialogs)} groups in the userbot's dialogs")
    return _dialogs.get(int(group_id))


async def inspect_group(group_id: int) -> GroupAccess:
    """Whether the userbot is in group_id and may add members there. Read-only.
    Raises UserbotUnavailable; any other Telegram error propagates."""
    async with _session() as client:
        entity = await _dialog_entity(client, group_id)
        if entity is None or isinstance(entity, (ChatForbidden, ChannelForbidden)) \
                or getattr(entity, "left", False):
            return GroupAccess(NOT_MEMBER)
        if stale_basic_group(entity):
            return GroupAccess(CANNOT_ADD, "basic group migrated/deactivated: pti has a stale id")
        try:
            perms = await client.get_permissions(entity, "me")
        except errors.UserNotParticipantError:
            return GroupAccess(NOT_MEMBER)
        if can_add(entity, perms):
            return GroupAccess(CAN_ADD)
        return GroupAccess(CANNOT_ADD, "the userbot has no right to add members")


async def add_bot(group_id: int, bot_username: str) -> None:
    """Add @bot_username to group_id, at least ADD_SPACING after the previous add.
    "Already a participant" counts as done. Raises UserbotUnavailable / UserbotFlood,
    LookupError (the group left the userbot's dialogs) or the Telegram error."""
    global _last_add_at
    async with _session() as client:
        if _last_add_at is not None:
            wait = ADD_SPACING - (_now() - _last_add_at)
            if wait > 0:
                logger.info(f"[userbot] Spacing adds: waiting {wait:.0f}s")
                await _sleep(wait)
        entity = await _dialog_entity(client, group_id)
        if entity is None:
            raise LookupError("the group is no longer in the userbot's dialogs")
        bot = await client.get_input_entity(bot_username)
        _last_add_at = _now()
        try:
            await add_member(client, entity, bot)
        except errors.UserAlreadyParticipantError:
            logger.info(f"[userbot] @{bot_username} already in group {group_id}")


async def send_message(peer: str, text: str) -> None:
    """A direct message from the userbot (peer: a @username or the bot's username)."""
    async with _session() as client:
        await client.send_message(peer, text)


async def my_user_id() -> int | None:
    """The userbot account's own user id: USERBOT_USER_ID when set, else get_me (once).
    None when neither works."""
    global _me_id
    raw = str(config.USERBOT_USER_ID or "").strip()
    if raw:
        try:
            return int(raw)
        except ValueError:
            logger.warning("[userbot] USERBOT_USER_ID is not an integer — asking Telegram")
    if _me_id is not None:
        return _me_id
    try:
        async with _session() as client:
            me = await client.get_me()
    except Exception as e:
        logger.error(f"[userbot] Could not read the userbot's own id: {e}")
        return None
    _me_id = int(me.id) if me is not None else None
    return _me_id


async def close() -> None:
    """Disconnect now (service shutdown)."""
    global _idle_task
    task, _idle_task = _idle_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass
    await _drop_client()
