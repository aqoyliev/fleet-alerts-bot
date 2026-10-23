import os
import re

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
# Companies whose cell_phone events driver-alerts acts on: comma list of slugs, any case,
# e.g. "jrd". Empty = every company. Others are answered 202 and dropped untouched.
DRIVER_ALERTS_COMPANIES = [s.strip().lower() for s in env.list("DRIVER_ALERTS_COMPANIES", [])
                           if s.strip()]
# The Vitality driver's name not in the group (title or its pti group_drivers): "skip"
# (default — nothing is sent, skip_reason name_mismatch) or "send" (send anyway, logged).
DRIVER_ALERTS_NAME_MISMATCH = env.str("DRIVER_ALERTS_NAME_MISMATCH", "skip").strip().lower()

# pti-checker-bot's own Postgres, one per company, read (read-only) for the live truck ->
# driver group mapping: PTI_DATABASE_URL_<SLUG>, the slug upper-cased with anything not
# a letter or digit as "_", e.g. PTI_DATABASE_URL_JRD. A company with one is routed
# only from it (utils/pti_groups.py); without one, from the imported driver_groups.
PTI_DATABASE_URL_PREFIX = "PTI_DATABASE_URL_"


def pti_database_url(company_slug: str | None) -> str:
    """The company's pti-checker-bot database URL, "" when not configured."""
    suffix = re.sub(r"[^0-9A-Z]", "_", (company_slug or "").strip().upper())
    if not suffix:
        return ""
    return os.environ.get(PTI_DATABASE_URL_PREFIX + suffix, "").strip()


def slug_env_suffix(company_slug: str | None) -> str:
    """The slug as a variable-name suffix: upper-cased, anything not A-Z/0-9 as "_"."""
    return re.sub(r"[^0-9A-Z]", "_", (company_slug or "").strip().upper())


# The userbot (utils/userbot.py, utils/bot_join.py): a Telegram user account that adds
# the driver bot to a driver group the bot is missing from, or asks HOS / the admin.
# USERBOT_ENABLED is the kill switch (default off: the whole userbot path is skipped).
# USERBOT_SESSION is a Telethon StringSession — a login, keep it secret, and never use
# it from two places at once (Telegram may revoke it: AUTH_KEY_DUPLICATED).
# USERBOT_USER_ID: the userbot account's own user id, where the driver bot sends the
# "could not add" notices; empty = read once from Telegram (get_me).
USERBOT_ENABLED = env.bool("USERBOT_ENABLED", False)
USERBOT_SESSION = env.str("USERBOT_SESSION", "")
USERBOT_API_ID = env.str("USERBOT_API_ID", "")
USERBOT_API_HASH = env.str("USERBOT_API_HASH", "")
USERBOT_USER_ID = env.str("USERBOT_USER_ID", "")
# HOS staff the userbot asks to add it to a group it is not in: HOS_USERNAMES_<SLUG>
# (slug as for PTI_DATABASE_URL_<SLUG>), a comma list of Telegram usernames, "@" optional,
# e.g. HOS_USERNAMES_JRD=@hos_anna,@hos_bek. Empty/unset = the admin is told instead.
HOS_USERNAMES_PREFIX = "HOS_USERNAMES_"


def hos_usernames(company_slug: str | None) -> list[str]:
    """The company's HOS usernames, without "@", in the configured order."""
    suffix = slug_env_suffix(company_slug)
    if not suffix:
        return []
    raw = os.environ.get(HOS_USERNAMES_PREFIX + suffix, "")
    return [u.strip().lstrip("@").strip() for u in raw.split(",") if u.strip().lstrip("@").strip()]


# The driver alerts run in their own Railway service (driver_service.py). web forwards
# each cell_phone event to it at DRIVER_ALERTS_FORWARD_URL over the private network,
# e.g. http://driver-alerts.railway.internal:8080/events, with the shared secret in the
# X-Forward-Secret header (set the same value on both services). Empty URL on web = no
# forwarding (feature off); driver-alerts refuses to start with an empty secret. The
# DRIVER_ALERTS_* settings above matter only in the driver-alerts service.
DRIVER_ALERTS_FORWARD_URL = env.str("DRIVER_ALERTS_FORWARD_URL", "")
DRIVER_ALERTS_FORWARD_SECRET = env.str("DRIVER_ALERTS_FORWARD_SECRET", "")

# Vitality ELD (DriveHOS Global Integration API v2): who is driving a truck, for the
# driver alerts. One provider key for this bot (rate-limited, shared with the ELD
# anomaly bot); each carrier's own key is companies.vitality_company_key.
# Empty provider key = Vitality is not queried and the event's driver is used.
VITALITY_PROVIDER_KEY = env.str("VITALITY_PROVIDER_KEY", "")
VITALITY_BASE_URL = env.str("VITALITY_BASE_URL", "https://api.drivehos.app")
