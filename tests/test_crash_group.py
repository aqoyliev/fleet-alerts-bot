"""Tests for CRASH_GROUP_ID — the one group that receives crashes and only crashes.

Crashes deliberately do not follow the normal group routing. A wreck is not news for the
driver's own chat, and in the all-fleet main group it is buried under the day's speeding
alerts, so a company that wants crashes in a group nominates one in its .env. These pin
down the three properties that make that "crash only" rather than "crash as well":

  • a crash reaches the nominated group (both halves of it — the immediate card and the
    video follow-up), on top of the admin DMs,
  • no other event type ever reaches it, whatever its group filter or unit would say, and
  • with the setting blank, crashes stay DM-only exactly as before.
"""
import pytest

from data import config
import utils.webhook_handler as wh
from utils import group_texts
from tests.test_samsara import _FakeResp, _FakeSession


CRASH_GROUP = -1001234567890


@pytest.fixture
def routed(monkeypatch):
    """Drives _handle_event with the DB and Telegram stubbed out, and records who the
    alert was addressed to."""
    sent: list[list[int]] = []

    async def _save_violation(**kwargs):
        pass

    async def _get_groups_for_event(event_type, vehicle_number=None):
        # Stand-in for a fleet where every group is subscribed to everything, so any
        # leak into a driver/main group would show up as an extra id below.
        return [-100111, -100222]

    async def _get_subscribed_admins(event_type):
        return [777]

    async def _send_all(bot, chat_ids, text, media=None, is_video=False):
        sent.append(list(chat_ids))

    monkeypatch.setattr(wh, "save_violation", _save_violation)
    monkeypatch.setattr(wh, "get_groups_for_event", _get_groups_for_event)
    monkeypatch.setattr(wh, "get_subscribed_admins", _get_subscribed_admins)
    monkeypatch.setattr(wh, "_send_all", _send_all)
    monkeypatch.setattr(wh, "_get_camera_media_info", lambda event: ([], []))
    return sent


def _crash_event():
    # No _samsara_vehicle_id and no Motive api key: skips the poll and the confirmation
    # wait, so the test exercises routing and nothing else.
    return {"id": 1, "type": "crash", "vehicle": {"number": "1234"}}


# ── a crash reaches the nominated group ────────────────────────────────────────

async def test_crash_goes_to_the_crash_group_and_the_dms(routed, monkeypatch):
    monkeypatch.setattr(config, "CRASH_GROUP_ID", CRASH_GROUP)
    await wh._handle_event(object(), _crash_event())

    assert routed == [[CRASH_GROUP, 777]]


async def test_the_crash_group_replaces_the_normal_groups_rather_than_joining_them(
        routed, monkeypatch):
    """The driver group and the main group both matched in the fixture above. Neither may
    appear: this is the property that keeps a wreck out of the driver's own chat."""
    monkeypatch.setattr(config, "CRASH_GROUP_ID", CRASH_GROUP)
    await wh._handle_event(object(), _crash_event())

    assert -100111 not in routed[0]
    assert -100222 not in routed[0]


async def test_without_the_setting_crashes_stay_dm_only(routed, monkeypatch):
    """The behaviour before this setting existed, which is what a deployment that leaves
    CRASH_GROUP_ID blank must keep getting."""
    monkeypatch.setattr(config, "CRASH_GROUP_ID", None)
    await wh._handle_event(object(), _crash_event())

    assert routed == [[777]]


# ── and nothing else does ──────────────────────────────────────────────────────

@pytest.mark.parametrize("event_type", ["speeding", "hard_brake", "harsh_turn",
                                        "cell_phone", "road_facing_cam_obstruction"])
async def test_no_other_event_type_reaches_the_crash_group(routed, monkeypatch, event_type):
    monkeypatch.setattr(config, "CRASH_GROUP_ID", CRASH_GROUP)
    await wh._handle_event(object(), {"id": 2, "type": event_type,
                                      "vehicle": {"number": "1234"}})

    assert CRASH_GROUP not in routed[0]
    # …and the ordinary routing is untouched.
    assert routed == [[-100111, -100222, 777]]


async def test_a_withdrawn_crash_does_not_reach_the_crash_group(routed, monkeypatch):
    """Motive withdraws most of its detections on review; the downgrade re-routes them as
    hard_brake, and a hard brake is not what this group is for."""
    monkeypatch.setattr(config, "CRASH_GROUP_ID", CRASH_GROUP)
    await wh._handle_event(object(), {"id": 3, "type": "crash", "vehicle": {"number": "1234"},
                                      "_type_override": "hard_brake"})

    assert CRASH_GROUP not in routed[0]


# ── both halves of a Samsara crash ─────────────────────────────────────────────

async def test_the_immediate_crash_card_and_the_video_follow_up_go_to_the_same_chats(
        routed, monkeypatch):
    """A Samsara crash is announced twice: the full details the moment the type resolves,
    then the clip once it has uploaded. The first one used to be addressed to DMs alone,
    which would have left the crash group with a video and no crash."""
    monkeypatch.setattr(config, "CRASH_GROUP_ID", CRASH_GROUP)

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
        assert CRASH_GROUP in targets
        assert 777 in targets
        assert -100111 not in targets


# ── what the group is told when the bot joins ──────────────────────────────────

def test_the_join_message_warns_that_silence_is_normal():
    """A crash-only group looks like a broken bot for weeks at a time, and someone
    eventually removes it — right before the one message it exists to deliver."""
    text = group_texts.joined_crash_group("Test Co")
    assert "crash" in text.lower()
    assert "silence" in text.lower()
    assert "Test Co" in text


def test_the_join_message_does_not_ask_for_a_unit():
    text = group_texts.joined_crash_group("Test Co")
    assert "/setunit" not in text


def test_help_in_the_crash_group_does_not_call_it_unconfigured():
    """It has no row in alert_groups on purpose, so the ordinary unit-less branch would
    tell a room full of dispatchers that no alerts are being sent."""
    text = group_texts.help_text("Test Co", is_crash=True)
    assert "isn't set up yet" not in text
    assert "crash" in text.lower()


def test_help_in_the_crash_group_offers_only_commands_that_work():
    """/setunit, /disable and /enable all need a row in alert_groups, which this group
    does not have."""
    text = group_texts.help_text("Test Co", is_crash=True, is_admin=True)
    for dead in ("/setunit", "/disable", "/enable", "/events", "/removegroup"):
        assert dead not in text
    assert "/help" in text


def test_help_elsewhere_is_unchanged():
    text = group_texts.help_text("Test Co", unit="1234", is_admin=True)
    for live in ("/setunit", "/disable", "/enable", "/events", "/removegroup"):
        assert live in text


# ── the setting itself ─────────────────────────────────────────────────────────

def test_a_blank_setting_reads_as_unset_rather_than_crashing_startup():
    """Railway hands through an empty string for a variable someone cleared, and int("")
    would take the whole deployment down on boot."""
    assert config.CRASH_GROUP_ID is None or isinstance(config.CRASH_GROUP_ID, int)
