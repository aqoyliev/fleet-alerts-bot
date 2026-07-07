from utils.db_api import db


async def is_admin(telegram_id: int, company_id: int | None = None) -> bool:
    """Returns True if user is a super admin, or an active admin for the given company."""
    row = await db.fetchrow(
        "SELECT id, is_super, is_active FROM admins WHERE telegram_id = $1",
        telegram_id,
    )
    if not row or not row["is_active"]:
        return False
    if row["is_super"]:
        return True
    if company_id is None:
        return True
    exists = await db.fetchval(
        "SELECT 1 FROM admin_companies WHERE admin_id = $1 AND company_id = $2",
        row["id"], company_id,
    )
    return exists is not None


async def is_super_admin(telegram_id: int) -> bool:
    row = await db.fetchrow(
        "SELECT is_super, is_active FROM admins WHERE telegram_id = $1",
        telegram_id,
    )
    return bool(row and row["is_active"] and row["is_super"])


async def add_admin(telegram_id: int, added_by: int | None = None, is_super: bool = False) -> int:
    """Creates an admin record. User must already exist in users table. Returns admin id."""
    return await db.fetchval(
        """
        INSERT INTO admins (telegram_id, added_by, is_super)
        VALUES ($1, $2, $3)
        ON CONFLICT (telegram_id) DO UPDATE SET is_active = TRUE
        RETURNING id
        """,
        telegram_id, added_by, is_super,
    )


async def assign_company(admin_id: int, company_id: int):
    await db.execute(
        "INSERT INTO admin_companies (admin_id, company_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
        admin_id, company_id,
    )


async def get_subscribed_admins(event_type: str, company_slug: str) -> list[int]:
    """Returns telegram_ids of active admins who want a DM for this event type
    and have access to the given company (super admins get all companies)."""
    rows = await db.fetch(
        """
        SELECT a.telegram_id
        FROM admin_subscriptions sub
        JOIN admins a ON a.id = sub.admin_id
        JOIN companies c ON c.slug = $2
        WHERE a.is_active = TRUE
          AND (sub.event_type = $1 OR sub.event_type = 'all')
          AND (
              a.is_super = TRUE
              OR EXISTS (
                  SELECT 1 FROM admin_companies ac
                  WHERE ac.admin_id = a.id AND ac.company_id = c.id
              )
          )
        """,
        event_type, company_slug,
    )
    return [r["telegram_id"] for r in rows]


async def get_all_admins() -> list[dict]:
    """Returns all admins joined with user info, ordered by creation date."""
    rows = await db.fetch(
        """
        SELECT a.id, a.telegram_id, a.is_super, a.is_active, a.created_at,
               u.full_name, u.username
        FROM admins a
        JOIN users u ON u.telegram_id = a.telegram_id
        ORDER BY a.created_at
        """
    )
    return [dict(r) for r in rows]


async def get_admin_by_id(admin_id: int) -> dict | None:
    """Returns a single admin with user info, or None if not found."""
    row = await db.fetchrow(
        """
        SELECT a.id, a.telegram_id, a.is_super, a.is_active, a.created_at,
               u.full_name, u.username
        FROM admins a
        JOIN users u ON u.telegram_id = a.telegram_id
        WHERE a.id = $1
        """,
        admin_id,
    )
    return dict(row) if row else None


async def get_admin_companies(admin_id: int) -> list[int]:
    """Returns list of company_ids the admin has been explicitly assigned to."""
    rows = await db.fetch(
        "SELECT company_id FROM admin_companies WHERE admin_id = $1",
        admin_id,
    )
    return [r["company_id"] for r in rows]


async def set_admin_active(admin_id: int, is_active: bool) -> None:
    """Activate or deactivate an admin."""
    await db.execute(
        "UPDATE admins SET is_active = $2 WHERE id = $1",
        admin_id, is_active,
    )


async def delete_admin(admin_id: int) -> None:
    """Permanently remove an admin record."""
    await db.execute("DELETE FROM admins WHERE id = $1", admin_id)


async def revoke_company(admin_id: int, company_id: int) -> None:
    """Remove an admin's access to a specific company."""
    await db.execute(
        "DELETE FROM admin_companies WHERE admin_id = $1 AND company_id = $2",
        admin_id, company_id,
    )


async def get_admin_subscriptions(telegram_id: int) -> list[str]:
    """Returns list of event_types the admin is subscribed to for personal DMs."""
    rows = await db.fetch(
        """
        SELECT sub.event_type
        FROM admin_subscriptions sub
        JOIN admins a ON a.id = sub.admin_id
        WHERE a.telegram_id = $1
        """,
        telegram_id,
    )
    return [r["event_type"] for r in rows]


async def toggle_subscription(telegram_id: int, event_type: str) -> None:
    """Toggle a personal DM subscription for an event type. Adds if absent, removes if present."""
    admin_id = await db.fetchval("SELECT id FROM admins WHERE telegram_id = $1", telegram_id)
    exists = await db.fetchval(
        "SELECT 1 FROM admin_subscriptions WHERE admin_id = $1 AND event_type = $2",
        admin_id, event_type,
    )
    if exists:
        await db.execute(
            "DELETE FROM admin_subscriptions WHERE admin_id = $1 AND event_type = $2",
            admin_id, event_type,
        )
    else:
        await db.execute(
            "INSERT INTO admin_subscriptions (admin_id, event_type) VALUES ($1, $2)",
            admin_id, event_type,
        )
