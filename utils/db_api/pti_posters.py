from utils.db_api import db


async def get_poster_file_ids(fingerprint: str) -> list[str]:
    """The Telegram file_ids for the posters currently on disk, or [] if they have never
    been uploaded by this bot.

    Keyed by a fingerprint of the poster bytes rather than stored as a bare list: a
    file_id names a specific file on Telegram's side, so the day someone replaces a poster
    the cached ids would keep re-sending last season's artwork forever, and nothing about
    that failure looks like a failure. A changed fingerprint simply misses the cache.
    """
    row = await db.fetchrow(
        "SELECT file_ids FROM pti_poster_cache WHERE fingerprint = $1", fingerprint
    )
    return list(row["file_ids"]) if row else []


async def save_poster_file_ids(fingerprint: str, file_ids: list[str]) -> None:
    """Remember what Telegram called the posters it has just been given.

    Rows for superseded fingerprints are dropped in the same breath. They are harmless but
    permanently dead: a file_id belongs to one bot token and one set of bytes, and nothing
    will ever ask for those again.
    """
    await db.execute(
        "INSERT INTO pti_poster_cache (fingerprint, file_ids) VALUES ($1, $2) "
        "ON CONFLICT (fingerprint) DO UPDATE SET file_ids = EXCLUDED.file_ids",
        fingerprint, file_ids,
    )
    await db.execute("DELETE FROM pti_poster_cache WHERE fingerprint <> $1", fingerprint)
