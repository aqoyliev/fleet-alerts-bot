import io
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import types
from aiogram.utils.exceptions import MessageCantBeEdited, MessageNotModified

from loader import dp
from data import config
from utils.db_api.admins import is_admin, is_super_admin
from utils.db_api.violations import get_top_violators, get_vehicle_events
from utils.tg_text import esc
from utils.webhook_handler import EVENT_TYPE_MAP
from keyboards.inline.violations import (
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


def _date_range_label(since: datetime, until: datetime) -> str:
    # until is exclusive (start of the next period), so the last covered day is until - 1
    end = until - timedelta(days=1)
    if since.year == end.year:
        return f"{since.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"
    return f"{since.strftime('%b %d, %Y')} – {end.strftime('%b %d, %Y')}"


def _format_top10_text(rows: list[dict], date_label: str, company_name: str, event_type: str) -> str:
    if event_type == "speeding":
        header = (
            f"📊 <b>{esc(company_name)} — {date_label}</b>\n"
            f"<i>🚨 Speeding only</i>\n"
        )
    elif event_type == "other":
        header = f"📊 <b>{esc(company_name)} — {date_label}</b>\n<i>⚠️ Other violations (excl. speeding)</i>\n"
    else:
        header = f"📊 <b>{esc(company_name)} — {date_label}</b>\n"

    if not rows:
        return header + "\n✅ No violations found."

    lines = [header]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. 🚛 Unit {esc(row['vehicle_number'])} — {row['total']}")
    return "\n".join(lines)


async def _edit_or_send(call: types.CallbackQuery, text: str, reply_markup, parse_mode="HTML"):
    """Try to edit the existing message; if too old, send a new one; if unchanged, do nothing."""
    try:
        await call.message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except MessageNotModified:
        pass
    except MessageCantBeEdited:
        await call.message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)


async def _show_top10(call: types.CallbackQuery, period: str, event_type: str):
    since, until = _period_range(period)
    rows = await get_top_violators(since, until=until, event_type=event_type, limit=10)
    text = _format_top10_text(rows, _date_range_label(since, until), config.COMPANY_NAME, event_type)
    kb = top10_keyboard(rows, period, event_type)
    await _edit_or_send(call, text, kb)
    await call.answer()


# Entry point — called from start menu
async def show_violations_menu(message: types.Message):
    if not await is_admin(message.from_user.id):
        await message.answer("⛔ Access denied.")
        return
    await message.answer("Select event type:", reply_markup=event_type_keyboard())


# The callbacks below read their arguments from the END of the callback data. The build
# this replaced put the company slug first ("viol_etype:hf:speeding"), and those
# keyboards are still sitting in admins' chats: read from the right, both forms land on
# the same values, and a button pressed on last week's message does what it says.

# Back: to event type selection
@dp.callback_query_handler(lambda c: c.data == "viol_bk_et" or c.data.startswith("viol_bk_et:"))
async def cb_back_event_type(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    await _edit_or_send(call, "Select event type:", event_type_keyboard(), parse_mode=None)
    await call.answer()


# Event type selected → show top10, default last_week
@dp.callback_query_handler(lambda c: c.data.startswith("viol_etype:"))
async def cb_event_type(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    event_type = call.data.split(":")[-1]
    await _show_top10(call, "last_week", event_type)


# Period toggle
@dp.callback_query_handler(lambda c: c.data.startswith("viol_toggle:"))
async def cb_period_toggle(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    event_type, period = call.data.split(":")[-2:]
    if period not in PERIOD_LABELS:
        period = "last_week"
    await _show_top10(call, period, event_type)


# Download full report
@dp.callback_query_handler(lambda c: c.data.startswith("viol_dl:"))
async def cb_download(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    period, event_type = call.data.split(":")[-2:]
    if period not in PERIOD_LABELS:
        period = "last_week"

    await call.answer("Generating report...")
    super_admin = await is_super_admin(call.from_user.id)

    company_name = config.COMPANY_NAME
    since, until = _period_range(period)
    rows = await get_top_violators(since, until=until, event_type=event_type, limit=50)

    now = datetime.now(tz=ET)
    date_range = f"{since.strftime('%b %d')} — {until.strftime('%b %d, %Y')}"
    lines = [
        f"{company_name} — {PERIOD_LABELS[period]} ({date_range})",
        f"Generated: {now.strftime('%b %d, %Y %I:%M %p ET')}",
        "",
    ]
    if event_type == "speeding":
        lines.append("Filter: Speeding only")
        lines.append("")
    elif event_type == "other":
        lines.append("Filter: Other violations (excl. speeding)")
        lines.append("")

    rank = 1
    for row in rows:
        unit = row["vehicle_number"]
        events = await get_vehicle_events(unit, since, until=until, event_type=event_type)

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
                day_lines.append(f"     • {day}, {', '.join(times)} — {len(times)} time{'s' if len(times) != 1 else ''}")
            if not day_lines:
                continue
            total_shown = sum(len(v) for v in by_date.values())
            lines.append(f"{rank}. Unit {unit} — {total_shown} speeding event{'s' if total_shown != 1 else ''}")
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
    filename = f"{config.COMPANY_SLUG}_{period}_report.txt"
    await call.message.answer_document(
        types.InputFile(io.BytesIO(content), filename=filename),
        caption=f"📊 {company_name} — {PERIOD_LABELS[period]}"
    )
