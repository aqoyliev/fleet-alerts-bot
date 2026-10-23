"""Tests for web's side of the driver alerts: utils/driver_forward.py (the
fire-and-forget POST to the driver-alerts service) and how _handle_event uses it.
No network: the forwarder's HTTP session is a recorder, and _handle_event's own
collaborators (DB look-ups, the Samsara poll, downloads) are faked."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

import utils.driver_forward as fwd
import utils.webhook_handler as wh

URL = "http://driver-alerts.railway.internal:8080/events"
SECRET = "s3cr3t-value-for-tests"


# ── fake HTTP ────────────────────────────────────────────────────────────────────

class _FakeResponse:
    def __init__(self, status):
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Answers each post() with the next item of `outcomes`: an int status or an
    exception to raise."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.posts = []
        self.closed = False

    def post(self, url, data=None, headers=None, timeout=None):
        self.posts.append({"url": url, "data": data, "headers": dict(headers or {}),
                           "timeout": timeout})
        outcome = self.outcomes.pop(0) if self.outcomes else 202
        if isinstance(outcome, BaseException):
            raise outcome
        return _FakeResponse(outcome)

    async def close(self):
        self.closed = True


@pytest.fixture
def forwarder(monkeypatch):
    """Configured forwarder with a recording session and no real sleeps."""
    sleeps = []

    async def _no_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(fwd.config, "DRIVER_ALERTS_FORWARD_URL", URL)
    monkeypatch.setattr(fwd.config, "DRIVER_ALERTS_FORWARD_SECRET", SECRET)
    monkeypatch.setattr(fwd, "_sleep", _no_sleep)

    def _install(*outcomes):
        session = _FakeSession(*outcomes)
        monkeypatch.setattr(fwd, "_get_http_session", lambda: session)
        return session

    _install.sleeps = sleeps
    return _install


def _payload(**over):
    p = fwd.build_payload(company_slug="co", alert_source="motive", event_id=901,
                          raw_event_id="901", vehicle="T1",
                          occurred_at=datetime(2026, 10, 5, 15, tzinfo=timezone.utc),
                          base_text="<b>CELL PHONE USAGE</b>", video_urls=["http://v.mp4"],
                          image_urls=[], event={"id": "901", "type": "cell_phone"})
    p.update(over)
    return p


# ── build_payload ────────────────────────────────────────────────────────────────

def test_payload_shape_and_json_safety():
    weird = {"id": "901", "type": "cell_phone", "when": datetime(2026, 10, 5, 15),
             "nested": {"t": datetime(2026, 1, 1, tzinfo=timezone.utc)}, "obj": object()}
    p = fwd.build_payload(company_slug="co", alert_source="samsara", event_id=-42,
                          raw_event_id="uuid-1", vehicle="T1",
                          occurred_at=datetime(2026, 10, 5, 11, tzinfo=timezone.utc),
                          base_text="text", video_urls=("http://a",), image_urls=None,
                          event=weird)
    assert set(p) == {"company_slug", "alert_source", "event_id", "raw_event_id", "vehicle",
                      "occurred_at", "base_text", "video_urls", "image_urls", "event"}
    assert p["event_id"] == -42 and p["raw_event_id"] == "uuid-1"
    assert p["occurred_at"] == "2026-10-05T11:00:00+00:00"
    assert p["video_urls"] == ["http://a"] and p["image_urls"] == []
    assert p["event"]["when"] == "2026-10-05 15:00:00"           # stringified, not lost
    assert isinstance(p["event"]["obj"], str)
    assert json.loads(json.dumps(p)) == p                          # plain JSON throughout


def test_payload_reads_naive_occurrence_as_utc_and_converts_aware_to_utc():
    from zoneinfo import ZoneInfo
    naive = fwd.build_payload(company_slug="co", alert_source="motive", event_id=1,
                              raw_event_id=1, vehicle="T1", occurred_at=datetime(2026, 10, 5, 15),
                              base_text="", video_urls=[], image_urls=[], event={})
    ny = fwd.build_payload(company_slug="co", alert_source="motive", event_id=1,
                           raw_event_id=1, vehicle="T1",
                           occurred_at=datetime(2026, 10, 5, 11, tzinfo=ZoneInfo("America/New_York")),
                           base_text="", video_urls=[], image_urls=[], event={})
    assert naive["occurred_at"] == ny["occurred_at"] == "2026-10-05T15:00:00+00:00"
    assert naive["raw_event_id"] == "1"


# ── forward_cell_phone_event ─────────────────────────────────────────────────────

async def test_no_url_means_no_http(monkeypatch):
    monkeypatch.setattr(fwd.config, "DRIVER_ALERTS_FORWARD_URL", "  ")
    session = _FakeSession()
    opened = []
    monkeypatch.setattr(fwd, "_get_http_session", lambda: opened.append(1) or session)
    assert fwd.is_enabled() is False
    await fwd.forward_cell_phone_event(_payload())
    assert opened == [] and session.posts == []


async def test_posts_json_with_the_secret_header(forwarder):
    session = forwarder(202)
    p = _payload()
    await fwd.forward_cell_phone_event(p)
    assert len(session.posts) == 1
    post = session.posts[0]
    assert post["url"] == URL
    assert post["headers"]["X-Forward-Secret"] == SECRET
    assert post["headers"]["Content-Type"] == "application/json"
    assert json.loads(post["data"]) == p
    assert post["timeout"].total == 5


async def test_retries_network_errors_and_5xx_then_succeeds(forwarder):
    session = forwarder(OSError("connection refused"), 503, 202)
    await fwd.forward_cell_phone_event(_payload())
    assert len(session.posts) == 3
    assert forwarder.sleeps == [0.5, 1.5]


async def test_gives_up_after_two_retries_without_raising(forwarder, caplog):
    session = forwarder(asyncio.TimeoutError(), 500, OSError("down"), 202)
    with caplog.at_level("WARNING", logger=fwd.logger.name):
        await fwd.forward_cell_phone_event(_payload())
    assert len(session.posts) == 3                                 # 1 try + 2 retries
    assert any("Gave up" in r.getMessage() for r in caplog.records)
    assert all(SECRET not in r.getMessage() for r in caplog.records)


async def test_4xx_is_not_retried(forwarder, caplog):
    session = forwarder(401)
    with caplog.at_level("ERROR", logger=fwd.logger.name):
        await fwd.forward_cell_phone_event(_payload())
    assert len(session.posts) == 1 and forwarder.sleeps == []
    assert any("HTTP 401" in r.getMessage() for r in caplog.records)
    assert all(SECRET not in r.getMessage() for r in caplog.records)


async def test_never_raises_even_when_the_session_cannot_be_made(forwarder, monkeypatch):
    def _boom():
        raise RuntimeError("no loop")
    monkeypatch.setattr(fwd, "_get_http_session", _boom)
    await fwd.forward_cell_phone_event(_payload())
    await fwd.forward_cell_phone_event({"not": "a normal payload", "x": object()})


async def test_close_closes_the_lazily_made_session(monkeypatch):
    monkeypatch.setattr(fwd, "_http_session", None)
    session = fwd._get_http_session()
    assert fwd._get_http_session() is session
    await fwd.close()
    assert session.closed and fwd._http_session is None


# ── _handle_event ────────────────────────────────────────────────────────────────

def _async_const(value):
    async def _f(*_a, **_k):
        return value
    return _f


class _FakeMainBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, parse_mode=None, disable_web_page_preview=None):
        self.sent.append(chat_id)

    async def send_video(self, chat_id, media, caption=None, parse_mode=None):
        self.sent.append(chat_id)


@pytest.fixture
def handle(monkeypatch):
    """Fakes _handle_event's collaborators; returns a setup(group_ids, dm_ids) that
    gives back (forwarded payloads, downloaded urls)."""
    forwarded, downloads = [], []

    async def fake_forward(payload):
        forwarded.append(payload)

    async def fake_download(url):
        downloads.append(url)
        return b"VID"

    def _setup(group_ids, dm_ids):
        monkeypatch.setattr(wh, "get_company_name", _async_const("Co"))
        monkeypatch.setattr(wh, "save_violation", _async_const(None))
        monkeypatch.setattr(wh, "get_groups_for_event", _async_const(group_ids))
        monkeypatch.setattr(wh, "get_subscribed_admins", _async_const(dm_ids))
        monkeypatch.setattr(wh, "forward_cell_phone_event", fake_forward)
        monkeypatch.setattr(wh, "driver_alerts_enabled", lambda: True)
        monkeypatch.setattr(wh, "_download", fake_download)
        return forwarded, downloads

    return _setup


async def _drain():
    while wh._background_tasks:
        await asyncio.gather(*list(wh._background_tasks), return_exceptions=True)
    await asyncio.sleep(0)


def _motive_cell_phone(event_id="901", **extra):
    return {"id": event_id, "type": "cell_phone", "vehicle": {"number": "T1"},
            "start_time": "2026-10-05T15:00:00Z", **extra}


async def test_other_types_are_not_forwarded(handle):
    forwarded, _ = handle([1], [])
    bot = _FakeMainBot()
    await wh._handle_event(bot, {"id": "900", "type": "hard_brake", "vehicle": {"number": "T1"},
                                 "start_time": "2026-10-05T15:00:00Z"}, "co")
    await _drain()
    assert bot.sent == [1] and forwarded == []


async def test_cell_phone_without_company_targets_is_still_forwarded(handle):
    media = {"available": True, "downloadable_videos": {"driver_facing_plain_url": "http://i.mp4"},
             "downloadable_images": {}}
    forwarded, downloads = handle([], [])
    bot = _FakeMainBot()
    await wh._handle_event(bot, _motive_cell_phone(camera_media=media), "co")
    await _drain()
    assert bot.sent == []
    assert downloads == []                      # nobody in web needs the bytes
    assert len(forwarded) == 1
    p = forwarded[0]
    assert p["event_id"] == 901 and p["raw_event_id"] == "901"
    assert p["company_slug"] == "co" and p["alert_source"] == "motive" and p["vehicle"] == "T1"
    assert p["occurred_at"] == "2026-10-05T15:00:00+00:00"
    assert "CELL PHONE USAGE" in p["base_text"]
    assert p["video_urls"] == ["http://i.mp4"] and p["image_urls"] == []
    assert p["event"]["id"] == "901"
    assert "media" not in p and "samsara_api_key" not in p
    assert json.loads(json.dumps(p)) == p


async def test_cell_phone_with_targets_sends_company_alerts_and_forwards(handle):
    forwarded, _ = handle([1], [2])
    bot = _FakeMainBot()
    await wh._handle_event(bot, _motive_cell_phone("902"), "co")
    await _drain()
    assert bot.sent == [1, 2] and len(forwarded) == 1


async def test_forwarding_off_and_no_targets_skips(handle, monkeypatch):
    forwarded, _ = handle([], [])
    monkeypatch.setattr(wh, "driver_alerts_enabled", lambda: False)
    bot = _FakeMainBot()
    await wh._handle_event(bot, _motive_cell_phone("903"), "co")
    await _drain()
    assert forwarded == [] and bot.sent == []


async def test_forwarding_off_leaves_company_alerts_alone(handle, monkeypatch):
    forwarded, _ = handle([1], [])
    monkeypatch.setattr(wh, "driver_alerts_enabled", lambda: False)
    bot = _FakeMainBot()
    await wh._handle_event(bot, _motive_cell_phone("904"), "co")
    await _drain()
    assert forwarded == [] and bot.sent == [1]


async def test_forward_is_fire_and_forget(handle, monkeypatch):
    handle([1], [])
    release, started = asyncio.Event(), []

    async def slow_forward(payload):
        started.append(payload)
        await release.wait()

    monkeypatch.setattr(wh, "forward_cell_phone_event", slow_forward)
    bot = _FakeMainBot()
    await asyncio.wait_for(wh._handle_event(bot, _motive_cell_phone("905"), "co", "KEY"), 1)
    assert bot.sent == [1]
    assert len(wh._background_tasks) == 1        # handler returned, forward still open
    await asyncio.sleep(0)
    assert started and started[0]["alert_source"] == "motive"
    release.set()
    await _drain()
    assert wh._background_tasks == set()


async def test_failing_forward_never_costs_the_company_alert(handle, monkeypatch, caplog):
    handle([1], [])

    async def broken_forward(payload):
        raise RuntimeError("kaput")

    monkeypatch.setattr(wh, "forward_cell_phone_event", broken_forward)
    bot = _FakeMainBot()
    with caplog.at_level("ERROR", logger=wh.logger.name):
        await wh._handle_event(bot, _motive_cell_phone("906"), "co")
        await _drain()
    assert bot.sent == [1]
    assert any("driver forward" in r.getMessage() for r in caplog.records)


async def test_samsara_cell_phone_is_forwarded_without_the_api_key(handle, monkeypatch):
    forwarded, downloads = handle([1], [])

    async def fake_poll(vehicle_id, timestamp_ms, api_key, on_first=None):
        return {"harshEventType": "Mobile Usage", "downloadInwardVideoUrl": "http://i.mp4",
                "location": {"address": "I-95"}}

    monkeypatch.setattr(wh, "_fetch_samsara_harsh_event", fake_poll)
    bot = _FakeMainBot()
    event = {"id": "uuid-abc", "type": "harsh_event", "_source": "samsara",
             "_samsara_vehicle_id": "v9", "_samsara_timestamp_ms": 1759676400000,
             "vehicle": {"number": "T1"}, "start_time": "2026-10-05T15:00:00Z"}
    await asyncio.wait_for(wh._handle_event(bot, event, "co", "SAMSARA_KEY"), 1)
    await _drain()
    assert bot.sent == [1] and downloads == ["http://i.mp4"]    # company alert unaffected
    assert len(forwarded) == 1
    p = forwarded[0]
    assert p["alert_source"] == "samsara"
    assert p["event_id"] == wh._event_id_to_bigint("uuid-abc") and p["raw_event_id"] == "uuid-abc"
    assert p["event"]["_samsara_vehicle_id"] == "v9"
    assert p["event"]["_samsara_timestamp_ms"] == 1759676400000
    assert p["event"]["_source"] == "samsara"
    assert p["video_urls"] == ["http://i.mp4"]
    assert "SAMSARA_KEY" not in json.dumps(p)                     # receiver reads it from the DB
