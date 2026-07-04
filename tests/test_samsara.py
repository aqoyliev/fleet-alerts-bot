"""Tests for the Samsara integration in utils/webhook_handler.py.

Covers the parts that have no I/O (parsing, event-id hashing, formatting) plus the
harsh-event poll state machine, driven through a faked aiohttp session so no network
or sleeping happens.
"""
import asyncio

import pytest

import utils.webhook_handler as wh


# ── pure helpers ───────────────────────────────────────────────────────────────

def test_event_id_to_bigint_samsara_uuid_is_stable_and_in_range():
    uid = "550e8400-e29b-41d4-a716-446655440000"
    a = wh._event_id_to_bigint(uid)
    assert isinstance(a, int) and -(2 ** 63) <= a < 2 ** 63
    assert wh._event_id_to_bigint(uid) == a  # deterministic across calls


def test_event_id_to_bigint_motive_numeric_passthrough_and_none():
    assert wh._event_id_to_bigint("12345") == 12345
    assert wh._event_id_to_bigint(67890) == 67890
    assert wh._event_id_to_bigint(None) is None
    assert wh._event_id_to_bigint("") is None


@pytest.mark.parametrize("etype", [
    "harsh_event", "harsh_acceleration", "harsh_turn", "hard_cornering",
    "inattentive_driving", "drowsy_driving", "no_seat_belt",
])
def test_new_samsara_event_types_registered(etype):
    assert etype in wh.EVENT_TYPE_MAP
    assert etype in wh.ALLOWED_TYPES


# ── _parse_samsara ──────────────────────────────────────────────────────────────

def test_parse_alert_incident_harsh_event():
    incident = {
        "eventType": "AlertIncident", "eventId": "abc-1",
        "eventTime": "2026-05-22T15:00:00Z",
        "data": {
            "happenedAtTime": "2026-05-22T15:00:00Z",
            "incidentUrl": "https://cloud.samsara.com/o/1/fleet/x/1747929600000",
            "conditions": [{"details": {"harshEvent": {"vehicle": {"id": "v9", "name": "Unit 42"}}}}],
        },
    }
    et, norm = wh._parse_samsara(incident)
    assert et == "harsh_event"
    assert norm["_samsara_vehicle_id"] == "v9"
    assert norm["_samsara_timestamp_ms"] == 1747929600000
    assert norm["vehicle"]["number"] == "Unit 42"
    assert norm["_source"] == "samsara"


def test_parse_speeding_flat_and_severe_nested():
    et, n = wh._parse_samsara({
        "eventType": "SpeedingEventStarted", "eventId": "s1",
        "data": {"vehicle": {"name": "Unit 7"}, "severityLevel": "Heavy",
                 "startTime": "2026-05-22T15:00:00Z"},
    })
    assert et == "speeding" and n["severity"] == "high"

    et2, n2 = wh._parse_samsara({
        "eventType": "SevereSpeedingStarted", "eventId": "s2",
        "data": {"data": {"vehicle": {"name": "Unit 8"}, "startTime": "2026-05-22T15:00:00Z"}},
    })
    assert et2 == "speeding" and n2["severity"] == "critical"


def test_parse_unknown_event_type_ignored():
    assert wh._parse_samsara({"eventType": "GeoFenceEntry", "eventId": "g1"}) == ("", {})


# ── formatting ──────────────────────────────────────────────────────────────────

def test_samsara_alerts_tagged_motive_not():
    crash = {"type": "crash", "_source": "samsara", "vehicle": {"number": "Unit 42"},
             "start_time": "2026-05-22T15:00:00Z", "location": "I-95 N"}
    initial = wh._format_crash_initial(crash, "DM World")
    assert "Video pending" in initial and "via Samsara" in initial
    assert "CRASH" in wh._format_crash_video_caption(crash)

    motive = {"type": "hard_brake", "vehicle": {"number": "Unit 1"},
              "start_time": "2026-05-22T15:00:00Z", "location": "Main St"}
    assert "via Samsara" not in wh._format_event(motive)


# ── _fetch_samsara_harsh_event poll loop (faked HTTP) ───────────────────────────

class _FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return ""


