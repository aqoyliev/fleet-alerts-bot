from utils.db_api import db


async def save_violation(vehicle_number: str, event_type: str,
                         event_id: int | None, occurred_at, severity: str | None = None) -> None:
    await db.execute(
        """
        INSERT INTO violations (vehicle_number, event_type, event_id, severity, occurred_at)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (event_id) DO NOTHING
        """,
        vehicle_number, event_type, event_id, severity, occurred_at,
    )


async def get_violations_by_type(since, until) -> list[dict]:
    """Returns (event_type, vehicle_number, count) for all violations in the window, ordered by type then count desc."""
    rows = await db.fetch(
        """
        SELECT event_type, vehicle_number, COUNT(*) AS total
        FROM violations
        WHERE occurred_at >= $1 AND occurred_at < $2
        GROUP BY event_type, vehicle_number
        ORDER BY event_type, total DESC
        """,
        since, until,
    )
    return [dict(r) for r in rows]


async def get_top_violators(since, until=None, event_type: str | None = None,
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
        WHERE occurred_at >= $1 AND occurred_at < $2 {type_clause}
        GROUP BY vehicle_number
        ORDER BY total DESC
        LIMIT $3
        """,
        since, until, limit,
    )
    return [dict(r) for r in rows]


async def get_vehicle_breakdown(vehicle_number: str, since,
                                event_type: str | None = None) -> list[dict]:
    """Returns violation counts per event type for a specific vehicle."""
    if event_type == "speeding":
        type_clause = "AND event_type = 'speeding'"
    elif event_type == "other":
        type_clause = "AND event_type != 'speeding'"
    else:
        type_clause = ""

    rows = await db.fetch(
        f"""
        SELECT event_type, COUNT(*) AS total
        FROM violations
        WHERE vehicle_number = $1 AND occurred_at >= $2 {type_clause}
        GROUP BY event_type
        ORDER BY total DESC
        """,
        vehicle_number, since,
    )
    return [dict(r) for r in rows]


async def get_vehicle_events(vehicle_number: str, since, until=None,
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
        WHERE vehicle_number = $1 AND occurred_at >= $2 AND occurred_at < $3
          {type_clause}
        ORDER BY occurred_at DESC
        """,
        vehicle_number, since, until,
    )
    return [dict(r) for r in rows]
