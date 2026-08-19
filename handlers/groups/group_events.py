import logging
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.exceptions import MessageNotModified

from loader import dp, bot
from data import config
from utils.db_api.groups import (
    group_exists, get_group_event_types, register_group, get_group,
    set_group_enabled, remove_group, set_group_event_types, toggle_group_event_type,
)
from utils.db_api.admins import get_all_admins, is_admin
from utils.db_api.violations import get_violations_by_type, get_top_violators
from utils.group_parser import extract_vehicle_number
from utils import group_texts
from utils.samsara.client import lookup_unit, suggest_units, SamsaraUnavailable
from utils.webhook_handler import EVENT_TYPE_MAP
from keyboards.inline.group_settings import group_events_keyboard

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
GROUP_TYPES = [types.ChatType.GROUP, types.ChatType.SUPERGROUP]


def _report_text(company_name: str, rows: list[dict], date_str: str) -> str:
    header = f"📊 <b>Daily Violations Report</b>\n<b>{company_name}</b> — {date_str}\n"
    if not rows:
        return header + "\n✅ No violations today."

    by_type: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_type[row["event_type"]].append(row)

    def _sort_key(et):
        if et == "speeding":
            return (0, 0)
        return (1, -sum(v["total"] for v in by_type[et]))

    lines = [header]
    for event_type in sorted(by_type, key=_sort_key):
        emoji, title = EVENT_TYPE_MAP.get(event_type, ("⚠️", event_type.replace("_", " ").title()))
        lines.append(f"\n{emoji} <b>{title}</b>")
        for v in by_type[event_type]:
            lines.append(f"  🚛 {v['vehicle_number']} — {v['total']}")
    return "\n".join(lines)


def _report_keyboard(period: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📅 Today" if period != "today" else "✅ Today", callback_data="grp_report:today"),
        InlineKeyboardButton("📅 Yesterday" if period != "yesterday" else "✅ Yesterday", callback_data="grp_report:yesterday"),
    )
    return kb


