"""Get the driver bot into a driver group it is missing from, through the userbot.

Called by notify_driver (via bot_membership.request_bot_join) when an alert's group —
routed, name-checked — answers bot_not_in_group. The event is already claimed in
driver_notifications at that point; when this returns True the same notify_driver call
goes on and sends it, so no second claim is involved.

The flow, one group at a time per process (a lock), each step logged:

1. The driver bot's membership is read again, uncached: an alert that waited behind
   another one for the same group sees that one's result.
2. The userbot looks the group up among its own dialogs (read-only):
   - member, may add members (creator / admin with invite_users / members may invite)
     -> it adds the driver bot (utils.userbot.add_bot, adds spaced 30 s apart), waits
     ADD_RECHECK_DELAY, re-reads the membership; in -> True, the alert is sent.
   - member, cannot add (no right, a migrated/deactivated basic group, a Telegram
     error, or the add did not take) -> the admin is told: the DRIVER BOT sends a DM to
     the userbot's own account (USERBOT_USER_ID, else its get_me).
   - not a member -> the userbot DMs ONE HOS staff member (HOS_USERNAMES_<SLUG>, see
     pick_hos) asking to be added. No HOS configured, or that DM fails -> the admin.
3. The userbot unusable (flood wait, revoked session, cannot connect) -> the admin is
   told once per flood period / per OUTAGE_NOTICE_INTERVAL, not once per group, and
   nothing is recorded for the group (so it gets its normal turn afterwards).

A bot can only DM a user who has started it. The first time the admin DM fails with
"bot can't initiate conversation", the userbot sends /start to the driver bot (once per
process) and the DM is retried.

De-duplication: driver_bot_join_requests holds one row per action, written before it
is taken. A group gets at most one add and one message (HOS or admin) per DEDUP_HOURS,
whatever their outcome; an unreadable table means nothing is done (fail closed).

DRY RUN (DRIVER_ALERTS_DRY_RUN): nothing outward and nothing written — no add, no DM,
no /start; "would add" / "would message HOS @x" / "would notify admin" is logged with
the unit and group id. The read-only checks (membership, the userbot's dialogs and
rights) still run. The same 24 h de-dup is kept in memory so the log matches what live
would do. USERBOT_ENABLED=false (the default) skips all of this with one log line.

Never raises.
"""
import asyncio
import html
import logging
import time
from dataclasses import dataclass

from aiogram.utils.exceptions import CantInitiateConversation

from data import config
from utils import bot_membership, userbot
from utils.db_api.companies import get_company_name
from utils.db_api.driver_groups import (
    recent_join_actions,
    record_join_request,
    set_join_request_outcome,
)

logger = logging.getLogger(__name__)

ACTION_ADD = "add"
ACTION_HOS = "hos_message"
ACTION_ADMIN = "admin_message"
MESSAGE_ACTIONS = {ACTION_HOS, ACTION_ADMIN}

DEDUP_HOURS = 24
ADD_RECHECK_DELAY = 2.0
OUTAGE_NOTICE_INTERVAL = 3600.0

_now = time.monotonic       # tests patch these
_sleep = asyncio.sleep

_lock: asyncio.Lock | None = None
_bot_usernames: dict[int, str] = {}
_start_sent = False
# DRY RUN's stand-in for the table: (slug, group id) -> {action: monotonic time}
_dry_done: dict[tuple[str, int], dict[str, float]] = {}
# "flood" / "down" -> monotonic time until which the admin has been told
_outage_told_until: dict[str, float] = {}


def _reset_state() -> None:
    """Forget everything (tests)."""
    global _lock, _start_sent
    _lock, _start_sent = None, False
    _bot_usernames.clear()
    _dry_done.clear()
    _outage_told_until.clear()


# ── wording ──────────────────────────────────────────────────────────────────────

def pick_hos(usernames: list[str], group_id: int | None = None) -> str | None:
    """The one HOS staff member to ask about group_id: the first configured. (Kept
    separate so a rotation can be added here later.)"""
    return usernames[0] if usernames else None


