import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import types
from aiogram.utils.exceptions import MessageCantBeEdited, MessageNotModified

from loader import dp
from utils.db_api.admins import is_admin, is_super_admin
from utils.db_api.companies import get_accessible_companies
from utils.db_api.violations import get_top_violators, get_vehicle_events
from utils.db_api.companies import get_company_name as _get_company_name
from utils.webhook_handler import EVENT_TYPE_MAP
from keyboards.inline.violations import (
    companies_keyboard,
    event_type_keyboard,
    top10_keyboard,
    PERIOD_LABELS,
)
ET = ZoneInfo("America/New_York")


def _period_range(period: str) -> tuple[datetime, datetime]:
    now = datetime.now(tz=ET)
    if period == "today":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
        until = now
    elif period == "last_week":
        start_of_this_week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        since = start_of_this_week - timedelta(weeks=1)
        until = start_of_this_week
    else:  # last_month
        first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        since = (first_of_this_month - timedelta(days=1)).replace(day=1)
        until = first_of_this_month
    return since, until


def _format_top10_text(rows: list[dict], period_label: str, company_name: str, event_type: str) -> str:
    if event_type == "speeding":
        header = (
            f"📊 <b>{company_name} — {period_label}</b>\n"
            f"<i>🚨 Speeding only</i>\n"
            f"<i>ℹ️ Download report shows days with 3+ speeding events per unit</i>\n"
        )
    elif event_type == "other":
        header = f"📊 <b>{company_name} — {period_label}</b>\n<i>⚠️ Other violations (excl. speeding)</i>\n"
    else:
        header = f"📊 <b>{company_name} — {period_label}</b>\n"

    if not rows:
        return header + "\n✅ No violations found."

    lines = [header]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. 🚛 Unit {row['vehicle_number']} — {row['total']}")
    return "\n".join(lines)


async def _edit_or_send(call: types.CallbackQuery, text: str, reply_markup, parse_mode="HTML"):
    """Try to edit the existing message; if too old, send a new one; if unchanged, do nothing."""
    try:
        await call.message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except MessageNotModified:
        pass
    except MessageCantBeEdited:
        await call.message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)


async def _show_top10(call: types.CallbackQuery, company_slug: str, period: str, event_type: str):
    company_name = await _get_company_name(company_slug) or company_slug
    since, until = _period_range(period)
    rows = await get_top_violators(company_slug, since, until=until, event_type=event_type, limit=10)
    text = _format_top10_text(rows, PERIOD_LABELS[period], company_name, event_type)
    kb = top10_keyboard(rows, company_slug, period, event_type)
    await _edit_or_send(call, text, kb)
    await call.answer()


# Entry point — called from start menu
async def show_violations_menu(message: types.Message):
    if not await is_admin(message.from_user.id):
        await message.answer("⛔ Access denied.")
        return
    companies = await get_accessible_companies(message.from_user.id)
    if not companies:
        await message.answer("No companies configured.")
        return
    await message.answer("Select a company:", reply_markup=companies_keyboard(companies))


# Back: to company list
@dp.callback_query_handler(lambda c: c.data == "viol_bk_co")
async def cb_back_companies(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    companies = await get_accessible_companies(call.from_user.id)
    await _edit_or_send(call, "Select a company:", companies_keyboard(companies), parse_mode=None)
    await call.answer()


# Back: to event type selection
@dp.callback_query_handler(lambda c: c.data.startswith("viol_bk_et:"))
async def cb_back_event_type(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    company_slug = call.data.split(":")[1]
    await _edit_or_send(call, "Select event type:", event_type_keyboard(company_slug), parse_mode=None)
    await call.answer()


# Company selected → event type
@dp.callback_query_handler(lambda c: c.data.startswith("viol_company:"))
async def cb_company(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    company_slug = call.data.split(":")[1]
    companies = await get_accessible_companies(call.from_user.id)
    if not any(c["slug"] == company_slug for c in companies):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    await _edit_or_send(call, "Select event type:", event_type_keyboard(company_slug), parse_mode=None)
    await call.answer()


# Event type selected → show top10, default last_week
@dp.callback_query_handler(lambda c: c.data.startswith("viol_etype:"))
async def cb_event_type(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    _, company_slug, event_type = call.data.split(":")
    await _show_top10(call, company_slug, "last_week", event_type)


# Period toggle
@dp.callback_query_handler(lambda c: c.data.startswith("viol_toggle:"))
async def cb_period_toggle(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    _, company_slug, event_type, period = call.data.split(":")
    await _show_top10(call, company_slug, period, event_type)


# Download full report
@dp.callback_query_handler(lambda c: c.data.startswith("viol_dl:"))
async def cb_download(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    parts = call.data.split(":")
    company_slug = parts[1]
    period = parts[2]
    event_type = parts[3]

    await call.answer("Generating report...")
    super_admin = await is_super_admin(call.from_user.id)

    company_name = await _get_company_name(company_slug) or company_slug
    since, until = _period_range(period)
    rows = await get_top_violators(company_slug, since, until=until, event_type=event_type, limit=50)

    now = datetime.now(tz=ET)
    date_range = f"{since.strftime('%b %d')} — {until.strftime('%b %d, %Y')}"
    lines = [
        f"{company_name} — {PERIOD_LABELS[period]} ({date_range})",
        f"Generated: {now.strftime('%b %d, %Y %I:%M %p ET')}",
        "",
    ]
    if event_type == "speeding":
        lines.append("Filter: Speeding only")
        lines.append("Note: Only days with 3+ speeding events per unit are listed.")
        lines.append("")
    elif event_type == "other":
        lines.append("Filter: Other violations (excl. speeding)")
        lines.append("")

    rank = 1
    for row in rows:
        unit = row["vehicle_number"]
        events = await get_vehicle_events(company_slug, unit, since, until=until, event_type=event_type)

        if event_type == "speeding":
            by_date: dict[str, list[str]] = {}
            for e in events:
                local = e["occurred_at"].astimezone(ET)
                day = local.strftime("%b %d")
                time_str = local.strftime("%I:%M %p").lstrip("0")
                if super_admin and e.get("severity"):
                    time_str += f" ({e['severity'].title()})"
                by_date.setdefault(day, []).append(time_str)
            day_lines = []
            for day, times in by_date.items():
                if len(times) >= 3:
                    day_lines.append(f"     • {day}, {', '.join(times)} — {len(times)} times")
            if not day_lines:
                continue
            total_shown = sum(len(by_date[d]) for d in by_date if len(by_date[d]) >= 3)
            lines.append(f"{rank}. Unit {unit} — {total_shown} speeding events")
            lines.extend(day_lines)
        else:
            total = row["total"]
            lines.append(f"{rank}. Unit {unit} — {total} event{'s' if total != 1 else ''}")
            for e in events:
                local = e["occurred_at"].astimezone(ET)
                ts = local.strftime("%b %d, %I:%M %p ET")
                if event_type in (None, "other") and e.get("event_type"):
                    _, title = EVENT_TYPE_MAP.get(e["event_type"], ("🚨", e["event_type"].upper()))
                    lines.append(f"     • {ts} — {title}")
                else:
                    lines.append(f"     • {ts}")

        lines.append("")
        rank += 1

    content = "\n".join(lines).encode("utf-8")
    filename = f"{company_slug}_{period}_report.txt"
    await call.message.answer_document(
        types.InputFile(io.BytesIO(content), filename=filename),
        caption=f"📊 {company_name} — {PERIOD_LABELS[period]}"
    )
