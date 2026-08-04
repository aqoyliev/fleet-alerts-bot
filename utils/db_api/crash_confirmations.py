"""Durable record of the Motive crash confirmation wait.

_motive_crash_is_real holds a crash detection for _CRASH_CONFIRM_DELAY before asking
Motive's API whether it still stands. That wait lives in a fire-and-forget task, so
without a row on disk two things are invisible:

  * whether the gate is doing anything. Railway keeps hours of logs, and "no crash
    alerts went out" cannot by itself tell a gate that is catching withdrawals apart
    from a Motive detector that simply stopped tripping. The verdict tally can.
  * a crash held mid-wait when the process restarts. It used to vanish — the one place
    the design failed closed rather than open.

A row is written before the wait and its verdict filled in after, so anything still
undecided at startup is a wait that was interrupted.

This is the single-company build: every row belongs to config.COMPANY_SLUG, so unlike
the multi-company original there is no company column to scope by.
"""
import json

from utils.db_api import db

VERDICT_CONFIRMED = "confirmed"  # Motive's API still lists it — a real crash
VERDICT_WITHDRAWN = "withdrawn"  # gone from the API — downgraded, no crash alert
VERDICT_UNKNOWN = "unknown"      # no API key or the lookup failed — sent unconfirmed
VERDICT_EXPIRED = "expired"      # found pending at startup but too stale to alert on


async def record_pending(event_id: int | None, payload: dict) -> None:
    """Note that a crash has entered the confirmation wait.

    A redelivery that gets past the in-memory dedup lands on the same event_id and is
    left alone: it runs its own confirmation, and whichever finishes last writes the
    verdict. They are asking the same API about the same detection, so they agree."""
    if event_id is None:
        return
    await db.execute(
        """
        INSERT INTO motive_crash_confirmations (event_id, payload)
        VALUES ($1, $2::jsonb)
        ON CONFLICT (event_id) DO NOTHING
        """,
        event_id, json.dumps(payload, default=str),
    )


async def record_verdict(event_id: int | None, verdict: str) -> None:
    """Close out a pending confirmation. No-op if the row was never written."""
    if event_id is None:
        return
    await db.execute(
        """
        UPDATE motive_crash_confirmations
        SET verdict = $2, decided_at = NOW()
        WHERE event_id = $1
        """,
        event_id, verdict,
    )


async def get_pending_confirmations() -> list[dict]:
    """Confirmations that never reached a verdict — i.e. interrupted by a restart."""
    rows = await db.fetch(
        """
        SELECT event_id, payload, detected_at
        FROM motive_crash_confirmations
        WHERE verdict IS NULL
        ORDER BY detected_at
        """
    )
    return [dict(r) for r in rows]


async def get_verdict_counts(since) -> dict[str, int]:
    """Verdict tally since `since` — the evidence that the gate is live."""
    rows = await db.fetch(
        """
        SELECT verdict, COUNT(*) AS total
        FROM motive_crash_confirmations
        WHERE detected_at >= $1 AND verdict IS NOT NULL
        GROUP BY verdict
        """,
        since,
    )
    return {r["verdict"]: r["total"] for r in rows}
