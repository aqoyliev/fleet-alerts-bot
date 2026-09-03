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

# The one Telegram group this deployment talks to. There is no per-unit routing here
# (that's the single-company branch this was forked from) — every event, every
# vehicle, every provider, goes to this one chat. Telegram group ids are negative.
#
# This value only SEEDS the group on first boot (see ensure_group in
# utils/db_api/groups.py). From then on the database is authoritative, so the bot can
# self-heal when Telegram upgrades a basic group to a supergroup (which changes its
# chat id) without losing track of where alerts go. Changing this env var after the
# first boot has no effect — update the `alert_group` row instead if the group ever
# needs to be repointed by hand.
GROUP_CHAT_ID = env.int("GROUP_CHAT_ID")

# Minimum severity a speeding event must reach to be alerted ("low"/"medium"/"high"/"critical").
SPEEDING_MIN_SEVERITY = env.str("SPEEDING_MIN_SEVERITY", "high")

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
