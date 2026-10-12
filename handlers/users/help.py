from aiogram import types
from aiogram.dispatcher.filters.builtin import CommandHelp

from loader import dp
from utils.db_api.admins import is_super_admin


@dp.message_handler(CommandHelp(), chat_type=types.ChatType.PRIVATE)
async def bot_help(message: types.Message):
    """DM only. The text below is a wall of admin-oriented instructions, and typed in
    the alerts group it would dump all of it into the chat for everyone; the group has
    its own, shorter /help in handlers/groups/group_events.py."""
    text = [
        "📋 <b>HF Alerts Bot</b>\n",
        "This bot monitors your fleet and reports safety violations.\n",
        "<b>Commands:</b>",
        "/start — Open the main menu",
        "/help — Show this help message",
        "/report today | yesterday — Violations grouped by event type",
        "/top [N] — Top N violators today (default 10, max 50)\n",
        "<b>🖥 Admin Panel</b>",
        "The ☰ <b>Menu</b> button, or the button under /start. A live dashboard, the "
        "full alert feed, and the alerts group's settings — which event types it "
        "receives, and a switch to pause it.\n",
        "<b>Violations Report</b>",
        "From the main menu, tap <b>Violations Report</b> to view top offending units.",
        "• Choose <b>Speeding</b> or <b>Other Violations</b>",
        "• Toggle between <b>Last Week</b> and <b>Last Month</b>",
        "• Download a full detailed report as a text file\n",
        "<b>⚙️ Settings</b>",
        "Choose which event types are also sent to you personally, here in this chat.\n",
        "<b>In the alerts group</b>",
        "/report and /top work there too, for everyone in the group.",
        "/disable · /enable — Pause or resume the group's alerts (bot admins only)\n",
        "<b>✉️ Contact Support</b>",
        "Tap it on the keyboard and send anything — text, a photo, a video. It reaches "
        "the people who run this bot, and their answer arrives in this chat.",
    ]
    if await is_super_admin(message.from_user.id):
        text += [
            "\n\n🔑 <b>Super Admin Features:</b>",
            "Manage admins from the <b>🖥 Admin Panel</b> — add, activate, deactivate, "
            "promote and remove them on its Admins screen.",
        ]
    await message.answer("\n".join(text), parse_mode="HTML")
