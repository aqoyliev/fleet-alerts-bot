"""Tests for Motive crash confirmation in utils/webhook_handler.py.

Motive fires its crash webhook the instant the detector trips, before review runs, and
withdraws the detection from its API when review rejects it. Measured 2026-07-29..08-01:
59 of 62 crash webhooks we alerted on were gone from the API afterwards. These cover the
downgrade that follows from that, driven through a faked lookup so nothing sleeps or
touches the network.
"""
import pytest

import utils.webhook_handler as wh


# ── classification ─────────────────────────────────────────────────────────────

def test_type_override_wins_over_payload_type():
    """The withdrawal downgrade has to survive _format_event re-deriving the type."""
    event = {"type": "crash", "_type_override": "hard_brake"}
    assert wh._get_event_type(event) == "hard_brake"


def test_type_override_blocks_critical_hard_brake_repromotion():
    """Without the override checked first, the critical-severity rule would put a
    withdrawn detection straight back into the crash channel."""
    event = {"type": "hard_brake", "severity": "critical", "_type_override": "hard_brake"}
    assert wh._get_event_type(event) == "hard_brake"
    # …and the promotion still works when nothing was withdrawn.
    assert wh._get_event_type({"type": "hard_brake", "severity": "critical"}) == "crash"


def test_motive_native_crash_type_is_unchanged_without_override():
    assert wh._get_event_type({"type": "crash"}) == "crash"


# ── alert wording ──────────────────────────────────────────────────────────────

def test_unconfirmed_crash_card_says_so():
    event = {"type": "crash", "vehicle": {"number": "2460"}, "_crash_unconfirmed": True}
    text = wh._format_event(event)
    assert "Unconfirmed" in text


def test_confirmed_crash_card_carries_no_unconfirmed_note():
    event = {"type": "crash", "vehicle": {"number": "2460"}}
    assert "Unconfirmed" not in wh._format_event(event)


# ── dedup ──────────────────────────────────────────────────────────────────────

def test_motive_dedup_key_separates_media_from_nomedia():
    """Motive redelivers ~3x at 40s; the repeat that finally carries the clip must not
    be suppressed along with the byte-identical ones."""
    bare = {"id": 123}
    with_media = {
        "id": 123,
        "camera_media": {
            "available": True,
            "downloadable_videos": {"front_facing_plain_url": "https://x/v.mp4"},
        },
    }
    k_bare = wh._motive_dedup_key(bare, "jrd")
    k_media = wh._motive_dedup_key(with_media, "jrd")
    assert k_bare != k_media
    assert wh._motive_dedup_key(bare, "cross") != k_bare  # company scoped
    assert wh._motive_dedup_key({}, "jrd") == ""          # id-less is never deduped


# ── confirmation ───────────────────────────────────────────────────────────────

@pytest.fixture
def no_sleep(monkeypatch):
    async def _instant(_seconds):
        return None
    monkeypatch.setattr(wh.asyncio, "sleep", _instant)


@pytest.fixture
def api_key(monkeypatch):
    async def _key(_slug):
        return "test-key"
    monkeypatch.setattr(wh, "get_motive_api_key", _key)


def _lookup_returning(monkeypatch, value):
    calls = []

    async def _fake(key, event_id, occurred_at):
        calls.append(event_id)
        return value

    monkeypatch.setattr(wh, "crash_still_listed", _fake)
    return calls


async def test_still_listed_confirms_the_crash(monkeypatch, no_sleep, api_key):
    _lookup_returning(monkeypatch, True)
    assert await wh._motive_crash_is_real({"id": 1588349106}, "jrd") is True


async def test_withdrawn_detection_is_rejected(monkeypatch, no_sleep, api_key):
    _lookup_returning(monkeypatch, False)
    assert await wh._motive_crash_is_real({"id": 1590006245}, "cross") is False


async def test_decision_is_taken_at_the_final_probe(monkeypatch, no_sleep, api_key):
    """The intermediate look-ups are observations for tuning the delay — a detection
    still present at 60s but withdrawn by 180s must come out False."""
    results = iter([True, True, False])

    async def _fake(key, event_id, occurred_at):
        return next(results)

    monkeypatch.setattr(wh, "crash_still_listed", _fake)
    assert await wh._motive_crash_is_real({"id": 1}, "jrd") is False


async def test_lookup_failure_fails_open(monkeypatch, no_sleep, api_key):
    """No evidence is not evidence of no crash."""
    _lookup_returning(monkeypatch, None)
    assert await wh._motive_crash_is_real({"id": 1}, "jrd") is None


async def test_missing_api_key_fails_open_without_calling_the_api(monkeypatch, no_sleep):
    async def _no_key(_slug):
        return None

    monkeypatch.setattr(wh, "get_motive_api_key", _no_key)
    calls = _lookup_returning(monkeypatch, True)
    assert await wh._motive_crash_is_real({"id": 1}, "nokey") is None
    assert calls == []
