"""What the bot does inside a group chat — which is very little.

There is one group that matters — the alerts group (utils/db_api/groups.py) — and no way
to make another one matter from inside Telegram: adding the bot to a chat registers
nothing, there is no /setunit, and a chat that is not the alerts group gets no answer to
anything typed in it. That is the whole difference from single-company, where every
truck has a group of its own and most of this file was about pairing them up.

Nothing about the group is controlled from the group either. Muting it is a switch in
the admin panel and nowhere else: there is no /disable or /enable here, and nobody is
messaged when the bot is added to or removed from a chat.

/report and /top are not here. They also answer in an admin's DM, so they live with the
other private-chat commands in handlers/users/report.py.
"""
import logging

from aiogram import types

from loader import dp
from data import config
from utils.db_api.groups import is_the_group
from utils.tg_text import esc

logger = logging.getLogger(__name__)
GROUP_TYPES = [types.ChatType.GROUP, types.ChatType.SUPERGROUP]


@dp.message_handler(commands=["help"], chat_type=GROUP_TYPES)
async def cmd_group_help(message: types.Message):
    """/help inside the alerts group. Separate from the DM handler in
    handlers/users/help.py, which is admin-oriented and private-only."""
    if not await is_the_group(message.chat.id):
        return
    text = (
        f"🚛 <b>{esc(config.COMPANY_NAME)} — Fleet Alerts</b>\n\n"
        "This group receives the fleet's safety alerts as they happen, and a report of "
        "the day's violations every night at midnight ET.\n\n"
        "/report today — violations so far today, by type\n"
        "/report yesterday — the same for yesterday\n"
        "/top — today's top violators (/top 20 for more)\n"
    )
    await message.reply(text, parse_mode="HTML", disable_web_page_preview=True)


@dp.my_chat_member_handler()
async def on_bot_chat_member_update(update: types.ChatMemberUpdated):
    """Log the bot being added to or removed from a group, and do nothing else.

    Nothing is registered, nothing is muted and nobody is told. The log line carries the
    chat's id, which is how a maintainer finds the id of a group to configure. A group
    the bot has been removed from is simply tried again on the next alert (see
    _drop_unreachable in utils/webhook_handler.py), so putting the bot back is the
    entire fix and there is no state here to fall out of step with it.
    """
    old = update.old_chat_member.status
    new = update.new_chat_member.status
    chat = update.chat

    if chat.type not in GROUP_TYPES:
        return

    if new in ("member", "administrator") and old in ("left", "kicked"):
        logger.info(f"Bot added to {chat.type} '{chat.title}' (id={chat.id})")
    elif new in ("left", "kicked") and old in ("member", "administrator"):
        logger.info(f"Bot removed from {chat.type} '{chat.title}' (id={chat.id})")
