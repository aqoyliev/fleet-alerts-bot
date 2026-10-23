"""Tell the driver about their own cell phone use.

A cell_phone event goes, besides the company's groups and admin DMs, to the driver:
the clip (or photo) and the alert text are posted to that truck's driver Telegram group
through the separate driver-alerts bot, and emailed to the driver. The driver is also
written onto the violation row so the company can charge them at month end.

The first cell_phone event of a driver in a day (America/New_York) gets exactly the text
the company group gets. The 2nd and later of the same day carry a warning that the driver
will be charged for it, and from the 3rd on a line saying how many that is today. Events
before DRIVER_ALERTS_COUNT_FROM (the go-live floor) are not counted, so switching on
mid-day never charges for events the driver was not told about. Each event is notified at most once, however many times its webhook is
redelivered (driver_notifications is claimed before anything is sent).

A SAMSARA event is also gated on Samsara's own review: after the claim and before
anything is sent, its safety event is looked up (retried for ~8 minutes, since it lands
in the API minutes after the webhook) and the driver is told only while its
coachingState is needsReview / needsCoaching / needsRecognition. Any other state, or no
safety event / a failing API, and nothing is sent (fail closed). The state is written
to the violation either way, and a rejected event is not counted toward later
ordinals. The caller runs a Samsara notification as a background task so that wait
never holds up the webhook handling. Motive events are not gated.

Only a driver Vitality identified is notified (a driver from the event payload or
none -> skip_reason "driver_unknown", nothing sent, email included).

Routing. A company with a pti-checker-bot database (PTI_DATABASE_URL_<SLUG>) is routed
only from it (utils/pti_groups.py: the live groups, refreshed every minute): no group,
an ambiguous unit or the database unreachable -> skip_reason pti_no_group /
pti_ambiguous / pti_unavailable, nothing sent. Any other company uses the imported
driver_groups mapping as before. The Vitality name must then match the group — the pti
title or any of its group_drivers names, or (driver_groups) the Telegram title, cached
an hour — by driver_name_in_title; with DRIVER_ALERTS_NAME_MISMATCH=skip (the default)
a mismatch is skip_reason name_mismatch (name_unverified when the title is
unreadable). driver_notifications.name_match records TRUE / FALSE / NULL. Last, the
driver bot's own membership is checked (utils/bot_membership.py, read-only):
not in the group / muted -> bot_not_in_group / bot_cannot_post. Not in the group: with
USERBOT_ENABLED the userbot adds the bot and, once it is in, this same (already
claimed) event is sent; otherwise it asks HOS / the admin (utils/bot_join.py) and the
event stays skipped. All of this runs in DRY RUN too (the userbot step only logs what
it would do), and one decision line per event is logged. A skipped event sends nothing,
email included. DRIVER_ALERTS_COMPANIES limits which companies are handled at all.

notify_driver never raises: it runs after the company alerts went out, and nothing
here may cost them or the rest of the event handling.
"""
import asyncio
import html
import io
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram.types import InputFile, InputMediaPhoto, InputMediaVideo
from aiogram.utils.exceptions import (
    BotKicked,
    ChatNotFound,
    GroupDeactivated,
    MigrateToChat,
    RetryAfter,
    Unauthorized,
)

from data import config
from utils import bot_membership, pti_groups
from utils.db_api import db
from utils.db_api.driver_groups import (
    ensure_driver_schema,
    get_driver_group,
    migrate_driver_group,
    deactivate_driver_group,
)
from utils.db_api.violations import (
    update_violation_driver,
    update_violation_coaching_state,
    count_driver_cell_phone_events,
)
from utils.samsara import safety

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# ── Driver-facing wording ────────────────────────────────────────────────────────
# Edit here. {amount} is CHARGE_AMOUNT_TEXT, e.g. " $50", empty until it is decided.
# {ordinal} is "3rd", {day} is the event's New York date, e.g. "Oct 23, 2026".
CHARGE_AMOUNT_TEXT = ""
CHARGE_HEADLINE = "⚠️ <b>WARNING: You will be charged{amount} for this violation.</b>"
REPEAT_NOTICE = "This is your {ordinal} cell phone use today ({day})."
EMAIL_SUBJECT = "Cell phone use detected - Truck {vehicle}"
EMAIL_SUBJECT_CHARGED = "Violation notice: cell phone use - Truck {vehicle} (charge applies)"

