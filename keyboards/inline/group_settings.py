from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from data.event_catalog import GROUP_FILTER_TYPES


def group_events_keyboard(allowed: list[str]) -> InlineKeyboardMarkup:
    """Toggle keyboard for a group's event filter. An empty `allowed` list means the
    group receives everything, so every row shows as ✅ in that state."""
    all_mode = not allowed
    allowed_set = set(allowed)
    kb = InlineKeyboardMarkup(row_width=1)
    for event_type, emoji, label in GROUP_FILTER_TYPES:
        on = all_mode or event_type in allowed_set
        icon = "✅" if on else "⬜"
        kb.add(InlineKeyboardButton(
            f"{icon} {emoji} {label}",
            callback_data=f"grpevt:tog:{event_type}",
        ))
    kb.add(InlineKeyboardButton("🔄 Reset to all events", callback_data="grpevt:all"))
    return kb
