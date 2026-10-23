import logging

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

    import asyncio
    asyncio.ensure_future(schedule_daily_reports(bot))


async def on_shutdown(_):
    await close_pool()
    # The executor closes the main bot's session itself; the driver bot is not attached
    # to any dispatcher, so its session is closed here the same way.
    from loader import driver_bot
    if driver_bot is not None:
        session = await driver_bot.get_session()
        await session.close()
    try:
        from utils import email_sender
        await email_sender.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Could not close the email session: {e}")
    try:
        from utils import driver_forward
        await driver_forward.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Could not close the driver-forward session: {e}")
    try:
        from utils import vitality
        await vitality.close()
    except Exception as e:
        logging.getLogger(__name__).warning(f"Could not close the Vitality session: {e}")


if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown,
                           skip_updates=True, relax=0.5, timeout=60)
