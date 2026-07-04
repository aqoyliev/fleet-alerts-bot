from aiogram import types


async def set_default_commands(dp):
    await dp.bot.set_my_commands(
        [
            types.BotCommand("start", "Open main menu"),
            types.BotCommand("help", "How to use this bot"),
            types.BotCommand("myid", "Show my Telegram user ID"),
            types.BotCommand("report", "Violations report: today / yesterday"),
            types.BotCommand("top", "Top N violators today (default 10)"),
            types.BotCommand("event_list", "Show event types this group receives"),
            types.BotCommand("setunit", "Set this group's unit number (e.g. /setunit 1234)"),
            types.BotCommand("events", "Admin: choose event types this group receives"),
        ]
    )
