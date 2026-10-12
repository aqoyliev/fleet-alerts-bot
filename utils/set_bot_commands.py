"""Command menus shown in Telegram's ☰ picker.

Menus are set per SCOPE rather than globally. A single global list would put the DM's
/start into the group, where it does nothing, and leave the group without the two
commands it actually uses. Telegram resolves the most specific scope first, so the group
list below is what the alerts group is offered and the private list is what an admin
sees in a DM.

Hiding a command does NOT disable it. /disable and /enable work when a bot admin types
them in the group — they are kept out of the picker so that everyone else in the chat is
not offered a switch they cannot use.
"""
from aiogram import types

_REPORT_COMMANDS = [
    types.BotCommand("report", "Violations report: today / yesterday"),
    types.BotCommand("top", "Top N violators today (default 10)"),
]

# DMs: the personal and admin surface.
_PRIVATE_COMMANDS = [
    types.BotCommand("start", "Open main menu"),
    types.BotCommand("help", "How to use this bot"),
    *_REPORT_COMMANDS,
]

# The alerts group. /start is absent on purpose: it opens the private main menu, which
# is a DM concept and does nothing useful typed into a group.
_GROUP_COMMANDS = [
    types.BotCommand("help", "What this bot posts here"),
    *_REPORT_COMMANDS,
]


async def set_default_commands(dp):
    bot = dp.bot
    # Default scope is the fallback for any chat type not covered below; the private
    # list is the sane thing to fall back to.
    await bot.set_my_commands(_PRIVATE_COMMANDS, scope=types.BotCommandScopeDefault())
    await bot.set_my_commands(_PRIVATE_COMMANDS, scope=types.BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(_GROUP_COMMANDS, scope=types.BotCommandScopeAllGroupChats())