@dp.message_handler(commands=["report"], chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP])
async def cmd_report(message: types.Message):
    if not await group_exists(message.chat.id):
        await message.reply("This group isn't configured to receive alerts.")
        return

    now_et = datetime.now(tz=ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    rows = await get_violations_by_type(since=yesterday_start, until=today_start)
    date_str = yesterday_start.strftime("%b %d, %Y")
    text = _report_text(config.COMPANY_NAME, rows, date_str)
    await message.reply(text, parse_mode="HTML", reply_markup=_report_keyboard("yesterday"))


@dp.callback_query_handler(lambda c: c.data.startswith("grp_report:"))
async def cb_report_toggle(call: types.CallbackQuery):
    period = call.data.split(":")[1]
    if not await group_exists(call.message.chat.id):
        await call.answer("This group isn't configured to receive alerts.", show_alert=True)
        return

    now_et = datetime.now(tz=ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        since, until = today_start, now_et
        date_str = today_start.strftime("%b %d, %Y")
    else:
        since = today_start - timedelta(days=1)
        until = today_start
        date_str = since.strftime("%b %d, %Y")

    rows = await get_violations_by_type(since=since, until=until)
    text = _report_text(config.COMPANY_NAME, rows, date_str)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=_report_keyboard(period))
    await call.answer()


@dp.message_handler(commands=["top"], chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP])
async def cmd_top(message: types.Message):
    if not await group_exists(message.chat.id):
        await message.reply("This group isn't configured to receive alerts.")
        return

    args = message.get_args()
    try:
        limit = max(1, min(int(args), 50)) if args else 10
    except ValueError:
        limit = 10

    now_et = datetime.now(tz=ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await get_top_violators(since=today_start, until=now_et, limit=limit)
    company_name = config.COMPANY_NAME
    date_str = today_start.strftime("%b %d, %Y")

    header = f"📊 <b>Top {limit} Violators</b>\n<b>{company_name}</b> — {date_str}\n"
    if not rows:
        text = header + "\n✅ No violations today."
    else:
        lines = [header]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. 🚛 {row['vehicle_number']} — {row['total']}")
        text = "\n".join(lines)

    await message.reply(text, parse_mode="HTML")


@dp.message_handler(commands=["help"], chat_type=GROUP_TYPES)
async def cmd_group_help(message: types.Message):
    """/help inside a group. Separate from the DM handler in handlers/users/help.py,
    which is admin-oriented and private-only; this one answers a driver's question in
    their own truck's chat.

    No group_exists guard on purpose — an unconfigured group is exactly when someone
    types /help, and the reply tells them how to configure it.
    """
    grp = await get_group(message.chat.id)
    is_main = config.MAIN_GROUP_ID is not None and message.chat.id == config.MAIN_GROUP_ID
    is_crash = config.CRASH_GROUP_ID is not None and message.chat.id == config.CRASH_GROUP_ID
    await message.reply(
        group_texts.help_text(
            config.COMPANY_NAME,
            unit=(grp or {}).get("vehicle_number"),
            is_main=is_main,
            is_crash=is_crash,
            is_admin=await is_admin(message.from_user.id),
        ),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


@dp.message_handler(commands=["event_list"], chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP])
async def cmd_event_list(message: types.Message):
    event_types = await get_group_event_types(message.chat.id)
    if not event_types:
        text = "📋 <b>Event Types</b>\n\n✅ All event types (no filter configured)"
    else:
        lines = ["📋 <b>Event Types</b>\n"]
        for et in event_types:
            emoji, title = EVENT_TYPE_MAP.get(et, ("⚠️", et.replace("_", " ").title()))
            lines.append(f"{emoji} {title}")
        text = "\n".join(lines)
    await message.reply(text, parse_mode="HTML")


async def _admin_ids() -> list[int]:
    """Active admin Telegram ids (DB admins ∪ bootstrap super-admins from config)."""
    ids: set[int] = set()
    try:
        ids.update(a["telegram_id"] for a in await get_all_admins() if a["is_active"])
    except Exception as e:
        logger.error(f"Could not load admins for notify: {e}")
    for a in config.ADMINS:
        try:
            ids.add(int(a))
        except (TypeError, ValueError):
            pass
    return list(ids)


async def _notify_admins_parse_failure(chat: types.Chat, title: str, description: str):
    """DM the admins that a group couldn't be auto-registered because no unit number
    was found, so they can fix its name/description and re-add the bot."""
    text = (
        "⚠️ <b>Couldn't register a group</b>\n\n"
        f"I was added to <b>{title or 'a group'}</b> "
        f"(id <code>{chat.id}</code>) but couldn't find a unit number in its name or "
        "description.\n\n"
        "Add the unit number (e.g. <code>UNIT: 1234</code> or <code>TRUCK# 1234</code>) "
        "to the group name or description, then remove and re-add me."
    )
    for admin_id in await _admin_ids():
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id} of parse failure: {e}")


async def _resolve_unit(unit: str) -> tuple[str, str]:
    """Check a unit against Samsara's vehicle roster and canonicalize it.

    Returns (status, value):
      ("ok", <name as Samsara spells it>)  — exists; store this, not what was typed
      ("missing", unit)                    — no such vehicle in the org
      ("unavailable", unit)                — there is a roster but it could not be read
      ("no_roster", unit)                  — this deployment has no Samsara at all

    Storing Samsara's own spelling is the point. Alert routing matches the stored unit
    against the vehicle name by strict equality, and this fleet names trucks "unit571"
    while its Telegram groups say "UNIT: 571" — so the two only ever meet if the
    roster's version is what goes in the database.

    That is why "unavailable" is kept apart from "no_roster". With a key configured
    there IS a canonical spelling; registering an unverified guess against it produces a
    group that looks configured and never receives anything, which is the failure this
    whole function exists to prevent — so the caller refuses and asks for a retry.
    Without a key there is no canonical spelling to disagree with (a Motive-only fleet),
    so what the dispatcher typed is all there is and registration proceeds.
    """
    if not config.SAMSARA_API_KEY:
        return "no_roster", unit
    try:
        canonical = await lookup_unit(config.SAMSARA_API_KEY, unit)
    except SamsaraUnavailable as e:
        logger.warning(f"Samsara unit check unavailable for '{unit}': {e}")
        return "unavailable", unit
    if canonical is None:
        return "missing", unit
    if canonical != unit:
        logger.info(f"Unit '{unit}' resolved to Samsara's '{canonical}'")
    return "ok", canonical


