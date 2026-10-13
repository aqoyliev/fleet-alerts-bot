"""Where an alert goes on this branch: the one alerts group, and the admins' DMs.

There is no per-vehicle group and no crash group to pick between, unlike single-company,
which this was taken from. What decides whether the group gets an alert is the group's
own state — configured, not muted, and the type on its filter — and get_alert_target
folds those into one answer. These tests drive _handle_event with that answer scripted.
"""
import pytest

import utils.webhook_handler as wh
from tests.test_samsara import _FakeResp, _FakeSession

GROUP = -1003786362177
ADMIN = 777


@pytest.fixture
def routed(monkeypatch):
    """Drives _handle_event with the DB and Telegram stubbed out, and records every
    send: who it was addressed to and what it said."""
    state = {"sent": [], "texts": [], "asked": [], "allowed": None}

    async def _save_violation(**kwargs):
        return True

    async def _get_alert_target(event_type=None):
        state["asked"].append(event_type)
        allowed = state["allowed"]
        return GROUP if allowed is None or event_type in allowed else None

    async def _get_subscribed_admins(event_type):
        return [ADMIN]

    async def _send_all(bot, chat_ids, text, media=None, is_video=False):
        state["sent"].append(list(chat_ids))
        state["texts"].append(text)

    async def _no_sleep(*_a, **_k):
        return None

    monkeypatch.setattr(wh, "save_violation", _save_violation)
    monkeypatch.setattr(wh, "get_alert_target", _get_alert_target)
    monkeypatch.setattr(wh, "get_subscribed_admins", _get_subscribed_admins)
    monkeypatch.setattr(wh, "_send_all", _send_all)
    monkeypatch.setattr(wh, "_get_camera_media_info", lambda event: ([], []))
    monkeypatch.setattr(wh.asyncio, "sleep", _no_sleep)
    return state


def _samsara_harsh(event_id=4):
    return {"id": event_id, "type": "harsh_event", "vehicle": {"number": "1234"},
            "_source": "samsara", "_samsara_vehicle_id": "v1", "_samsara_timestamp_ms": 1}


def _poll(monkeypatch, *payloads):
    monkeypatch.setattr(wh, "_get_http_session",
                        lambda: _FakeSession([_FakeResp(200, p) for p in payloads]))


# ── every vehicle, one group ───────────────────────────────────────────────────

@pytest.mark.parametrize("event_type", ["speeding", "hard_brake", "harsh_turn",
                                        "cell_phone", "crash"])
async def test_every_event_type_reaches_the_group_and_the_dms(routed, event_type):
    await wh._handle_event(object(), {"id": 1, "type": event_type, "_source": "samsara",
                                      "vehicle": {"number": "1234"}})

    assert routed["sent"] == [[GROUP, ADMIN]]


@pytest.mark.parametrize("unit", ["1234", "Unit 9", "", "TRK-77 O/O"])
async def test_the_vehicle_has_no_say_in_where_the_alert_goes(routed, unit):
    """Nothing is keyed on the unit: the group is asked for by event type alone."""
    await wh._handle_event(object(), {"id": 1, "type": "hard_brake",
                                      "vehicle": {"number": unit}})

    assert routed["sent"] == [[GROUP, ADMIN]]
    assert routed["asked"] == ["hard_brake"]


async def test_a_crash_goes_to_the_group_like_everything_else(routed):
    """The one deliberate departure from single-company, where a crash is kept out of
    every group. HF's group is its dispatch desk — it is exactly who a crash is for."""
    await wh._handle_event(object(), {"id": 2, "type": "crash", "_source": "samsara",
                                      "vehicle": {"number": "1234"}})

    assert routed["sent"] == [[GROUP, ADMIN]]
    assert routed["asked"] == ["crash"]


# ── what keeps an alert out of the group ───────────────────────────────────────

async def test_a_type_the_filter_leaves_out_reaches_only_the_dms(routed):
    routed["allowed"] = {"speeding"}
    await wh._handle_event(object(), {"id": 3, "type": "hard_brake",
                                      "vehicle": {"number": "1234"}})

    assert routed["sent"] == [[ADMIN]]


async def test_a_crash_the_filter_leaves_out_reaches_only_the_dms(routed):
    """Crash is on the filter like any other type, so it can be taken off it."""
    routed["allowed"] = {"speeding"}
    await wh._handle_event(object(), {"id": 3, "type": "crash", "_source": "samsara",
                                      "vehicle": {"number": "1234"}})

    assert routed["sent"] == [[ADMIN]]


async def test_a_muted_or_missing_group_gets_nothing_but_dms_still_do(routed):
    routed["allowed"] = set()      # get_alert_target answers None for everything
    await wh._handle_event(object(), {"id": 5, "type": "speeding",
                                      "vehicle": {"number": "1234"}})

    assert routed["sent"] == [[ADMIN]]