def hos_message(company: str, unit: str | None, driver_name: str | None) -> str:
    """"Can you please add me to this group?\\nJRD\\n4042 - John Smith"."""
    line = (unit or "?").strip() or "?"
    name = (driver_name or "").strip()
    if name:
        line += f" - {name}"
    return f"Can you please add me to this group?\n{company}\n{line}"


def admin_message(bot_username: str, company: str, unit: str | None, group_id: int,
                  reason: str) -> str:
    return (f"Could not add @{bot_username} to the group for {company} unit {unit or '?'} "
            f"(group {group_id}): {reason}. Please add it manually.")


async def company_display_name(company_slug: str) -> str:
    """companies.name, else the slug upper-cased."""
    try:
        name = await get_company_name(company_slug)
    except Exception as e:
        logger.warning(f"[driver-bot] Could not read the name of company {company_slug}: {e}")
        name = None
    return (name or "").strip() or (company_slug or "").strip().upper()


def _describe(e: Exception) -> str:
    return f"{type(e).__name__}: {e}"[:200]


# ── one request ──────────────────────────────────────────────────────────────────

@dataclass
class _Ctx:
    bot: object
    company_slug: str
    unit: str | None
    group_id: int
    driver_name: str | None
    dry: bool
    bot_username: str = ""
    company: str = ""

    @property
    def where(self) -> str:
        return f"{self.company_slug}/{self.unit} group {self.group_id}"

    @property
    def tag(self) -> str:
        return "[driver-bot] " + ("DRY RUN " if self.dry else "")


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


async def _bot_username(bot) -> str:
    bot_id = int(getattr(bot, "id", 0) or 0)
    if bot_id not in _bot_usernames:
        me = await bot.get_me()
        _bot_usernames[bot_id] = me.username
    return _bot_usernames[bot_id]


def _dry_recent(ctx: _Ctx) -> set[str]:
    done = _dry_done.get((ctx.company_slug, ctx.group_id), {})
    horizon = _now() - DEDUP_HOURS * 3600
    return {a for a, at in done.items() if at > horizon}


def _dry_mark(ctx: _Ctx, action: str) -> None:
    _dry_done.setdefault((ctx.company_slug, ctx.group_id), {})[action] = _now()


async def _record(ctx: _Ctx, action: str, detail: str | None = None) -> int | None:
    """The row guarding an action, written before it; None = could not, do not act."""
    try:
        return await record_join_request(ctx.company_slug, ctx.group_id, ctx.unit, action,
                                         "pending", detail)
    except Exception as e:
        logger.error(f"{ctx.tag}{ctx.where}: could not record the {action} — not doing it: {e}")
        return None


async def _outcome(ctx: _Ctx, request_id: int | None, outcome: str,
                   detail: str | None = None) -> None:
    if request_id is None:
        return
    try:
        await set_join_request_outcome(request_id, outcome, detail)
    except Exception as e:
        logger.error(f"{ctx.tag}{ctx.where}: could not record outcome {outcome}: {e}")


async def _dm_admin(ctx: _Ctx, text: str) -> bool:
    """The driver bot DMs the userbot's account. /start from the userbot once if the
    bot was never started there."""
    global _start_sent
    admin_id = await userbot.my_user_id()
    if admin_id is None:
        logger.error(f"{ctx.tag}{ctx.where}: the admin's user id is unknown (set "
                     f"USERBOT_USER_ID) — notice not sent")
        return False
    for attempt in (1, 2):
        try:
            await ctx.bot.send_message(admin_id, html.escape(text, quote=False),
                                       disable_web_page_preview=True)
            return True
        except CantInitiateConversation:
            if attempt == 2 or _start_sent:
                logger.error(f"{ctx.tag}{ctx.where}: the driver bot may not DM the admin "
                             f"(never started there) — notice not sent")
                return False
            _start_sent = True
            logger.info(f"{ctx.tag}The driver bot was never started by the admin account — "
                        f"sending /start to @{ctx.bot_username} from the userbot")
            try:
                await userbot.send_message(ctx.bot_username, "/start")
            except Exception as e:
                logger.error(f"{ctx.tag}Could not send /start from the userbot: {_describe(e)}")
                return False
        except Exception as e:
            logger.error(f"{ctx.tag}{ctx.where}: admin DM failed: {_describe(e)}")
            return False
    return False


