"""Tests for the morning PTI reminder — the pre-trip posters posted to every driver group.

It is a per-company extra on a branch that four deployments share, so the properties worth
pinning are mostly about restraint:

  • it is OFF unless PTI_REMINDER_TIME names a time, and a typo'd time is off, not fatal,
  • it reaches the drivers' own groups and not the office group,
  • a deploy landing on top of the 07:00 slot neither loses the morning nor repeats it,
  • a morning that could not be posted does not stay marked as sent, and
  • the posters are uploaded once and then re-sent by file_id, for ever.
"""
from datetime import date, datetime, timedelta

import pytest

from data import config
from utils import pti_reminder as pti
from utils.db_api import groups as grp


# ── the setting ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("07:00", (7, 0)),
    ("7:05", (7, 5)),
    ("23:59", (23, 59)),
    ("00:00", (0, 0)),
    ("  07:00  ", (7, 0)),
    ("7", (7, 0)),
])
def test_a_time_is_read_as_hour_and_minute(raw, expected):
    assert config._parse_hhmm(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None, "nonsense", "25:00", "07:60", "-1:00", "7:xx"])
def test_anything_unusable_reads_as_off_rather_than_crashing_startup(raw):
    """Blank is the normal state on the three deployments that did not ask for this, and a
    typo must cost a company its reminder, not its alerting."""
    assert config._parse_hhmm(raw) is None


async def test_with_no_time_set_the_loop_returns_without_sending(monkeypatch):
    sent = []
    monkeypatch.setattr(config, "PTI_REMINDER_AT", None)
    monkeypatch.setattr(pti, "send_pti_reminder", lambda *a, **k: sent.append(a))

    await pti.schedule_pti_reminders(object())

    assert sent == []


# ── when it fires ──────────────────────────────────────────────────────────────

def _et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=pti.ET)


def test_before_the_slot_it_waits_for_today():
    now = _et(2026, 10, 3, 5, 30)
    assert pti.next_slot(now, 7, 0, handled=None) == _et(2026, 10, 3, 7, 0)


def test_a_deploy_just_after_the_slot_still_posts_this_morning():
    """The reason the catch-up window exists: a push to this branch restarts four
    containers, and one of them coming up at 07:02 must not cost a fleet its reminder."""
    now = _et(2026, 10, 3, 7, 2)
    assert pti.next_slot(now, 7, 0, handled=None) == _et(2026, 10, 3, 7, 0)


def test_late_in_the_day_it_waits_for_tomorrow_instead():
    """"Check your truck before you set off" posted at 16:00 is not a reminder, it is noise."""
    now = _et(2026, 10, 3, 16, 0)
    assert pti.next_slot(now, 7, 0, handled=None) == _et(2026, 10, 4, 7, 0)


def test_the_window_ends_where_it_says_it_does():
    inside = _et(2026, 10, 3, 7, 0) + pti.CATCHUP_WINDOW - timedelta(minutes=1)
    outside = _et(2026, 10, 3, 7, 0) + pti.CATCHUP_WINDOW
    assert pti.next_slot(inside, 7, 0, handled=None).date() == date(2026, 10, 3)
    assert pti.next_slot(outside, 7, 0, handled=None).date() == date(2026, 10, 4)


def test_a_morning_this_process_already_served_is_not_re_entered():
    """Without this the loop would spin through the rest of the catch-up window, re-asking
    the database every pass whether it had already sent."""
    now = _et(2026, 10, 3, 7, 1)
    assert pti.next_slot(now, 7, 0, handled=date(2026, 10, 3)) == _et(2026, 10, 4, 7, 0)


# ── stand-ins for what Telegram answers ────────────────────────────────────────

class _Size:
    def __init__(self, file_id):
        self.file_id = file_id


class _Msg:
    """A sent photo message. `photo` is Telegram's list of generated sizes, smallest first."""
    def __init__(self, *file_ids):
        self.photo = [_Size(f) for f in file_ids]


POSTERS = [b"poster-1", b"poster-2", b"poster-3"]
UPLOADED = [_Msg("thumb-1", "big-1"), _Msg("thumb-2", "big-2"), _Msg("thumb-3", "big-3")]
BIG_IDS = ["big-1", "big-2", "big-3"]
DAY = date(2026, 10, 3)


