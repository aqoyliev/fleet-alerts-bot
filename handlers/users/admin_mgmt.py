from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.utils.exceptions import MessageCantBeEdited, MessageNotModified

from loader import dp
from states.admin_mgmt import AdminAdd
from utils.db_api.admins import (
    is_admin,
    is_super_admin,
    get_all_admins,
    get_admin_by_id,
    set_admin_active,
    delete_admin,
    add_admin,
)
from utils.db_api.users import ensure_user
from keyboards.inline.admin_mgmt import (
    admin_list_keyboard,
    admin_detail_keyboard,
    admin_remove_confirm_keyboard,
    add_admin_cancel_keyboard,
)

LIST_TITLE = "👥 <b>Admins</b>\n\nSelect an admin to view."

ADD_PROMPT = (
    "➕ <b>Add a new admin</b>\n\n"
    "Do any one of these:\n"
    "• <b>Forward</b> a message from the person to me, or\n"
    "• Share their <b>contact</b>, or\n"
    "• Send their <b>numeric user ID</b>.\n\n"
    "<i>Tip: if they have Telegram Premium with forwarding privacy on, a forward won't "
    "reveal their ID — ask them to open the bot and send /myid, then paste that number here.</i>"
)


async def _edit_or_send(call: types.CallbackQuery, text: str, reply_markup, parse_mode="HTML"):
    try:
        await call.message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except MessageNotModified:
        pass
    except MessageCantBeEdited:
        await call.message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)


def _format_admin_detail(admin: dict) -> str:
    uname = f" (@{admin['username']})" if admin["username"] else ""
    status = "✅ Active" if admin["is_active"] else "⛔ Inactive"
    role = "⭐ Super admin" if admin["is_super"] else "Admin"
    created = admin["created_at"].strftime("%b %d, %Y") if admin.get("created_at") else "—"
    return (
        f"👤 <b>{admin['full_name']}</b>{uname}\n"
        f"ID: <code>{admin['telegram_id']}</code>\n"
        f"Role: {role}\n"
        f"Status: {status}\n"
        f"Added: {created}"
    )


async def _show_admin_list(call: types.CallbackQuery, is_super: bool):
    admins = await get_all_admins()
    if not admins:
        await _edit_or_send(call, "No admins found.", None)
        return
    await _edit_or_send(call, LIST_TITLE, admin_list_keyboard(admins, is_super))


async def _show_admin_detail(call: types.CallbackQuery, admin_id: int, is_super: bool):
    admin = await get_admin_by_id(admin_id)
    if not admin:
        await call.answer("Admin not found.", show_alert=True)
        await _show_admin_list(call, is_super)
        return
    await _edit_or_send(call, _format_admin_detail(admin), admin_detail_keyboard(admin, is_super))


# ── Entry point (all admins; super admins additionally get controls) ────────────────

@dp.message_handler(text="👥 Admins")
async def btn_admin_mgmt(message: types.Message):
    if not await is_admin(message.from_user.id):
        return
    is_super = await is_super_admin(message.from_user.id)
    admins = await get_all_admins()
    if not admins:
        await message.answer("No admins found.")
        return
    await message.answer(LIST_TITLE, reply_markup=admin_list_keyboard(admins, is_super), parse_mode="HTML")


@dp.callback_query_handler(lambda c: c.data == "adm_list")
async def cb_adm_list(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    await _show_admin_list(call, await is_super_admin(call.from_user.id))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("adm_detail:"))
async def cb_adm_detail(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    admin_id = int(call.data.split(":")[1])
    await _show_admin_detail(call, admin_id, await is_super_admin(call.from_user.id))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "adm_bk_list")
async def cb_adm_bk_list(call: types.CallbackQuery):
    if not await is_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    await _show_admin_list(call, await is_super_admin(call.from_user.id))
    await call.answer()


# ── Mutations (super admins only) ───────────────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data.startswith("adm_toggle_active:"))
async def cb_adm_toggle_active(call: types.CallbackQuery):
    if not await is_super_admin(call.from_user.id):
        await call.answer("⛔ Super admins only.", show_alert=True)
        return
    admin_id = int(call.data.split(":")[1])
    admin = await get_admin_by_id(admin_id)
    if not admin:
        await call.answer("Admin not found.", show_alert=True)
        return
    if admin["is_super"]:
        await call.answer("⛔ Super admins can't be deactivated here.", show_alert=True)
        return
    await set_admin_active(admin_id, not admin["is_active"])
    await _show_admin_detail(call, admin_id, is_super=True)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("adm_remove:"))
