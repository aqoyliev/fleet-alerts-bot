"""Command menus shown in Telegram's ☰ picker.

Menus are set per SCOPE rather than globally. A single global list leaks the private
-chat commands into every driver group, where a driver taps /myid or /help and gets
either silence or a wall of admin text. Telegram resolves the most specific scope
first, so the group list below is what a driver sees and the private list is what an
admin sees in a DM.

Hiding a command does NOT disable it. /events, /event_list, /disable, /enable and
/removegroup all still work when typed in a group — they are admin tools, kept out of
the picker so drivers are not offered them.
"""
from aiogram import types

# DMs: the personal and admin surface. /report and /top are group-only handlers, so
# they are deliberately absent here.
_PRIVATE_COMMANDS = [
    types.BotCommand("start", "Open main menu"),
    types.BotCommand("help", "How to use this bot"),
    types.BotCommand("myid", "Show my Telegram user ID"),
]

# Driver groups. These exist to RECEIVE that unit's alerts, not to be queried, so the
# menu is just setup and an off switch. /report, /top, /event_list, /events, /enable and
# /removegroup still work when typed — they are kept out of the picker so drivers are
# not offered them.
_GROUP_COMMANDS = [
    types.BotCommand("start", "Open main menu"),
    types.BotCommand("setunit", "Set this group's unit number (e.g. /setunit 1234)"),
    types.BotCommand("disable", "Admin: mute this group's alerts"),
]


async def set_default_commands(dp):
    bot = dp.bot
    # Default scope is the fallback for any chat type not covered below; the private
    # list is the sane thing to fall back to.
    await bot.set_my_commands(_PRIVATE_COMMANDS, scope=types.BotCommandScopeDefault())
    await bot.set_my_commands(_PRIVATE_COMMANDS, scope=types.BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(_GROUP_COMMANDS, scope=types.BotCommandScopeAllGroupChats())
