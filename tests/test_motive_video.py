"""Tests for the Motive clip follow-up, and for tailgating being an event at all.

Motive sends the webhook the instant its detector trips, which is before the dashcam clip
has been transcoded: the delivery says a clip exists and every URL in it is still null.
Motive then re-delivers the event once the clip is ready -- under the same id, which the
durable dedup has already recorded, so that delivery is refused as a duplicate and the
video never arrived at all. The alert went out with no footage and nothing to say about
why.

So the clip is now fetched, and the alert waits for it: one message with the card and the
footage in it. Sending the card first and the clip behind it was tried and reads wrong --
the video is part of the alert, not a second alert about the same thing.

  * Only one state is polled -- Motive saying "a clip exists, not ready yet" -- so an
    alert that was never going to have footage is not delayed looking for any.
  * A clip that never turns up costs the wait and nothing else: the same card goes out
    that would have gone out anyway.
"""
import pytest

from data import config
import utils.motive as mot
import utils.webhook_handler as wh
from data.event_catalog import GROUP_FILTER_TYPE_SET
from tests.test_samsara import _FakeResp, _FakeSession


def _event(**over):
    """A Motive hard-brake delivery whose clip is still transcoding."""
    event = {
        "id": 3328325693,
        "type": "hard_brake",
        "start_time": "2026-04-08T11:28:52Z",
        "current_vehicle": {"id": 2238865, "number": "1269"},
        "metadata": {"severity": "high"},
        "camera_media": {
            "id": 3328325693,
            "available": True,
            "downloadable_videos": {
                "front_facing_plain_url": None,
                "driver_facing_plain_url": None,
            },
            "auto_transcode_status": "not started",
        },
    }
    event.update(over)
    return event


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setattr(config, "MOTIVE_API_KEY", "test-key")


# -- which events are worth going back for ------------------------------------

def test_a_clip_that_is_still_transcoding_is_worth_waiting_for(api_key):
    assert wh._motive_clip_is_coming(_event()) is True


def test_motive_saying_there_is_no_footage_is_taken_at_its_word(api_key):
    """available=false is an answer, not a delay. Polling it would promise a video to
    every truck that has no camera on it."""
    event = _event()
    event["camera_media"]["available"] = False
    assert wh._motive_clip_is_coming(event) is False


def test_a_payload_with_no_camera_block_is_not_polled(api_key):
    """A speeding event: no camera_media, and no type to look itself up by either."""
    assert wh._motive_clip_is_coming(
        {"id": 1, "action": "speeding_event_created"}) is False


def test_an_event_that_already_has_its_urls_needs_nothing(api_key):
    event = _event()
    event["camera_media"]["downloadable_videos"]["front_facing_plain_url"] = "https://x/f.mp4"
    assert wh._motive_clip_is_coming(event) is False


def test_a_samsara_event_has_its_own_poll(api_key):
    assert wh._motive_clip_is_coming(_event(_source="samsara")) is False


def test_without_an_api_key_there_is_nothing_to_ask(monkeypatch):
    monkeypatch.setattr(config, "MOTIVE_API_KEY", "")
    assert wh._motive_clip_is_coming(_event()) is False


# -- the follow-up itself ------------------------------------------------------

@pytest.fixture
def poll(monkeypatch, api_key):
    """Runs the wait with no real waiting. The test scripts the lookups through
    state["script"] and reads what was slept out of state["slept"]."""
    state = {"slept": [], "lookups": 0}

    async def _no_sleep(seconds):
        state["slept"].append(seconds)

    def _script(results):
        async def _find(key, event_id, occurred_at, event_types):
            state["lookups"] += 1
            state["asked_for"] = event_types
            return results[min(state["lookups"] - 1, len(results) - 1)]
        monkeypatch.setattr(wh, "find_performance_event", _find)

    monkeypatch.setattr(wh.asyncio, "sleep", _no_sleep)
    state["script"] = _script
    return state


def _ready_row():
    return {"id": 3328325693, "camera_media": {
        "available": True,
        "downloadable_videos": {"front_facing_plain_url": "https://x/f.mp4",
                                "driver_facing_plain_url": "https://x/d.mp4"},
    }}


