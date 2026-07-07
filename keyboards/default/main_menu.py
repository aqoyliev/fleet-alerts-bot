from aiogram import types


def main_menu_keyboard(is_super: bool = False) -> types.ReplyKeyboardMarkup:
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(types.KeyboardButton("📊 Violations Report"))
    if is_super:
        kb.add(types.KeyboardButton("👥 Admin Management"))
    kb.add(types.KeyboardButton("⚙️ Settings"))
    return kb
