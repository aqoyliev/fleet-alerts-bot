from utils.db_api import db


async def is_admin(telegram_id: int) -> bool:
    """Returns True if the user is an active admin (super or regular)."""
    row = await db.fetchrow(
        "SELECT is_active FROM admins WHERE telegram_id = $1",
        telegram_id,
    )
    return bool(row and row["is_active"])


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


async def transfer_super_admin(current_telegram_id: int, target_admin_id: int) -> None:
    """Move super-admin status from the current holder to another admin, atomically:
    promote the target (and ensure it's active) and demote the current super to a
    regular admin. Done in one transaction so there is never a moment with no super."""
    async with db.pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE admins SET is_super = TRUE, is_active = TRUE WHERE id = $1",
                target_admin_id,
            )
            await conn.execute(
                "UPDATE admins SET is_super = FALSE WHERE telegram_id = $1",
                current_telegram_id,
            )


async def get_subscribed_admins(event_type: str) -> list[int]:
    """Returns telegram_ids of active admins who want a personal DM for this event type."""
    rows = await db.fetch(
        """
        SELECT a.telegram_id
        FROM admin_subscriptions sub
        JOIN admins a ON a.id = sub.admin_id
        WHERE a.is_active = TRUE
          AND (sub.event_type = $1 OR sub.event_type = 'all')
        """,
        event_type,
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


async def set_admin_active(admin_id: int, is_active: bool) -> None:
    """Activate or deactivate an admin."""
    await db.execute(
        "UPDATE admins SET is_active = $2 WHERE id = $1",
        admin_id, is_active,
    )


async def delete_admin(admin_id: int) -> None:
    """Permanently remove an admin record."""
    await db.execute("DELETE FROM admins WHERE id = $1", admin_id)


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
