from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage

from data import config

bot = Bot(token=config.BOT_TOKEN, parse_mode=types.ParseMode.HTML, timeout=300)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Second bot, used only to post to drivers' own groups (utils/driver_alerts.py). It is
# send-only — no dispatcher polls it. None when DRIVER_ALERTS_BOT_TOKEN is unset.
driver_bot = (Bot(token=config.DRIVER_ALERTS_BOT_TOKEN, parse_mode=types.ParseMode.HTML, timeout=300)
              if config.DRIVER_ALERTS_BOT_TOKEN else None)
