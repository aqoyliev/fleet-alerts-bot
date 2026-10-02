from datetime import date

from utils.db_api import db


async def claim_pti_run(day: date) -> bool:
    """Try to take ownership of one morning's PTI reminder. True means this caller is the
    one that should post it; False means it has already gone out.

    The claim is the INSERT itself rather than a SELECT followed by one, because the
    question being asked is "has this morning been sent", and two processes — the old
    container and the new one during a Railway deploy — can ask it at the same second.
    ON CONFLICT DO NOTHING turns that race into a single winner at the database, which is
    the only place both of them agree.
    """
    row = await db.fetchrow(
        "INSERT INTO pti_reminder_runs (sent_on) VALUES ($1) "
        "ON CONFLICT (sent_on) DO NOTHING RETURNING sent_on",
        day,
    )
    return row is not None


async def record_pti_run(day: date, group_count: int) -> None:
    """Note how many driver groups the album actually reached.

    Kept separate from the claim so the claim stays a pure "am I the one": this is written
    afterwards, when the number is known, and it is the only evidence that a reminder which
    the log says fired was not posted into an empty fleet.
    """
    await db.execute(
        "UPDATE pti_reminder_runs SET group_count = $2 WHERE sent_on = $1",
        day, group_count,
    )


async def release_pti_run(day: date) -> None:
    """Give the claim back when the send could not be attempted at all — posters missing,
    the database unreachable mid-way, the group list unreadable.

    Without this a failed morning would stay marked as sent and the next restart, still
    inside the catch-up window, would skip it. A claim is only worth keeping once there is
    something to show for it.
    """
    await db.execute("DELETE FROM pti_reminder_runs WHERE sent_on = $1", day)