async def _say(chat_id: int, text: str):
    """Post to the group, tolerating the send failing.

    Everything this is used for is a setup instruction, and a group that won't accept the
    message (bot muted, restricted, removed again in the same second) must not take the
    registration down with it — the admin DM still reports the same problem.
    """
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML",
                               disable_web_page_preview=True)
    except Exception as e:
        logger.warning(f"Could not post setup message to {chat_id}: {e}")


async def _notify_admins_unknown_unit(chat: types.Chat, title: str, unit: str,
                                      suggestions: list[str]):
    """DM the admins that a group named a unit Samsara has never heard of — usually a
    typo in the group title, or a truck not yet added to the Samsara org."""
    hint = ("\n\nClosest units in Samsara: "
            + ", ".join(f"<code>{s}</code>" for s in suggestions)) if suggestions else ""
    text = (
        "⚠️ <b>Couldn't register a group</b>\n\n"
        f"I was added to <b>{title or 'a group'}</b> (id <code>{chat.id}</code>) and read "
        f"unit <code>{unit}</code> from its name, but no such vehicle exists in Samsara."
        f"{hint}\n\n"
        "Fix the group name, or set it directly with <code>/setunit &lt;unit&gt;</code>."
    )
    for admin_id in await _admin_ids():
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id} of unknown unit: {e}")


@dp.my_chat_member_handler()
async def on_bot_chat_member_update(update: types.ChatMemberUpdated):
    old = update.old_chat_member.status
    new = update.new_chat_member.status
    chat = update.chat

    # Groups only. This update also fires for a DM when someone blocks or unblocks the
    # bot, and every branch below treats the chat as a group that failed to register —
    # which used to DM the admins about it, and would now instruct the user to /setunit.
    if chat.type not in GROUP_TYPES:
        return

    added = new in ("member", "administrator") and old in ("left", "kicked")
    removed = new in ("left", "kicked") and old in ("member", "administrator")

    if added:
        logger.info(f"Bot added to {chat.type} '{chat.title}' (id={chat.id})")

        # The membership update carries the title but not the description — fetch the
        # full chat so we can parse both.
        description = ""
        try:
            full = await bot.get_chat(chat.id)
            description = full.description or ""
        except Exception as e:
            logger.warning(f"Could not fetch chat {chat.id} description: {e}")

        title = chat.title or ""
        vehicle = extract_vehicle_number(title, description)
        is_main = config.MAIN_GROUP_ID is not None and chat.id == config.MAIN_GROUP_ID

        # Checked before anything else, including the unit parse: the crash group's title
        # may well contain digits, and matching one of them would register it as a driver
        # group and start sending it a truck's speeding alerts.
        if config.CRASH_GROUP_ID is not None and chat.id == config.CRASH_GROUP_ID:
            # No register_group call on purpose — see the CRASH_GROUP_ID note in
            # data/config.py. Routing for this chat comes from the config, so a DB row
            # would only add ways for it to be wrong.
            logger.info(f"Bot added to CRASH group (id={chat.id}) — crash alerts only")
            await _say(chat.id, group_texts.joined_crash_group(config.COMPANY_NAME))
            return

        if vehicle is None and not is_main:
            logger.warning(f"No unit number for group '{title}' (id={chat.id}) — not registering")
            # Say it in the group as well as to the admins: the people who can rename the
            # chat or run /setunit are the ones sitting in it, and until one of them does,
            # the bot looks installed while sending nothing.
            await _say(chat.id, group_texts.joined_needs_unit())
            await _notify_admins_parse_failure(chat, title, description)
            return

        # Main group registers with a NULL vehicle (receives all units); driver groups
        # register with their parsed unit number.
        if is_main:
            await register_group(chat.id, title, None)
            logger.info(f"Registered MAIN group (id={chat.id})")
            await _say(chat.id, group_texts.joined_main_group(config.COMPANY_NAME))
            return

        # The title gives bare digits ("UNIT: 571" → "571") but Samsara may name the
        # same truck "unit571". Register the roster's spelling or the group receives
        # nothing, silently.
        status, resolved = await _resolve_unit(vehicle)
        if status == "unavailable":
            # Registering the parsed spelling unverified would very likely store
            # something the roster does not match, leaving the group silent. Say so and
            # let whoever is in the chat run /setunit once Samsara answers again.
            logger.warning(f"Group '{title}' (id={chat.id}) parsed unit {vehicle}, "
                           f"but Samsara could not be reached — not registering")
            await _say(chat.id, group_texts.joined_roster_unavailable(vehicle))
            return
        if status == "missing":
            logger.warning(f"Group '{title}' (id={chat.id}) parsed unit {vehicle}, "
                           f"which is not in Samsara — not registering")
            # One roster lookup, two audiences: the group is told how to fix it, the
            # admins are told it happened.
            suggestions = await suggest_units(config.SAMSARA_API_KEY, vehicle)
            await _say(chat.id, group_texts.joined_unknown_unit(
                config.COMPANY_NAME, vehicle, suggestions))
            await _notify_admins_unknown_unit(chat, title, vehicle, suggestions)
            return

        await register_group(chat.id, title, resolved)
        logger.info(f"Registered group id={chat.id} → unit {resolved}"
                    + (" (no Samsara roster to verify against)" if status == "no_roster" else ""))
        await _say(chat.id, group_texts.joined_registered(resolved))

    elif removed:
        logger.info(f"Bot removed from {chat.type} '{chat.title}' (id={chat.id})")