@pytest.fixture
def posted(monkeypatch):
    """Drives send_pti_reminder with the database, the posters and Telegram stubbed out."""
    state = {"claim": True, "groups": [-100111, -100222, -100333], "cached": [],
             "reply": UPLOADED, "head": [], "bulk": [], "saved": [], "recorded": [],
             "released": []}

    async def _claim(day):
        return state["claim"]

    async def _groups():
        if isinstance(state["groups"], Exception):
            raise state["groups"]
        return state["groups"]

    async def _get_ids(fp):
        return list(state["cached"])

    async def _save_ids(fp, ids):
        state["saved"].append((fp, list(ids)))

    async def _send_with_retry(bot, chat_id, text, media=None, is_video=False):
        state["head"].append({"chat": chat_id, "text": text, "media": media})
        if isinstance(state["reply"], Exception):
            raise state["reply"]
        return state["reply"]

    async def _send_all(bot, chat_ids, text, media=None, is_video=False):
        state["bulk"].append({"chats": list(chat_ids), "text": text,
                              "media": media, "is_video": is_video})

    async def _record(day, count):
        state["recorded"].append((day, count))

    async def _release(day):
        state["released"].append(day)

    monkeypatch.setattr(pti, "claim_pti_run", _claim)
    monkeypatch.setattr(pti, "get_driver_groups", _groups)
    monkeypatch.setattr(pti, "get_poster_file_ids", _get_ids)
    monkeypatch.setattr(pti, "save_poster_file_ids", _save_ids)
    monkeypatch.setattr(pti, "_send_with_retry", _send_with_retry)
    monkeypatch.setattr(pti, "_send_all", _send_all)
    monkeypatch.setattr(pti, "record_pti_run", _record)
    monkeypatch.setattr(pti, "release_pti_run", _release)
    monkeypatch.setattr(pti, "_posters", list(POSTERS))
    return state


# ── who gets it ────────────────────────────────────────────────────────────────

async def test_the_album_reaches_every_driver_group(posted):
    assert await pti.send_pti_reminder(object(), DAY) == 3

    reached = [posted["head"][0]["chat"]] + posted["bulk"][0]["chats"]
    assert reached == [-100111, -100222, -100333]
    assert posted["recorded"] == [(DAY, 3)]


async def test_it_is_sent_as_one_album_with_the_caption(posted):
    await pti.send_pti_reminder(object(), DAY)

    assert posted["head"][0]["media"] == POSTERS
    assert "PTI" in posted["head"][0]["text"]
    assert posted["bulk"][0]["is_video"] is False


async def test_the_office_group_is_not_on_the_list():
    """get_driver_groups is the complement of get_all_groups: the main/catch-all group has
    a NULL unit and is what the daily digest goes to, not this."""
    captured = {}

    async def _fetch(sql, *args):
        captured["sql"] = " ".join(sql.split())
        return []

    original = grp.db.fetch
    grp.db.fetch = _fetch
    try:
        await grp.get_driver_groups()
    finally:
        grp.db.fetch = original

    assert "vehicle_number IS NOT NULL" in captured["sql"]
    # Muting is how a group that kicked the bot stops being posted to (see
    # _drop_unreachable); a daily reminder honours it like every alert does.
    assert "COALESCE(enabled, TRUE)" in captured["sql"]


# ── uploaded once, then re-sent by id ──────────────────────────────────────────

async def test_only_the_first_group_is_sent_the_bytes(posted):
    """The whole point: one upload for the fleet, not one per chat per day."""
    await pti.send_pti_reminder(object(), DAY)

    assert posted["head"][0]["media"] == POSTERS, "the first chat carries the upload"
    assert posted["bulk"][0]["media"] == BIG_IDS, "everyone after it gets file_ids"


async def test_the_file_ids_are_remembered_for_tomorrow(posted):
    await pti.send_pti_reminder(object(), DAY)

    assert len(posted["saved"]) == 1
    fp, ids = posted["saved"][0]
    assert ids == BIG_IDS
    assert fp == pti.fingerprint(POSTERS)


async def test_the_largest_size_is_the_one_kept():
    """Telegram returns several sizes; caching a thumbnail's id would quietly turn the
    album into something nobody can read."""
    assert pti.file_ids_of(UPLOADED) == BIG_IDS


async def test_a_later_morning_uploads_nothing_at_all(posted):
    posted["cached"] = BIG_IDS

    assert await pti.send_pti_reminder(object(), DAY) == 3

    assert posted["head"] == [], "nothing should go through the upload path"
    assert posted["bulk"][0]["chats"] == [-100111, -100222, -100333]
    assert posted["bulk"][0]["media"] == BIG_IDS
    assert posted["saved"] == [], "already cached — nothing new to write"