async def test_with_nobody_to_tell_nothing_is_sent(routed, monkeypatch):
    async def _nobody(event_type):
        return []

    routed["allowed"] = set()
    monkeypatch.setattr(wh, "get_subscribed_admins", _nobody)
    await wh._handle_event(object(), {"id": 6, "type": "speeding",
                                      "vehicle": {"number": "1234"}})

    assert routed["sent"] == []


# ── a Samsara crash is two messages, to the same chats ─────────────────────────

async def test_the_immediate_crash_card_and_the_video_follow_up_go_to_the_same_chats(
        routed, monkeypatch):
    """A Samsara crash is announced twice: the full details the moment the type
    resolves, then the clip once it has uploaded. Both halves go where a crash goes."""
    _poll(monkeypatch, {"harshEventType": "Crash",
                        "downloadForwardVideoUrl": "f", "downloadInwardVideoUrl": "i"})

    await wh._handle_event(object(), _samsara_harsh(), samsara_api_key="key")

    assert routed["sent"] == [[GROUP, ADMIN], [GROUP, ADMIN]]
    assert "Video pending" in routed["texts"][0]


async def test_a_filtered_out_crash_keeps_both_halves_out_of_the_group(routed, monkeypatch):
    routed["allowed"] = {"speeding"}
    _poll(monkeypatch, {"harshEventType": "Crash",
                        "downloadForwardVideoUrl": "f", "downloadInwardVideoUrl": "i"})

    await wh._handle_event(object(), _samsara_harsh(), samsara_api_key="key")

    assert routed["sent"] == [[ADMIN], [ADMIN]]


# ── an event Samsara never finished describing ─────────────────────────────────

async def test_an_empty_samsara_record_goes_to_the_dms_alone_and_says_why(routed, monkeypatch):
    """Samsara announced a harsh event and its record stayed {} for the whole poll: no
    type, no location, no clip. That is kept out of the group — a card saying "HARSH
    EVENT" and nothing else is noise there — but an admin who asked for DMs is told,
    marked as a probable error, so a run of them is noticed."""
    _poll(monkeypatch, {})

    await wh._handle_event(object(), _samsara_harsh(), samsara_api_key="key")

    assert routed["sent"] == [[ADMIN]]
    assert routed["texts"][0].startswith("🔍 <i>Possible error — incomplete Samsara data</i>")
    assert "HARSH EVENT" in routed["texts"][0]
    # The group was not even asked: its filter has no bearing on this.
    assert routed["asked"] == []


async def test_a_complete_record_is_not_marked_as_an_error(routed, monkeypatch):
    _poll(monkeypatch, {"harshEventType": "Harsh Braking", "location": {"address": "I-80"}})

    await wh._handle_event(object(), _samsara_harsh(), samsara_api_key="key")

    assert routed["sent"] == [[GROUP, ADMIN]]
    assert "Possible error" not in routed["texts"][0]
    assert "HARSH BRAKE" in routed["texts"][0]
    assert "No camera media available" in routed["texts"][0]


@pytest.mark.parametrize("harsh_type", ["Obstructed Camera", "Tailgating"])
async def test_a_type_hf_does_not_alert_on_is_dropped_whole(routed, monkeypatch, harsh_type):
    """Not stored, not sent to the group, not sent to a DM."""
    saved = []

    async def _save(**kwargs):
        saved.append(kwargs)
        return True

    monkeypatch.setattr(wh, "save_violation", _save)
    _poll(monkeypatch, {"harshEventType": harsh_type})

    await wh._handle_event(object(), _samsara_harsh(), samsara_api_key="key")

    assert routed["sent"] == []
    assert saved == []


# ── what is stored ─────────────────────────────────────────────────────────────

async def test_the_stored_unit_has_its_label_stripped(routed, monkeypatch):
    """Samsara's roster spells one truck three ways. The report groups by this string,
    so the label comes off before the row is written or one truck becomes three."""
    saved = []

    async def _save(**kwargs):
        saved.append(kwargs)
        return True

    monkeypatch.setattr(wh, "save_violation", _save)
    for spelled in ("1196", "Unit 1196", "unit #1196"):
        await wh._handle_event(object(), {"id": spelled, "type": "hard_brake",
                                          "vehicle": {"number": spelled}})

    assert [row["vehicle_number"] for row in saved] == ["1196", "1196", "1196"]


# ── speeding threshold ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("severity, alerted", [
    ("low", False), ("medium", True), ("high", True), ("critical", True),
])
async def test_speeding_is_alerted_from_medium_up(routed, severity, alerted):
    """HF's rule, and the default on this branch; single-company starts at high."""
    await wh._handle_event(object(), {"id": 8, "type": "speeding", "severity": severity,
                                      "vehicle": {"number": "1234"}})

    assert routed["sent"] == ([[GROUP, ADMIN]] if alerted else [])
