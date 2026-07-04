import logging
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from loader import dp, bot
from data import config
from utils.db_api.groups import group_exists, get_group_event_types, register_group
from utils.db_api.admins import get_all_admins
from utils.db_api.violations import get_violations_by_type, get_top_violators
from utils.group_parser import extract_vehicle_number
from utils.webhook_handler import EVENT_TYPE_MAP

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


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


@dp.my_chat_member_handler()
async def on_bot_chat_member_update(update: types.ChatMemberUpdated):
    old = update.old_chat_member.status
    new = update.new_chat_member.status
    chat = update.chat

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

        if vehicle is None and not is_main:
            logger.warning(f"No unit number for group '{title}' (id={chat.id}) — not registering")
            await _notify_admins_parse_failure(chat, title, description)
            return

        # Main group registers with a NULL vehicle (receives all units); driver groups
        # register with their parsed unit number.
        await register_group(chat.id, title, None if is_main else vehicle)
        if is_main:
            logger.info(f"Registered MAIN group (id={chat.id})")
        else:
            logger.info(f"Registered group id={chat.id} → unit {vehicle}")

    elif removed:
        logger.info(f"Bot removed from {chat.type} '{chat.title}' (id={chat.id})")