# Telegram's caption limit, counted on the text as displayed (tags stripped).
_CAPTION_LIMIT = 1024
_ATTEMPTS = 3
_RETRY_DELAY = 5.0
_TAG_RE = re.compile(r"<[^>]+>")
_NAME_TOKEN_RE = re.compile(r"[^\W\d_]{2,}")

# A group's Telegram title, per chat id, for an hour: chat id -> (monotonic time, title).
_TITLE_TTL = 3600.0
_title_cache: dict[int, tuple[float, str]] = {}


# ── helpers ──────────────────────────────────────────────────────────────────────

def _get_driver_bot():
    """The driver-alerts bot, or None when DRIVER_ALERTS_BOT_TOKEN is unset. Imported
    lazily: loader builds the bots, and this module must stay importable without it."""
    try:
        from loader import driver_bot
        return driver_bot
    except Exception as e:
        logger.error(f"[driver] Could not load the driver-alerts bot: {e}")
        return None


def is_enabled() -> bool:
    """Whether driver notifications are switched on (a driver bot is configured)."""
    return _get_driver_bot() is not None


def day_start_et(occurred_at: datetime | None) -> datetime:
    """Midnight America/New_York of the day the event falls in, tz-aware.
    A naive timestamp is read as UTC (what _parse_occurred and asyncpg both assume)."""
    ts = occurred_at or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    local = ts.astimezone(ET)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def day_label(day_start: datetime) -> str:
    """"Oct 23, 2026" — no zero-padded day."""
    return f"{day_start.strftime('%b')} {day_start.day}, {day_start.year}"


_warned_count_from: set[str] = set()


