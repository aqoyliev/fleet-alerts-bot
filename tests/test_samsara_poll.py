"""Async tests for the Samsara harsh-event poll loop (_fetch_samsara_harsh_event).

aiohttp is stubbed in conftest, so we replace the module-level _http_session with a
fake whose .get() returns an async context manager yielding a canned response, and we
patch asyncio.sleep to a no-op so the 20s waits are instant.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import utils.webhook_handler as wh


# ── fakes ─────────────────────────────────────────────────────────────────────────

def _resp(status=200, json_data=None):
    r = MagicMock()
    r.status = status
    r.json = AsyncMock(return_value=json_data or {})
    r.text = AsyncMock(return_value="")
    return r

def _async_cm(resp):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm

def _session(responses):
    """Fake aiohttp session. responses is a list of canned responses, one per .get()
    call; the last one repeats for any further calls."""
    session = MagicMock()
    cms = [_async_cm(r) for r in responses]

    def _get(*args, **kwargs):
        return cms[min(session.get.call_count - 1, len(cms) - 1)]

    session.get = MagicMock(side_effect=_get)
    return session


@pytest.fixture(autouse=True)
def _no_sleep_and_key(monkeypatch):
    # Kill the 20s waits and give the header an API key (config stub has none).
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(wh.config, "SAMSARA_API_KEY", "test", raising=False)


def _install(monkeypatch, responses):
    session = _session(responses)
    monkeypatch.setattr(wh, "_http_session", session)
    return session


# ── tests ───────────────────────────────────────────────────────────────────────

async def test_both_urls_returns_immediately(monkeypatch):
    data = {"harshEventType": "Harsh Braking",
            "downloadForwardVideoUrl": "fwd", "downloadInwardVideoUrl": "in"}
    session = _install(monkeypatch, [_resp(json_data=data)])
    result = await wh._fetch_samsara_harsh_event("veh1", 123)
    assert result == data
    assert session.get.call_count == 1  # no extra polling once both clips are ready

async def test_inward_only_short_circuits_on_inward(monkeypatch):
    # "Mobile Usage" → cell_phone (inward-only): the inward clip alone is enough.
    data = {"harshEventType": "Mobile Usage", "downloadInwardVideoUrl": "in"}
    session = _install(monkeypatch, [_resp(json_data=data)])
    result = await wh._fetch_samsara_harsh_event("veh1", 123)
    assert result == data
    assert session.get.call_count == 1  # didn't wait for a forward URL that never comes

async def test_crash_extends_window_to_15_and_never_early_bails(monkeypatch):
    # Crash with no clips ever: must poll the full extended window, not bail at 3.
    data = {"harshEventType": "Crash"}
    session = _install(monkeypatch, [_resp(json_data=data)])
    result = await wh._fetch_samsara_harsh_event("veh1", 123)
    assert session.get.call_count == 15
    assert result == data  # falls back to last_data after the window

async def test_http_error_gives_up_after_one_call(monkeypatch):
    session = _install(monkeypatch, [_resp(status=500)])
    result = await wh._fetch_samsara_harsh_event("veh1", 123)
    assert result is None
    assert session.get.call_count == 1

async def test_standard_event_no_media_bails_at_three(monkeypatch):
    # Forward-facing type (hard_brake) with no clips ever → bail at attempt 3.
    data = {"harshEventType": "Harsh Braking"}
    session = _install(monkeypatch, [_resp(json_data=data)])
    result = await wh._fetch_samsara_harsh_event("veh1", 123)
    assert session.get.call_count == 3
    assert result == data  # sends with no media rather than losing the alert


# ── on_first hook ─────────────────────────────────────────────────────────────────

async def test_on_first_invoked_once_with_first_typed_response(monkeypatch):
    data = {"harshEventType": "Harsh Braking",
            "downloadForwardVideoUrl": "f", "downloadInwardVideoUrl": "i"}
    _install(monkeypatch, [_resp(json_data=data)])
    hook = AsyncMock()
    await wh._fetch_samsara_harsh_event("veh1", 123, on_first=hook)
    hook.assert_awaited_once()
    assert hook.await_args.args[0] == data

async def test_on_first_not_invoked_on_obstructed(monkeypatch):
    _install(monkeypatch, [_resp(json_data={"harshEventType": "Obstructed Camera"})])
    hook = AsyncMock()
    result = await wh._fetch_samsara_harsh_event("veh1", 123, on_first=hook)
    assert result is None
    hook.assert_not_awaited()  # obstructed events are dropped before the hook

async def test_on_first_invoked_once_across_crash_window(monkeypatch):
    # Crash with no media ever: polls the full 15-attempt window but the hook (which
    # persists + fires the pending alert) must fire exactly once.
    session = _install(monkeypatch, [_resp(json_data={"harshEventType": "Crash"})])
    hook = AsyncMock()
    await wh._fetch_samsara_harsh_event("veh1", 123, on_first=hook)
    assert session.get.call_count == 15
    hook.assert_awaited_once()


# ── _handle_event: early persist + crash pending ──────────────────────────────────

def _patch_handle_event_deps(monkeypatch, groups, dms):
    save = AsyncMock()
    sent_text = []

    async def _fake_send_text(bot, chat_id, text, retries, delay):
        sent_text.append((chat_id, text))

    monkeypatch.setattr(wh, "save_violation", save)
    monkeypatch.setattr(wh, "get_groups_for_event", AsyncMock(return_value=groups))
    monkeypatch.setattr(wh, "get_subscribed_admins", AsyncMock(return_value=dms))
    monkeypatch.setattr(wh, "_send_text", _fake_send_text)
    monkeypatch.setattr(wh, "_download", AsyncMock(return_value=b"vid"))
    send_full = AsyncMock(return_value=None)
    monkeypatch.setattr(wh, "_send_with_retry", send_full)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(wh.config, "SAMSARA_API_KEY", "test", raising=False)
    return save, sent_text, send_full


def _samsara_event():
    return {
        "id": "uuid-1", "type": "harsh_event", "_source": "samsara",
        "_samsara_vehicle_id": "v1", "_samsara_timestamp_ms": 123,
        "vehicle": {"number": "528609"}, "driver": {"name": ""},
        "start_time": "2026-04-29T09:15:31Z",
    }

async def test_handle_event_crash_full_alert_first_then_video(monkeypatch):
    save, sent_text, send_full = _patch_handle_event_deps(monkeypatch, groups=[111], dms=[222])
    # attempt 1: Crash, no media (fires hook → early save + FULL text alert)
    # attempt 2: Crash, both URLs (video follow-up)
    r1 = _resp(json_data={"harshEventType": "Crash", "location": {"address": "I-95 N"}})
    r2 = _resp(json_data={"harshEventType": "Crash", "downloadForwardVideoUrl": "f",
                          "downloadInwardVideoUrl": "i", "location": {"address": "I-95 N"}})
    monkeypatch.setattr(wh, "_http_session", _session([r1, r2]))

    await wh._handle_event(MagicMock(), _samsara_event(), company_slug="hf")

    # persisted exactly once, as a crash, before the poll finished
    save.assert_awaited_once()
    assert save.await_args.kwargs["event_type"] == "crash"
    # FIRST message: full details to both targets (not a bare "pending" line)
    assert {cid for cid, _ in sent_text} == {111, 222}
    assert all("CRASH DETECTED" in t for _, t in sent_text)
    assert all("I-95 N" in t for _, t in sent_text)        # full card has the location
    # SECOND message: the video, SHORT caption (first alert already had the location),
    # to both targets
    assert send_full.await_count == 2
    assert all("💥 <b>CRASH</b>" in c.args[2] and "DETECTED" not in c.args[2]
               for c in send_full.await_args_list)

async def test_handle_event_noncrash_persists_early_without_pending(monkeypatch):
    save, sent_text, send_full = _patch_handle_event_deps(monkeypatch, groups=[111], dms=[])
    data = {"harshEventType": "Harsh Braking", "downloadForwardVideoUrl": "f",
            "downloadInwardVideoUrl": "i"}
    monkeypatch.setattr(wh, "_http_session", _session([_resp(json_data=data)]))

    await wh._handle_event(MagicMock(), _samsara_event(), company_slug="hf")

    save.assert_awaited_once()
    assert save.await_args.kwargs["event_type"] == "hard_brake"
    assert sent_text == []          # non-crash → no early alert
    assert send_full.await_count == 1

async def test_handle_event_crash_video_caption_falls_back_to_full_when_location_late(monkeypatch):
    save, sent_text, send_full = _patch_handle_event_deps(monkeypatch, groups=[111], dms=[222])
    # attempt 1: Crash, NO location yet (first alert can't include it)
    # attempt 2: Crash, both URLs + location now present
    r1 = _resp(json_data={"harshEventType": "Crash"})
    r2 = _resp(json_data={"harshEventType": "Crash", "downloadForwardVideoUrl": "f",
                          "downloadInwardVideoUrl": "i", "location": {"address": "I-95 N"}})
    monkeypatch.setattr(wh, "_http_session", _session([r1, r2]))

    await wh._handle_event(MagicMock(), _samsara_event(), company_slug="hf")

    # First alert went out WITHOUT the location...
    assert all("I-95 N" not in t for _, t in sent_text)
    # ...so the video follow-up upgrades to the full caption to deliver it.
    assert send_full.await_count == 2
    assert all("I-95 N" in c.args[2] and "CRASH DETECTED" in c.args[2]
               for c in send_full.await_args_list)

async def test_handle_event_crash_no_media_full_alert_then_closure_note(monkeypatch):
    save, sent_text, send_full = _patch_handle_event_deps(monkeypatch, groups=[111], dms=[222])
    # Crash that never produces a clip → polls the full window, no media.
    monkeypatch.setattr(wh, "_http_session",
                        _session([_resp(json_data={"harshEventType": "Crash", "location": {"address": "I-95 N"}})]))

    await wh._handle_event(MagicMock(), _samsara_event(), company_slug="hf")

    # First alert: full details reached BOTH groups and DMs at detection.
    full_alerts = [(cid, t) for cid, t in sent_text if "CRASH DETECTED" in t]
    assert {cid for cid, _ in full_alerts} == {111, 222}
    assert all("I-95 N" in t for _, t in full_alerts)
    # No video → no clip is sent (the video path stays untouched)...
    assert send_full.await_count == 0
    # ...but a short closure note tells the SAME targets no video is coming, so they're
    # not left waiting on the first alert's "Video pending…" line.
    closure = [(cid, t) for cid, t in sent_text if "No video available" in t]
    assert {cid for cid, _ in closure} == {111, 222}
