"""Command menus shown in Telegram's ☰ picker.

Menus are set per SCOPE rather than globally. A single global list would put the
private-chat commands into the group too, where a driver taps /help and gets admin text
meant for a DM. Telegram resolves the most specific scope first, so the group list below
is what's offered in the one configured group and the private list is what an admin sees
in a DM.

/start is absent from the group list on purpose: it opens the private main menu, which
is a DM concept and does nothing useful typed into a group.
"""
from aiogram import types

# DMs: the personal and admin surface. /report and /top are group-only handlers, so
# they are deliberately absent here.
_PRIVATE_COMMANDS = [
    types.BotCommand("start", "Open main menu"),
    types.BotCommand("help", "How to use this bot"),
]

# The one configured group.
_GROUP_COMMANDS = [
    types.BotCommand("help", "What this bot posts here"),
    types.BotCommand("report", "Yesterday's violations"),
    types.BotCommand("top", "Today's top violators"),
    types.BotCommand("disable", "Mute alerts in this group"),
    types.BotCommand("enable", "Turn alerts back on"),
]


async def set_default_commands(dp):
    bot = dp.bot
    # Default scope is the fallback for any chat type not covered below; the private
    # list is the sane thing to fall back to.
    await bot.set_my_commands(_PRIVATE_COMMANDS, scope=types.BotCommandScopeDefault())
    await bot.set_my_commands(_PRIVATE_COMMANDS, scope=types.BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(_GROUP_COMMANDS, scope=types.BotCommandScopeAllGroupChats())
