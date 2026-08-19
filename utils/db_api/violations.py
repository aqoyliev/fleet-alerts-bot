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


# ── admin panel ─────────────────────────────────────────────────────────────────
# The queries below back the Mini App. They are separate from the ones above rather than
# generalizations of them because the bot's reports answer "what did this unit do" and the
# panel answers "what is the fleet doing" — widening the existing ones would put an
# optional vehicle filter on queries whose whole purpose is that the filter is required.

async def get_totals(since, until) -> dict:
    """The dashboard's headline numbers, in one round trip rather than four."""
    row = await db.fetchrow(
        """
        SELECT COUNT(*)                                         AS total,
               COUNT(*) FILTER (WHERE event_type =  'speeding') AS speeding,
               COUNT(*) FILTER (WHERE event_type =  'crash')    AS crashes,
               COUNT(DISTINCT vehicle_number)                   AS units
        FROM violations
        WHERE occurred_at >= $1 AND occurred_at < $2
        """,
        since, until,
    )
    return dict(row) if row else {"total": 0, "speeding": 0, "crashes": 0, "units": 0}


async def get_type_counts(since, until) -> list[dict]:
    """Fleet-wide count per event type, for the dashboard's bar list.

    get_violations_by_type is per (type, vehicle) because the daily digest needs that
    breakdown. Rolling it up here instead of in Python means the chart doesn't ship one
    row per vehicle to draw sixteen bars.
    """
    rows = await db.fetch(
        """
        SELECT event_type, COUNT(*) AS total
        FROM violations
        WHERE occurred_at >= $1 AND occurred_at < $2
        GROUP BY event_type
        ORDER BY total DESC
        """,
        since, until,
    )
    return [dict(r) for r in rows]


async def get_daily_counts(since, until) -> list[dict]:
    """Per-day totals for the dashboard trend, with quiet days present as zeroes.

    generate_series is what puts the zeroes in, and it matters: a week with nothing on
    Sunday would otherwise draw six bars under a label saying seven days, which reads as
    a quiet week rather than as one day with no alerts.

    The join carries the occurred_at range as well as the date equality. The range is the
    half an index can use (violations_occurred, schemas.sql:75); without it the date
    expression alone would scan the whole table every time the dashboard is opened.

    Days are bucketed in America/New_York because every other window in this bot is
    (utils/daily_report.py, handlers/users/violations.py). A chart bucketed in UTC would
    quietly disagree with the numbers the same fleet reads in its /report message.
    """
    rows = await db.fetch(
        """
        SELECT d::date AS day, COUNT(v.id) AS total
        FROM generate_series(
                 ($1 AT TIME ZONE 'America/New_York')::date,
                 (($2 AT TIME ZONE 'America/New_York') - interval '1 microsecond')::date,
                 interval '1 day') AS d
        LEFT JOIN violations v
               ON v.occurred_at >= $1 AND v.occurred_at < $2
              AND (v.occurred_at AT TIME ZONE 'America/New_York')::date = d::date
        GROUP BY d
        ORDER BY d
        """,
        since, until,
    )
    return [dict(r) for r in rows]


async def get_recent_events(limit: int = 50, before_ts=None, before_id: int | None = None,
                            event_type: str | None = None,
                            vehicle_number: str | None = None) -> list[dict]:
    """The fleet-wide alert feed, newest first.

    Paging is a keyset on (occurred_at, id), not LIMIT/OFFSET. The feed is append-heavy:
    with an OFFSET, every alert that arrives while a dispatcher reads page 1 shifts the
    window, and page 2 silently skips exactly that many rows. Comparing the tuple rather
    than occurred_at alone is what stops two events sharing a timestamp from losing one.

    Filters are bound parameters, never interpolated — unlike the closed-set type_clause
    above, these values come from a browser.
    """
    rows = await db.fetch(
        """
        SELECT id, vehicle_number, event_type, severity, occurred_at
        FROM violations
        WHERE ($1::timestamptz IS NULL OR (occurred_at, id) < ($1, $2::bigint))
          AND ($3::text IS NULL OR event_type = $3)
          AND ($4::text IS NULL OR vehicle_number = $4)
        ORDER BY occurred_at DESC, id DESC
        LIMIT $5
        """,
        before_ts, before_id, event_type, vehicle_number, limit,
    )
    return [dict(r) for r in rows]


async def get_counts_by_vehicle(since) -> dict[str, int]:
    """Alerts per unit since `since`, for the count on each row of the groups list.

    Returned as a plain mapping rather than joined onto alert_groups: the two tables are
    tied only by a bare vehicle_number string with no foreign key between them, so the
    join would be an outer one against an unconstrained column. The caller attaches the
    counts in Python, where a missing unit is simply zero.
    """
    rows = await db.fetch(
        """
        SELECT vehicle_number, COUNT(*) AS total
        FROM violations
        WHERE occurred_at >= $1
        GROUP BY vehicle_number
        """,
        since,
    )
    return {r["vehicle_number"]: r["total"] for r in rows}


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
