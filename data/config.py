from environs import Env

# environs kutubxonasidan foydalanish
env = Env()
env.read_env()

# .env fayl ichidan quyidagilarni o'qiymiz
BOT_TOKEN = env.str("BOT_TOKEN")
ADMINS = env.list("ADMINS")
IP = env.str("ip")

# Telegram group to send alerts
GROUP_CHAT_ID = env.int("GROUP_CHAT_ID")

# PostgreSQL
DATABASE_URL = env.str("DATABASE_URL")

# Driver cell-phone alerts run in their own Railway service (driver_service.py on the
# feature/driver-cell-phone-alerts branch). web forwards each cell_phone event to it at
# DRIVER_ALERTS_FORWARD_URL over the private network, e.g.
# http://driver-alerts.railway.internal:8080/events, with the shared secret in the
# X-Forward-Secret header (same value on both services). Empty URL = no forwarding.
DRIVER_ALERTS_FORWARD_URL = env.str("DRIVER_ALERTS_FORWARD_URL", "")
DRIVER_ALERTS_FORWARD_SECRET = env.str("DRIVER_ALERTS_FORWARD_SECRET", "")
