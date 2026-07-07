from aiogram import types
from aiogram.dispatcher import FSMContext
from aiogram.utils.exceptions import MessageCantBeEdited, MessageNotModified

from loader import dp
from states.admin_mgmt import AdminAdd
from utils.db_api.admins import (
    is_super_admin,
    get_all_admins,
    get_admin_by_id,
    get_admin_companies,
    set_admin_active,
    delete_admin,
    add_admin,
    assign_company,
    revoke_company,
)
from utils.db_api.companies import get_all_companies
from keyboards.inline.admin_mgmt import (
    admin_list_keyboard,
    admin_detail_keyboard,
    admin_remove_confirm_keyboard,
    admin_companies_keyboard,
    add_admin_cancel_keyboard,
)


async def _edit_or_send(call: types.CallbackQuery, text: str, reply_markup, parse_mode="HTML"):
    try:
        await call.message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except MessageNotModified:
        pass
    except MessageCantBeEdited:
        await call.message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)


def _format_admin_detail(admin: dict, company_names: list[str]) -> str:
    uname = f" (@{admin['username']})" if admin["username"] else ""
    status = "✅ Active" if admin["is_active"] else "⛔ Inactive"
    if admin["is_super"]:
        access = "All companies (super admin)"
    elif company_names:
        access = ", ".join(company_names)
    else:
        access = "No companies assigned"
    return (
        f"👤 <b>{admin['full_name']}</b>{uname}\n"
        f"Status: {status}\n"
        f"Access: {access}"
    )


async def _show_admin_list(call: types.CallbackQuery):
    admins = await get_all_admins()
    if not admins:
        await _edit_or_send(call, "No admins found.", None)
        return
    text = "👥 <b>Admin Management</b>\n\nSelect an admin to manage:"
    await _edit_or_send(call, text, admin_list_keyboard(admins))


async def _show_admin_detail(call: types.CallbackQuery, admin_id: int):
    admin = await get_admin_by_id(admin_id)
    if not admin:
        await call.answer("Admin not found.", show_alert=True)
        await _show_admin_list(call)
        return
    assigned_ids = await get_admin_companies(admin_id)
    companies = await get_all_companies()
    company_map = {c["id"]: c["name"] for c in companies}
    company_names = [company_map[cid] for cid in assigned_ids if cid in company_map]
    text = _format_admin_detail(admin, company_names)
    await _edit_or_send(call, text, admin_detail_keyboard(admin))


# Entry point from main menu button
@dp.message_handler(text="👥 Admin Management")
async def btn_admin_mgmt(message: types.Message):
    if not await is_super_admin(message.from_user.id):
        await message.answer("⛔ Only super admins can manage admins.")
        return
    admins = await get_all_admins()
    if not admins:
        await message.answer("No admins found.")
        return
    text = "👥 <b>Admin Management</b>\n\nSelect an admin to manage:"
    await message.answer(text, reply_markup=admin_list_keyboard(admins), parse_mode="HTML")


@dp.callback_query_handler(lambda c: c.data == "adm_list")
async def cb_adm_list(call: types.CallbackQuery):
    if not await is_super_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    await _show_admin_list(call)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("adm_detail:"))
async def cb_adm_detail(call: types.CallbackQuery):
    if not await is_super_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    admin_id = int(call.data.split(":")[1])
    await _show_admin_detail(call, admin_id)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("adm_toggle_active:"))
async def cb_adm_toggle_active(call: types.CallbackQuery):
    if not await is_super_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    admin_id = int(call.data.split(":")[1])
    admin = await get_admin_by_id(admin_id)
    if not admin:
        await call.answer("Admin not found.", show_alert=True)
        return
    await set_admin_active(admin_id, not admin["is_active"])
    await _show_admin_detail(call, admin_id)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("adm_remove:"))
async def cb_adm_remove(call: types.CallbackQuery):
    if not await is_super_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
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
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    admin_id = int(call.data.split(":")[1])
    admin = await get_admin_by_id(admin_id)
    if not admin:
        await call.answer("Admin not found.", show_alert=True)
        await _show_admin_list(call)
        return
    if admin["telegram_id"] == call.from_user.id:
        await call.answer("⛔ You cannot remove yourself.", show_alert=True)
        return
    if admin["is_super"]:
        await call.answer("⛔ Super admins cannot be removed through this panel.", show_alert=True)
        return
    await delete_admin(admin_id)
    await call.answer("Admin removed.")
    await _show_admin_list(call)


