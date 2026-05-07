from utils.db_api import db


async def get_company_name(slug: str) -> str | None:
    row = await db.fetchrow("SELECT name FROM companies WHERE slug = $1", slug)
    return row["name"] if row else None


async def get_all_companies() -> list[dict]:
    rows = await db.fetch("SELECT id, slug, name FROM companies ORDER BY id")
    return [dict(r) for r in rows]


async def get_accessible_companies(telegram_id: int) -> list[dict]:
    """Returns companies the admin can access. Super admins get all; regular admins get assigned ones only."""
    rows = await db.fetch(
        """
        SELECT c.id, c.slug, c.name
        FROM companies c
        WHERE EXISTS (
            SELECT 1 FROM admins a
            WHERE a.telegram_id = $1
              AND a.is_active = TRUE
              AND (
                  a.is_super = TRUE
                  OR EXISTS (
                      SELECT 1 FROM admin_companies ac
                      WHERE ac.admin_id = a.id AND ac.company_id = c.id
                  )
              )
        )
        ORDER BY c.id
        """,
        telegram_id,
    )
    return [dict(r) for r in rows]


async def get_company_by_group_id(telegram_group_id: int) -> dict | None:
    """Returns {id, slug, name} for the company that owns this group, or None."""
    row = await db.fetchrow(
        """
        SELECT c.id, c.slug, c.name
        FROM company_groups cg
        JOIN companies c ON c.id = cg.company_id
        WHERE cg.telegram_group_id = $1
        LIMIT 1
        """,
        telegram_group_id,
    )
    return dict(row) if row else None


async def get_company_groups(company_slug: str) -> list[int]:
    """Returns all telegram_group_ids configured for a company (ignores event type filter)."""
    rows = await db.fetch(
        """
        SELECT cg.telegram_group_id
        FROM company_groups cg
        JOIN companies c ON c.id = cg.company_id
        WHERE c.slug = $1
        """,
        company_slug,
    )
    return [r["telegram_group_id"] for r in rows]


async def update_group_chat_id(old_id: int, new_id: int) -> None:
    await db.execute(
        "UPDATE company_groups SET telegram_group_id = $1 WHERE telegram_group_id = $2",
        new_id, old_id,
    )


async def get_groups_for_event(company_slug: str, event_type: str) -> list[int]:
    """
    Returns telegram_group_ids that should receive this event type for the given company.
    - Company-specific groups: matched by slug, filtered by group_event_types if present.
    - Global groups (company_id IS NULL): matched for all companies, filtered by group_event_types if present.
    """
    rows = await db.fetch(
        """
        SELECT cg.telegram_group_id
        FROM company_groups cg
        LEFT JOIN companies c ON c.id = cg.company_id
        WHERE (c.slug = $1 OR cg.company_id IS NULL)
          AND (
              NOT EXISTS (
                  SELECT 1 FROM group_event_types WHERE group_id = cg.id
              )
              OR EXISTS (
                  SELECT 1 FROM group_event_types
                  WHERE group_id = cg.id AND event_type = $2
              )
          )
        """,
        company_slug, event_type,
    )
    return [r["telegram_group_id"] for r in rows]