# ── Manual group management ─────────────────────────────────────────────────────────

async def _require_admin_group(message: types.Message) -> bool:
    """Guard for admin-only group commands: caller must be a bot admin and the group
    must be registered. Replies with the reason and returns False when either fails."""
    if not await is_admin(message.from_user.id):
        await message.reply("⛔ Only bot admins can do that.")
        return False
    if not await group_exists(message.chat.id):
        await message.reply("This group isn't configured. Set its unit with /setunit first.")
        return False
    return True


async def _require_registered_group(message: types.Message) -> bool:
    """Guard for the group commands anyone may use — the group only has to be
    registered. Drivers run their own group's mute switch, so the alerts they are being
    sent are theirs to silence; the admins are told about it either way."""
    if not await group_exists(message.chat.id):
        await message.reply("This group isn't configured. Set its unit with /setunit first.")
        return False
    return True


@dp.message_handler(commands=["setunit"], chat_type=GROUP_TYPES)
async def cmd_setunit(message: types.Message):
    """Manually set (or correct) this group's unit number — open to anyone in the group,
    for when the bot couldn't auto-detect it from the name/description."""
    if config.MAIN_GROUP_ID is not None and message.chat.id == config.MAIN_GROUP_ID:
        await message.reply("This is the main group — it receives every unit and can't be tied to one.")
        return

    unit = (message.get_args() or "").strip().lstrip("#").strip()
    if not unit or not any(c.isdigit() for c in unit) or len(unit) > 50:
        await message.reply("Usage: <code>/setunit 1234</code>", parse_mode="HTML")
        return

    # Verify the unit exists before registering. A typo registers a group that looks
    # configured and then silently never receives anything — much harder to notice
    # later than being told "no such unit" right now.
    typed = unit
    status, unit = await _resolve_unit(unit)

    if status == "missing":
        suggestions = await suggest_units(config.SAMSARA_API_KEY, typed)
        hint = ("\n\nDid you mean: "
                + ", ".join(f"<code>{s}</code>" for s in suggestions)) if suggestions else ""
        await message.reply(
            f"❌ No unit <code>{typed}</code> found in Samsara.{hint}",
            parse_mode="HTML",
        )
        logger.info(f"Rejected /setunit {typed} in group {message.chat.id} — not in Samsara")
        return

    if status == "unavailable":
        # The roster is the authority on how this unit is spelled, and storing a guess
        # against it registers a group that silently never receives anything.
        await message.reply(
            "⚠️ Couldn't reach Samsara to verify that unit, so it wasn't saved — "
            "the spelling has to match the roster exactly or this group would receive "
            "nothing. Please try again in a few minutes.",
        )
        logger.warning(f"Deferred /setunit {typed} in group {message.chat.id} — Samsara unreachable")
        return

    note = ""
    if status == "ok" and unit != typed:
        note = f"\n\n<i>Matched Samsara's <code>{unit}</code>.</i>"

    await register_group(message.chat.id, message.chat.title or "", unit)
    await message.reply(
        f"✅ Unit set to <code>{unit}</code>. This group will now receive its alerts." + note,
        parse_mode="HTML",
    )
    logger.info(f"Unit for group {message.chat.id} set to {unit} by {message.from_user.id}")