@dp.callback_query_handler(lambda c: c.data.startswith("adm_companies:"))
async def cb_adm_companies(call: types.CallbackQuery):
    if not await is_super_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    admin_id = int(call.data.split(":")[1])
    admin = await get_admin_by_id(admin_id)
    if not admin:
        await call.answer("Admin not found.", show_alert=True)
        return
    assigned_ids = await get_admin_companies(admin_id)
    all_companies = await get_all_companies()
    uname = f" (@{admin['username']})" if admin["username"] else ""
    text = f"🏢 <b>Company access for {admin['full_name']}</b>{uname}\n\nTap a company to toggle access:"
    await _edit_or_send(call, text, admin_companies_keyboard(admin_id, all_companies, assigned_ids))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("adm_co_toggle:"))
async def cb_adm_co_toggle(call: types.CallbackQuery):
    if not await is_super_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    _, admin_id_str, company_id_str = call.data.split(":")
    admin_id = int(admin_id_str)
    company_id = int(company_id_str)
    assigned_ids = await get_admin_companies(admin_id)
    if company_id in assigned_ids:
        await revoke_company(admin_id, company_id)
    else:
        await assign_company(admin_id, company_id)
    # Refresh the companies screen
    admin = await get_admin_by_id(admin_id)
    if not admin:
        await call.answer("Admin not found.", show_alert=True)
        return
    new_assigned = await get_admin_companies(admin_id)
    all_companies = await get_all_companies()
    uname = f" (@{admin['username']})" if admin["username"] else ""
    text = f"🏢 <b>Company access for {admin['full_name']}</b>{uname}\n\nTap a company to toggle access:"
    await _edit_or_send(call, text, admin_companies_keyboard(admin_id, all_companies, new_assigned))
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "adm_bk_list")
async def cb_adm_bk_list(call: types.CallbackQuery):
    if not await is_super_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    await _show_admin_list(call)
    await call.answer()


@dp.callback_query_handler(lambda c: c.data.startswith("adm_bk_detail:"))
async def cb_adm_bk_detail(call: types.CallbackQuery):
    if not await is_super_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    admin_id = int(call.data.split(":")[1])
    await _show_admin_detail(call, admin_id)
    await call.answer()


# Add admin via FSM
@dp.callback_query_handler(lambda c: c.data == "adm_add_admin")
async def cb_adm_add_admin(call: types.CallbackQuery, state: FSMContext):
    if not await is_super_admin(call.from_user.id):
        await call.answer("⛔ Access denied.", show_alert=True)
        return
    await AdminAdd.waiting_for_id.set()
    await call.message.answer(
        "📝 Send me the Telegram ID of the user to add as admin.\n"
        "<i>They must have started the bot first.</i>",
        reply_markup=add_admin_cancel_keyboard(),
    )
    await call.answer()


@dp.callback_query_handler(lambda c: c.data == "adm_add_cancel", state=AdminAdd.waiting_for_id)
async def cb_adm_add_cancel(call: types.CallbackQuery, state: FSMContext):
    await state.finish()
    await call.message.edit_text("❌ Cancelled.")
    await call.answer()


@dp.message_handler(state=AdminAdd.waiting_for_id)
async def msg_adm_add_id(message: types.Message, state: FSMContext):
    if not await is_super_admin(message.from_user.id):
        await state.finish()
        return
    raw = message.text.strip()
    if not raw.lstrip("-").isdigit():
        await message.answer(
            "⚠️ That doesn't look like a Telegram ID. Send a numeric ID, or tap Cancel.",
            reply_markup=add_admin_cancel_keyboard(),
        )
        return
    new_id = int(raw)
    try:
        await add_admin(telegram_id=new_id, added_by=message.from_user.id, is_super=False)
        await state.finish()
        await message.answer(f"✅ Admin {new_id} added successfully.")
    except Exception as e:
        await state.finish()
        if "foreign key" in str(e).lower():
            await message.answer(f"❌ User {new_id} has never started the bot. Ask them to send /start first.")
        else:
            await message.answer(f"❌ Error: {e}")
        return
    # Show refreshed admin list
    admins = await get_all_admins()
    text = "👥 <b>Admin Management</b>\n\nSelect an admin to manage:"
    await message.answer(text, reply_markup=admin_list_keyboard(admins))
