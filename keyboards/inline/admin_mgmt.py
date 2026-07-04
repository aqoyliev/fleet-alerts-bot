from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def admin_list_keyboard(admins: list[dict], is_super: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    for a in admins:
        icon = "✅" if a["is_active"] else "⛔"
        star = "⭐ " if a["is_super"] else ""
        uname = f" (@{a['username']})" if a["username"] else ""
        kb.add(InlineKeyboardButton(
            f"{icon} {star}{a['full_name']}{uname}",
            callback_data=f"adm_detail:{a['id']}",
        ))
    if is_super:
        kb.add(InlineKeyboardButton("➕ Add Admin", callback_data="adm_add_admin"))
    return kb


def add_admin_cancel_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("❌ Cancel", callback_data="adm_add_cancel"))
    return kb


def admin_detail_keyboard(admin: dict, is_super: bool = False, is_self: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    if is_super and not admin["is_super"]:
        # Controls over a regular admin.
        toggle_label = "✅ Activate" if not admin["is_active"] else "⛔ Deactivate"
        kb.row(
            InlineKeyboardButton(toggle_label, callback_data=f"adm_toggle_active:{admin['id']}"),
            InlineKeyboardButton("🗑 Remove", callback_data=f"adm_remove:{admin['id']}"),
        )
    elif is_super and admin["is_super"] and is_self:
        # A super admin can't remove themselves — they hand the role to someone else.
        kb.add(InlineKeyboardButton("🔁 Transfer super admin", callback_data="adm_transfer_start"))
    kb.add(InlineKeyboardButton("◀ Back to List", callback_data="adm_bk_list"))
    return kb


def admin_transfer_choose_keyboard(admins: list[dict]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    for a in admins:
        uname = f" (@{a['username']})" if a["username"] else ""
        kb.add(InlineKeyboardButton(
            f"⭐ {a['full_name']}{uname}",
            callback_data=f"adm_transfer_to:{a['id']}",
        ))
    kb.add(InlineKeyboardButton("◀ Back", callback_data="adm_bk_list"))
    return kb


def admin_transfer_confirm_keyboard(admin_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("✅ Yes, transfer", callback_data=f"adm_transfer_confirm:{admin_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data="adm_bk_list"),
    )
    return kb


def admin_remove_confirm_keyboard(admin_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("✅ Yes, Remove", callback_data=f"adm_remove_confirm:{admin_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"adm_detail:{admin_id}"),
    )
    return kb
