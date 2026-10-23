"""Tests for the driver-alerts service (driver_service.py): the secret check, payload
validation, the 202-then-background processing and the health endpoint. Served with
aiohttp's own test server on localhost; notify_driver, the media download and the DB
look-up are faked, so no bot, Postgres or provider is contacted."""
import asyncio
from datetime import datetime, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

import driver_service as ds
import utils.driver_forward as fwd

SECRET = "service-secret-for-tests"


def _body(**over):
    body = {
        "company_slug": "co",
        "alert_source": "samsara",
        "event_id": -42,
        "raw_event_id": "uuid-abc",
        "vehicle": "T1",
        "occurred_at": "2026-10-05T15:00:00+00:00",
        "base_text": "<b>CELL PHONE USAGE</b>",
        "video_urls": ["http://i.mp4"],
        "image_urls": [],
        "event": {"id": "uuid-abc", "type": "cell_phone", "_source": "samsara",
                  "_samsara_vehicle_id": "v9"},
    }
    body.update(over)
    return body


@pytest.fixture
def fakes(monkeypatch):
    rec = {"notify": [], "downloads": [], "creds": []}

    async def fake_notify(**kwargs):
        rec["notify"].append(kwargs)

    async def fake_download(video_urls, image_urls):
        rec["downloads"].append((list(video_urls), list(image_urls)))
        return [b"VID"], bool(video_urls)

    async def fake_creds(slug):
        rec["creds"].append(slug)
        return "SAMSARA_KEY", "webhook-secret"

    monkeypatch.setattr(ds, "notify_driver", fake_notify)
    monkeypatch.setattr(ds, "download_media", fake_download)
    monkeypatch.setattr(ds, "get_samsara_credentials", fake_creds)
    monkeypatch.setattr(ds.driver_alerts, "is_enabled", lambda: True)
    return rec


@pytest.fixture
async def client():
    c = TestClient(TestServer(ds.create_app(SECRET, manage_resources=False)))
    await c.start_server()
    try:
        yield c
    finally:
        await c.close()


async def _drain():
    while ds._tasks:
        await asyncio.gather(*list(ds._tasks), return_exceptions=True)
    await asyncio.sleep(0)


async def _post(client, body, secret=SECRET):
    headers = {} if secret is None else {"X-Forward-Secret": secret}
    return await client.post("/events", json=body, headers=headers)


# ── auth ─────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("secret", [None, "", "wrong", SECRET + "x"])
async def test_missing_or_wrong_secret_is_401(client, fakes, secret):
    r = await _post(client, _body(), secret)
    assert r.status == 401
    await _drain()
    assert fakes["notify"] == [] and fakes["downloads"] == []


def test_refuses_to_start_without_a_secret(monkeypatch):
    with pytest.raises(ValueError):
        ds.create_app("")
    monkeypatch.setattr(ds.config, "DRIVER_ALERTS_FORWARD_SECRET", "")
    monkeypatch.setattr(ds.web, "run_app", lambda *a, **k: pytest.fail("must not start"))
    with pytest.raises(SystemExit):
        ds.main()


# ── validation ───────────────────────────────────────────────────────────────────

async def test_non_json_body_is_400(client, fakes):
    r = await client.post("/events", data=b"not json{",
                          headers={"X-Forward-Secret": SECRET, "Content-Type": "application/json"})
    assert r.status == 400


@pytest.mark.parametrize("bad", [
    {"company_slug": ""},
    {"company_slug": None},
    {"alert_source": "geotab"},
    {"event": "nope"},
    {"event_id": "123"},
    {"event_id": True},
    {"occurred_at": "yesterday"},
    {"occurred_at": 12345},
    {"video_urls": "http://i.mp4"},
    {"image_urls": [1, 2]},
    {"vehicle": 5},
])
async def test_malformed_payload_is_400(client, fakes, bad):
    r = await _post(client, _body(**bad))
    assert r.status == 400
    await _drain()
    assert fakes["notify"] == []


async def test_a_json_array_is_400(client, fakes):
    r = await _post(client, [_body()])
    assert r.status == 400


# ── processing ───────────────────────────────────────────────────────────────────

async def test_samsara_event_is_accepted_and_notified_with_media_and_key(client, fakes):
    r = await _post(client, _body())
    assert r.status == 202
    await _drain()
    assert fakes["downloads"] == [(["http://i.mp4"], [])]
    assert fakes["creds"] == ["co"]
    assert len(fakes["notify"]) == 1
    n = fakes["notify"][0]
    assert n["company_slug"] == "co" and n["event_id"] == -42 and n["vehicle"] == "T1"
    assert n["media"] == [b"VID"] and n["is_video"] is True
    assert n["samsara_api_key"] == "SAMSARA_KEY"
    assert n["occurred_at"] == datetime(2026, 10, 5, 15, tzinfo=timezone.utc)
    assert n["base_text"] == "<b>CELL PHONE USAGE</b>"
    assert n["video_urls"] == ["http://i.mp4"] and n["image_urls"] == []
    assert n["event"]["_samsara_vehicle_id"] == "v9"


