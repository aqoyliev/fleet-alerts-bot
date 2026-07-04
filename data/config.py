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

# ── PostgreSQL ──────────────────────────────────────────────────────────────────
DATABASE_URL = env.str("DATABASE_URL")

# ── Single company this deployment serves ───────────────────────────────────────
# This build is a per-company template: one deployment == one company. Everything
# that used to live in the `companies` table is now supplied here via the .env file,
# so the same branch can be cloned and configured for any company (hf, mz-cargo, …)
# without code or shared-DB changes. See .env.example.
COMPANY_SLUG = env.str("COMPANY_SLUG")            # short id, e.g. "hf" — used in report filenames
COMPANY_NAME = env.str("COMPANY_NAME")            # display name, e.g. "HF Trucking"

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
