from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandHelp

from loader import dp
from utils.db_api.admins import is_super_admin


@dp.message_handler(CommandHelp())
async def bot_help(message: types.Message):
    text = [
        "📋 <b>HF Alerts Bot</b>\n",
        "This bot monitors your fleet and reports safety violations.\n",
        "<b>Commands:</b>",
        "/start — Open the main menu",
        "/help — Show this help message",
        "/report today | yesterday — Violations grouped by event type",
        "/top [N] — Top N violators today (default 10, max 50)\n",
        "<b>Violations Report</b>",
        "From the main menu, tap <b>Violations Report</b> to view top offending units.",
        "• Choose <b>Speeding</b> or <b>Other Violations</b>",
        "• Toggle between <b>Last Week</b> and <b>Last Month</b>",
        "• Download a full detailed report as a text file",
    ]
    if await is_super_admin(message.from_user.id):
        text += [
            "\n\n🔑 <b>Super Admin Features:</b>",
            "Use <b>👥 Admin Management</b> from the main menu to add, activate, deactivate, remove admins, and control company access.",
        ]
    await message.answer("\n".join(text), parse_mode="HTML")