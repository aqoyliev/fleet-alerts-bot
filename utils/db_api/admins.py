from data import config
from utils.db_api import db


def is_maintainer(telegram_id: int | None) -> bool:
    """Is this id one of the deployment's owner accounts (config.ADMINS)?

    Two things follow from it, and they are separate: the account is hidden from the
    👥 Admins panel, and it is permanently a super admin. The second is what stops a
    maintainer from being locked out of their own deployment by the panel — see
    is_super_admin below.

    Hiding is about visibility only, never permission. Nothing in this module filters a
    maintainer out of a query: they stay in get_all_admins() so alerts and admin DMs keep
    reaching them, and the panel is the only place that hides them.
    """
    try:
        return int(telegram_id) in config.HIDDEN_ADMIN_IDS
    except (TypeError, ValueError):
        return False


def visible_admins(admins: list[dict], viewer_telegram_id: int) -> list[dict]:
    """The admin rows `viewer_telegram_id` is allowed to see in the panel.

    A maintainer sees the real, unfiltered list — otherwise they could not see their own
    account or manage the team they are hiding from.
    """
    if is_maintainer(viewer_telegram_id):
        return list(admins)
    return [a for a in admins if not is_maintainer(a["telegram_id"])]


async def is_admin(telegram_id: int) -> bool:
    """Returns True if the user is an active admin (super or regular)."""
    if is_maintainer(telegram_id):
        return True
    row = await db.fetchrow(
        "SELECT is_active FROM admins WHERE telegram_id = $1",
        telegram_id,
    )
    return bool(row and row["is_active"])


async def is_super_admin(telegram_id: int) -> bool:
    """Returns True for an active super admin — and always for a maintainer.

    The config override is not a convenience. Every route out of the super-admin role
    runs through this panel, and one of them (transfer) demotes whoever uses it: a
    maintainer who hands the role to a customer loses their own management access, with
    no way back that doesn't involve a direct DB write. Deriving the answer from
    config.ADMINS instead of the row makes that unlosable.
    """
    if is_maintainer(telegram_id):
        return True
    row = await db.fetchrow(
        "SELECT is_super, is_active FROM admins WHERE telegram_id = $1",
        telegram_id,
    )
    return bool(row and row["is_active"] and row["is_super"])


async def seed_super_admins(telegram_ids: list[int]) -> None:
    """Ensure every maintainer id from config.ADMINS is an active super admin.

    Runs on startup so a brand-new deployment has a working super admin without any
    manual DB step, and so an existing one is put back the way config says it should be.

    This used to be ON CONFLICT DO NOTHING, on the reasoning that a super admin who
    stepped down via transfer shouldn't be re-promoted by a restart. That reasoning
    belonged to the old meaning of ADMINS, when it was a generic bootstrap list that
    might name the customer's own people. It now names the maintainer, for whom stepping
    down is not a thing that should be possible — and leaving the row demoted only made
    the DB disagree with is_super_admin().
    """
    from utils.db_api.users import ensure_user
    for tid in telegram_ids:
        await ensure_user(tid)
        await db.execute(
            "INSERT INTO admins (telegram_id, is_super, is_active) VALUES ($1, TRUE, TRUE) "
            "ON CONFLICT (telegram_id) DO UPDATE SET is_super = TRUE, is_active = TRUE",
            tid,
        )


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


async def promote_to_super(admin_id: int) -> None:
    """Make an existing admin a super admin, leaving whoever promoted them super too.

    This is the difference from transfer_super_admin, which hands the role over and
    demotes the current holder. A company needs a second super admin without its
    maintainer stepping down, so promotion had to stop costing the promoter their access.
    """
    await db.execute(
        "UPDATE admins SET is_super = TRUE WHERE id = $1", admin_id
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
