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

# Resend (driver evidence emails)
RESEND_API_KEY = env.str("RESEND_API_KEY", "")
EMAIL_FROM = env.str("EMAIL_FROM", "")

# Driver alerts: a second bot that posts cell-phone-use evidence to each truck's driver
# group. Empty token = the feature is off. DRY_RUN logs what would be sent instead.
DRIVER_ALERTS_BOT_TOKEN = env.str("DRIVER_ALERTS_BOT_TOKEN", "")
DRIVER_ALERTS_DRY_RUN = env.bool("DRIVER_ALERTS_DRY_RUN", False)
# Go-live floor for the daily count: events before it are never counted, so a driver's
# first notice after a mid-day switch-on is not already a charge. ISO date (2026-11-01,
# midnight New York) or ISO datetime (naive = New York). Empty = count from day start.
DRIVER_ALERTS_COUNT_FROM = env.str("DRIVER_ALERTS_COUNT_FROM", "")

# Vitality ELD (DriveHOS Global Integration API v2): who is driving a truck, for the
# driver alerts. One provider key for this bot (rate-limited, shared with the ELD
# anomaly bot); each carrier's own key is companies.vitality_company_key.
# Empty provider key = Vitality is not queried and the event's driver is used.
VITALITY_PROVIDER_KEY = env.str("VITALITY_PROVIDER_KEY", "")
VITALITY_BASE_URL = env.str("VITALITY_BASE_URL", "https://api.drivehos.app")
