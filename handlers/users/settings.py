from aiogram import types
from aiogram.utils.exceptions import MessageCantBeEdited, MessageNotModified

from loader import dp
from utils.db_api.admins import is_admin, get_admin_subscriptions, toggle_subscription
from keyboards.inline.settings import settings_menu_keyboard, notifications_keyboard


async def _edit_or_send(call: types.CallbackQuery, text: str, reply_markup, parse_mode="HTML"):
    try:
        await call.message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except MessageNotModified:
        pass
    except MessageCantBeEdited:
        await call.message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)


@dp.message_handler(text="⚙️ Settings")
async def btn_settings(message: types.Message):
    if not await is_admin(message.from_user.id):
        return
    await message.answer("⚙️ <b>Settings</b>", reply_markup=settings_menu_keyboard())


@dp.callback_query_handler(lambda c: c.data == "settings_menu")
async def cb_settings_menu(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    await _edit_or_send(call, "⚙️ <b>Settings</b>", settings_menu_keyboard())
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "settings_notif")
async def cb_settings_notif(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    subscribed = await get_admin_subscriptions(call.from_user.id)
    text = "🔔 <b>My Notifications</b>\n\nTap an event type to toggle personal DM alerts:"
    await _edit_or_send(call, text, notifications_keyboard(subscribed))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("settings_notif_toggle:"))
async def cb_settings_notif_toggle(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    event_type = call.data.split(":")[1]
    await toggle_subscription(call.from_user.id, event_type)
    subscribed = await get_admin_subscriptions(call.from_user.id)
    text = "🔔 <b>My Notifications</b>\n\nTap an event type to toggle personal DM alerts:"
    await _edit_or_send(call, text, notifications_keyboard(subscribed))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "settings_bk_menu")
async def cb_settings_bk_menu(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    await _edit_or_send(call, "⚙️ <b>Settings</b>", settings_menu_keyboard())
    await call.answer()
