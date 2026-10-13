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

# ── The company this deployment serves ──────────────────────────────────────────
# One deployment == one company, and this branch is MZ CARGO's. Everything that used to
# live in the `companies` table is supplied here instead. Both values default to MZ's
# own so that the deployment that already exists keeps booting with the variables it
# already has — set them only to run this branch for somebody else.
COMPANY_SLUG = env.str("COMPANY_SLUG", "mzcargo")         # short id, used in report filenames
# The display name, on every report and in the panel's header. Left unset, a deployment
# upgraded from the old schema shows the name its `companies` row has always carried
# (see adopt_legacy_company in utils/db_api/legacy.py) and only a fresh database falls
# back to the default below; setting the variable overrules both.
_company_name_raw = env.str("COMPANY_NAME", "").strip()
COMPANY_NAME = _company_name_raw or "MZ CARGO INC"
COMPANY_NAME_FROM_ENV = bool(_company_name_raw)

# The one Telegram group this deployment posts to. There is no per-unit routing on this
# branch — that is single-company, which this was taken from — so every vehicle's alerts
# go to this one chat. Telegram group ids are negative.
#
# This value only SEEDS the group, and only when the database has nothing better: a
# deployment upgraded from the old multi-company schema carries its group over from
# `company_groups` (see ensure_group in utils/db_api/groups.py), and from the first boot
# on the `alert_group` row is authoritative. That is what lets the bot follow the chat
# when Telegram upgrades it to a supergroup and its id changes. Blank, or 0 as the old
# config defaulted it, means "not set".
_group_raw = env.str("GROUP_CHAT_ID", "").strip()
try:
    GROUP_CHAT_ID = int(_group_raw) or None
except ValueError:
    GROUP_CHAT_ID = None

# Minimum severity a speeding event must reach to be alerted ("low"/"medium"/"high"/"critical").
# "medium" is MZ's rule and has been since before this was a setting.
SPEEDING_MIN_SEVERITY = env.str("SPEEDING_MIN_SEVERITY", "medium")


# ── Admin Mini App ──────────────────────────────────────────────────────────────

def _resolve_webapp_url(explicit: str, railway_domain: str) -> str:
    """Where this deployment is reachable from a phone — the base URL the admin panel is
    served from and the one the 🖥 Admin Panel keyboard button points at.

    Railway publishes the service's own public hostname into the container as
    RAILWAY_PUBLIC_DOMAIN (a bare host, no scheme), so on Railway this needs no
    configuration at all. That is worth reading rather than asking for: it is the single
    value the panel requires, it is the one thing the process genuinely cannot infer about
    itself, and a deployment where somebody forgot to paste it is a panel with no way in.

    An explicit WEBAPP_URL still wins, because a custom domain — or anything sitting in
    front of the service — is a fact only the operator knows.

    Blank remains a supported state and means "serve the panel but don't advertise it":
    the routes still answer, the keyboard button simply isn't offered. That keeps a
    deployment with no public domain booting normally instead of pinning a broken URL
    onto every admin's keyboard.
    """
    if explicit:
        return explicit.rstrip("/")
    if railway_domain:
        host = railway_domain.rstrip("/")
        # Railway hands over a bare hostname, but tolerate one that already carries a
        # scheme rather than producing "https://https://…".
        return host if host.startswith(("http://", "https://")) else f"https://{host}"
    return ""


WEBAPP_URL = _resolve_webapp_url(
    env.str("WEBAPP_URL", "").strip(),
    env.str("RAILWAY_PUBLIC_DOMAIN", "").strip(),
)

# MZ CARGO runs both providers: part of the fleet reports through Samsara and part
# through Motive, into the same group. Each needs its own pair of values below.

# ── Samsara ─────────────────────────────────────────────────────────────────────
# API key is the bearer token used for the harsh-event poll callback; the webhook
# secret signs inbound Samsara webhooks.
SAMSARA_API_KEY = env.str("SAMSARA_API_KEY", "")
SAMSARA_WEBHOOK_SECRET = env.str("SAMSARA_WEBHOOK_SECRET", "")

# ── Motive / KeepTruckin ────────────────────────────────────────────────────────
# Motive signs each webhook with HMAC-SHA1 over the raw body in X-KT-Webhook-Signature.
MOTIVE_WEBHOOK_SECRET = env.str("MOTIVE_WEBHOOK_SECRET", "")
# Read-only API token for a Motive org. Two jobs:
#   * confirming a crash detection before alerting -- Motive withdraws the detections its
#     review rejects, so absence there is what tells a false crash from a real one; and
#   * fetching the dashcam clip, which is not ready when the webhook fires.
# Without it every crash goes out marked "Unconfirmed" (the gate fails open) and no
# Motive alert ever gets its clip.
MOTIVE_API_KEY = env.str("MOTIVE_API_KEY", "")

# ── Webhook endpoint hardening ──────────────────────────────────────────────────
# Both webhook URLs are public, and an unsigned one accepts whatever is posted to it:
# anyone who guesses the host can invent a crash and put it in front of every admin.
# So a missing provider secret now refuses the delivery instead of trusting it.
#
# The escape hatch is for a provider that genuinely cannot sign (or a first hour of
# bring-up before the secret is pasted in): set ALLOW_UNSIGNED_WEBHOOKS=true and every
# unsigned delivery is accepted again, loudly, one warning per request.
ALLOW_UNSIGNED_WEBHOOKS = env.str("ALLOW_UNSIGNED_WEBHOOKS", "").strip().lower() in {
    "1", "true", "yes", "on",
}

# How far a Samsara delivery's own timestamp may be from ours before it is refused.
# The signature covers that timestamp, so this is what stops a captured-and-replayed
# delivery from being accepted again days later.
SAMSARA_MAX_SKEW_SECONDS = 300

# Port for the aiohttp server that serves the webhooks and the admin panel. Railway and
# Heroku both hand the app its port in $PORT; 8080 is only the local default, and
# hard-coding it means a platform that picks a different port gets no server at all.
WEBHOOK_PORT = env.int("PORT", 8080)
