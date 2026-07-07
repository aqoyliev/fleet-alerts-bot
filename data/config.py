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

# Samsara API (optional — enriches speeding alerts that arrive without speed data)
SAMSARA_API_TOKEN = env.str("SAMSARA_API_KEY", "") or env.str("SAMSARA_API_TOKEN", "")
SAMSARA_API_URL = env.str("SAMSARA_API_URL", "https://api.samsara.com")