def parse_count_from(raw: str | None) -> datetime | None:
    """DRIVER_ALERTS_COUNT_FROM as a tz-aware datetime: a bare date is midnight New York,
    a naive datetime is New York time. Empty or invalid -> None (invalid warns once)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        if raw not in _warned_count_from:
            _warned_count_from.add(raw)
            logger.warning(f"[driver] DRIVER_ALERTS_COUNT_FROM={raw!r} is not an ISO date or "
                           f"datetime — ignored, counting from the day start")
        return None
    return ts.replace(tzinfo=ET) if ts.tzinfo is None else ts


def _name_tokens(text: str | None) -> set[str]:
    return set(_NAME_TOKEN_RE.findall((text or "").lower()))


def driver_name_in_title(name: str | None, title: str | None) -> bool:
    """Whether a driver's name appears in a driver group's title.

    Both are reduced to lowercase alphabetic tokens of 2+ letters (split on anything
    that is not a letter), so "SMITH, JOHN" matches "John A Smith" whatever the order,
    commas or middle initials. It is a match when at least min(2, number of name
    tokens) of the name's tokens are in the title's tokens; a name with no usable token
    or an empty title never matches."""
    wanted = _name_tokens(name)
    if not wanted:
        return False
    return len(wanted & _name_tokens(title)) >= min(2, len(wanted))


NAME_IN_TITLE = "title"
NAME_IN_GROUP_DRIVERS = "group_drivers"
NAME_NOWHERE = "none"
NAME_UNKNOWN = "unknown"   # nothing to compare with (title unreadable / empty)


def name_check(name: str | None, title: str | None,
               driver_names=()) -> tuple[bool | None, str]:
    """(name_match, source): whether the driver is the group title's or one of the
    group's drivers' (pti group_drivers, their Telegram names), and which matched.
    None / NAME_UNKNOWN when there is nothing to compare with."""
    names = [n for n in (driver_names or ()) if (n or "").strip()]
    if not (title or "").strip() and not names:
        return None, NAME_UNKNOWN
    if driver_name_in_title(name, title):
        return True, NAME_IN_TITLE
    if any(driver_name_in_title(name, n) for n in names):
        return True, NAME_IN_GROUP_DRIVERS
    return False, NAME_NOWHERE


def company_enabled(company_slug: str | None) -> bool:
    """DRIVER_ALERTS_COMPANIES allowlist; empty = every company."""
    allowed = config.DRIVER_ALERTS_COMPANIES
    return not allowed or (company_slug or "").strip().lower() in allowed


_warned_mismatch_mode: set[str] = set()


def _skip_on_name_mismatch() -> bool:
    """DRIVER_ALERTS_NAME_MISMATCH: "send" sends anyway; "skip" and anything else skip."""
    mode = (config.DRIVER_ALERTS_NAME_MISMATCH or "skip").strip().lower()
    if mode == "send":
        return False
    if mode != "skip" and mode not in _warned_mismatch_mode:
        _warned_mismatch_mode.add(mode)
        logger.warning(f"[driver] DRIVER_ALERTS_NAME_MISMATCH={mode!r} is neither skip nor send "
                       f"— skipping on a mismatch")
    return True


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def build_driver_message(base_text: str, ordinal: int, day_label: str) -> str:
    """The driver-facing text. The 1st event of the day is the company's alert as it
    is; ordinal >= 2 gets the charge warning on top, and ordinal >= 3 also says which
    cell phone use of the day this is."""
    base = (base_text or "").strip()
    if ordinal < 2:
        return base
    parts = [CHARGE_HEADLINE.format(amount=CHARGE_AMOUNT_TEXT), base]
    if ordinal >= 3:
        parts.append(REPEAT_NOTICE.format(ordinal=_ordinal(ordinal), day=day_label))
    return "\n\n".join(p for p in parts if p)


def _plain(text: str) -> str:
    return html.unescape(_TAG_RE.sub("", text or ""))


def _email_bodies(text: str, links: list[str]) -> tuple[str, str]:
    """(html, plain) email bodies. The Telegram HTML (<b>, <i>, <code>) is valid email
    HTML as it is; only line breaks need converting."""
    plain = _plain(text)
    body = (text or "").replace("\n", "<br>\n")
    if links:
        plain += "\n\nEvidence:\n" + "\n".join(links)
        items = "".join(
            f'<li><a href="{html.escape(u, quote=True)}">{html.escape(u)}</a></li>' for u in links
        )
        body += f"<br>\n<br>\n<b>Evidence:</b><ul>{items}</ul>"
    return f'<div style="font-family:Arial,sans-serif;font-size:14px">{body}</div>', plain


def _clip_still_coming(event: dict, video_urls: list[str], image_urls: list[str]) -> bool:
    """A Motive delivery that says it has media but carries no URL yet: the clip is
    still transcoding and arrives on a later delivery of the same event (which the
    webhook dedup lets through). The driver gets that one, with the evidence, rather
    than this one — once claimed, an event is never notified again."""
    if event.get("_source") == "samsara" or video_urls or image_urls:
        return False
    return bool((event.get("camera_media") or {}).get("available"))


# ── driver_notifications bookkeeping ─────────────────────────────────────────────

async def _claim(event_id: int, company_slug: str, vehicle: str) -> bool:
    """True if this call is the first to notify about event_id."""
    claimed = await db.fetchval(
        """
        INSERT INTO driver_notifications (event_id, company_slug, vehicle_number)
        VALUES ($1, $2, $3)
        ON CONFLICT (event_id) DO NOTHING
        RETURNING event_id
        """,
        event_id, company_slug, (vehicle or "")[:50] or None,
    )
    return claimed is not None


async def _record_driver(event_id: int, driver_ref: str | None, ordinal: int) -> None:
    await db.execute(
        "UPDATE driver_notifications SET driver_ref = $2, ordinal = $3 WHERE event_id = $1",
        event_id, (driver_ref or None) and str(driver_ref)[:100], ordinal,
    )


async def _record_sent(event_id: int, sent_group: bool, sent_email: bool,
                       name_match: bool | None = None) -> None:
    await db.execute(
        "UPDATE driver_notifications SET sent_group = $2, sent_email = $3, name_match = $4 "
        "WHERE event_id = $1",
        event_id, sent_group, sent_email, name_match,
    )


async def _record_name_match(event_id: int, name_match: bool | None) -> None:
    await db.execute(
        "UPDATE driver_notifications SET name_match = $2 WHERE event_id = $1",
        event_id, name_match,
    )


async def _record_gate(event_id: int, coaching_state: str | None,
                       skip_reason: str | None) -> None:
    await db.execute(
        "UPDATE driver_notifications SET coaching_state = $2, skip_reason = $3 WHERE event_id = $1",
        event_id, (coaching_state or None) and str(coaching_state)[:30],
        (skip_reason or None) and str(skip_reason)[:50],
    )


async def _quietly(coro, what: str) -> None:
    """Run a bookkeeping write; losing it must not lose the notification."""
    try:
        await coro
    except Exception as e:
        logger.error(f"[driver] Could not {what}: {e}")


# ── Telegram ─────────────────────────────────────────────────────────────────────

async def _group_title(bot, chat_id: int) -> str | None:
    """The group's current Telegram title, cached for an hour; None when it cannot be
    read (a failed read is not cached, so the next event tries again). A group that
    migrated to a supergroup is treated as unreadable: the send that follows handles
    the migration."""
    now = time.monotonic()
    cached = _title_cache.get(chat_id)
    if cached and now - cached[0] < _TITLE_TTL:
        return cached[1]
    try:
        chat = await bot.get_chat(chat_id)
    except MigrateToChat:
        logger.info(f"[driver] Group {chat_id} migrated to a supergroup — title not read")
        return None
    except Exception as e:
        logger.warning(f"[driver] Could not read the title of group {chat_id}: "
                       f"{type(e).__name__}: {e}")
        return None
    title = (getattr(chat, "title", None) or "").strip()
    if not title:
        return None
    _title_cache[chat_id] = (now, title)
    return title


def _is_gone(exc: Exception) -> bool:
    """The driver bot will never post in this chat again: kicked, or the chat is dead."""
    if isinstance(exc, (BotKicked, GroupDeactivated)):
        return True
    msg = str(exc).lower()
    return isinstance(exc, Unauthorized) and ("not a member" in msg or "chat was deleted" in msg)


async def _deliver(chat_id: int, send) -> tuple[str, int]:
    """Run send(chat_id) with retries. Returns (status, chat_id) — the id may have
    changed through a supergroup migration. status: "ok", "failed" (worth a fallback)
    or "stop" (this chat cannot be posted to; no fallback)."""
    for attempt in range(1, _ATTEMPTS + 1):
        try:
            await send(chat_id)
            return "ok", chat_id
        except MigrateToChat as e:
            new_id = e.migrate_to_chat_id
            logger.warning(f"[driver] Group {chat_id} migrated to {new_id} — updating and retrying")
            await _quietly(migrate_driver_group(chat_id, new_id), f"migrate driver group {chat_id}")
            chat_id = new_id
        except RetryAfter as e:
            logger.warning(f"[driver] Flood control for {chat_id}, waiting {e.timeout}s")
            await asyncio.sleep(e.timeout + 1)
        except Exception as e:
            if _is_gone(e):
                logger.warning(f"[driver] Group {chat_id} unreachable ({type(e).__name__}: {e}) — "
                               f"deactivating; re-importing the mapping turns it back on")
                await _quietly(deactivate_driver_group(chat_id), f"deactivate driver group {chat_id}")
                return "stop", chat_id
            if isinstance(e, (ChatNotFound, Unauthorized)):
                # Not found: the driver bot was probably never added to this group, which
                # adding it fixes with no other step — so the mapping is kept. A bare
                # Unauthorized is a bad token, which no chat-level action would fix.
                logger.error(f"[driver] Cannot post to {chat_id} ({type(e).__name__}: {e}) — skipping")
                return "stop", chat_id
            logger.warning(f"[driver] Send to {chat_id} failed (attempt {attempt}/{_ATTEMPTS}): "
                           f"{type(e).__name__}: {e}")
            if attempt < _ATTEMPTS:
                await asyncio.sleep(_RETRY_DELAY)
    return "failed", chat_id


async def _send_to_group(bot, chat_id: int, text: str, media: list[bytes] | None,
                         is_video: bool) -> bool:
    """Post the evidence and text to one driver group. Media first; if it cannot be
    sent, the text alone, so the driver still hears about it. True if the text arrived."""
    async def _send_text(cid):
        await bot.send_message(cid, text, parse_mode="HTML", disable_web_page_preview=True)

    if media:
        fits = len(_plain(text)) <= _CAPTION_LIMIT
        caption = text if fits else None
        ext = "mp4" if is_video else "jpg"

        async def _send_media(cid):
            if len(media) == 1:
                f = InputFile(io.BytesIO(media[0]), filename=f"media_1.{ext}")
                send_fn = bot.send_video if is_video else bot.send_photo
                await send_fn(cid, f, caption=caption, parse_mode="HTML" if caption else None)
                return
            kind = InputMediaVideo if is_video else InputMediaPhoto
            items = [
                kind(InputFile(io.BytesIO(b), filename=f"media_{i + 1}.{ext}"),
                     caption=caption if i == 0 else None,
                     parse_mode="HTML" if (i == 0 and caption) else None)
                for i, b in enumerate(media)
            ]
            await bot.send_media_group(cid, items)

        status, chat_id = await _deliver(chat_id, _send_media)
        if status == "stop":
            return False
        if status == "ok" and fits:
            return True
        if status == "failed":
            logger.error(f"[driver] Media failed for {chat_id}, sending text only")
    status, chat_id = await _deliver(chat_id, _send_text)
    return status == "ok"


# ── email ────────────────────────────────────────────────────────────────────────

async def _send_email(to: str, subject: str, text: str, media: list[bytes] | None,
                      is_video: bool, links: list[str], event_id: int) -> bool:
    try:
        from utils import email_sender  # lazy: written separately, may be absent
    except Exception as e:
        logger.warning(f"[driver] Email module unavailable: {e}")
        return False
    if not email_sender.is_configured():
        logger.info("[driver] Email not configured — skipping driver email")
        return False
    attachments = []
    if media:
        try:
            attachments = email_sender.pick_attachments(media, is_video) or []
        except Exception as e:
            logger.warning(f"[driver] Could not prepare attachments: {e}")
    # Links as the fallback for whatever did not fit as an attachment.
    missing = not media or len(attachments) < len(media)
    html_body, plain_body = _email_bodies(text, links if missing else [])
    return bool(await email_sender.send_email(to, subject, html_body, plain_body,
                                              attachments or None,
                                              idempotency_key=f"cell-phone-{event_id}"))


# ── Samsara coaching-state gate ──────────────────────────────────────────────────

SKIP_NOT_ALLOWED = "coaching_state_not_allowed"
SKIP_NOT_FOUND = "safety_event_not_found"
SKIP_LOOKUP_ERROR = "safety_lookup_error"
SKIP_NO_API_KEY = "no_samsara_api_key"
SKIP_NO_VEHICLE_ID = "no_samsara_vehicle_id"
SKIP_DRIVER_UNKNOWN = "driver_unknown"


def _samsara_event_time(event: dict, occurred_at: datetime | None) -> datetime:
    """When the event happened, for matching its safety event: the harsh-event
    timestamp (ms) the webhook carried, else the parsed occurrence time."""
    ms = event.get("_samsara_timestamp_ms")
    if isinstance(ms, (int, float)) and ms > 0:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    ts = occurred_at or datetime.now(timezone.utc)
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


async def _samsara_gate(event: dict, event_id: int, occurred_at: datetime | None,
                        api_key: str | None) -> tuple[str, str | None]:
    """Wait for the Samsara safety event and decide. Returns (coaching_state,
    skip_reason): skip_reason None means the driver may be notified. The state (or a
    stand-in from utils.samsara.safety) is recorded on the violation and the
    notification row, first as pending so an interrupted wait leaves a trace."""
    vehicle_id = str(event.get("_samsara_vehicle_id") or "")
    if not api_key:
        state, reason = safety.STATE_LOOKUP_ERROR, SKIP_NO_API_KEY
    elif not vehicle_id:
        state, reason = safety.STATE_LOOKUP_ERROR, SKIP_NO_VEHICLE_ID
    else:
        await _quietly(update_violation_coaching_state(event_id, safety.STATE_PENDING),
                       f"mark violation {event_id} as awaiting its coaching state")
        await _quietly(_record_gate(event_id, safety.STATE_PENDING, None),
                       f"mark notification {event_id} as awaiting its coaching state")
        result = await safety.wait_for_safety_event(api_key, vehicle_id,
                                                    _samsara_event_time(event, occurred_at))
        if result.status == "found":
            state = result.coaching_state or "unknown"
            reason = None if safety.is_allowed(state) else SKIP_NOT_ALLOWED
            logger.info(f"[driver] Event {event_id}: Samsara safety event "
                        f"{result.event.get('id')} coachingState={state} → "
                        f"{'notify' if reason is None else 'skip'}")
        elif result.status == "error":
            state, reason = safety.STATE_LOOKUP_ERROR, SKIP_LOOKUP_ERROR
        else:
            state, reason = safety.STATE_NOT_FOUND, SKIP_NOT_FOUND
    if reason in (SKIP_NO_API_KEY, SKIP_NO_VEHICLE_ID, SKIP_LOOKUP_ERROR, SKIP_NOT_FOUND):
        logger.warning(f"[driver] Event {event_id}: no Samsara coaching state ({reason}) — "
                       f"not notifying the driver")
    await _quietly(update_violation_coaching_state(event_id, state),
                   f"record the coaching state on violation {event_id}")
    await _quietly(_record_gate(event_id, state, reason),
                   f"record the coaching-state decision for notification {event_id}")
    return state, reason


# ── routing: which group, is it this driver's, can the bot post there ────────────

SKIP_PTI_UNAVAILABLE = "pti_unavailable"
SKIP_PTI_NO_GROUP = "pti_no_group"
SKIP_PTI_AMBIGUOUS = "pti_ambiguous"
SKIP_NAME_MISMATCH = "name_mismatch"
SKIP_NAME_UNVERIFIED = "name_unverified"
# Also bot_membership.REASON_*: bot_not_in_group, bot_cannot_post, group_migrated,
# bot_check_failed.

_PTI_SKIPS = {
    pti_groups.REASON_NO_GROUP: SKIP_PTI_NO_GROUP,
    pti_groups.REASON_AMBIGUOUS: SKIP_PTI_AMBIGUOUS,
    pti_groups.REASON_UNAVAILABLE: SKIP_PTI_UNAVAILABLE,
}

ROUTE_PTI = "pti"
ROUTE_IMPORT = "driver_groups"


@dataclass
class _Route:
    source: str                     # ROUTE_PTI | ROUTE_IMPORT
    group_id: int | None = None
    unit: str | None = None         # the unit as the routing source spells it
    name_match: bool | None = None
    name_source: str = "n/a"        # NAME_* once there is a group
    skip_reason: str | None = None  # a routing failure (pti only)
    detail: str = ""


async def _route(bot, company_slug: str, vehicle: str, driver_name: str,
                 event_id: int) -> _Route:
    """The truck's driver group and whether it is this driver's. Never raises."""
    if pti_groups.is_configured(company_slug):
        # pti only: a missing or doubtful answer is a skip, never a fallback to the import.
        lookup = await pti_groups.find_group(company_slug, vehicle)
        if not lookup.ok:
            detail = ", ".join(f"{g.group_id} (unit {g.unit_number})" for g in lookup.matches)
            return _Route(ROUTE_PTI, skip_reason=_PTI_SKIPS.get(lookup.reason, SKIP_PTI_UNAVAILABLE),
                          detail=f"matches {detail}" if detail else "")
        g = lookup.group
        name_match, source = name_check(driver_name, g.title, g.driver_names)
        if not name_match:
            logger.debug(f"[driver] Event {event_id}: driver {driver_name!r}, pti group "
                         f"{g.group_id} title {g.title!r}, drivers {list(g.driver_names)!r}")
        return _Route(ROUTE_PTI, g.group_id, g.unit_number, name_match, source)

    try:
        group_id = await get_driver_group(company_slug, vehicle)
    except Exception as e:
        logger.error(f"[driver] Could not look up the driver group for {company_slug}/{vehicle}: {e}")
        group_id = None
    if group_id is None:
        return _Route(ROUTE_IMPORT)
    title = await _group_title(bot, group_id)
    if title is None:
        logger.warning(f"[driver] Event {event_id} {company_slug}/{vehicle}: title of "
                       f"group {group_id} unreadable — name match unknown")
    name_match, source = name_check(driver_name, title)
    if name_match is False:
        logger.debug(f"[driver] Event {event_id}: driver {driver_name!r}, group title {title!r}")
    return _Route(ROUTE_IMPORT, group_id, vehicle, name_match, source)


def _decision_line(*, dry: bool, event_id: int, company_slug: str, vehicle: str, who: str,
                   route: _Route, membership, ordinal: int, charged: bool, state_note: str,
                   decision: str) -> str:
    """One line per event with everything needed to audit where it went and why."""
    if membership is None:
        bot_state = "n/a"
    else:
        bot_state = membership.status + (f" ({membership.detail})" if membership.reason
                                          and membership.detail else "")
    detail = f" [{route.detail}]" if route.detail else ""
    group = route.group_id if route.group_id is not None else "none"
    return (f"[driver] {'DRY RUN ' if dry else ''}Event {event_id} {company_slug}/{vehicle}: "
            f"route={route.source} group={group} unit={route.unit or '-'}{detail} "
            f"driver={who!r} name_match={route.name_match} ({route.name_source}) "
            f"bot={bot_state} #{ordinal} today, charged={charged}{state_note} → {decision}")


# ── entry point ──────────────────────────────────────────────────────────────────

async def notify_driver(*, company_slug: str, event: dict, event_id: int | None, vehicle: str,
                        occurred_at: datetime | None, base_text: str,
                        media: list[bytes] | None, is_video: bool,
                        video_urls: list[str] | None, image_urls: list[str] | None,
                        samsara_api_key: str | None = None,
                        clip_wait_over: bool = False) -> None:
    """Notify the driver of a cell_phone event: driver group (via the driver bot) and
    email, and record the driver on the violation. A Samsara event first waits on its
    coaching state (see _samsara_gate) — minutes, so callers run it in the background.
    clip_wait_over: the caller has waited for the clip and none came — send text-only
    instead of deferring to a later delivery (see _clip_still_coming). Never raises."""
    try:
        bot = _get_driver_bot()
        if bot is None:
            return
        video_urls, image_urls = list(video_urls or []), list(image_urls or [])
        if not company_enabled(company_slug):
            # driver_service drops these already; this keeps any other caller in line.
            logger.debug(f"[driver] Event {event_id}: {company_slug} not in "
                         f"DRIVER_ALERTS_COMPANIES — ignored")
            return
        if event_id is None:
            logger.warning(f"[driver] Event without an id for {company_slug}/{vehicle} — "
                           f"cannot be made idempotent, not notifying the driver")
            return
        if not clip_wait_over and _clip_still_coming(event, video_urls, image_urls):
            logger.info(f"[driver] Event {event_id}: clip not ready yet — notifying the driver "
                        f"on the delivery that carries it")
            return

        await ensure_driver_schema()
        if not await _claim(event_id, company_slug, vehicle):
            logger.info(f"[driver] Event {event_id} already notified — skipping redelivery")
            return

        from utils.driver_lookup import DriverInfo, resolve_driver
        try:
            driver = await resolve_driver(company_slug, event, vehicle, occurred_at)
        except Exception as e:
            logger.error(f"[driver] Driver lookup failed for {company_slug}/{vehicle}: {e}", exc_info=True)
            driver = DriverInfo()

        await _quietly(update_violation_driver(event_id, driver.ref, driver.name or None,
                                               driver.source, driver.on_duty),
                       f"record the driver on violation {event_id}")
        who = driver.name or driver.ref or "unknown driver"

        coaching_state = None
        if event.get("_source") == "samsara":
            coaching_state, skip_reason = await _samsara_gate(event, event_id, occurred_at,
                                                              samsara_api_key)
            if skip_reason is not None:
                dry = "DRY RUN " if config.DRIVER_ALERTS_DRY_RUN else ""
                logger.info(f"[driver] {dry}Event {event_id} {company_slug}/{vehicle} ({who}): "
                            f"skipped — coachingState={coaching_state} ({skip_reason})")
                return

        if driver.source != "vitality" or not (driver.name or "").strip():
            dry = "DRY RUN " if config.DRIVER_ALERTS_DRY_RUN else ""
            logger.info(f"[driver] {dry}Event {event_id} {company_slug}/{vehicle} ({who}): "
                        f"skipped — driver not identified by Vitality (source={driver.source}) "
                        f"({SKIP_DRIVER_UNKNOWN})")
            await _quietly(_record_gate(event_id, coaching_state, SKIP_DRIVER_UNKNOWN),
                           f"record the driver-unknown decision for notification {event_id}")
            return

        day_start = day_start_et(occurred_at)
        before = occurred_at or datetime.now(timezone.utc)
        if before.tzinfo is None:
            before = before.replace(tzinfo=timezone.utc)
        # Count from the go-live floor when it falls inside the day; the label stays the day's.
        count_from = parse_count_from(config.DRIVER_ALERTS_COUNT_FROM)
        window_start = max(day_start, count_from) if count_from else day_start
        try:
            # Keyed on the driver's name first (stable across the sources that can
            # identify them), then ref, then the truck — see violations._DRIVER_KEY_CLAUSES.
            prior = await count_driver_cell_phone_events(company_slug, driver.name, driver.ref,
                                                         vehicle, window_start, before, event_id)
        except Exception as e:
            # Unknown count: read as the first. A missed warning can be corrected by
            # the office; a charge warning sent in error cannot be unsent.
            logger.error(f"[driver] Could not count prior events for {company_slug}/{vehicle}: {e}")
            prior = 0
        ordinal = prior + 1
        await _quietly(_record_driver(event_id, driver.ref, ordinal),
                       f"record notification {event_id}")

        text = build_driver_message(base_text, ordinal, day_label(day_start))
        charged = ordinal >= 2
        subject = (EMAIL_SUBJECT_CHARGED if charged else EMAIL_SUBJECT).format(vehicle=vehicle)
        links = video_urls or image_urls
        state_note = f", coachingState={coaching_state}" if coaching_state else ""

        route = await _route(bot, company_slug, vehicle, driver.name, event_id)
        group_id, name_match = route.group_id, route.name_match

        # Checked even when the name already decided, so the decision line shows it.
        membership = None
        if group_id is not None:
            membership = await bot_membership.check(bot, group_id)
            if (membership.reason == bot_membership.REASON_MIGRATED and route.source == ROUTE_IMPORT
                    and membership.migrate_to):
                # The imported mapping follows the migration for the next event. (pti's
                # own bot updates its database; until then that group is skipped.)
                await _quietly(migrate_driver_group(group_id, membership.migrate_to),
                               f"migrate driver group {group_id}")

        skip_reason = route.skip_reason
        if skip_reason is None and group_id is not None:
            if name_match is not True and _skip_on_name_mismatch():
                skip_reason = SKIP_NAME_MISMATCH if name_match is False else SKIP_NAME_UNVERIFIED
            elif not membership.can_post:
                skip_reason = membership.reason

        dry = config.DRIVER_ALERTS_DRY_RUN
        joined = False
        if skip_reason == bot_membership.REASON_NOT_IN_GROUP:
            # The userbot may add the bot (utils/bot_join.py; logs only in DRY RUN). This
            # event is already claimed, so it is sent right here once the bot is in.
            joined = await bot_membership.request_bot_join(
                bot, company_slug, route.unit, group_id, membership,
                driver_name=driver.name, dry_run=dry)
            if joined:
                membership = await bot_membership.check(bot, group_id)
                skip_reason = membership.reason

        if skip_reason is not None:
            decision = f"skipped ({skip_reason})"
        elif group_id is None:
            decision = "no driver group — email only"
        elif name_match is not True:
            decision = "name mismatch — " + ("would send anyway" if dry else "sending anyway")
        else:
            decision = "would send" if dry else "sending"
        if joined and skip_reason is None:
            decision += " (driver bot just added by the userbot)"
        line = _decision_line(dry=dry, event_id=event_id, company_slug=company_slug,
                              vehicle=vehicle, who=who, route=route, membership=membership,
                              ordinal=ordinal, charged=charged, state_note=state_note,
                              decision=decision)

        if skip_reason is not None:
            logger.info(line)
            await _quietly(_record_gate(event_id, coaching_state, skip_reason),
                           f"record the {skip_reason} decision for notification {event_id}")
            await _quietly(_record_name_match(event_id, name_match),
                           f"record the name match of notification {event_id}")
            return

        if dry:
            logger.info(f"{line} (email={'yes' if driver.email else 'no'}, "
                        f"media={len(media or [])}) — text:\n{_plain(text)}")
            return
        logger.info(line)

        sent_group = False
        if group_id is None:
            logger.info(f"[driver] No driver group mapped for {company_slug}/{vehicle} — "
                        f"skipping Telegram for event {event_id}")
        else:
            try:
                sent_group = await _send_to_group(bot, group_id, text, media, is_video)
            except Exception as e:
                logger.error(f"[driver] Driver group send failed for event {event_id}: {e}", exc_info=True)
            if not sent_group:
                bot_membership.invalidate(group_id)   # re-check before the next send

        sent_email = False
        if driver.email:
            try:
                sent_email = await _send_email(driver.email, subject, text, media, is_video,
                                               links, event_id)
            except Exception as e:
                logger.error(f"[driver] Driver email failed for event {event_id}: {e}", exc_info=True)
        else:
            logger.info(f"[driver] No email for {who} ({company_slug}/{vehicle}) — skipping email")

        await _quietly(_record_sent(event_id, sent_group, sent_email, name_match),
                       f"record delivery of notification {event_id}")
        logger.info(f"[driver] Event {event_id} {company_slug}/{vehicle} ({who}): #{ordinal} "
                    f"today, charged={charged}{state_note}, group={sent_group}, "
                    f"name_match={name_match}, email={sent_email}")
    except Exception as e:
        logger.error(f"[driver] notify_driver failed for event {event_id}: {e}", exc_info=True)
