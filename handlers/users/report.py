"""/report and /top — the two report commands, in the alerts group and in an admin's DM.

In the group they answer anyone, because the group is the audience those numbers are
for. In a DM they answer admins only. Any other group gets silence: the bot may have
been added to a chat it does not post to, and that chat has no business reading this
fleet's violations.
"""
from datetime import datetime, timedelta

from aiogram import types
from aiogram.dispatcher.filters import Command

from data import config
from loader import dp
from utils.daily_report import ET, _format_daily_report
from utils.db_api.admins import is_admin
from utils.db_api.groups import is_the_group
from utils.db_api.violations import get_top_violators, get_violations_by_type
from utils.tg_text import esc

GROUP_TYPES = (types.ChatType.GROUP, types.ChatType.SUPERGROUP)


def _report_date_range(period: str) -> tuple[datetime, datetime, str]:
    now_et = datetime.now(tz=ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return today_start, now_et, today_start.strftime("%b %d, %Y") + " (so far)"
    since = today_start - timedelta(days=1)
    return since, today_start, since.strftime("%b %d, %Y")


def _format_top_report(company_name: str, rows: list[dict], date_str: str, limit: int) -> str:
    header = f"🏆 <b>Top {limit} Violators</b>\n<b>{esc(company_name)}</b> — {date_str}"
    if not rows:
        return header + "\n\n✅ No violations."
    lines = [header, ""]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. 🚛 Unit {esc(row['vehicle_number'])} — {row['total']}")
    return "\n".join(lines)


async def _may_ask(message: types.Message) -> bool:
    """Is this chat one that gets an answer? The alerts group, or an admin's DM."""
    if message.chat.type in GROUP_TYPES:
        return await is_the_group(message.chat.id)
    return await is_admin(message.from_user.id)


@dp.message_handler(Command("report"))
async def cmd_report(message: types.Message):
    if not await _may_ask(message):
        return
    period = message.get_args().strip().lower()
    if period not in ("today", "yesterday"):
        await message.answer("Usage: /report today  or  /report yesterday")
        return

    since, until, date_str = _report_date_range(period)
    rows = await get_violations_by_type(since, until)
    await message.answer(_format_daily_report(config.COMPANY_NAME, rows, date_str),
                         parse_mode="HTML")


@dp.message_handler(Command("top"))
async def cmd_top(message: types.Message):
    if not await _may_ask(message):
        return
    try:
        limit = max(1, min(int(message.get_args().strip()), 50))
    except (ValueError, TypeError):
        limit = 10

    since, until, date_str = _report_date_range("today")
    rows = await get_top_violators(since, until=until, limit=limit)
    await message.answer(_format_top_report(config.COMPANY_NAME, rows, date_str, limit),
                         parse_mode="HTML")
