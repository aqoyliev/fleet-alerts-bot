from aiogram import types


def main_menu_keyboard() -> types.ReplyKeyboardMarkup:
    # Admin management lives in the Mini App now, not here — see its Admins screen.
    #
    # No admin-panel button here either. The Mini App is reached from the ☰ Menu button,
    # which handlers/users/start.py points at it per chat, and from the inline button on
    # /start.
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("📊 Violations Report"))
    kb.add(types.KeyboardButton("⚙️ Settings"))
    return kb