async def cb_adm_remove(call: types.CallbackQuery):
    if not await is_super_admin(call.from_user.id):
        await call.answer("⛔ Super admins only.", show_alert=True)
        return
    admin_id = int(call.data.split(":")[1])
    admin = await get_admin_by_id(admin_id)
    if not admin:
        await call.answer("Admin not found.", show_alert=True)
        return
    uname = f" (@{admin['username']})" if admin["username"] else ""
    text = (
        f"⚠️ Remove <b>{admin['full_name']}</b>{uname} as admin?\n"
        "This cannot be undone."
    )
    await _edit_or_send(call, text, admin_remove_confirm_keyboard(admin_id))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("adm_remove_confirm:"))
async def cb_adm_remove_confirm(call: types.CallbackQuery):
    if not await is_super_admin(call.from_user.id):
        await call.answer("⛔ Super admins only.", show_alert=True)
        return
    admin_id = int(call.data.split(":")[1])
    admin = await get_admin_by_id(admin_id)
    if not admin:
        await call.answer("Admin not found.", show_alert=True)
        await _show_admin_list(call, is_super=True)
        return
    if admin["telegram_id"] == call.from_user.id:
        await call.answer("⛔ You cannot remove yourself.", show_alert=True)
        return
    if admin["is_super"]:
        await call.answer("⛔ Super admins cannot be removed through this panel.", show_alert=True)
        return
    await delete_admin(admin_id)
    await call.answer("Admin removed.")
    await _show_admin_list(call, is_super=True)


# ── Add admin (super admins only) via FSM ───────────────────────────────────────────

@dp.callback_query_handler(lambda c: c.data == "adm_add_admin")
async def cb_adm_add_admin(call: types.CallbackQuery, state: FSMContext):
    if not await is_super_admin(call.from_user.id):
        await call.answer("⛔ Super admins only.", show_alert=True)
        return
    await AdminAdd.waiting_for_id.set()
    await call.message.answer(ADD_PROMPT, parse_mode="HTML", reply_markup=add_admin_cancel_keyboard())
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "adm_add_cancel", state=AdminAdd.waiting_for_id)
async def cb_adm_add_cancel(call: types.CallbackQuery, state: FSMContext):
    await state.finish()
    await call.message.edit_text("❌ Cancelled.")
    await call.answer()


async def _finish_add(message: types.Message, state: FSMContext, new_id: int, display_name: str | None):
    """Create the admin record and confirm. The users row is guaranteed to exist by the
    caller (ensure_user), so the admins FK never fails here."""
    try:
        await add_admin(telegram_id=new_id, added_by=message.from_user.id, is_super=False)
    except Exception as e:
        await state.finish()
        await message.answer(f"❌ Could not add admin: {e}")
        return
    await state.finish()
    name = display_name or str(new_id)
    await message.answer(
        f"✅ <b>{name}</b> added as admin.\n"
        f"ID: <code>{new_id}</code>\n\n"
        "<i>They'll receive DM alerts once they open the bot and tap Start.</i>",
        parse_mode="HTML",
    )
    admins = await get_all_admins()
    await message.answer(LIST_TITLE, parse_mode="HTML", reply_markup=admin_list_keyboard(admins, is_super=True))


@dp.message_handler(state=AdminAdd.waiting_for_id, content_types=types.ContentTypes.ANY)
async def msg_adm_add(message: types.Message, state: FSMContext):
    if not await is_super_admin(message.from_user.id):
        await state.finish()
        return

    # 1) Forwarded from a visible user → we get id + name + username.
    fwd = message.forward_from
    if fwd is not None:
        await ensure_user(fwd.id, fwd.full_name, fwd.username)
        await _finish_add(message, state, fwd.id, fwd.full_name)
        return

    # 2) Forwarded, but the sender is hidden (privacy setting, common with Premium).
    if message.forward_date is not None or message.forward_sender_name:
        name = message.forward_sender_name or "This person"
        await message.answer(
            f"🔒 <b>{name}</b> has forwarding privacy on (common with Telegram Premium), "
            "so I can't read their user ID from a forwarded message.\n\n"
            "Please send me their <b>numeric user ID</b> instead — they can get it by "
            "opening the bot and sending /myid.",
            parse_mode="HTML",
            reply_markup=add_admin_cancel_keyboard(),
        )
        return

    # 3) Shared contact.
    if message.contact and message.contact.user_id:
        c = message.contact
        cname = f"{c.first_name or ''} {c.last_name or ''}".strip() or None
        await ensure_user(c.user_id, cname)
        await _finish_add(message, state, c.user_id, cname)
        return

    # 4) Raw numeric id.
    raw = (message.text or "").strip()
    if raw.lstrip("-").isdigit():
        new_id = int(raw)
        await ensure_user(new_id)  # placeholder row if they've never started the bot
        await _finish_add(message, state, new_id, None)
        return

    # Anything else → re-prompt.
    await message.answer(
        "⚠️ I couldn't read a user from that. Forward a message from the person, share "
        "their contact, or send their numeric user ID — or tap Cancel.",
        reply_markup=add_admin_cancel_keyboard(),
    )
