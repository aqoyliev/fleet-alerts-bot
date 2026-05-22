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
    created_at              TIMESTAMPTZ  DEFAULT NOW()
);

-- Add Samsara columns to an existing companies table (CREATE TABLE IF NOT EXISTS
-- above is a no-op when the table already exists, so these run for upgrades).
ALTER TABLE companies ADD COLUMN IF NOT EXISTS samsara_api_key        VARCHAR(255);
ALTER TABLE companies ADD COLUMN IF NOT EXISTS samsara_webhook_secret VARCHAR(255);

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
    vehicle_number VARCHAR(50) NOT NULL,
    event_type   VARCHAR(50)  NOT NULL,
    event_id     BIGINT       UNIQUE,
    severity     VARCHAR(20),
    occurred_at  TIMESTAMPTZ  NOT NULL,
    created_at   TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS violations_company_occurred ON violations (company_slug, occurred_at);
CREATE INDEX IF NOT EXISTS violations_vehicle ON violations (vehicle_number);
