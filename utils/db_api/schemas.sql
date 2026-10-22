-- Motive Alerts Bot — Database Schema

CREATE TABLE IF NOT EXISTS companies (
    id                      SERIAL PRIMARY KEY,
    slug                    VARCHAR(50)  UNIQUE NOT NULL,
    name                    VARCHAR(255) NOT NULL,
    speeding_min_severity   VARCHAR(20)  NOT NULL DEFAULT 'high',
    -- Per-company Samsara credentials. NULL = company has no Samsara fleet.
    -- API key is the bearer token used for the harsh-event poll callback;
    -- webhook secret signs inbound Samsara webhooks (NULL = skip verification).
    samsara_api_key         VARCHAR(255),
    samsara_webhook_secret  VARCHAR(255),
    -- Per-company Motive (née KeepTruckin) webhook signing secret. NULL = skip
    -- verification. Motive signs with HMAC-SHA1 over the raw body in
    -- X-KT-Webhook-Signature (NOT SHA256, and NOT an X-Motive-* header).
    motive_webhook_secret   VARCHAR(255),
    -- Per-company Motive API key (x-api-key), used to confirm crash detections
    -- against /v2/driver_performance_events. Each company is a separate Motive org,
    -- so this cannot be a single global key. NULL = crashes are sent unconfirmed.
    motive_api_key          VARCHAR(255),
    -- Per-company Vitality ELD (DriveHOS) company key (X-API-Company-Key), used to look
    -- up who is driving a truck for the driver alerts. The provider key is global
    -- (VITALITY_PROVIDER_KEY). NULL = the driver is taken from the event.
    vitality_company_key    VARCHAR(255),
    created_at              TIMESTAMPTZ  DEFAULT NOW()
);

-- Add Samsara/Motive columns to an existing companies table (CREATE TABLE IF NOT
-- EXISTS above is a no-op when the table already exists, so these run for upgrades).
ALTER TABLE companies ADD COLUMN IF NOT EXISTS samsara_api_key        VARCHAR(255);
ALTER TABLE companies ADD COLUMN IF NOT EXISTS samsara_webhook_secret VARCHAR(255);
ALTER TABLE companies ADD COLUMN IF NOT EXISTS motive_webhook_secret  VARCHAR(255);
ALTER TABLE companies ADD COLUMN IF NOT EXISTS motive_api_key         VARCHAR(255);
ALTER TABLE companies ADD COLUMN IF NOT EXISTS vitality_company_key   VARCHAR(255);

