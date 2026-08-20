from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandHelp

from loader import dp
from data import config
from utils.db_api.admins import is_super_admin


@dp.message_handler(CommandHelp(), chat_type=types.ChatType.PRIVATE)
async def bot_help(message: types.Message):
    """DM only. The text below is a wall of admin-oriented instructions, and typed in a
    driver group it dumped all of it into the chat for everyone."""
    text = [
        f"📋 <b>{config.COMPANY_NAME} — Fleet Alerts Bot</b>\n",
        "This bot monitors your fleet and reports safety violations from Motive and Samsara.\n",
        "<b>Commands:</b>",
        "/start — Open the main menu",
        "/help — Show this help message\n",
        "<b>In a driver's group</b>",
        "/help — What the bot posts there, and whether the group is set up (anyone)",
        "/setunit 1234 — Set this group's unit number (anyone)",
        "/events — Choose which event types this group gets (admins)",
        "/disable · /enable — Mute or unmute this group's alerts (anyone in that group)",
        "/removegroup — Unregister this group (admins)\n",
        "<b>Violations Report</b>",
        "From the main menu, tap <b>Violations Report</b> to view top offending units.",
        "• Choose <b>Speeding</b> or <b>Other Violations</b>",
        "• Toggle between <b>Last Week</b> and <b>Last Month</b>",
        "• Download a full detailed report as a text file\n",
        "<b>Speeding report note:</b> The download only lists days where a unit had <b>3 or more</b> speeding events.",
    ]
    if await is_super_admin(message.from_user.id):
        text += [
            "\n\n🔑 <b>Super Admin Features:</b>",
            "Manage admins from the <b>🖥 Admin Panel</b> — open it from the ☰ menu or the "
            "button under /start. Add, activate, deactivate, and remove admins there.",
        ]
    await message.answer("\n".join(text), parse_mode="HTML")