def _still_transcoding_row():
    return {"id": 3328325693, "camera_media": {
        "available": True,
        "downloadable_videos": {"front_facing_plain_url": None,
                                "driver_facing_plain_url": None},
        "auto_transcode_status": "in progress",
    }}


async def test_a_ready_clip_comes_back_as_its_urls(poll):
    poll["script"]([("found", _ready_row())])
    assert await wh._wait_for_motive_clip(_event()) == (
        ["https://x/f.mp4", "https://x/d.mp4"], [])


async def test_a_row_that_omits_the_available_flag_is_still_a_clip(poll):
    """The flag was already read off the delivery. Requiring the API row to repeat it
    would make the wait depend on two payload shapes agreeing."""
    row = _ready_row()
    del row["camera_media"]["available"]
    poll["script"]([("found", row)])

    video_urls, _ = await wh._wait_for_motive_clip(_event())
    assert video_urls == ["https://x/f.mp4", "https://x/d.mp4"]


async def test_it_keeps_asking_while_the_clip_transcodes(poll):
    poll["script"]([("found", _still_transcoding_row()),
                    ("found", _still_transcoding_row()),
                    ("found", _ready_row())])

    video_urls, _ = await wh._wait_for_motive_clip(_event())
    assert poll["lookups"] == 3
    assert video_urls


async def test_a_failed_lookup_is_not_a_verdict(poll):
    """Unlike the crash check, a miss here means Motive's index has not caught up: the
    window is keyed on this event's own day and type. So it asks again."""
    poll["script"]([("error", None), ("absent", None), ("found", _ready_row())])

    video_urls, _ = await wh._wait_for_motive_clip(_event())
    assert poll["lookups"] == 3
    assert video_urls


async def test_a_clip_that_never_arrives_costs_the_wait_and_nothing_else(poll):
    poll["script"]([("found", _still_transcoding_row())])

    assert await wh._wait_for_motive_clip(_event()) == ([], [])
    assert poll["lookups"] == len(wh._MOTIVE_VIDEO_PROBES)


async def test_it_asks_under_the_type_motive_filed_the_event_as(poll):
    """A critical hard_brake is a crash to the bot and a hard_brake to Motive. Asking
    for the routed type would look the event up under a name Motive never gave it."""
    event = _event(metadata={"severity": "critical"})
    assert wh._get_event_type(event) == "crash"

    poll["script"]([("found", _ready_row())])
    await wh._wait_for_motive_clip(event)

    assert poll["asked_for"] == "hard_brake"


async def test_the_waits_add_up_to_the_probe_marks(poll):
    poll["script"]([("found", _still_transcoding_row())])
    await wh._wait_for_motive_clip(_event())

    # Each sleep is the gap to the next mark, not the mark itself.
    marks = wh._MOTIVE_VIDEO_PROBES
    assert poll["slept"] == [marks[0], *(b - a for a, b in zip(marks, marks[1:]))]


# -- one message, card and clip together ---------------------------------------

@pytest.fixture
def alerted(monkeypatch, api_key):
    """Drives _handle_event with the database and Telegram stubbed out, recording every
    message it sent and whether the clip was waited for."""
    state = {"sent": [], "waited": 0}

    async def _save_violation(**kwargs):
        return True

    async def _groups(event_type, vehicle_number=None):
        return [-100111]

    async def _admins(event_type):
        return [777]

    async def _send_all(bot, chat_ids, text, media=None, is_video=False):
        state["sent"].append({"text": text, "media": media, "is_video": is_video})

    async def _wait(event):
        state["waited"] += 1
        return state.get("urls", (["https://x/f.mp4"], []))

    async def _download(video_urls, image_urls):
        return ([b"clip"] if video_urls or image_urls else []), bool(video_urls)

    monkeypatch.setattr(wh, "save_violation", _save_violation)
    monkeypatch.setattr(wh, "get_groups_for_event", _groups)
    monkeypatch.setattr(wh, "get_subscribed_admins", _admins)
    monkeypatch.setattr(wh, "_send_all", _send_all)
    monkeypatch.setattr(wh, "_wait_for_motive_clip", _wait)
    monkeypatch.setattr(wh, "_download_media", _download)
    return state


