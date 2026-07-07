from aiogram import types

from utils.webhook_handler import EVENT_TYPE_MAP

PERIOD_LABELS = {
    "last_week": "Last Week",
    "last_month": "Last Month",
}


def companies_keyboard(companies: list[dict]) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=1)
    for c in companies:
        kb.add(types.InlineKeyboardButton(c["name"], callback_data=f"viol_company:{c['slug']}"))
    return kb


def event_type_keyboard(company_slug: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🚨 Speeding", callback_data=f"viol_etype:{company_slug}:speeding"),
        types.InlineKeyboardButton("⚠️ Other Violations", callback_data=f"viol_etype:{company_slug}:other"),
    )
    kb.add(types.InlineKeyboardButton("◀ Back", callback_data="viol_bk_co"))
    return kb


def top10_keyboard(rows: list[dict], company_slug: str, period: str, event_type: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    # Period toggle row — checkmark on the active period
    period_buttons = []
    for key, label in PERIOD_LABELS.items():
        mark = "✅ " if key == period else ""
        period_buttons.append(types.InlineKeyboardButton(
            f"{mark}{label}",
            callback_data=f"viol_toggle:{company_slug}:{event_type}:{key}"
        ))
    kb.row(*period_buttons)
    if rows:
        kb.add(types.InlineKeyboardButton(
            "📥 Download Full Report",
            callback_data=f"viol_dl:{company_slug}:{period}:{event_type}"
        ))
    kb.add(types.InlineKeyboardButton("◀ Back", callback_data=f"viol_bk_et:{company_slug}"))
    return kb
