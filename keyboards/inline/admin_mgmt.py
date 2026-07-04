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


def admin_detail_keyboard(admin: dict, is_super: bool = False) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    # Only super admins get controls, and never over another super admin.
    if is_super and not admin["is_super"]:
        toggle_label = "✅ Activate" if not admin["is_active"] else "⛔ Deactivate"
        kb.row(
            InlineKeyboardButton(toggle_label, callback_data=f"adm_toggle_active:{admin['id']}"),
            InlineKeyboardButton("🗑 Remove", callback_data=f"adm_remove:{admin['id']}"),
        )
    kb.add(InlineKeyboardButton("◀ Back to List", callback_data="adm_bk_list"))
    return kb


def admin_remove_confirm_keyboard(admin_id: int) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("✅ Yes, Remove", callback_data=f"adm_remove_confirm:{admin_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"adm_detail:{admin_id}"),
    )
    return kb
