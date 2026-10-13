-- Fleet Alerts Bot — Database Schema (one company, one alerts group)
--
-- This deployment serves exactly ONE company and posts to ONE Telegram group; the
-- company's identity and provider credentials come from the environment (see
-- data/config.py), so there is no `companies` table and no table of groups here.
--
-- Every statement is CREATE ... IF NOT EXISTS, and the whole file runs on every boot.
-- A database that predates this schema — HF's and MZ CARGO's ran the multi-company one — still holds
-- `companies`, `company_groups`, `group_event_types` and `admin_companies`. Nothing here
-- drops or rewrites them: the group and its event filter are copied out once (see
-- utils/db_api/legacy.py) and the old tables are left exactly as they were, so the
-- previous build still runs against this database if a deploy has to be rolled back.

CREATE TABLE IF NOT EXISTS users (
    telegram_id   BIGINT       PRIMARY KEY,
    full_name     VARCHAR(255) NOT NULL,
    username      VARCHAR(255),
    language_code VARCHAR(10),
    created_at    TIMESTAMPTZ  DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  DEFAULT NOW()
);

-- crash_dm is the one DM that is ON by default: an admin who never opened Settings is
-- still told about a collision, in a chat it cannot scroll out of sight in. It is a
-- column rather than a seeded admin_subscriptions row so that turning it off survives a
-- restart.
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

-- The single Telegram group this deployment sends its alerts to.
--
-- A true singleton, not a table of groups: id is pinned to 1 by the CHECK constraint, so
-- there is exactly one row, ever. It is seeded on first boot (ensure_group) from the old
-- schema's company_groups row where there is one, else from GROUP_CHAT_ID, and from then
-- on the row — not the env var — is authoritative. That is what lets migrate_group()
-- repoint it when Telegram upgrades a basic group to a supergroup without the seed
-- re-inserting a stale duplicate.
CREATE TABLE IF NOT EXISTS alert_group (
    id                INT     PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    telegram_group_id BIGINT  NOT NULL,
    enabled           BOOLEAN NOT NULL DEFAULT TRUE
);

-- Which event types that group receives — an ALLOWLIST: no rows means every type.
--
-- Its own table rather than the old group_event_types, which a legacy database already
-- has under that name with a foreign key into company_groups. Reusing the name would
-- mean either writing rows the old key rejects or rewiring a table the previous build
-- still reads.
CREATE TABLE IF NOT EXISTS alert_group_event_types (
    event_type VARCHAR(50) PRIMARY KEY
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

-- Support relay: which message in a maintainer's DM belongs to which user.
--
-- Written when a message is relayed (header and forwarded copy both get a row), read
-- when the maintainer replies to one of them. Nothing else can supply the answer — a
-- forwarded message names its author only when that author allows it, and a reply
-- otherwise has no sender to route back to.
--
-- No foreign key to users: whoever wrote in may never have pressed /start, and a message
-- reaching a human matters more than the bookkeeping around it.
CREATE TABLE IF NOT EXISTS support_relays (
    admin_chat_id    BIGINT      NOT NULL,
    admin_msg_id     BIGINT      NOT NULL,
    user_telegram_id BIGINT      NOT NULL,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (admin_chat_id, admin_msg_id)
);
