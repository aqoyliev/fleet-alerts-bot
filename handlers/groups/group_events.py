import logging
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from loader import dp, bot
from data import config
from utils.db_api.groups import is_the_group, set_group_enabled
from utils.db_api.admins import get_all_admins, is_admin
from utils.db_api.violations import get_violations_by_type, get_top_violators
from utils.webhook_handler import EVENT_TYPE_MAP

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
GROUP_TYPES = [types.ChatType.GROUP, types.ChatType.SUPERGROUP]


def _report_text(company_name: str, rows: list[dict], date_str: str) -> str:
    header = f"📊 <b>Daily Violations Report</b>\n<b>{company_name}</b> — {date_str}\n"
    if not rows:
        return header + "\n✅ No violations today."

    by_type: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_type[row["event_type"]].append(row)

    def _sort_key(et):
        if et == "speeding":
            return (0, 0)
        return (1, -sum(v["total"] for v in by_type[et]))

    lines = [header]
    for event_type in sorted(by_type, key=_sort_key):
        emoji, title = EVENT_TYPE_MAP.get(event_type, ("⚠️", event_type.replace("_", " ").title()))
        lines.append(f"\n{emoji} <b>{title}</b>")
        for v in by_type[event_type]:
            lines.append(f"  🚛 {v['vehicle_number']} — {v['total']}")
    return "\n".join(lines)


def _report_keyboard(period: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📅 Today" if period != "today" else "✅ Today", callback_data="grp_report:today"),
        InlineKeyboardButton("📅 Yesterday" if period != "yesterday" else "✅ Yesterday", callback_data="grp_report:yesterday"),
    )
    return kb


async def _require_the_group(chat_id: int) -> bool:
    """Every group command here only makes sense in the one configured group — there is
    no other group whose alerts /report or /top could possibly be describing."""
    return await is_the_group(chat_id)


@dp.message_handler(commands=["report"], chat_type=GROUP_TYPES)
async def cmd_report(message: types.Message):
    if not await _require_the_group(message.chat.id):
        return

    now_et = datetime.now(tz=ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    rows = await get_violations_by_type(since=yesterday_start, until=today_start)
    date_str = yesterday_start.strftime("%b %d, %Y")
    text = _report_text(config.COMPANY_NAME, rows, date_str)
    await message.reply(text, parse_mode="HTML", reply_markup=_report_keyboard("yesterday"))


@dp.callback_query_handler(lambda c: c.data.startswith("grp_report:"))
async def cb_report_toggle(call: types.CallbackQuery):
    if not await _require_the_group(call.message.chat.id):
        await call.answer()
        return

    period = call.data.split(":")[1]
    now_et = datetime.now(tz=ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        since, until = today_start, now_et
        date_str = today_start.strftime("%b %d, %Y")
    else:
        since = today_start - timedelta(days=1)
        until = today_start
        date_str = since.strftime("%b %d, %Y")

    rows = await get_violations_by_type(since=since, until=until)
    text = _report_text(config.COMPANY_NAME, rows, date_str)
    await call.message.edit_text(text, parse_mode="HTML", reply_markup=_report_keyboard(period))
    await call.answer()


@dp.message_handler(commands=["top"], chat_type=GROUP_TYPES)
async def cmd_top(message: types.Message):
    if not await _require_the_group(message.chat.id):
        return

    args = message.get_args()
    try:
        limit = max(1, min(int(args), 50)) if args else 10
    except ValueError:
        limit = 10

    now_et = datetime.now(tz=ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    rows = await get_top_violators(since=today_start, until=now_et, limit=limit)
    company_name = config.COMPANY_NAME
    date_str = today_start.strftime("%b %d, %Y")

    header = f"📊 <b>Top {limit} Violators</b>\n<b>{company_name}</b> — {date_str}\n"
    if not rows:
        text = header + "\n✅ No violations today."
    else:
        lines = [header]
        for i, row in enumerate(rows, 1):
            lines.append(f"{i}. 🚛 {row['vehicle_number']} — {row['total']}")
        text = "\n".join(lines)

    await message.reply(text, parse_mode="HTML")


@dp.message_handler(commands=["help"], chat_type=GROUP_TYPES)
async def cmd_group_help(message: types.Message):
    """/help inside a group. Separate from the DM handler in handlers/users/help.py,
    which is admin-oriented and private-only."""
    text = (
        f"🚛 <b>{config.COMPANY_NAME} — Fleet Alerts</b>\n\n"
        "This group receives every vehicle's alerts.\n\n"
        "/report — yesterday's violations\n"
        "/top — today's top violators\n"
        "/mute — pause alerts in this group\n"
        "/unmute — resume alerts\n"
    )
    if await is_admin(message.from_user.id):
        text += "\nYou're a bot admin — use the 🖥 Admin Panel for dashboard/alerts/admins."
    await message.reply(text, parse_mode="HTML", disable_web_page_preview=True)


async def _admin_ids() -> list[int]:
    """Active admin Telegram ids (DB admins ∪ bootstrap super-admins from config)."""
    ids: set[int] = set()
    try:
        ids.update(a["telegram_id"] for a in await get_all_admins() if a["is_active"])
    except Exception as e:
        logger.error(f"Could not load admins for notify: {e}")
    for a in config.ADMINS:
        try:
            ids.add(int(a))
        except (TypeError, ValueError):
            pass
    return list(ids)


@dp.my_chat_member_handler()
async def on_bot_chat_member_update(update: types.ChatMemberUpdated):
    """Track when the bot is added to or removed from a chat, and self-heal the mute
    if it was kicked from the one configured group and someone re-adds it — see
    _drop_unreachable in utils/webhook_handler.py, which is the only thing that mutes
    a group automatically."""
    old = update.old_chat_member.status
    new = update.new_chat_member.status
    chat = update.chat

    if chat.type not in GROUP_TYPES:
        return

    added = new in ("member", "administrator") and old in ("left", "kicked")
    removed = new in ("left", "kicked") and old in ("member", "administrator")

    if added:
        logger.info(f"Bot added to {chat.type} '{chat.title}' (id={chat.id})")
        if await is_the_group(chat.id):
            await set_group_enabled(chat.id, True)
            logger.info(f"Re-added to the configured group (id={chat.id}) — alerts unmuted")
    elif removed:
        logger.info(f"Bot removed from {chat.type} '{chat.title}' (id={chat.id})")


@dp.message_handler(commands=["disable", "mute"], chat_type=GROUP_TYPES)
async def cmd_disable(message: types.Message):
    """Mute alerts to this group. Open to anyone in the group; every admin is DMed."""
    if not await _require_the_group(message.chat.id):
        return
    await set_group_enabled(message.chat.id, False)
    await message.reply(
        "🔕 Alerts <b>disabled</b> for this group.\n\n"
        "Send <b>/enable</b> here to turn them back on.",
        parse_mode="HTML",
    )
    who = message.from_user.full_name
    text = f"🔕 <b>Alerts muted</b> by {who} (chat <code>{message.chat.id}</code>)."
    for admin_id in await _admin_ids():
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id} of mute: {e}")


@dp.message_handler(commands=["enable", "unmute"], chat_type=GROUP_TYPES)
async def cmd_enable(message: types.Message):
    """Unmute alerts to this group. Open to anyone, necessarily: whoever can mute it
    must be able to undo it."""
    if not await _require_the_group(message.chat.id):
        return
    await set_group_enabled(message.chat.id, True)
    await message.reply("🔔 Alerts <b>enabled</b> for this group.", parse_mode="HTML")
