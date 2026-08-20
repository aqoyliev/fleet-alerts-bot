import json
import logging

from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandStart

from loader import dp, bot
from data import config

logger = logging.getLogger(__name__)
from utils.db_api.users import upsert_user
from utils.db_api.admins import is_admin
from keyboards.default.main_menu import main_menu_keyboard


def _panel_button() -> types.InlineKeyboardMarkup | None:
    """An inline button that opens the admin Mini App, or None if no URL is configured.

    The reply keyboard already carries one, so this is a second way in rather than a
    replacement — and it exists because the two launch contexts are not equally supported.
    Some clients hand a Mini App its signed credential when it is opened from an inline
    button but not from a keyboard button, which leaves the panel loading and then
    refusing to authenticate anything. Offering both means the dispatcher has a route that
    works without anyone having to know which client they are on.

    web_app is a plain dict for the reason given in keyboards/default/main_menu.py:
    aiogram 2.15 has no WebAppInfo type and serializes unknown kwargs verbatim.
    """
    if not config.WEBAPP_URL:
        return None
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton(
        "🖥 Open Admin Panel", web_app={"url": f"{config.WEBAPP_URL}/panel/"}))
    return kb


async def _set_menu_button(chat_id: int, to_panel: bool) -> None:
    """Point this one chat's ☰ Menu button at the admin panel, or put it back.

    Scoped to a chat rather than set as the bot's default on purpose. The default applies
    to every private chat the bot has, so every driver who DMs it would find "Admin
    Panel" sitting where their command list used to be and get a permission error for
    tapping it. Setting it on the chat of somebody who just passed is_admin puts the offer
    exactly where it belongs — and clearing it again when they don't means a removed
    admin's Menu button stops advertising a door they can no longer open.

    Sent through Bot.request because aiogram 2.15 predates Bot API 6.0 and has neither
    setChatMenuButton nor MenuButtonWebApp. The raw call behaves identically on 2.15 and
    on the 2.25.2 a fresh build would install, which is why it is preferred over adding a
    dependency bump under a working bot.

    Failure is logged and swallowed: a menu button that didn't stick is a smaller problem
    than /start raising.
    """
    if to_panel:
        menu_button = {"type": "web_app", "text": "Admin Panel",
                       "web_app": {"url": f"{config.WEBAPP_URL}/panel/"}}
    else:
        menu_button = {"type": "commands"}
    try:
        await bot.request("setChatMenuButton", {
            "chat_id": chat_id,
            "menu_button": json.dumps(menu_button),
        })
    except Exception as e:
        logger.warning(f"Could not set the menu button for chat {chat_id}: {e}")


@dp.message_handler(CommandStart())
async def bot_start(message: types.Message):
    await upsert_user(
        telegram_id=message.from_user.id,
        full_name=message.from_user.full_name,
        username=message.from_user.username,
        language_code=message.from_user.language_code,
    )
    if not await is_admin(message.from_user.id):
        if config.WEBAPP_URL:
            await _set_menu_button(message.chat.id, to_panel=False)
        await message.answer("⛔ You don't have access to this bot.")
        return
    if config.WEBAPP_URL:
        await _set_menu_button(message.chat.id, to_panel=True)
    await message.answer(
        f"Welcome, {message.from_user.full_name}!",
        reply_markup=main_menu_keyboard()
    )
    panel = _panel_button()
    if panel is not None:
        await message.answer(
            "Fleet at a glance — groups, alerts and admins in one place.\n"
            "Also on the ☰ <b>Menu</b> button, any time.",
            parse_mode="HTML",
            reply_markup=panel,
        )



@dp.message_handler(text="📊 Violations Report")
async def btn_violations(message: types.Message):
    if not await is_admin(message.from_user.id):
        return
    from handlers.users.violations import show_violations_menu
    await show_violations_menu(message)


