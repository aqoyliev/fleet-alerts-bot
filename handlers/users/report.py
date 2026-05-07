from datetime import datetime, timedelta

from aiogram import types
from aiogram.dispatcher.filters import Command

from keyboards.inline.violations import report_company_picker_keyboard
from loader import dp
from utils.daily_report import ET, _format_daily_report
from utils.db_api.admins import is_admin
from utils.db_api.companies import get_accessible_companies, get_company_by_group_id
from utils.db_api.violations import get_top_violators, get_violations_by_type


def _report_date_range(period: str) -> tuple[datetime, datetime, str]:
    now_et = datetime.now(tz=ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return today_start, now_et, today_start.strftime("%b %d, %Y") + " (so far)"
    since = today_start - timedelta(days=1)
    return since, today_start, since.strftime("%b %d, %Y")


def _format_top_report(company_name: str, rows: list[dict], date_str: str, limit: int) -> str:
    header = f"🏆 <b>Top {limit} Violators</b>\n<b>{company_name}</b> — {date_str}"
    if not rows:
        return header + "\n\n✅ No violations."
    lines = [header, ""]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. 🚛 Unit {row['vehicle_number']} — {row['total']}")
    return "\n".join(lines)


async def _send_report(target: types.Message, company_slug: str, company_name: str, period: str):
    since, until, date_str = _report_date_range(period)
    rows = await get_violations_by_type(company_slug, since, until)
    await target.answer(_format_daily_report(company_name, rows, date_str), parse_mode="HTML")


async def _send_top(target: types.Message, company_slug: str, company_name: str, limit: int):
    since, until, date_str = _report_date_range("today")
    rows = await get_top_violators(company_slug, since, until=until, limit=limit)
    await target.answer(_format_top_report(company_name, rows, date_str, limit), parse_mode="HTML")


@dp.message_handler(Command("report"))
async def cmd_report(message: types.Message):
    period = message.get_args().strip().lower()
    if period not in ("today", "yesterday"):
        await message.answer("Usage: /report today  or  /report yesterday")
        return

    if message.chat.type in ("group", "supergroup"):
        co = await get_company_by_group_id(message.chat.id)
        if not co:
            return
        await _send_report(message, co["slug"], co["name"], period)
    else:
        if not await is_admin(message.from_user.id):
            return
        companies = await get_accessible_companies(message.from_user.id)
        if not companies:
            return
        if len(companies) == 1:
            await _send_report(message, companies[0]["slug"], companies[0]["name"], period)
        else:
            await message.answer("Select a company:", reply_markup=report_company_picker_keyboard(companies, f"rpt:{period}"))


@dp.message_handler(Command("top"))
async def cmd_top(message: types.Message):
    try:
        limit = max(1, min(int(message.get_args().strip()), 50))
    except (ValueError, TypeError):
        limit = 10

    if message.chat.type in ("group", "supergroup"):
        co = await get_company_by_group_id(message.chat.id)
        if not co:
            return
        await _send_top(message, co["slug"], co["name"], limit)
    else:
        if not await is_admin(message.from_user.id):
            return
        companies = await get_accessible_companies(message.from_user.id)
        if not companies:
            return
        if len(companies) == 1:
            await _send_top(message, companies[0]["slug"], companies[0]["name"], limit)
        else:
            await message.answer("Select a company:", reply_markup=report_company_picker_keyboard(companies, f"top:{limit}"))


@dp.callback_query_handler(lambda c: c.data.startswith("rpt_pick:"))
async def cb_report_pick(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    # callback_data: "rpt_pick:{cmd}:{arg}:{slug}"
    _, cmd, arg, slug = call.data.split(":", 3)
    companies = await get_accessible_companies(call.from_user.id)
    co = next((c for c in companies if c["slug"] == slug), None)
    if not co:
        await call.answer("Not found.", show_alert=True)
        return
    try:
        await call.message.delete()
    except Exception:
        pass
    if cmd == "rpt":
        await _send_report(call.message, slug, co["name"], arg)
    else:
        await _send_top(call.message, slug, co["name"], int(arg))
    await call.answer()
