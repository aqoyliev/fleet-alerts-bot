from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandStart

from loader import dp
from utils.db_api.users import upsert_user
from utils.db_api.admins import is_admin, is_super_admin
from keyboards.default.main_menu import main_menu_keyboard


@dp.message_handler(CommandStart())
async def bot_start(message: types.Message):
    await upsert_user(
        telegram_id=message.from_user.id,
        full_name=message.from_user.full_name,
        username=message.from_user.username,
        language_code=message.from_user.language_code,
    )
    if not await is_admin(message.from_user.id):
        await message.answer("⛔ You don't have access to this bot.")
        return
    is_super = await is_super_admin(message.from_user.id)
    await message.answer(
        f"Welcome, {message.from_user.full_name}!",
        reply_markup=main_menu_keyboard(is_super=is_super)
    )



@dp.message_handler(commands=["myid", "id"], chat_type=types.ChatType.PRIVATE)
async def cmd_myid(message: types.Message):
    """Tell any user their own Telegram ID. Lets a prospective admin (including Premium
    users whose forwards hide their account) fetch the number a super admin needs to add
    them. Also records their name so the eventual add shows it."""
    await upsert_user(
        telegram_id=message.from_user.id,
        full_name=message.from_user.full_name,
        username=message.from_user.username,
        language_code=message.from_user.language_code,
    )
    premium = " ⭐" if getattr(message.from_user, "is_premium", False) else ""
    await message.answer(
        f"🆔 Your Telegram user ID is <code>{message.from_user.id}</code>{premium}\n\n"
        "Send this number to your administrator so they can add you.",
        parse_mode="HTML",
    )


@dp.message_handler(text="📊 Violations Report")
async def btn_violations(message: types.Message):
    if not await is_admin(message.from_user.id):
        return
    from handlers.users.violations import show_violations_menu
    await show_violations_menu(message)


