from utils.db_api import db


async def ensure_user(telegram_id: int, full_name: str | None = None,
                      username: str | None = None) -> None:
    """Make sure a users row exists so admins can reference it (FK from admins).

    Used when adding an admin from a forwarded message / contact / raw id, possibly
    before that person has ever opened the bot. Inserts a minimal row when absent
    (placeholder name if we don't know it). On an existing row, real values refresh
    it — but a missing name/username never clobbers what's already stored."""
    await db.execute(
        """
        INSERT INTO users (telegram_id, full_name, username)
        VALUES ($1, $2, $3)
        ON CONFLICT (telegram_id) DO UPDATE
            SET full_name  = COALESCE($4, users.full_name),
                username   = COALESCE($3, users.username),
                updated_at = NOW()
        """,
        telegram_id, full_name or f"User {telegram_id}", username, full_name,
    )


async def upsert_user(telegram_id: int, full_name: str, username: str | None, language_code: str | None):
    await db.execute(
        """
        INSERT INTO users (telegram_id, full_name, username, language_code)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (telegram_id) DO UPDATE
            SET full_name     = EXCLUDED.full_name,
                username      = EXCLUDED.username,
                language_code = EXCLUDED.language_code,
                updated_at    = NOW()
        """,
        telegram_id, full_name, username, language_code,
    )
