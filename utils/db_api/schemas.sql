-- Fleet Alerts Bot — Database Schema (single-group build)
--
-- This deployment serves exactly ONE company and ONE Telegram group; its identity,
-- provider credentials and destination group come from the .env file (see
-- data/config.py), so there is no `companies` table and no per-vehicle group table
-- here. Everything below is scoped implicitly to that one company and that one group.

CREATE TABLE IF NOT EXISTS users (
    telegram_id   BIGINT       PRIMARY KEY,
    full_name     VARCHAR(255) NOT NULL,
    username      VARCHAR(255),
    language_code VARCHAR(10),
    created_at    TIMESTAMPTZ  DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  DEFAULT NOW()
);

-- crash_dm is the one notification that is ON by default: crashes never go to a group,
-- so an admin's DM is the only place a crash is ever reported, and the cost of a missed
-- one is not symmetric with the cost of an unwanted one. It is a column rather than a
-- seeded admin_subscriptions row so that turning it off survives a restart.
CREATE TABLE IF NOT EXISTS admins (
    id          SERIAL      PRIMARY KEY,
    telegram_id BIGINT      UNIQUE NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    is_super    BOOLEAN     DEFAULT FALSE,
    added_by    BIGINT      REFERENCES users(telegram_id),
    is_active   BOOLEAN     DEFAULT TRUE,
    crash_dm    BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Per-admin personal DM subscriptions — an ALLOWLIST: a type is delivered only if listed
-- (event_type = 'all' subscribes to every type). Crash is the exception and is not
-- represented here at all; it lives in admins.crash_dm because it defaults to ON.
CREATE TABLE IF NOT EXISTS admin_subscriptions (
    admin_id   INT         NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,
    PRIMARY KEY (admin_id, event_type)
);

-- The single Telegram group this deployment sends every alert to (config.GROUP_CHAT_ID).
--
-- A true singleton, not a table of groups: id is pinned to 1 by the CHECK constraint, so
-- there is exactly one row, ever. It's seeded from GROUP_CHAT_ID on first boot
-- (ensure_group) and from then on the row — not the env var — is authoritative, which is
-- what lets migrate_group() repoint it when Telegram upgrades a basic group to a
-- supergroup without the seed re-inserting a stale duplicate.
CREATE TABLE IF NOT EXISTS alert_group (
    id                INT     PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    telegram_group_id BIGINT  NOT NULL,
    enabled           BOOLEAN NOT NULL DEFAULT TRUE
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
