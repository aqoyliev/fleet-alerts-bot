import asyncio
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from aiogram import executor

from loader import dp, bot
import middlewares, filters, handlers
from utils.notify_admins import on_startup_notify
from utils.set_bot_commands import set_default_commands
from utils.webhook_handler import start_webhook_server
from utils.db_api import init_pool, close_pool
from utils.daily_report import schedule_daily_reports


async def on_startup(dispatcher):
    await init_pool()
    await set_default_commands(dispatcher)
    await on_startup_notify(dispatcher)
    await start_webhook_server(bot, port=int(os.environ.get("PORT", 8081)))
    asyncio.create_task(schedule_daily_reports(bot))


async def on_shutdown(_):
    await close_pool()


if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown,
                           skip_updates=True, relax=0.5, timeout=60)