class _FakeSession:
    """Returns scripted responses in order; repeats the last once exhausted."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, *a, **k):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


@pytest.fixture
def patch_poll(monkeypatch):
    """Patch the session factory + sleep so the poll runs instantly. Returns a setter
    that installs scripted responses and yields the live session for assertions."""
    state = {}

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(wh.asyncio, "sleep", _no_sleep)

    def _install(responses):
        session = _FakeSession(responses)
        # The poll now reuses one shared session via _get_http_session() instead of
        # opening a ClientSession per call, so patch the accessor.
        monkeypatch.setattr(wh, "_get_http_session", lambda: session)
        state["session"] = session
        return session

    state["install"] = _install
    return state


async def test_poll_returns_when_both_urls_ready_first_attempt(patch_poll):
    session = patch_poll["install"]([
        _FakeResp(200, {"harshEventType": "Harsh Braking",
                        "downloadForwardVideoUrl": "f", "downloadInwardVideoUrl": "i"}),
    ])
    data = await wh._fetch_samsara_harsh_event("v1", 1, "key")
    assert data["harshEventType"] == "Harsh Braking"
    assert session.calls == 1


async def test_poll_inward_only_type_short_circuits(patch_poll):
    # Mobile Usage -> cell_phone (inward-only): inward URL alone is enough.
    session = patch_poll["install"]([
        _FakeResp(200, {"harshEventType": "Mobile Usage", "downloadInwardVideoUrl": "i"}),
    ])
    data = await wh._fetch_samsara_harsh_event("v1", 1, "key")
    assert data is not None
    assert session.calls == 1


async def test_poll_crash_extends_window_past_three_attempts(patch_poll):
    # Four crash responses with no media, then media — proves it did NOT bail at 3.
    responses = [_FakeResp(200, {"harshEventType": "Crash"}) for _ in range(4)]
    responses.append(_FakeResp(200, {"harshEventType": "Crash",
                                     "downloadForwardVideoUrl": "f",
                                     "downloadInwardVideoUrl": "i"}))
    session = patch_poll["install"](responses)
    data = await wh._fetch_samsara_harsh_event("v1", 1, "key")
    assert data["downloadForwardVideoUrl"] == "f"
    assert session.calls == 5


async def test_poll_noncrash_gives_up_after_three_attempts(patch_poll):
    session = patch_poll["install"]([
        _FakeResp(200, {"harshEventType": "Harsh Braking"}),
    ])
    data = await wh._fetch_samsara_harsh_event("v1", 1, "key")
    assert data is not None          # returns last_data, sent with no media
    assert session.calls == 3


async def test_poll_obstructed_camera_skips(patch_poll):
    patch_poll["install"]([_FakeResp(200, {"harshEventType": "Obstructed Camera"})])
    assert await wh._fetch_samsara_harsh_event("v1", 1, "key") is None


async def test_poll_http_error_gives_up(patch_poll):
    patch_poll["install"]([_FakeResp(500, {})])
    assert await wh._fetch_samsara_harsh_event("v1", 1, "key") is None


async def test_poll_on_first_hook_fires_once(patch_poll):
    patch_poll["install"]([
        _FakeResp(200, {"harshEventType": "Crash"}),
        _FakeResp(200, {"harshEventType": "Crash",
                        "downloadForwardVideoUrl": "f", "downloadInwardVideoUrl": "i"}),
    ])
    seen = []

    async def _on_first(data):
        seen.append(data.get("harshEventType"))

    await wh._fetch_samsara_harsh_event("v1", 1, "key", on_first=_on_first)
    assert seen == ["Crash"]  # exactly once, on the first typed response


# ── resource-use optimizations ──────────────────────────────────────────────────

def _async_const(value):
    """An async function that ignores its args and returns `value`."""
    async def _f(*_a, **_k):
        return value
    return _f


class _FakeBot:
    """Records which chats each send went to, so we can count per-recipient sends."""
    def __init__(self):
        self.video_calls = []
        self.photo_calls = []
        self.media_group_calls = []
        self.message_calls = []

    async def send_video(self, chat_id, media, caption=None, parse_mode=None):
        self.video_calls.append(chat_id)

    async def send_photo(self, chat_id, media, caption=None, parse_mode=None):
        self.photo_calls.append(chat_id)

    async def send_media_group(self, chat_id, media):
        self.media_group_calls.append(chat_id)

    async def send_message(self, chat_id, text, parse_mode=None, disable_web_page_preview=None):
        self.message_calls.append(chat_id)


def test_is_duplicate_suppresses_repeat_then_evicts_after_ttl(monkeypatch):
    wh._seen_event_ids.clear()
    clock = {"now": 1000.0}
    monkeypatch.setattr(wh.time, "monotonic", lambda: clock["now"])

    assert wh._is_duplicate("evt-A") is False   # first sighting
    assert wh._is_duplicate("evt-A") is True    # immediate redelivery suppressed
    assert wh._is_duplicate("") is False        # empty id is never a duplicate

    # Past the TTL window the stale entry is evicted from the front, so A is new again.
    clock["now"] += wh._DEDUP_TTL + 1
    assert wh._is_duplicate("evt-A") is False
    assert len(wh._seen_event_ids) == 1         # only the fresh A remains; B-less, no leak


async def test_download_media_downloads_each_url_once(monkeypatch):
    calls = []

    async def _fake_download(url):
        calls.append(url)
        return b"DATA-" + url.encode()

    monkeypatch.setattr(wh, "_download", _fake_download)

    media, is_video = await wh._download_media(["v1", "v2"], [])
    assert calls == ["v1", "v2"]                 # each url fetched exactly once
    assert media == [b"DATA-v1", b"DATA-v2"]
    assert is_video is True

    calls.clear()
    media, is_video = await wh._download_media([], ["img1"])
    assert calls == ["img1"]
    assert is_video is False                     # images, not video


async def test_handle_event_downloads_media_once_for_all_recipients(monkeypatch):
    """The expensive provider download happens a single time and the bytes are reused
    for every recipient — not re-downloaded per chat."""
    downloads = []

    async def _fake_download(url):
        downloads.append(url)
        return b"VIDEOBYTES"

    monkeypatch.setattr(wh, "_download", _fake_download)
    monkeypatch.setattr(wh, "save_violation", _async_const(None))
    monkeypatch.setattr(wh, "get_groups_for_event", _async_const([1, 2]))
    monkeypatch.setattr(wh, "get_subscribed_admins", _async_const([3]))

    bot = _FakeBot()
    event = {
        "id": "555",
        "type": "hard_brake",
        "vehicle": {"number": "TRUCK-1"},
        "start_time": "2026-05-23T00:00:00Z",
        "camera_media": {
            "available": True,
            "downloadable_videos": {"front_facing_plain_url": "http://vid/forward.mp4"},
            "downloadable_images": {},
        },
    }
    await wh._handle_event(bot, event)

    assert len(downloads) == 1                   # ONE download for the single clip...
    assert bot.video_calls == [1, 2, 3]          # ...delivered to all three recipients