async def _notify_admin(ctx: _Ctx, reason: str, *, record: bool = True) -> None:
    text = admin_message(ctx.bot_username, ctx.company, ctx.unit, ctx.group_id, reason)
    if ctx.dry:
        logger.info(f"{ctx.tag}{ctx.where}: would notify admin: {text}")
        if record:
            _dry_mark(ctx, ACTION_ADMIN)
        return
    request_id = None
    if record:
        request_id = await _record(ctx, ACTION_ADMIN, reason)
        if request_id is None:
            return
    ok = await _dm_admin(ctx, text)
    await _outcome(ctx, request_id, "sent" if ok else "failed")
    if ok:
        logger.info(f"{ctx.tag}{ctx.where}: admin notified ({reason})")


async def _outage(ctx: _Ctx, reason: str) -> None:
    """The userbot cannot be used: tell the admin once per flood period / per
    OUTAGE_NOTICE_INTERVAL (for any group), record nothing for this group."""
    flood = userbot.flood_remaining()
    key = "flood" if flood > 0 else "down"
    now = _now()
    if _outage_told_until.get(key, 0.0) > now:
        logger.info(f"{ctx.tag}{ctx.where}: userbot unavailable ({reason}) — admin already told")
        return
    _outage_told_until[key] = now + (flood if flood > 0 else OUTAGE_NOTICE_INTERVAL)
    logger.warning(f"{ctx.tag}{ctx.where}: userbot unavailable ({reason}) — telling the admin")
    await _notify_admin(ctx, f"the userbot is unavailable ({reason})", record=False)


async def _ask_hos(ctx: _Ctx) -> None:
    env_name = config.HOS_USERNAMES_PREFIX + config.slug_env_suffix(ctx.company_slug)
    hos = pick_hos(config.hos_usernames(ctx.company_slug), ctx.group_id)
    if not hos:
        logger.warning(f"{ctx.tag}{ctx.where}: the userbot is not in the group and no HOS "
                       f"configured ({env_name}) — telling the admin instead")
        await _notify_admin(ctx, f"the userbot is not in this group and no HOS is configured "
                                 f"({env_name})")
        return
    text = hos_message(ctx.company, ctx.unit, ctx.driver_name)
    if ctx.dry:
        logger.info(f"{ctx.tag}{ctx.where}: would message HOS @{hos}: {text!r}")
        _dry_mark(ctx, ACTION_HOS)
        return
    request_id = await _record(ctx, ACTION_HOS, f"@{hos}")
    if request_id is None:
        return
    try:
        await userbot.send_message(hos, text)
    except Exception as e:
        await _outcome(ctx, request_id, "failed", f"@{hos}: {_describe(e)}")
        logger.error(f"{ctx.tag}{ctx.where}: message to HOS @{hos} failed: {_describe(e)}")
        await _notify_admin(ctx, f"the userbot is not in this group and messaging HOS @{hos} "
                                 f"failed ({_describe(e)})")
        return
    await _outcome(ctx, request_id, "sent")
    logger.info(f"{ctx.tag}{ctx.where}: asked HOS @{hos} to add the userbot")


async def _add(ctx: _Ctx, message_done: bool) -> bool:
    if ctx.dry:
        logger.info(f"{ctx.tag}{ctx.where}: would add @{ctx.bot_username} "
                    f"(unit {ctx.unit}, group {ctx.group_id})")
        _dry_mark(ctx, ACTION_ADD)
        return False
    request_id = await _record(ctx, ACTION_ADD)
    if request_id is None:
        return False
    try:
        await userbot.add_bot(ctx.group_id, ctx.bot_username)
    except userbot.UserbotFlood as e:
        outcome, reason = "flood_wait", f"Telegram rate-limited the userbot (FloodWait {e.seconds}s)"
    except userbot.UserbotUnavailable as e:
        outcome, reason = "unavailable", f"the userbot is unavailable ({e})"
    except Exception as e:
        outcome, reason = "add_failed", _describe(e)
    else:
        await _sleep(ADD_RECHECK_DELAY)
        m = await bot_membership.check(ctx.bot, ctx.group_id, use_cache=False)
        if m.can_post:
            await _outcome(ctx, request_id, "added")
            logger.info(f"{ctx.tag}{ctx.where}: the userbot added @{ctx.bot_username} "
                        f"(now {m.status})")
            return True
        outcome = "not_in_after_add"
        reason = f"the add went through but the bot is {m.status}" + (f" ({m.detail})" if m.detail else "")
    await _outcome(ctx, request_id, outcome, reason)
    logger.warning(f"{ctx.tag}{ctx.where}: could not add @{ctx.bot_username}: {reason}")
    if message_done:
        logger.info(f"{ctx.tag}{ctx.where}: a notice went out in the last {DEDUP_HOURS}h — "
                    f"not telling the admin again")
        return False
    await _notify_admin(ctx, reason)
    return False


