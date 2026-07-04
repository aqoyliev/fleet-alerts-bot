from aiogram import types

PERIOD_LABELS = {
    "last_week": "Last Week",
    "last_month": "Last Month",
}


def event_type_keyboard() -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("🚨 Speeding", callback_data="viol_etype:speeding"),
        types.InlineKeyboardButton("⚠️ Other Violations", callback_data="viol_etype:other"),
    )
    return kb


def top10_keyboard(rows: list[dict], period: str, event_type: str) -> types.InlineKeyboardMarkup:
    kb = types.InlineKeyboardMarkup(row_width=2)
    # Period toggle row — checkmark on the active period
    period_buttons = []
    for key, label in PERIOD_LABELS.items():
        mark = "✅ " if key == period else ""
        period_buttons.append(types.InlineKeyboardButton(
            f"{mark}{label}",
            callback_data=f"viol_toggle:{event_type}:{key}"
        ))
    kb.row(*period_buttons)
    if rows:
        kb.add(types.InlineKeyboardButton(
            "📥 Download Full Report",
            callback_data=f"viol_dl:{period}:{event_type}"
        ))
    kb.add(types.InlineKeyboardButton("◀ Back", callback_data="viol_bk_et"))
    return kb
