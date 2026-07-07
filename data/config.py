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