async def _run(ctx: _Ctx) -> bool:
    fresh = await bot_membership.check(ctx.bot, ctx.group_id, use_cache=False)
    if fresh.can_post:
        logger.info(f"{ctx.tag}{ctx.where}: the driver bot is in the group now ({fresh.status})")
        return True
    if fresh.reason != bot_membership.REASON_NOT_IN_GROUP:
        logger.info(f"{ctx.tag}{ctx.where}: driver bot {fresh.status} ({fresh.reason}) — "
                    f"nothing to join")
        return False

    try:
        recent = await recent_join_actions(ctx.company_slug, ctx.group_id, DEDUP_HOURS)
    except Exception as e:
        logger.error(f"{ctx.tag}{ctx.where}: join history unreadable — doing nothing: {e}")
        return False
    if ctx.dry:
        recent = set(recent) | _dry_recent(ctx)
    add_done, message_done = ACTION_ADD in recent, bool(recent & MESSAGE_ACTIONS)
    if add_done and message_done:
        logger.info(f"{ctx.tag}{ctx.where}: add and notice already done in the last "
                    f"{DEDUP_HOURS}h — nothing more")
        return False

    ctx.bot_username = await _bot_username(ctx.bot)
    ctx.company = await company_display_name(ctx.company_slug)

    reason = userbot.unusable_reason()
    if reason:
        await _outage(ctx, reason)
        return False
    try:
        access = await userbot.inspect_group(ctx.group_id)
    except userbot.UserbotUnavailable as e:
        await _outage(ctx, str(e))
        return False
    except Exception as e:
        access = userbot.GroupAccess(userbot.CANNOT_ADD,
                                     f"could not read the userbot's rights ({_describe(e)})")
    logger.info(f"{ctx.tag}{ctx.where}: userbot {access.status}"
                f"{f' ({access.reason})' if access.reason else ''}")

    if access.status == userbot.CAN_ADD:
        if add_done:
            logger.info(f"{ctx.tag}{ctx.where}: add already tried in the last {DEDUP_HOURS}h — "
                        f"not retrying")
            return False
        return await _add(ctx, message_done)
    if message_done:
        logger.info(f"{ctx.tag}{ctx.where}: a notice went out in the last {DEDUP_HOURS}h — "
                    f"not sending another")
        return False
    if access.status == userbot.NOT_MEMBER:
        await _ask_hos(ctx)
    else:
        await _notify_admin(ctx, access.reason)
    return False


async def request_join(bot, company_slug: str, unit: str | None, group_id: int, *,
                       driver_name: str | None = None, dry_run: bool = False) -> bool:
    """Try to get the driver bot into group_id (see the module docstring). True only
    when the bot can post there now. Never raises."""
    ctx = _Ctx(bot, company_slug, unit, int(group_id), driver_name, dry_run)
    if not userbot.is_enabled():
        logger.info(f"{ctx.tag}{ctx.where}: driver bot not in the group; userbot off "
                    f"(USERBOT_ENABLED=false) — add it by hand")
        return False
    if not userbot.is_configured():
        logger.warning(f"{ctx.tag}{ctx.where}: driver bot not in the group; USERBOT_ENABLED "
                       f"but USERBOT_SESSION / USERBOT_API_ID / USERBOT_API_HASH missing")
        return False
    try:
        async with _get_lock():
            return await _run(ctx)
    except Exception as e:
        logger.error(f"{ctx.tag}{ctx.where}: join request failed: {_describe(e)}", exc_info=True)
        return False
