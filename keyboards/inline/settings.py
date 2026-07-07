from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from utils.webhook_handler import EVENT_TYPE_MAP


def settings_menu_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(InlineKeyboardButton("🔔 My Notifications", callback_data="settings_notif"))
    return kb


def notifications_keyboard(subscribed: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    for event_type, (emoji, title) in EVENT_TYPE_MAP.items():
        icon = "✅" if event_type in subscribed else "❌"
        kb.add(InlineKeyboardButton(
            f"{icon} {emoji} {title.title()}",
            callback_data=f"settings_notif_toggle:{event_type}",
        ))
    kb.add(InlineKeyboardButton("◀ Back", callback_data="settings_bk_menu"))
    return kb
