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


async def get_company_slug_by_group(telegram_group_id: int) -> str | None:
    row = await db.fetchrow(
        """
        SELECT c.slug FROM companies c
        JOIN company_groups cg ON cg.company_id = c.id
        WHERE cg.telegram_group_id = $1
        """,
        telegram_group_id,
    )
    return row["slug"] if row else None


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


async def get_samsara_credentials(slug: str) -> tuple[str | None, str | None]:
    """Returns (samsara_api_key, samsara_webhook_secret) for the company, or (None, None).

    Each company is a separate Samsara organization, so the API token (for the
    harsh-event poll) and the webhook signing secret are stored per-company rather
    than as a single global key."""
    row = await db.fetchrow(
        "SELECT samsara_api_key, samsara_webhook_secret FROM companies WHERE slug = $1",
        slug,
    )
    if not row:
        return None, None
    return row["samsara_api_key"], row["samsara_webhook_secret"]


async def get_speeding_min_severity(slug: str) -> str:
    """Returns the minimum severity level for speeding alerts (e.g., 'high', 'medium', 'critical')."""
    row = await db.fetchrow("SELECT speeding_min_severity FROM companies WHERE slug = $1", slug)
    return row["speeding_min_severity"] if row else "high"


async def get_group_event_types(telegram_group_id: int) -> list[str]:
    """Returns the list of event types configured for a group. Empty list = all types allowed."""
    rows = await db.fetch(
        """
        SELECT get.event_type
        FROM group_event_types get
        JOIN company_groups cg ON cg.id = get.group_id
        WHERE cg.telegram_group_id = $1
        ORDER BY get.event_type
        """,
        telegram_group_id,
    )
    return [r["event_type"] for r in rows]
