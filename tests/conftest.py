import os

# data.config reads these at import time; set dummy values so the real module loads
# during tests. The bot/DB are never contacted — the tests below only exercise pure
# parsing/formatting helpers and the Samsara poll loop (with a faked HTTP session).
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("ADMINS", "1")
os.environ.setdefault("DATABASE_URL", "postgresql://u:p@localhost/db")
os.environ.setdefault("COMPANY_SLUG", "testco")
os.environ.setdefault("COMPANY_NAME", "Test Co")
# Optional in production (blank just hides the panel button), but set here so the
# keyboard test has a URL to assert on.
os.environ.setdefault("WEBAPP_URL", "https://example.test")

# Everything that rules how a company's alerts behave is pinned to "not set". One branch
# runs several companies and these are what tell them apart, so a developer's own .env —
# which config.py reads, and which is some real company's — must not decide what the
# suite asserts. Blank is the baseline every test starts from: no provider configured
# (Samsara's wording, no provider line on a card), the core event list, speeding from
# "medium". A test about any other arrangement sets it up itself.
for _name in ("SAMSARA_API_KEY", "SAMSARA_WEBHOOK_SECRET", "MOTIVE_API_KEY",
              "MOTIVE_WEBHOOK_SECRET", "EXTRA_EVENT_TYPES", "SPEEDING_MIN_SEVERITY",
              "GROUP_CHAT_ID", "ALLOW_UNSIGNED_WEBHOOKS"):
    os.environ[_name] = ""
