-- One-time setup for HAULAGE FREIGHT LLC on a fresh Railway Postgres DB.
-- Run schema first, then this seed:
--   psql "$DATABASE_URL" -f utils/db_api/schemas.sql
--   psql "$DATABASE_URL" -f seed.sql
-- Akbar (telegram_id 8678782589) is the sole admin and gets the super flag,
-- which grants global access across any future companies in this DB.

BEGIN;

-- 1. Company row
INSERT INTO companies (slug, name)
VALUES ('hf', 'HAULAGE FREIGHT LLC')
ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name;

-- 2. Main Telegram group for this company
INSERT INTO company_groups (company_id, telegram_group_id, label)
SELECT id, -5231861833::BIGINT, 'main'
FROM companies WHERE slug = 'hf'
ON CONFLICT DO NOTHING;

-- 3. Event-type filter for that group (8 supported Samsara types)
WITH g AS (
    SELECT cg.id
    FROM company_groups cg
    JOIN companies c ON c.id = cg.company_id
    WHERE c.slug = 'hf' AND cg.label = 'main'
)
INSERT INTO group_event_types (group_id, event_type)
SELECT g.id, t.event_type
FROM g, (VALUES
    ('hard_brake'),
    ('harsh_acceleration'),
    ('harsh_turn'),
    ('cell_phone'),
    ('drowsy_driving'),
    ('no_seat_belt'),
    ('speeding'),
    ('crash'),
    ('stop_sign_violation'),
    ('forward_collision_warning')
) AS t(event_type)
ON CONFLICT DO NOTHING;

-- 4. Sole admin: Akbar (super, so future companies inherit access automatically)
INSERT INTO users (telegram_id, full_name, username)
VALUES (8678782589::BIGINT, 'Akbar', NULL)
ON CONFLICT (telegram_id) DO NOTHING;

INSERT INTO admins (telegram_id, is_super, is_active)
VALUES (8678782589::BIGINT, TRUE, TRUE)
ON CONFLICT (telegram_id) DO UPDATE SET is_super = TRUE, is_active = TRUE;

COMMIT;
