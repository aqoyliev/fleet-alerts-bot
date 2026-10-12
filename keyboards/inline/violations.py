from aiogram import types

# Labels come from the report module, which is what actually builds the periods. Keeping
# a second copy here is how the chat and the Mini App end up disagreeing about what
# "Last Week" means. Re-exported, because handlers import the name from this module.
from utils.violation_report import PERIOD_LABELS  # noqa: F401

# The two windows this toggle row offers. PERIOD_LABELS also carries "today", which the
# panel shows as a third segment -- there is no room for a third button here.
PERIOD_TOGGLE = ("last_week", "last_month")


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
    for key in PERIOD_TOGGLE:
        label = PERIOD_LABELS[key]
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