CREATE TABLE IF NOT EXISTS company_groups (
    id                SERIAL PRIMARY KEY,
    company_id        INT         REFERENCES companies(id) ON DELETE CASCADE,  -- NULL = all companies
    telegram_group_id BIGINT      NOT NULL,
    label             VARCHAR(100),
    -- Which alert provider this group receives: 'motive', 'samsara', or NULL = all.
    -- Companies with a Samsara fleet get two groups: the original one restricted to
    -- 'motive' and a new one restricted to 'samsara'.
    alert_source      VARCHAR(20),
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE company_groups ADD COLUMN IF NOT EXISTS alert_source VARCHAR(20);

CREATE TABLE IF NOT EXISTS group_event_types (
    group_id   INT         NOT NULL REFERENCES company_groups(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,
    PRIMARY KEY (group_id, event_type)
);

CREATE TABLE IF NOT EXISTS users (
    telegram_id   BIGINT       PRIMARY KEY,
    full_name     VARCHAR(255) NOT NULL,
    username      VARCHAR(255),
    language_code VARCHAR(10),
    created_at    TIMESTAMPTZ  DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admins (
    id          SERIAL      PRIMARY KEY,
    telegram_id BIGINT      UNIQUE NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    is_super    BOOLEAN     DEFAULT FALSE,
    added_by    BIGINT      REFERENCES users(telegram_id),
    is_active   BOOLEAN     DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS admin_companies (
    admin_id   INT NOT NULL REFERENCES admins(id)    ON DELETE CASCADE,
    company_id INT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    PRIMARY KEY (admin_id, company_id)
);

CREATE TABLE IF NOT EXISTS admin_subscriptions (
    admin_id   INT         NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,
    PRIMARY KEY (admin_id, event_type)
);

CREATE TABLE IF NOT EXISTS violations (
    id           BIGSERIAL    PRIMARY KEY,
    company_slug VARCHAR(50)  NOT NULL,
    vehicle_number VARCHAR(50) NOT NULL,
    event_type   VARCHAR(50)  NOT NULL,
    event_id     BIGINT       UNIQUE,
    severity     VARCHAR(20),
    occurred_at  TIMESTAMPTZ  NOT NULL,
    created_at   TIMESTAMPTZ  DEFAULT NOW(),
    -- Which provider reported it: 'motive' or 'samsara'. A company with both fleets has
    -- one group per provider (company_groups.alert_source), and a report run in one of
    -- them must count only that provider's events.
    -- NULL means the row predates this column. Those are read as Motive, which is what
    -- they overwhelmingly are: Samsara was wired up per company well after Motive.
    source       VARCHAR(20)
);

ALTER TABLE violations ADD COLUMN IF NOT EXISTS source VARCHAR(20);

CREATE INDEX IF NOT EXISTS violations_company_occurred ON violations (company_slug, occurred_at);
CREATE INDEX IF NOT EXISTS violations_vehicle ON violations (vehicle_number);

-- Motive crash detections put through the confirmation wait in _motive_crash_is_real.
-- The row is written BEFORE the wait and the verdict filled in after, which buys two
-- things the in-memory task cannot:
--   * an audit trail. A quiet crash channel on its own cannot distinguish a gate that
--     is catching withdrawals from a Motive detector that stopped tripping; the
--     verdict tally can.
--   * recoverability. The wait runs in a fire-and-forget task, so a deploy inside it
--     used to drop the alert silently. A row still verdict IS NULL at startup is
--     exactly that case, and gets resumed.
-- payload is the event as received, so the resume can re-run the normal alert path.
CREATE TABLE IF NOT EXISTS motive_crash_confirmations (
    event_id     BIGINT       PRIMARY KEY,
    company_slug VARCHAR(50)  NOT NULL,
    payload      JSONB        NOT NULL,
    -- NULL = still waiting. Otherwise confirmed / withdrawn / unknown / expired.
    verdict      VARCHAR(20),
    detected_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    decided_at   TIMESTAMPTZ
);

-- Startup reads only the undecided rows, which are near-always zero; a partial index
-- keeps that lookup off the full table.
CREATE INDEX IF NOT EXISTS motive_crash_confirmations_pending
    ON motive_crash_confirmations (detected_at) WHERE verdict IS NULL;

-- ── Driver notifications (cell phone use) ───────────────────────────────────────
-- A cell_phone event is also sent to the driver: to the truck's own driver Telegram
-- group (through the separate driver-alerts bot) and by email. Everything below is
-- additive, and utils/db_api/driver_groups.ensure_driver_schema() re-applies the same
-- statements at runtime, because this file is applied by hand.

-- Which Telegram group belongs to which truck. unit_key is utils.unit_key.normalize_unit
-- of the unit: provider vehicle names and the imported groups list spell units
-- differently ("T-120" / "t120 "), so lookups compare the normalized key only.
-- unit_number keeps the spelling it was imported with, for display.
CREATE TABLE IF NOT EXISTS driver_groups (
    company_slug      VARCHAR(50)  NOT NULL,
    unit_key          VARCHAR(50)  NOT NULL,
    unit_number       VARCHAR(50),
    telegram_group_id BIGINT       NOT NULL,
    -- Cleared when the driver bot is kicked from the group (or the group is gone);
    -- re-importing the mapping sets it again.
    is_active         BOOLEAN      DEFAULT TRUE,
    created_at        TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (company_slug, unit_key)
);

CREATE INDEX IF NOT EXISTS driver_groups_tgid ON driver_groups (telegram_group_id);

-- Who was driving, so the company can charge the driver at month end. All nullable:
-- rows written before this existed, and events whose driver could not be resolved,
-- carry none of it. driver_ref is the stable id (provider or QuickManage); driver_source
-- says where it came from ('event', 'samsara', 'motive', 'quickmanage', 'unknown').
ALTER TABLE violations ADD COLUMN IF NOT EXISTS driver_ref    VARCHAR(100);
ALTER TABLE violations ADD COLUMN IF NOT EXISTS driver_name   VARCHAR(255);
ALTER TABLE violations ADD COLUMN IF NOT EXISTS driver_source VARCHAR(30);
ALTER TABLE violations ADD COLUMN IF NOT EXISTS on_duty       BOOLEAN;

CREATE INDEX IF NOT EXISTS violations_driver_ref ON violations (company_slug, driver_ref);

-- One row per event the driver was notified about. The row is claimed BEFORE anything
-- is sent (INSERT ... ON CONFLICT DO NOTHING), so a redelivered webhook neither sends
-- twice nor counts twice. ordinal = this driver's Nth cell_phone event in the calendar
-- month (America/New_York); 2nd and later carry the charge warning.
CREATE TABLE IF NOT EXISTS driver_notifications (
    event_id       BIGINT       PRIMARY KEY,
    company_slug   VARCHAR(50)  NOT NULL,
    vehicle_number VARCHAR(50),
    driver_ref     VARCHAR(100),
    ordinal        INT,
    sent_group     BOOLEAN      NOT NULL DEFAULT FALSE,
    sent_email     BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ  DEFAULT NOW()
);

-- Samsara coaching-state gate. A Samsara cell_phone event reaches the driver only while
-- its Samsara safety event's coachingState is needsReview / needsCoaching /
-- needsRecognition. coaching_state is that value, or a stand-in when Samsara gave none:
-- 'lookup_pending' (waiting; left behind if a restart cut the wait), 'not_found',
-- 'lookup_error'. NULL = not looked up (Motive, or written before this existed). Any
-- non-NULL value outside the allowed three is neither counted nor charged.
-- skip_reason says why a claimed notification was not sent; NULL = it was (or is pending).
ALTER TABLE violations ADD COLUMN IF NOT EXISTS coaching_state VARCHAR(30);
ALTER TABLE driver_notifications ADD COLUMN IF NOT EXISTS coaching_state VARCHAR(30);
ALTER TABLE driver_notifications ADD COLUMN IF NOT EXISTS skip_reason    VARCHAR(50);

-- QuickManage carrier id, for looking up the driver of a truck. NULL = not linked.
ALTER TABLE companies ADD COLUMN IF NOT EXISTS quickmanage_carrier_id VARCHAR(100);
