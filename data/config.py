from environs import Env

# environs kutubxonasidan foydalanish
env = Env()
env.read_env()

# ── Telegram ────────────────────────────────────────────────────────────────────
BOT_TOKEN = env.str("BOT_TOKEN")
# Bootstrap super-admin Telegram IDs. Everyone listed here is treated as a super
# admin (they can manage other admins). Additional regular admins are added at
# runtime through the Admin Management UI.
ADMINS = env.list("ADMINS")


def _id_set(raw) -> set[int]:
    """Parse a comma-separated env list of Telegram ids, dropping anything unparseable
    rather than failing startup over a stray comma."""
    ids: set[int] = set()
    for value in raw or []:
        try:
            ids.add(int(str(value).strip()))
        except (TypeError, ValueError):
            pass
    return ids


# The bootstrap ids above are the MAINTAINER's accounts, and they are hidden from the
# in-bot 👥 Admins panel. The company's own admins are the ones added at runtime through
# that panel; whoever set the deployment up is not part of the team it manages.
#
# This is a DISPLAY rule and nothing else. A hidden id keeps every ounce of its access:
# it stays a row in `admins`, still passes is_admin/is_super_admin, and still receives
# every alert and admin DM. What changes is that other admins don't see it in the panel
# and therefore cannot deactivate, remove, or transfer the super-admin role to it.
# A hidden admin looking at the panel sees everyone, themselves included.
#
# To make a bootstrap account visible again, drop it from ADMINS: seeding is
# ON CONFLICT DO NOTHING (see seed_super_admins), so the existing row — and its access —
# survives being removed from this list.
HIDDEN_ADMIN_IDS = _id_set(ADMINS)

# ── PostgreSQL ──────────────────────────────────────────────────────────────────
DATABASE_URL = env.str("DATABASE_URL")

# ── Single company this deployment serves ───────────────────────────────────────
# This build is a per-company template: one deployment == one company. Everything
# that used to live in the `companies` table is now supplied here via the .env file,
# so the same branch can be cloned and configured for any company (hf, mz-cargo, …)
# without code or shared-DB changes. See .env.example.
COMPANY_SLUG = env.str("COMPANY_SLUG")            # short id, e.g. "hf" — used in report filenames
COMPANY_NAME = env.str("COMPANY_NAME")            # display name, e.g. "HF Trucking"

# The company's single "main" Telegram group — the dispatcher/office chat that
# receives EVERY unit's alerts. Each driver's own group (auto-registered when the
# bot is added, keyed by the unit number parsed from its title/description) receives
# only that unit's alerts, on top of this one. Telegram group ids are negative.
# Leave blank/unset if there is no all-fleet main group. Read as a string first so an
# empty value (MAIN_GROUP_ID=) is treated as "unset" rather than crashing startup.
_main_group_raw = env.str("MAIN_GROUP_ID", "").strip()
MAIN_GROUP_ID = int(_main_group_raw) if _main_group_raw else None

# Minimum severity a speeding event must reach to be alerted ("low"/"medium"/"high"/"critical").
SPEEDING_MIN_SEVERITY = env.str("SPEEDING_MIN_SEVERITY", "high")

# ── Samsara (optional — leave blank if this company has no Samsara fleet) ────────
# API key is the bearer token used for the harsh-event poll callback; the webhook
# secret signs inbound Samsara webhooks (blank = skip signature verification).
SAMSARA_API_KEY = env.str("SAMSARA_API_KEY", "")
SAMSARA_WEBHOOK_SECRET = env.str("SAMSARA_WEBHOOK_SECRET", "")

# ── Motive / KeepTruckin (optional — leave blank to skip signature verification) ─
# Motive signs each webhook with HMAC-SHA1 over the raw body in X-KT-Webhook-Signature.
MOTIVE_WEBHOOK_SECRET = env.str("MOTIVE_WEBHOOK_SECRET", "")
# Read-only API token for this company's Motive org. Used to confirm a crash detection
# against /v2/driver_performance_events before alerting: Motive withdraws detections its
# review rejects, so absence there is what tells a false crash from a real one. Leave
# blank and crashes still alert, but flagged unconfirmed (see _motive_crash_is_real).
MOTIVE_API_KEY = env.str("MOTIVE_API_KEY", "")
