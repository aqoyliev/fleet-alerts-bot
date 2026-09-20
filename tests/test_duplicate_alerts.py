"""An event that has already been alerted on is not alerted on again.

Providers redeliver: Motive retries a delivery it thinks went unanswered and sends
speeding_event_updated under the same id as the _created it follows, and Samsara repeats
an eventId outside the in-memory window. The violations.event_id UNIQUE constraint always
caught those at the table — but the row was silently skipped and the message sent anyway,
so the fleet saw the same alert twice.

The rule now: the insert decides. A row that was actually inserted means "new, alert on
it"; a conflict means it has been handled already and the task stops. A database failure
deliberately breaks the other way — a duplicate alert is a nuisance, a missing one is the
product failing.
"""

import pytest

import utils.webhook_handler as wh


@pytest.fixture
def routed(monkeypatch):
    """_handle_event with Telegram and the group/DM lookups stubbed, recording sends."""
    sent: list[list[int]] = []

    async def _send_all(bot, chat_ids, text, media=None, is_video=False):
        sent.append(list(chat_ids))

    async def _groups(event_type, vehicle_number=None):
        return [-100111]

    async def _admins(event_type):
        return [777]

    monkeypatch.setattr(wh, "get_groups_for_event", _groups)
    monkeypatch.setattr(wh, "get_subscribed_admins", _admins)
    monkeypatch.setattr(wh, "_send_all", _send_all)
    monkeypatch.setattr(wh, "_get_camera_media_info", lambda event: ([], []))
    return sent


def _event(event_id=42):
    return {"id": event_id, "type": "hard_brake", "vehicle": {"number": "1234"}}


def _saves(monkeypatch, answers):
    """Stub save_violation with a scripted sequence of answers, recording its calls."""
    calls = []

    async def _save(**kwargs):
        calls.append(kwargs)
        answer = answers[min(len(calls) - 1, len(answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(wh, "save_violation", _save)
    return calls


async def test_a_new_event_is_alerted_on(routed, monkeypatch):
    _saves(monkeypatch, [True])
    await wh._handle_event(object(), _event())

    assert routed == [[-100111, 777]]


async def test_a_redelivered_event_is_not_alerted_on_twice(routed, monkeypatch):
    """The second delivery finds its row already there and stops before any send."""
    _saves(monkeypatch, [True, False])

    await wh._handle_event(object(), _event())
    await wh._handle_event(object(), _event())

    assert routed == [[-100111, 777]]


async def test_a_database_failure_still_alerts(routed, monkeypatch):
    """Bookkeeping may fail; the alert may not. Better a duplicate than a silence."""
    _saves(monkeypatch, [RuntimeError("pool is gone")])

    await wh._handle_event(object(), _event())

    assert routed == [[-100111, 777]]


async def test_the_stored_row_describes_the_event_it_alerted_on(routed, monkeypatch):
    calls = _saves(monkeypatch, [True])
    await wh._handle_event(object(), _event(event_id=99))

    assert calls[0]["event_type"] == "hard_brake"
    assert calls[0]["vehicle_number"] == "1234"
    assert calls[0]["event_id"] == 99


async def test_an_event_with_no_id_is_always_treated_as_new(routed, monkeypatch):
    """No id means nothing to conflict with, so save_violation answers True and the
    alert goes out — the safe direction for a payload we can't deduplicate."""
    calls = _saves(monkeypatch, [True])
    await wh._handle_event(object(), {"type": "hard_brake", "vehicle": {"number": "1"}})

    assert calls[0]["event_id"] is None
    assert routed == [[-100111, 777]]
