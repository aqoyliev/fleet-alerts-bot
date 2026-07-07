from utils.db_api import db


async def save_violation(company_slug: str, vehicle_number: str, event_type: str,
                         event_id: int | None, occurred_at, severity: str | None = None) -> None:
    await db.execute(
        """
        INSERT INTO violations (company_slug, vehicle_number, event_type, event_id, severity, occurred_at)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (event_id) DO NOTHING
        """,
        company_slug, vehicle_number, event_type, event_id, severity, occurred_at,
    )


async def get_violations_by_type(company_slug: str, since, until) -> list[dict]:
    """Returns (event_type, vehicle_number, count) for all violations in the window, ordered by type then count desc."""
    rows = await db.fetch(
        """
        SELECT event_type, vehicle_number, COUNT(*) AS total
        FROM violations
        WHERE company_slug = $1 AND occurred_at >= $2 AND occurred_at < $3
        GROUP BY event_type, vehicle_number
        ORDER BY event_type, total DESC
        """,
        company_slug, since, until,
    )
    return [dict(r) for r in rows]


async def get_top_violators(company_slug: str, since, until=None, event_type: str | None = None,
                             limit: int = 10) -> list[dict]:
    """Returns top vehicles ranked by violation count.
    event_type=None → all, 'speeding' → speeding only, 'other' → all except speeding.
    """
    from datetime import datetime, timezone
    if until is None:
        until = datetime.now(tz=timezone.utc)

    if event_type == "speeding":
        type_clause = "AND event_type = 'speeding'"
    elif event_type == "other":
        type_clause = "AND event_type != 'speeding'"
    else:
        type_clause = ""

    rows = await db.fetch(
        f"""
        SELECT vehicle_number, COUNT(*) AS total
        FROM violations
        WHERE company_slug = $1 AND occurred_at >= $2 AND occurred_at < $3 {type_clause}
        GROUP BY vehicle_number
        ORDER BY total DESC
        LIMIT $4
        """,
        company_slug, since, until, limit,
    )
    return [dict(r) for r in rows]


async def get_vehicle_breakdown(company_slug: str, vehicle_number: str, since,
                                event_type: str | None = None) -> list[dict]:
    """Returns violation counts per event type for a specific vehicle."""
    if event_type == "speeding":
        rows = await db.fetch(
            """
            SELECT event_type, COUNT(*) AS total
            FROM violations
            WHERE company_slug = $1 AND vehicle_number = $2 AND occurred_at >= $3
              AND event_type = 'speeding'
            GROUP BY event_type
            ORDER BY total DESC
            """,
            company_slug, vehicle_number, since,
        )
    elif event_type == "other":
        rows = await db.fetch(
            """
            SELECT event_type, COUNT(*) AS total
            FROM violations
            WHERE company_slug = $1 AND vehicle_number = $2 AND occurred_at >= $3
              AND event_type != 'speeding'
            GROUP BY event_type
            ORDER BY total DESC
            """,
            company_slug, vehicle_number, since,
        )
    else:
        rows = await db.fetch(
            """
            SELECT event_type, COUNT(*) AS total
            FROM violations
            WHERE company_slug = $1 AND vehicle_number = $2 AND occurred_at >= $3
            GROUP BY event_type
            ORDER BY total DESC
            """,
            company_slug, vehicle_number, since,
        )
    return [dict(r) for r in rows]


async def get_vehicle_events(company_slug: str, vehicle_number: str, since, until=None,
                             event_type: str | None = None) -> list[dict]:
    """Returns individual events with timestamps for a vehicle."""
    from datetime import datetime, timezone
    if until is None:
        until = datetime.now(tz=timezone.utc)

    if event_type == "speeding":
        type_clause = "AND event_type = 'speeding'"
    elif event_type == "other":
        type_clause = "AND event_type != 'speeding'"
    else:
        type_clause = ""

    rows = await db.fetch(
        f"""
        SELECT event_type, occurred_at, severity FROM violations
        WHERE company_slug = $1 AND vehicle_number = $2 AND occurred_at >= $3 AND occurred_at < $4
          {type_clause}
        ORDER BY occurred_at DESC
        """,
        company_slug, vehicle_number, since, until,
    )
    return [dict(r) for r in rows]


async def get_top_violators_all_companies(since) -> list[dict]:
    """Returns top 5 violators per company for daily auto-report."""
    rows = await db.fetch(
        """
        SELECT company_slug, vehicle_number, COUNT(*) AS total
        FROM violations
        WHERE occurred_at >= $1
        GROUP BY company_slug, vehicle_number
        ORDER BY company_slug, total DESC
        """,
        since,
    )
    return [dict(r) for r in rows]
