from aiogram import types


def main_menu_keyboard(is_super: bool = False) -> types.ReplyKeyboardMarkup:
    # Every admin sees the Admins panel; super admins get management controls inside it.
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("📊 Violations Report"))
    kb.add(types.KeyboardButton("👥 Admins"))
    kb.add(types.KeyboardButton("⚙️ Settings"))
    return kb