async def test_motive_event_gets_no_samsara_key(client, fakes):
    r = await _post(client, _body(alert_source="motive", event_id=901, raw_event_id="901",
                                  video_urls=[], image_urls=["http://p.jpg"],
                                  occurred_at="2026-10-05T15:00:00Z",
                                  event={"id": "901", "type": "cell_phone"}))
    assert r.status == 202
    await _drain()
    assert fakes["creds"] == []
    n = fakes["notify"][0]
    assert n["samsara_api_key"] is None and n["is_video"] is False
    assert n["occurred_at"] == datetime(2026, 10, 5, 15, tzinfo=timezone.utc)


async def test_202_comes_before_the_notification_finishes(client, fakes, monkeypatch):
    release, finished = asyncio.Event(), []

    async def slow_notify(**kwargs):
        await release.wait()             # stands in for the coaching-state wait
        finished.append(kwargs["event_id"])

    monkeypatch.setattr(ds, "notify_driver", slow_notify)
    r = await asyncio.wait_for(_post(client, _body()), 2)
    assert r.status == 202 and finished == []
    assert len(ds._tasks) == 1
    release.set()
    await _drain()
    assert finished == [-42] and ds._tasks == set()


async def test_null_occurrence_and_event_id_pass_through(client, fakes):
    r = await _post(client, _body(alert_source="motive", occurred_at=None, event_id=None))
    assert r.status == 202
    await _drain()
    n = fakes["notify"][0]
    assert n["occurred_at"] is None and n["event_id"] is None


async def test_samsara_key_lookup_failure_still_reaches_the_gate(client, fakes, monkeypatch):
    async def broken_creds(slug):
        raise RuntimeError("db down")

    monkeypatch.setattr(ds, "get_samsara_credentials", broken_creds)
    assert (await _post(client, _body())).status == 202
    await _drain()
    assert fakes["notify"][0]["samsara_api_key"] is None    # notify_driver fails closed


async def test_driver_bot_unset_skips_download_and_notify(client, fakes, monkeypatch):
    monkeypatch.setattr(ds.driver_alerts, "is_enabled", lambda: False)
    assert (await _post(client, _body())).status == 202
    await _drain()
    assert fakes["downloads"] == [] and fakes["notify"] == []


async def test_processing_errors_are_contained(client, fakes, monkeypatch, caplog):
    async def broken_download(video_urls, image_urls):
        raise RuntimeError("disk full")

    monkeypatch.setattr(ds, "download_media", broken_download)
    with caplog.at_level("ERROR", logger=ds.logger.name):
        assert (await _post(client, _body())).status == 202
        await _drain()
    assert fakes["notify"] == []
    assert any("disk full" in r.getMessage() for r in caplog.records)
    assert (await client.get("/health")).status == 200      # still serving


async def test_health(client):
    r = await client.get("/health")
    assert r.status == 200 and await r.text() == "OK"


# ── web → service, end to end over localhost ─────────────────────────────────────

async def test_forwarder_delivers_to_the_service(client, fakes, monkeypatch):
    monkeypatch.setattr(fwd.config, "DRIVER_ALERTS_FORWARD_URL", str(client.make_url("/events")))
    monkeypatch.setattr(fwd.config, "DRIVER_ALERTS_FORWARD_SECRET", SECRET)
    monkeypatch.setattr(fwd, "_http_session", None)
    payload = fwd.build_payload(company_slug="co", alert_source="samsara", event_id=-42,
                                raw_event_id="uuid-abc", vehicle="T1",
                                occurred_at=datetime(2026, 10, 5, 15, tzinfo=timezone.utc),
                                base_text="text", video_urls=["http://i.mp4"], image_urls=[],
                                event={"id": "uuid-abc", "_source": "samsara",
                                       "_samsara_vehicle_id": "v9"})
    try:
        await fwd.forward_cell_phone_event(payload)
    finally:
        await fwd.close()
    await _drain()
    assert len(fakes["notify"]) == 1
    assert fakes["notify"][0]["samsara_api_key"] == "SAMSARA_KEY"


async def test_forwarder_with_the_wrong_secret_is_refused_once(client, fakes, monkeypatch):
    monkeypatch.setattr(fwd.config, "DRIVER_ALERTS_FORWARD_URL", str(client.make_url("/events")))
    monkeypatch.setattr(fwd.config, "DRIVER_ALERTS_FORWARD_SECRET", "not-the-secret")
    monkeypatch.setattr(fwd, "_http_session", None)
    try:
        await fwd.forward_cell_phone_event(_body())
    finally:
        await fwd.close()
    await _drain()
    assert fakes["notify"] == []