@dp.message_handler(commands=["disable", "mute"], chat_type=GROUP_TYPES)
async def cmd_disable(message: types.Message):
    """Mute this group's alerts. Open to anyone in the group — it only silences that
    group's own unit — and every admin is DMed who did it."""
    if not await _require_registered_group(message):
        return
    await set_group_enabled(message.chat.id, False)
    # This reply is the ONLY place /enable is advertised — it is deliberately kept out
    # of the command menu, so the way back has to be stated here.
    await message.reply(
        "🔕 Alerts <b>disabled</b> for this group.\n\n"
        "Send <b>/enable</b> here to turn them back on.",
        parse_mode="HTML",
    )

    grp = await get_group(message.chat.id)
    unit = (grp or {}).get("vehicle_number")
    label = (grp or {}).get("title") or message.chat.title or "a group"
    unit_str = f" (unit {unit})" if unit else ""
    who = message.from_user.full_name
    text = (
        "🔕 <b>Group alerts muted</b>\n\n"
        f"<b>{label}</b>{unit_str} (id <code>{message.chat.id}</code>) was muted by {who}."
    )
    for admin_id in await _admin_ids():
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id} of mute: {e}")


@dp.message_handler(commands=["enable", "unmute"], chat_type=GROUP_TYPES)
async def cmd_enable(message: types.Message):
    """Unmute this group's alerts. Open to anyone, necessarily: whoever can mute a group
    must be able to undo it, or a driver can silence their own alerts for good."""
    if not await _require_registered_group(message):
        return
    await set_group_enabled(message.chat.id, True)
    await message.reply("🔔 Alerts <b>enabled</b> for this group.", parse_mode="HTML")


@dp.message_handler(commands=["removegroup"], chat_type=GROUP_TYPES)
async def cmd_removegroup(message: types.Message):
    """Unregister this group (admin only). The bot stays in the chat but sends nothing
    until it's re-registered with /setunit."""
    if not await _require_admin_group(message):
        return
    await remove_group(message.chat.id)
    await message.reply(
        "🗑 This group has been <b>unregistered</b> and will no longer receive alerts.\n"
        "Use /setunit to register it again.",
        parse_mode="HTML",
    )
    logger.info(f"Group {message.chat.id} unregistered by {message.from_user.id}")


def _events_text() -> str:
    return (
        "🎛 <b>Event types for this group</b>\n\n"
        "Tap to toggle. ✅ = received, ⬜ = blocked.\n"
        "When everything is ✅ the group receives every event type (including new ones)."
    )


@dp.message_handler(commands=["events"], chat_type=GROUP_TYPES)
async def cmd_events(message: types.Message):
    """Admin-only control of which event types this group receives."""
    if not await _require_admin_group(message):
        return
    allowed = await get_group_event_types(message.chat.id)
    await message.reply(_events_text(), parse_mode="HTML", reply_markup=group_events_keyboard(allowed))


@dp.callback_query_handler(lambda c: c.data.startswith("grpevt:"))
async def cb_group_events(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Admins only.", show_alert=True)
        return
    if not await group_exists(call.message.chat.id):
        await call.answer("This group isn't configured.", show_alert=True)
        return

    parts = call.data.split(":")
    action = parts[1]
    if action == "all":
        await set_group_event_types(call.message.chat.id, set())
        allowed = []
    else:  # "tog"
        allowed = await toggle_group_event_type(call.message.chat.id, parts[2])

    try:
        await call.message.edit_reply_markup(reply_markup=group_events_keyboard(allowed))
    except MessageNotModified:
        pass
    await call.answer()
