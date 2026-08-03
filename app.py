import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler

_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# Console (live tail) + rotating file (so HMAC 403s and other warnings survive a
# restart and can be grepped after the fact). 5 MB per file, 5 backups kept.
logging.basicConfig(
    level=logging.INFO,
    format=_LOG_FORMAT,
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            os.path.join(_LOG_DIR, "bot.log"),
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)

from aiogram import executor

from loader import dp, bot
import middlewares, filters, handlers
from utils.notify_admins import on_startup_notify
from utils.set_bot_commands import set_default_commands
from utils.webhook_handler import start_webhook_server, resume_pending_crash_confirmations
from utils.db_api import init_pool, close_pool
from utils.daily_report import schedule_daily_reports


async def on_startup(dispatcher):
    await init_pool()
    await set_default_commands(dispatcher)
    await on_startup_notify(dispatcher)
    await start_webhook_server(bot, port=8080)
    # Crash detections whose confirmation wait the last shutdown cut short.
    await resume_pending_crash_confirmations(bot)
    asyncio.create_task(schedule_daily_reports(bot))


async def on_shutdown(_):
    await close_pool()


if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown,
                           skip_updates=True, relax=0.5, timeout=60)
