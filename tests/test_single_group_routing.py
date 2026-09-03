"""Tests for the single-group build's routing: every event type, every vehicle, every
provider goes to the one configured group (config.GROUP_CHAT_ID) plus subscribed admin
DMs — there is no per-vehicle or per-provider group to pick between, unlike the
single-company branch this was forked from.
"""
import pytest

import utils.webhook_handler as wh
from tests.test_samsara import _FakeResp, _FakeSession

GROUP = -1001234567890


@pytest.fixture
def routed(monkeypatch):
    """Drives _handle_event with the DB and Telegram stubbed out, and records who the
    alert was addressed to."""
    sent: list[list[int]] = []

    async def _save_violation(**kwargs):
        pass

    async def _get_alert_target():
        return GROUP

    async def _get_subscribed_admins(event_type):
        return [777]

    async def _send_all(bot, chat_ids, text, media=None, is_video=False):
        sent.append(list(chat_ids))

    monkeypatch.setattr(wh, "save_violation", _save_violation)
    monkeypatch.setattr(wh, "get_alert_target", _get_alert_target)
    monkeypatch.setattr(wh, "get_subscribed_admins", _get_subscribed_admins)
    monkeypatch.setattr(wh, "_send_all", _send_all)
    monkeypatch.setattr(wh, "_get_camera_media_info", lambda event: ([], []))
    return sent


@pytest.mark.parametrize("event_type", ["speeding", "hard_brake", "harsh_turn",
                                        "cell_phone"])
async def test_every_event_type_reaches_the_one_group_and_the_dms(routed, event_type):
    await wh._handle_event(object(), {"id": 1, "type": event_type,
                                      "vehicle": {"number": "1234"}})

    assert routed == [[GROUP, 777]]


async def test_a_crash_reaches_only_the_dms_never_the_group(routed):
    """Crashes are DM-only, same default as single-company: a group only sees a crash if
    a dedicated crash group is configured, which this build doesn't offer at all."""
    await wh._handle_event(object(), {"id": 2, "type": "crash", "vehicle": {"number": "1234"}})

    assert routed == [[777]]


async def test_a_withdrawn_crash_still_reaches_the_group_as_a_hard_brake(routed):
    """A crash Motive later withdraws is re-typed to hard_brake before routing runs, so
    it takes the normal group route instead of the crash DM-only one."""
    await wh._handle_event(object(), {"id": 3, "type": "crash", "vehicle": {"number": "1234"},
                                      "_type_override": "hard_brake"})

    assert routed == [[GROUP, 777]]


async def test_a_muted_group_gets_nothing_but_dms_still_do(routed, monkeypatch):
    async def _muted():
        return None

    monkeypatch.setattr(wh, "get_alert_target", _muted)
    await wh._handle_event(object(), {"id": 5, "type": "speeding", "vehicle": {"number": "1234"}})

    assert routed == [[777]]


async def test_the_immediate_crash_card_and_the_video_follow_up_go_to_the_same_chats(
        routed, monkeypatch):
    """A Samsara crash is announced twice: the full details the moment the type resolves,
    then the clip once it has uploaded. Both halves are DM-only — neither should reach
    the group."""
    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(wh.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(wh, "_get_http_session", lambda: _FakeSession([
        _FakeResp(200, {"harshEventType": "Crash",
                        "downloadForwardVideoUrl": "f", "downloadInwardVideoUrl": "i"}),
    ]))

    await wh._handle_event(object(), {
        "id": 4, "type": "harsh_event", "vehicle": {"number": "1234"},
        "_samsara_vehicle_id": "v1", "_samsara_timestamp_ms": 1,
    }, samsara_api_key="key")

    assert len(routed) == 2, "expected the crash card and then the video follow-up"
    for targets in routed:
        assert GROUP not in targets
        assert 777 in targets
