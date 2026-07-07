from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandHelp

from loader import dp
from utils.db_api.admins import is_super_admin


@dp.message_handler(CommandHelp())
async def bot_help(message: types.Message):
    text = [
        "📋 <b>Motive Alerts Bot</b>\n",
        "This bot monitors your fleet and reports safety violations from the GoMotive platform.\n",
        "<b>Commands:</b>",
        "/start — Open the main menu",
        "/help — Show this help message\n",
        "<b>Violations Report</b>",
        "From the main menu, tap <b>Violations Report</b> to view top offending units by company.",
        "• Choose a company",
        "• Choose <b>Speeding</b> or <b>Other Violations</b>",
        "• Toggle between <b>Last Week</b> and <b>Last Month</b>",
        "• Download a full detailed report as a text file\n",
        "<b>Speeding report note:</b> The download only lists days where a unit had <b>3 or more</b> speeding events.",
    ]
    if await is_super_admin(message.from_user.id):
        text += [
            "\n\n🔑 <b>Super Admin Features:</b>",
            "Use <b>👥 Admin Management</b> from the main menu to add, activate, deactivate, remove admins, and control company access.",
        ]
    await message.answer("\n".join(text), parse_mode="HTML")