from aiogram import types


def main_menu_keyboard(is_super: bool = False) -> types.ReplyKeyboardMarkup:
    # Every admin sees the Admins panel; super admins get management controls inside it.
    #
    # No admin-panel button here. The Mini App is reached from the ☰ Menu button, which
    # handlers/users/start.py points at it per chat, and from the inline button on /start.
    # This keyboard stays the three things it has always been.
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("📊 Violations Report"))
    kb.add(types.KeyboardButton("👥 Admins"))
    kb.add(types.KeyboardButton("⚙️ Settings"))
    return kb
