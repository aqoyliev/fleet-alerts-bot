import os

# data.config reads these at import time; set dummy values so the real module loads
# during tests. The bot/DB are never contacted — the tests below only exercise pure
# parsing/formatting helpers and the Samsara poll loop (with a faked HTTP session).
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("ADMINS", "1")
os.environ.setdefault("ip", "127.0.0.1")
os.environ.setdefault("GROUP_CHAT_ID", "0")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
