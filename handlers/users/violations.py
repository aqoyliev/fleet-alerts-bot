import io

from aiogram import types
from aiogram.utils.exceptions import MessageCantBeEdited, MessageNotModified

from loader import dp
from data import config
from utils.db_api.admins import is_admin, is_super_admin
from utils.tg_text import esc
from utils.violation_report import (
    build_report_file, period_label, report_filename, top_units,
)
from keyboards.inline.violations import (
    event_type_keyboard,
    top10_keyboard,
    PERIOD_LABELS,
)

# The numbers themselves, the period windows and the downloadable file are all built by
# utils/violation_report.py, which the Mini App's Report screen calls too. What is left
# here is the chat flow: which message to edit, and what the keyboard under it says.


def _format_top10_text(rows: list[dict], label: str, company_name: str, event_type: str) -> str:
    if event_type == "speeding":
        header = (
            f"📊 <b>{esc(company_name)} — {label}</b>\n"
            f"<i>🚨 Speeding only</i>\n"
            f"<i>ℹ️ Download report shows days with 3+ speeding events per unit</i>\n"
        )
    elif event_type == "other":
        header = f"📊 <b>{esc(company_name)} — {label}</b>\n<i>⚠️ Other violations (excl. speeding)</i>\n"
    else:
        header = f"📊 <b>{esc(company_name)} — {label}</b>\n"

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
    rows = await top_units(period, event_type, limit=10)
    text = _format_top10_text(rows, PERIOD_LABELS[period], config.COMPANY_NAME, event_type)
    kb = top10_keyboard(rows, period, event_type)
    await _edit_or_send(call, text, kb)
    await call.answer()


# Entry point — called from start menu
async def show_violations_menu(message: types.Message):
    if not await is_admin(message.from_user.id):
        await message.answer("⛔ Access denied.")
        return
    await message.answer("Select event type:", reply_markup=event_type_keyboard())


# Back: to event type selection
@dp.callback_query_handler(lambda c: c.data == "viol_bk_et")
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
    event_type = call.data.split(":")[1]
    await _show_top10(call, "last_week", event_type)


# Period toggle
@dp.callback_query_handler(lambda c: c.data.startswith("viol_toggle:"))
async def cb_period_toggle(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    _, event_type, period = call.data.split(":")
    await _show_top10(call, period, event_type)


# Download full report
@dp.callback_query_handler(lambda c: c.data.startswith("viol_dl:"))
async def cb_download(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    parts = call.data.split(":")
    period = parts[1]
    event_type = parts[2]

    await call.answer("Generating report...")
    content = await build_report_file(
        period, event_type, config.COMPANY_NAME,
        show_severity=await is_super_admin(call.from_user.id),
    )
    await call.message.answer_document(
        types.InputFile(io.BytesIO(content),
                        filename=report_filename(config.COMPANY_SLUG, period)),
        caption=f"📊 {config.COMPANY_NAME} — {period_label(period)}"
    )
