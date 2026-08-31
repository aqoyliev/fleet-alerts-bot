from utils.db_api import db


# Restricting a report to one provider's events. A company with both fleets has one group
# per provider (company_groups.alert_source), and a report run inside one of them must
# count only what that group receives — otherwise the Samsara group reports Motive's
# numbers, which is what it did before this existed.
#
# 'motive' deliberately takes NULL too: rows written before the source column existed
# carry no provider, and reading them as Motive is right far more often than dropping
# them. 'samsara' is strict for the mirror-image reason — a NULL row is not evidence of a
# Samsara event, so counting one would be a wrong number rather than a missing one.
#
# A closed lookup rather than an interpolated value: the clause reaches an f-string, and
# nothing that came from outside this dict may.
_SOURCE_CLAUSES = {
    "motive":  "AND (source = 'motive' OR source IS NULL)",
    "samsara": "AND source = 'samsara'",
}


def _source_clause(source: str | None) -> str:
    """SQL fragment limiting a query to one provider. Unknown/None → no restriction."""
    return _SOURCE_CLAUSES.get(source or "", "")


async def save_violation(company_slug: str, vehicle_number: str, event_type: str,
                         event_id: int | None, occurred_at, severity: str | None = None,
                         source: str | None = None) -> None:
    await db.execute(
        """
        INSERT INTO violations (company_slug, vehicle_number, event_type, event_id, severity,
                                occurred_at, source)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT (event_id) DO NOTHING
        """,
        company_slug, vehicle_number, event_type, event_id, severity, occurred_at, source,
    )


async def get_violations_by_type(company_slug: str, since, until,
                                 source: str | None = None) -> list[dict]:
    """Returns (event_type, vehicle_number, count) for all violations in the window, ordered by type then count desc.

    `source` limits the count to one provider; None counts both."""
    rows = await db.fetch(
        f"""
        SELECT event_type, vehicle_number, COUNT(*) AS total
        FROM violations
        WHERE company_slug = $1 AND occurred_at >= $2 AND occurred_at < $3
          {_source_clause(source)}
        GROUP BY event_type, vehicle_number
        ORDER BY event_type, total DESC
        """,
        company_slug, since, until,
    )
    return [dict(r) for r in rows]


async def get_top_violators(company_slug: str, since, until=None, event_type: str | None = None,
                             limit: int = 10, source: str | None = None) -> list[dict]:
    """Returns top vehicles ranked by violation count.
    event_type=None → all, 'speeding' → speeding only, 'other' → all except speeding.
    `source` limits the ranking to one provider; None ranks over both.
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
          {_source_clause(source)}
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
