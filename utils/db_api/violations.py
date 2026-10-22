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


async def update_violation_driver(event_id: int | None, driver_ref: str | None,
                                  driver_name: str | None, driver_source: str | None,
                                  on_duty: bool | None) -> None:
    """Record who was driving for an already-saved violation — what the month-end
    charge is billed against. No-op without an event_id (the row cannot be found)."""
    if event_id is None:
        return
    await db.execute(
        """
        UPDATE violations
        SET driver_ref = $2, driver_name = $3, driver_source = $4, on_duty = $5
        WHERE event_id = $1
        """,
        event_id,
        (driver_ref or None) and str(driver_ref)[:100],
        (driver_name or None) and str(driver_name)[:255],
        (driver_source or None) and str(driver_source)[:30],
        on_duty,
    )


async def update_violation_coaching_state(event_id: int | None, coaching_state: str | None) -> None:
    """Record the Samsara safety event's coachingState (or one of the stand-ins in
    utils.samsara.safety) on a violation. It decides whether the driver was told, whether
    the event counts toward their daily ordinal, and whether it is charged at month end."""
    if event_id is None:
        return
    await db.execute(
        "UPDATE violations SET coaching_state = $2 WHERE event_id = $1",
        event_id, (coaching_state or None) and str(coaching_state)[:30],
    )


def _coaching_clause() -> str:
    """Rows a Samsara coaching-state gate rejected do not count. NULL — Motive, and rows
    from before the gate — still does. Built from the closed constant, never from input."""
    from utils.samsara.safety import ALLOWED_COACHING_STATES
    allowed = ", ".join(f"'{s}'" for s in sorted(ALLOWED_COACHING_STATES))
    return f"(coaching_state IS NULL OR coaching_state IN ({allowed}))"


# How a driver is identified when counting their violations, best key first. A closed
# choice rather than an interpolated value, like _SOURCE_CLAUSES above.
#
# The NAME leads because it is the only key every source agrees on: driver_ref depends on
# who answered ("vitality:<uuid>", "motive:<id>", or none), so the same driver's events on
# one day can carry different refs, and counting by ref would call their 2nd event the
# 1st. The trade-off: two different drivers with the same full name in one company on the
# same day are counted as one. That is rare and accepted.
#
# The SQL expression and normalize_driver_name must agree: collapse whitespace, trim,
# lower-case. Python's .lower() (not casefold) is used because it matches Postgres lower()
# for the letters that differ between the two (casefold folds e.g. "ß" to "ss").
# The vehicle form compares normalized units (utils.unit_key.normalize_unit, in SQL), so
# the same truck reported as "T-120" by one provider and "T120" by another is one truck.
_DRIVER_KEY_CLAUSES = {
    "name":    r"lower(btrim(regexp_replace(driver_name, '\s+', ' ', 'g'))) = $5",
    "driver":  "driver_ref = $5",
    "vehicle": "regexp_replace(upper(vehicle_number), '[^0-9A-Z]', '', 'g') = $5",
}


def normalize_driver_name(name: str | None) -> str:
    """The cross-source key for a driver's name: whitespace collapsed and trimmed,
    lower-cased. "" when there is no usable name."""
    return " ".join((name or "").split()).lower()


async def count_driver_cell_phone_events(company_slug: str, driver_name: str | None,
                                         driver_ref: str | None, vehicle_number: str | None,
                                         window_start, before_ts,
                                         exclude_event_id: int | None) -> int:
    """cell_phone violations of this driver in [window_start, before_ts), not counting
    `exclude_event_id` (the event being notified about), nor events whose Samsara
    coaching state kept them from the driver (see _coaching_clause).

    The driver is identified by, in order: their name (normalized, so it matches whichever
    source identified them — see _DRIVER_KEY_CLAUSES for why and the trade-off), else
    driver_ref, else the truck, which counts whoever drove it in the window — the best
    available without a driver identity."""
    from utils.unit_key import normalize_unit

    name_key = normalize_driver_name(driver_name)
    if name_key:
        clause, key = _DRIVER_KEY_CLAUSES["name"], name_key
    elif driver_ref:
        clause, key = _DRIVER_KEY_CLAUSES["driver"], str(driver_ref)
    else:
        key = normalize_unit(vehicle_number)
        if not key:
            return 0
        clause = _DRIVER_KEY_CLAUSES["vehicle"]
    count = await db.fetchval(
        f"""
        SELECT COUNT(*) FROM violations
        WHERE company_slug = $1 AND event_type = 'cell_phone'
          AND occurred_at >= $2 AND occurred_at < $3
          AND event_id IS DISTINCT FROM $4
          AND {_coaching_clause()}
          AND {clause}
        """,
        company_slug, window_start, before_ts, exclude_event_id, key,
    )
    return int(count or 0)
