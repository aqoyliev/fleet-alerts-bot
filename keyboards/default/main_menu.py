from aiogram import types

from data import config


def main_menu_keyboard(is_super: bool = False) -> types.ReplyKeyboardMarkup:
    # Every admin sees the Admins panel; super admins get management controls inside it.
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    if config.WEBAPP_URL:
        # web_app is passed as a plain dict, not a WebAppInfo: aiogram 2.15 predates Bot
        # API 6.0 and has no such type. TelegramObject serializes unknown kwargs verbatim,
        # so the field reaches Telegram intact — pinned by tests/test_main_menu_webapp.py.
        # Named here so nobody "fixes" this into an import that does not exist.
        #
        # This keyboard is the panel's entry point rather than the bot's ☰ menu button,
        # because it is only ever sent to a caller who already passed is_admin. A menu
        # button is set once for every private chat the bot has, so every driver who DMs
        # it would see "Admin Panel" and get a permission error for tapping it.
        kb.add(types.KeyboardButton("🖥 Admin Panel",
                                    web_app={"url": f"{config.WEBAPP_URL}/panel/"}))
    kb.add(types.KeyboardButton("📊 Violations Report"))
    kb.add(types.KeyboardButton("👥 Admins"))
    kb.add(types.KeyboardButton("⚙️ Settings"))
    return kb