async def test_the_card_and_the_clip_arrive_as_one_message(alerted):
    await wh._handle_event(object(), _event())

    assert alerted["waited"] == 1
    assert len(alerted["sent"]) == 1
    sent = alerted["sent"][0]
    assert sent["is_video"] is True
    assert sent["media"] == [b"clip"]
    # The whole card, not a bare caption: there is no earlier message to refer back to.
    assert "HARD BRAKE" in sent["text"]
    assert "1269" in sent["text"]


async def test_a_clip_that_never_came_still_sends_the_card_once(alerted):
    alerted["urls"] = ([], [])
    await wh._handle_event(object(), _event())

    assert len(alerted["sent"]) == 1
    assert "HARD BRAKE" in alerted["sent"][0]["text"]
    assert alerted["sent"][0]["media"] == []


async def test_an_event_with_no_footage_coming_is_not_delayed(alerted):
    event = _event()
    event["camera_media"]["available"] = False
    await wh._handle_event(object(), event)

    assert alerted["waited"] == 0
    assert len(alerted["sent"]) == 1


# -- tailgating ----------------------------------------------------------------

def test_tailgating_is_an_event_the_bot_handles():
    """Motive sends type='tailgating' and Samsara sends "Tailgating" -- the alias table
    has mapped it all along -- but it was missing from EVENT_TYPE_MAP, so the
    ALLOWED_TYPES gate dropped both with only a debug line to show for it."""
    assert "tailgating" in wh.ALLOWED_TYPES
    assert wh._SAMSARA_HARSH_TYPE_MAP["Tailgating"] == "tailgating"


def test_a_tailgating_card_has_a_title_of_its_own():
    card = wh._format_event({"id": 9003, "type": "tailgating",
                             "start_time": "2026-04-08T09:00:00Z",
                             "current_vehicle": {"number": "3030"}})
    assert "TAILGATING" in card
    assert "3030" in card


def test_a_group_can_filter_tailgating_like_any_other_type():
    """A type that routes but cannot be toggled is a type a group cannot turn off."""
    assert "tailgating" in GROUP_FILTER_TYPE_SET


# -- the lookup the follow-up and the crash check now share --------------------

@pytest.fixture
def fake_api(monkeypatch):
    def _install(responses):
        session = _FakeSession(responses)
        monkeypatch.setattr(mot.aiohttp, "ClientSession", lambda *a, **k: session)
        return session
    return _install


async def test_the_lookup_hands_back_the_row_it_found(fake_api):
    fake_api([_FakeResp(200, {"driver_performance_events": [
        {"driver_performance_event": {"id": 42, "camera_media": {"available": True}}},
    ], "total": 1})])
    status, row = await mot.find_performance_event("k", 42, "2026-04-08T09:00:00Z",
                                                  "hard_brake")
    assert status == "found"
    assert row["camera_media"] == {"available": True}


async def test_an_empty_window_is_an_absence_not_an_error(fake_api):
    fake_api([_FakeResp(200, {"driver_performance_events": [], "total": 0})])
    assert await mot.find_performance_event(
        "k", 42, "2026-04-08T09:00:00Z", "crash") == ("absent", None)


async def test_a_bad_status_is_an_error_not_an_absence(fake_api):
    fake_api([_FakeResp(500, {})])
    assert await mot.find_performance_event(
        "k", 42, "2026-04-08T09:00:00Z", "crash") == ("error", None)


@pytest.mark.parametrize("status, expected", [
    ("found", True), ("absent", False), ("error", None),
])
async def test_the_crash_verdict_still_reads_the_three_outcomes(monkeypatch, status,
                                                                expected):
    """The crash check now sits on top of the shared lookup. Its contract is unchanged:
    an error stays None, because no evidence is not evidence of no crash."""
    async def _find(key, event_id, occurred_at, event_types):
        assert event_types == "crash"
        return status, None
    monkeypatch.setattr(mot, "find_performance_event", _find)

    assert await mot.crash_still_listed("k", 1, "2026-04-08T09:00:00Z") is expected
