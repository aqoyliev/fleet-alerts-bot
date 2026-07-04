import os

# data.config reads these at import time; set dummy values so the real module loads
# during tests. The bot/DB are never contacted — the tests below only exercise pure
# parsing/formatting helpers and the Samsara poll loop (with a faked HTTP session).
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("ADMINS", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
os.environ.setdefault("COMPANY_SLUG", "testco")
os.environ.setdefault("COMPANY_NAME", "Test Co")
