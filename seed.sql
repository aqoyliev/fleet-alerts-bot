-- One-time setup for HAULAGE FREIGHT LLC on the shared mz_cargo_alerts_bot DB.
-- Schema already exists in this DB — do NOT re-run schemas.sql.
-- Fill in the placeholder <<TELEGRAM_GROUP_ID>> and run:
--   psql "$DATABASE_URL" -f seed.sql
-- Akbar (telegram_id 8678782589) is the sole admin — already a super-admin in
-- the existing DB, so no admin_companies row is needed (super applies globally).

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

-- (Admins step skipped: Akbar is the sole admin and already global super-admin.)

COMMIT;
