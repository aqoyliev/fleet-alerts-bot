"""What the bot does inside a group chat.

There is one group that matters — the alerts group (utils/db_api/groups.py) — and no way
to make another one matter from inside Telegram: adding the bot to a chat registers
nothing, there is no /setunit, and a chat that is not the alerts group gets no answer to
anything typed in it. That is the whole difference from single-company, where every
truck has a group of its own and most of this file was about pairing them up.

/report and /top are not here. They also answer in an admin's DM, so they live with the
other private-chat commands in handlers/users/report.py.
"""
import logging

from aiogram import types

from loader import dp, bot
from data import config
from utils.db_api.admins import get_all_admins, is_admin
from utils.db_api.groups import is_the_group, set_group_enabled
from utils.tg_text import esc

logger = logging.getLogger(__name__)
GROUP_TYPES = [types.ChatType.GROUP, types.ChatType.SUPERGROUP]


async def _admin_ids() -> list[int]:
    """Active admin Telegram ids (DB admins ∪ the maintainer ids from config)."""
    ids: set[int] = set()
    try:
        ids.update(a["telegram_id"] for a in await get_all_admins() if a["is_active"])
    except Exception as e:
        logger.error(f"Could not load admins for notify: {e}")
    ids.update(config.HIDDEN_ADMIN_IDS)
    return sorted(ids)


async def _tell_admins(text: str) -> None:
    """DM every admin. One who cannot be reached must not cost the rest the message."""
    for admin_id in await _admin_ids():
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")


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
    if await is_admin(message.from_user.id):
        text += (
            "\n<b>Bot admins</b>\n"
            "/disable · /enable — pause and resume alerts in this group\n"
            "Everything else is in the 🖥 Admin Panel, in your chat with the bot."
        )
    await message.reply(text, parse_mode="HTML", disable_web_page_preview=True)


@dp.my_chat_member_handler()
async def on_bot_chat_member_update(update: types.ChatMemberUpdated):
    """Notice the bot being added to or removed from a chat.

    Nothing is registered and nothing is muted here. Being added to some other chat is
    logged with its id — that line is how a maintainer finds the id to configure — and
    that is all. Leaving the alerts group is the one event worth interrupting somebody
    for: from that moment no alert can be delivered there, and the bot is the only party
    that knows it. The group's settings are left exactly as they were, so putting the
    bot back is the entire fix (see _drop_unreachable in utils/webhook_handler.py for
    why it is not muted in the meantime).
    """
    old = update.old_chat_member.status
    new = update.new_chat_member.status
    chat = update.chat

    if chat.type not in GROUP_TYPES:
        return

    added = new in ("member", "administrator") and old in ("left", "kicked")
    removed = new in ("left", "kicked") and old in ("member", "administrator")
    if not added and not removed:
        return

    logger.info(f"Bot {'added to' if added else 'removed from'} {chat.type} "
                f"'{chat.title}' (id={chat.id})")
    if not await is_the_group(chat.id):
        return

    if removed:
        await _tell_admins(
            f"⚠️ <b>The bot was removed from the alerts group</b> "
            f"(<i>{esc(chat.title)}</i>).\n\n"
            "Alerts can't be delivered there until it is added back. They still reach "
            "the admins who have DM notifications on."
        )
    else:
        await _tell_admins(
            f"✅ The bot is back in the alerts group (<i>{esc(chat.title)}</i>). "
            "Alerts are being delivered there again."
        )


async def _admin_in_the_group(message: types.Message) -> bool:
    """Gate for the mute commands: the alerts group, and a bot admin asking.

    Admins only, unlike single-company, where anyone in a driver's group may mute it.
    There a mute silences one truck's chat for the people sitting in it. Here it
    silences the fleet, for everybody, and the one who typed it may not be anyone the
    company has given that decision to.
    """
    if not await is_the_group(message.chat.id):
        return False
    if await is_admin(message.from_user.id):
        return True
    await message.reply("Only the bot's admins can pause or resume alerts here.")
    return False


@dp.message_handler(commands=["disable", "mute"], chat_type=GROUP_TYPES)
async def cmd_disable(message: types.Message):
    """Pause alerts to the group. Every admin is told, with who did it: a muted group is
    a fleet nobody is being warned about, and that must not be one person's secret."""
    if not await _admin_in_the_group(message):
        return
    await set_group_enabled(False)
    await message.reply(
        "🔕 Alerts <b>paused</b> for this group.\n\n"
        "Send <b>/enable</b> here to turn them back on.",
        parse_mode="HTML",
    )
    await _tell_admins(
        f"🔕 <b>Alerts paused</b> in the alerts group by "
        f"{esc(message.from_user.full_name)}.\n"
        "Nothing is posted there until somebody sends /enable in the group or switches "
        "it back on in the Admin Panel."
    )


@dp.message_handler(commands=["enable", "unmute"], chat_type=GROUP_TYPES)
async def cmd_enable(message: types.Message):
    """Resume alerts to the group."""
    if not await _admin_in_the_group(message):
        return
    await set_group_enabled(True)
    await message.reply("🔔 Alerts <b>resumed</b> for this group.", parse_mode="HTML")
