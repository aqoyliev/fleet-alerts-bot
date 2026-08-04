-- Fleet Alerts Bot — Database Schema (single-company build)
--
-- This deployment serves exactly ONE company; its identity and provider
-- credentials come from the .env file (see data/config.py), so there is no
-- `companies` table here. Everything below is scoped implicitly to that one
-- company.

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

-- Per-admin personal DM subscriptions. event_type = 'all' subscribes to every type.
CREATE TABLE IF NOT EXISTS admin_subscriptions (
    admin_id   INT         NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,
    PRIMARY KEY (admin_id, event_type)
);

-- Telegram groups that receive this company's alerts.
--
-- vehicle_number ties a group to a single unit: driver groups (auto-registered when
-- the bot is added, parsed from the group title/description) carry the unit number and
-- receive only that unit's alerts. The main group (config.MAIN_GROUP_ID) has a NULL
-- vehicle_number and receives every unit's alerts.
CREATE TABLE IF NOT EXISTS alert_groups (
    id                SERIAL      PRIMARY KEY,
    telegram_group_id BIGINT      NOT NULL UNIQUE,
    label             VARCHAR(100),
    title             VARCHAR(255),
    vehicle_number    VARCHAR(50),
    enabled           BOOLEAN     DEFAULT TRUE,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS alert_groups_vehicle ON alert_groups (vehicle_number);

-- Optional per-group event-type filter. A group with no rows here receives every type.
CREATE TABLE IF NOT EXISTS group_event_types (
    group_id   INT         NOT NULL REFERENCES alert_groups(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,
    PRIMARY KEY (group_id, event_type)
);

CREATE TABLE IF NOT EXISTS violations (
    id             BIGSERIAL    PRIMARY KEY,
    vehicle_number VARCHAR(50)  NOT NULL,
    event_type     VARCHAR(50)  NOT NULL,
    event_id       BIGINT       UNIQUE,
    severity       VARCHAR(20),
    occurred_at    TIMESTAMPTZ  NOT NULL,
    created_at     TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS violations_occurred ON violations (occurred_at);
CREATE INDEX IF NOT EXISTS violations_vehicle ON violations (vehicle_number);

-- Audit trail for the Motive crash confirmation wait (see utils/db_api/crash_confirmations.py).
--
-- A crash detection is held ~3 minutes before Motive's API is asked whether it still
-- stands. That wait is a fire-and-forget task, so a row is written before it starts and
-- the verdict filled in after: a row still NULL at startup is a wait a restart cut
-- short, and gets resumed. The verdict tally is also the only evidence that the gate is
-- catching false detections rather than sitting idle.
-- payload is the event as received, so the resume can re-run the normal alert path.
CREATE TABLE IF NOT EXISTS motive_crash_confirmations (
    event_id     BIGINT       PRIMARY KEY,
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
