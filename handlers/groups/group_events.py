import logging
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from loader import dp
from utils.db_api.companies import get_company_slug_by_group, get_company_name, get_group_event_types
from utils.db_api.violations import get_violations_by_type, get_top_violators
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
    slug = await get_company_slug_by_group(message.chat.id)
    if not slug:
        await message.reply("No company configured for this group.")
        return

    now_et = datetime.now(tz=ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    rows = await get_violations_by_type(slug, since=yesterday_start, until=today_start)
    company_name = await get_company_name(slug) or slug
    date_str = yesterday_start.strftime("%b %d, %Y")
    text = _report_text(company_name, rows, date_str)
    await message.reply(text, parse_mode="HTML", reply_markup=_report_keyboard("yesterday"))


@dp.callback_query_handler(lambda c: c.data.startswith("grp_report:"))
async def cb_report_toggle(call: types.CallbackQuery):
    period = call.data.split(":")[1]
    slug = await get_company_slug_by_group(call.message.chat.id)
    if not slug:
        await call.answer("No company configured for this group.", show_alert=True)
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

    rows = await get_violations_by_type(slug, since=since, until=until)
    company_name = await get_company_name(slug) or slug
    text = _report_text(company_name, rows, date_str)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=_report_keyboard(period))
    await call.answer()


@dp.message_handler(commands=["top"], chat_type=[types.ChatType.GROUP, types.ChatType.SUPERGROUP])
async def cmd_top(message: types.Message):
    slug = await get_company_slug_by_group(message.chat.id)
    if not slug:
        await message.reply("No company configured for this group.")
        return

    args = message.get_args()
    try:
        limit = max(1, min(int(args), 50)) if args else 10
    except ValueError:
        limit = 10

    now_et = datetime.now(tz=ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await get_top_violators(slug, since=today_start, until=now_et, limit=limit)
    company_name = await get_company_name(slug) or slug
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


@dp.my_chat_member_handler()
async def on_bot_chat_member_update(update: types.ChatMemberUpdated):
    old = update.old_chat_member.status
    new = update.new_chat_member.status
    chat = update.chat
    if new in ("member", "administrator") and old in ("left", "kicked"):
        logger.info(f"Bot added to {chat.type} '{chat.title}' (id={chat.id})")
    elif new in ("left", "kicked") and old in ("member", "administrator"):
        logger.info(f"Bot removed from {chat.type} '{chat.title}' (id={chat.id})")