async def test_replacing_a_poster_retires_the_cached_ids(posted):
    """A file_id points at specific bytes on Telegram's side. Without the fingerprint the
    cache would keep re-sending last season's artwork and look perfectly healthy."""
    assert pti.fingerprint(POSTERS) != pti.fingerprint([b"poster-1", b"NEW", b"poster-3"])


async def test_an_incomplete_reply_is_not_cached(posted):
    """Caching two ids for a three-poster album would silently drop one every morning
    after; re-uploading tomorrow is the cheaper mistake."""
    posted["reply"] = [_Msg("thumb-1", "big-1"), _Msg("thumb-2", "big-2")]

    await pti.send_pti_reminder(object(), DAY)

    assert posted["saved"] == []
    assert posted["bulk"][0]["media"] == POSTERS, "the rest still get a working album"


async def test_a_failed_first_send_still_serves_the_other_groups(posted):
    """One unreachable chat at the head of the list must not cost everyone behind it their
    reminder — the same isolation _send_all gives the rest."""
    posted["reply"] = RuntimeError("chat is gone")

    assert await pti.send_pti_reminder(object(), DAY) == 3

    assert posted["bulk"][0]["chats"] == [-100222, -100333]
    assert posted["bulk"][0]["media"] == POSTERS
    assert posted["saved"] == []


async def test_a_cache_write_failure_does_not_cost_the_morning(posted, monkeypatch):
    async def _boom(fp, ids):
        raise RuntimeError("db down")

    monkeypatch.setattr(pti, "save_poster_file_ids", _boom)

    assert await pti.send_pti_reminder(object(), DAY) == 3
    assert posted["bulk"][0]["media"] == BIG_IDS


# ── exactly once per morning ───────────────────────────────────────────────────

async def test_a_morning_already_claimed_is_not_posted_again(posted):
    """The claim is what makes a restart inside the catch-up window safe: the second
    container finds the row and says nothing."""
    posted["claim"] = False

    assert await pti.send_pti_reminder(object(), DAY) == 0
    assert posted["head"] == [] and posted["bulk"] == []


async def test_a_morning_that_could_not_be_prepared_gives_the_claim_back(posted):
    """Otherwise a database blip at 07:00 would mark the day sent and the next restart,
    still inside the window, would skip it for good."""
    posted["groups"] = RuntimeError("db down")

    assert await pti.send_pti_reminder(object(), DAY) == 0
    assert posted["bulk"] == []
    assert posted["released"] == [DAY]


async def test_a_fleet_with_no_driver_groups_records_the_morning_and_posts_nothing(posted):
    posted["groups"] = []

    assert await pti.send_pti_reminder(object(), DAY) == 0
    assert posted["head"] == [] and posted["bulk"] == []
    assert posted["recorded"] == [(DAY, 0)]


async def test_missing_posters_do_not_claim_the_morning(posted, monkeypatch):
    """An image that failed to ship is a deploy problem; leaving the day unclaimed means
    the fix is a redeploy rather than a redeploy plus a manual DELETE."""
    monkeypatch.setattr(pti, "_posters", [])

    assert await pti.send_pti_reminder(object(), DAY) == 0
    assert posted["bulk"] == []
    assert posted["recorded"] == []


# ── the posters themselves ─────────────────────────────────────────────────────

def test_the_posters_ship_with_the_image():
    """They are read off disk at runtime, so a rename or a missing file is only ever found
    in production otherwise."""
    posters = sorted(pti.POSTER_DIR.glob(pti.POSTER_GLOB))
    assert len(posters) == 3, f"expected 3 posters in {pti.POSTER_DIR}, found {len(posters)}"
    for p in posters:
        assert p.stat().st_size > 1024
        # Telegram rejects an album photo over 10 MB.
        assert p.stat().st_size < 10 * 1024 * 1024


def test_the_posters_are_loaded_in_filename_order():
    """Album order is send order, and the checklist is meant to be the one on top."""
    names = [p.name for p in sorted(pti.POSTER_DIR.glob(pti.POSTER_GLOB))]
    assert names == sorted(names)
    assert names[0].startswith("1-")


def test_the_caption_fits_a_telegram_album():
    assert len(pti.CAPTION) <= 1024
