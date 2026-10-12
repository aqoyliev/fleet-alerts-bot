"""The morning pre-trip-inspection reminder.

A per-company extra, like CRASH_GROUP_ID: every deployment runs this same branch, so the
feature is off unless PTI_REMINDER_TIME names a time, and the deployments that never set
it carry the code without ever noticing it.

What it does is post the posters in assets/pti/ as one album into every driver group at
that time each morning, Eastern. Not the office group — see get_driver_groups — and not
as an alert: nothing happened, this is the walk-around nobody does without a nudge.
"""
import asyncio
import hashlib
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram import Bot

from data import config
from utils.db_api.groups import get_driver_groups
from utils.db_api.pti_posters import get_poster_file_ids, save_poster_file_ids
from utils.db_api.pti_runs import claim_pti_run, record_pti_run, release_pti_run
# The broadcast helpers the alert path already uses. They are private to that module, but a
# daily post to forty driver chats needs exactly what they have learned: per-chat isolation,
# flood-control backoff, the supergroup-migration retry, and muting a group the bot has
# been removed from. A second, naive send loop here would re-learn all of it the hard way.
from utils.webhook_handler import _send_all, _send_with_retry

logger = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")

POSTER_DIR = Path(__file__).resolve().parents[1] / "assets" / "pti"

# Album order is the filename order, which is why the files are numbered: Telegram shows
# them in the order they are sent, and the checklist is meant to be the one on top.
POSTER_GLOB = "*.jpg"

# Telegram puts an album's caption on the first image only (see _send_with_retry), so this
# is the whole message. It repeats the posters' own instruction on purpose — the caption
# is what a driver sees in a notification preview without opening anything.
CAPTION = (
    "🛠 <b>PTI — yo'lga chiqishdan oldin</b>\n\n"
    "Truck va trailerni tekshiring. Nosozlik topsangiz, Safety yoki Maintenance'ga "
    "xabar bering — xavfli nosozlik bartaraf etilmaguncha yo'lga chiqmang."
)

# How late a missed slot may still be posted. A deploy at 07:02 must not cost the fleet
# its reminder; a container that comes up at 16:00 must not post "before you set off"
# into the middle of everyone's run. Three hours is the width of a morning dispatch.
CATCHUP_WINDOW = timedelta(hours=3)

_posters: list[bytes] | None = None


def load_posters() -> list[bytes]:
    """Read the album off disk once and keep it. The files ship with the image and never
    change between deploys, so re-reading ~700 KB every morning buys nothing; holding the
    bytes also means one read failure cannot silently halve tomorrow's album."""
    global _posters
    if _posters is None:
        _posters = [p.read_bytes() for p in sorted(POSTER_DIR.glob(POSTER_GLOB))]
    return _posters


def fingerprint(posters: list[bytes]) -> str:
    """Identifies this exact album. Cached file_ids are stored against it so that
    replacing a poster retires the ids that pointed at the old one."""
    h = hashlib.sha256()
    for p in posters:
        h.update(hashlib.sha256(p).digest())
    return h.hexdigest()[:32]


def file_ids_of(messages) -> list[str]:
    """Pull the file_id out of each photo Telegram just accepted.

    `photo` is the list of sizes Telegram generated; the last is the largest, and that is
    the one worth keeping — re-sending a thumbnail's id would quietly downgrade the album
    to something nobody can read.
    """
    ids = []
    for m in messages or []:
        sizes = getattr(m, "photo", None)
        if sizes:
            ids.append(sizes[-1].file_id)
    return ids


async def send_pti_reminder(bot: Bot, day: date) -> int:
    """Post the album to every driver group, once for the given morning. Returns the
    number of groups it was addressed to (0 if it did not send).

    The posters are uploaded at most once in this deployment's lifetime: the first group's
    send carries the bytes, Telegram answers with a file_id per image, and every chat after
    it — that morning and every morning after — is sent those ids instead. For a forty-truck
    fleet that is one ~700 KB upload rather than 28 MB a day.
    """
    posters = load_posters()
    if not posters:
        logger.error(f"[pti] no posters found in {POSTER_DIR} — nothing to send")
        return 0

    if not await claim_pti_run(day):
        logger.info(f"[pti] {day} already sent — skipping")
        return 0

    try:
        chat_ids = await get_driver_groups()
        print_fp = fingerprint(posters)
        cached = await get_poster_file_ids(print_fp)
    except Exception as e:
        # The claim is only worth keeping if something was posted; hand it back so a
        # restart inside the catch-up window gets another go at this morning.
        logger.error(f"[pti] could not prepare the album: {e}", exc_info=True)
        await release_pti_run(day)
        return 0

    if not chat_ids:
        logger.warning(f"[pti] {day}: no driver groups registered — nothing to post to")
        await record_pti_run(day, 0)
        return 0

    sources: list[bytes | str] = cached or posters
    logger.info(f"[pti] {day}: posting to {len(chat_ids)} driver group(s) "
                f"({'cached file_ids' if cached else 'uploading the posters'})")

    if cached:
        await _send_all(bot, chat_ids, CAPTION, media=sources, is_video=False)
    else:
        # The first chat pays the upload, and its reply is where the ids come from. It is
        # sent on its own rather than through _send_all precisely because that helper
        # discards what Telegram answered.
        head, tail = chat_ids[0], chat_ids[1:]
        messages = None
        try:
            messages = await _send_with_retry(bot, head, CAPTION, media=sources)
        except Exception as e:
            logger.error(f"[pti] send to {head} failed, continuing with the rest: {e}",
                         exc_info=True)
        ids = file_ids_of(messages)
        if len(ids) == len(posters):
            sources = ids
            try:
                await save_poster_file_ids(print_fp, ids)
                logger.info(f"[pti] cached {len(ids)} poster file_id(s) — "
                            f"no further uploads for this album")
            except Exception as e:
                # Worth the whole album going out anyway; the cost is re-uploading
                # tomorrow, not a missed reminder.
                logger.error(f"[pti] could not cache the file_ids: {e}")
        if tail:
            await _send_all(bot, tail, CAPTION, media=sources, is_video=False)

    await record_pti_run(day, len(chat_ids))
    return len(chat_ids)


def next_slot(now: datetime, hour: int, minute: int, handled: date | None) -> datetime:
    """When the reminder should next fire, given the clock and the last morning this
    process handled.

    Today's slot still counts for CATCHUP_WINDOW after it passes, which is what carries a
    reminder across a deploy that lands on top of it. `handled` is what stops the loop
    re-entering a slot it has just served and spinning through the rest of that window.
    """
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot.date() == handled or now >= slot + CATCHUP_WINDOW:
        slot += timedelta(days=1)
    return slot


async def schedule_pti_reminders(bot: Bot):
    """Runs forever, posting the PTI album each morning. Returns immediately — and says
    so once — when the company has not asked for one."""
    if config.PTI_REMINDER_AT is None:
        logger.info("[pti] PTI_REMINDER_TIME not set — daily reminder is off")
        return

    hour, minute = config.PTI_REMINDER_AT
    handled: date | None = None
    while True:
        now = datetime.now(tz=ET)
        slot = next_slot(now, hour, minute, handled)
        wait = (slot - now).total_seconds()
        logger.info(f"[pti] next reminder in {wait:.0f}s "
                    f"(at {slot.strftime('%Y-%m-%d %H:%M %Z')})")
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            await send_pti_reminder(bot, slot.date())
        except Exception as e:
            # One bad morning must not end the loop — that would silently retire the
            # feature until somebody redeployed.
            logger.error(f"[pti] reminder for {slot.date()} failed: {e}", exc_info=True)
        handled = slot.date()
