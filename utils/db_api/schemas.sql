-- Motive Alerts Bot — Database Schema

CREATE TABLE IF NOT EXISTS companies (
    id         SERIAL PRIMARY KEY,
    slug       VARCHAR(50)  UNIQUE NOT NULL,
    name       VARCHAR(255) NOT NULL,
    created_at TIMESTAMPTZ  DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS company_groups (
    id                SERIAL PRIMARY KEY,
    company_id        INT         REFERENCES companies(id) ON DELETE CASCADE,  -- NULL = all companies
    telegram_group_id BIGINT      NOT NULL,
    label             VARCHAR(100),
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

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
    vehicle_number VARCHAR(100) NOT NULL,
    event_type   VARCHAR(50)  NOT NULL,
    event_id     BIGINT       UNIQUE,
    severity     VARCHAR(20),
    occurred_at  TIMESTAMPTZ  NOT NULL,
    created_at   TIMESTAMPTZ  DEFAULT NOW()
);

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
