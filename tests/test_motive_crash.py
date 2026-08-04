"""Tests for Motive crash confirmation in utils/webhook_handler.py.

Motive fires its crash webhook the instant the detector trips, before review runs, and
withdraws the detection from its API when review rejects it. Measured 2026-07-29..08-01:
59 of 62 crash webhooks we alerted on were gone from the API afterwards. These cover the
downgrade that follows from that, driven through a faked lookup so nothing sleeps or
touches the network.

Single-company build: the Motive token comes from config.MOTIVE_API_KEY rather than a
per-company DB row, so there is no company slug threaded through any of this.
"""
import asyncio
from datetime import datetime, timedelta, timezone

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


def test_unconfirmed_note_is_crash_only():
    """A downgraded detection routes on as hard_brake — it must not carry the crash
    caveat into a hard-brake card."""
    event = {"type": "hard_brake", "vehicle": {"number": "2460"},
             "_crash_unconfirmed": True}
    assert "Unconfirmed" not in wh._format_event(event)


# ── confirmation ───────────────────────────────────────────────────────────────

@pytest.fixture
def no_sleep(monkeypatch):
    """Records what would have been slept, so the resume tests can assert on it."""
    slept = []

    async def _instant(seconds):
        slept.append(seconds)

    monkeypatch.setattr(wh.asyncio, "sleep", _instant)
    return slept


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setattr(wh.config, "MOTIVE_API_KEY", "test-key")


def _lookup_returning(monkeypatch, value):
    calls = []

    async def _fake(key, event_id, occurred_at):
        calls.append(event_id)
        return value

    monkeypatch.setattr(wh, "crash_still_listed", _fake)
    return calls


async def test_still_listed_confirms_the_crash(monkeypatch, no_sleep, api_key):
    _lookup_returning(monkeypatch, True)
    assert await wh._motive_crash_is_real({"id": 1588349106}) is True


async def test_withdrawn_detection_is_rejected(monkeypatch, no_sleep, api_key):
    _lookup_returning(monkeypatch, False)
    assert await wh._motive_crash_is_real({"id": 1590006245}) is False


async def test_decision_is_taken_at_the_final_probe(monkeypatch, no_sleep, api_key):
    """The intermediate look-ups are observations for tuning the delay — a detection
    still present at 60s but withdrawn by 180s must come out False."""
    results = iter([True, True, False])

    async def _fake(key, event_id, occurred_at):
        return next(results)

    monkeypatch.setattr(wh, "crash_still_listed", _fake)
    assert await wh._motive_crash_is_real({"id": 1}) is False


async def test_lookup_failure_fails_open(monkeypatch, no_sleep, api_key):
    """No evidence is not evidence of no crash."""
    _lookup_returning(monkeypatch, None)
    assert await wh._motive_crash_is_real({"id": 1}) is None


async def test_missing_api_key_fails_open_without_calling_the_api(monkeypatch, no_sleep):
    """MOTIVE_API_KEY is optional in .env — an unset one must not swallow crashes."""
    monkeypatch.setattr(wh.config, "MOTIVE_API_KEY", "")
    calls = _lookup_returning(monkeypatch, True)
    assert await wh._motive_crash_is_real({"id": 1}) is None
    assert calls == []


async def test_fresh_confirmation_waits_through_every_mark(monkeypatch, no_sleep, api_key):
    _lookup_returning(monkeypatch, True)
    await wh._motive_crash_is_real({"id": 1})
    assert no_sleep == [60, 60, 60]  # 0 → 60 → 120 → 180


# ── resume after a restart ─────────────────────────────────────────────────────

async def test_resume_only_waits_out_the_time_still_owed(monkeypatch, no_sleep, api_key):
    """A crash held 100s before the restart has 80s of the 180s left, not another 180."""
    calls = _lookup_returning(monkeypatch, True)
    assert await wh._motive_crash_is_real({"id": 1}, 100.0) is True
    assert no_sleep == [20, 60]  # 100 → 120 (observation), 120 → 180 (verdict)
    assert len(calls) == 2


async def test_resume_past_the_delay_decides_immediately(monkeypatch, no_sleep, api_key):
    """Down longer than the whole wait: Motive's review is long since in, so ask now."""
    calls = _lookup_returning(monkeypatch, False)
    assert await wh._motive_crash_is_real({"id": 1}, 900.0) is False
    assert no_sleep == []          # nothing left to wait for
    assert len(calls) == 1         # and no pointless observation look-ups


def test_verdict_names_cover_every_outcome():
    assert wh._verdict_name(True) == wh.VERDICT_CONFIRMED
    assert wh._verdict_name(False) == wh.VERDICT_WITHDRAWN
    assert wh._verdict_name(None) == wh.VERDICT_UNKNOWN


async def test_bookkeeping_failure_never_reaches_the_alert_path():
    """A database that missed the migration can be missing the table entirely.
    _handle_event catches everything — an unguarded raise here would take the crash
    alert down with it."""
    async def _fails():
        raise RuntimeError("relation motive_crash_confirmations does not exist")

    await wh._note_crash(_fails(), 1588349106)  # must not raise


@pytest.fixture
def captured_resumes(monkeypatch):
    """Stands in for _handle_event + record_verdict so resume can run without a DB."""
    resumed, verdicts = [], []

    async def _handle(bot, event, crash_resume_elapsed=None):
        resumed.append((event, crash_resume_elapsed))

    async def _verdict(event_id, verdict):
        verdicts.append((event_id, verdict))

    async def _counts(_since):
        return {}

    monkeypatch.setattr(wh, "_handle_event", _handle)
    monkeypatch.setattr(wh, "record_verdict", _verdict)
    monkeypatch.setattr(wh, "get_verdict_counts", _counts)
    return resumed, verdicts


def _pending(monkeypatch, *rows):
    async def _fake():
        return list(rows)
    monkeypatch.setattr(wh, "get_pending_confirmations", _fake)


def _row(event_id, age_seconds, payload):
    return {
        "event_id": event_id,
        "payload": payload,
        "detected_at": datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    }


async def test_interrupted_confirmation_is_resumed_with_its_elapsed_time(
        monkeypatch, captured_resumes):
    resumed, verdicts = captured_resumes
    _pending(monkeypatch, _row(1588349106, 90, {"id": 1588349106, "type": "crash"}))

    await wh.resume_pending_crash_confirmations(bot=None)
    await asyncio.sleep(0)  # let the created task run

    assert len(resumed) == 1
    event, elapsed = resumed[0]
    assert event["id"] == 1588349106
    assert 90 <= elapsed < 120       # carried across, not restarted from zero
    assert verdicts == []            # still undecided — the resumed run decides it


async def test_resumed_payload_is_decoded_when_asyncpg_hands_back_text(
        monkeypatch, captured_resumes):
    """JSONB comes out as a str without a codec registered, and _handle_event needs a dict."""
    resumed, _ = captured_resumes
    _pending(monkeypatch, _row(1, 10, '{"id": 1, "type": "crash"}'))

    await wh.resume_pending_crash_confirmations(bot=None)
    await asyncio.sleep(0)

    assert resumed[0][0] == {"id": 1, "type": "crash"}


async def test_stale_pending_crash_is_expired_not_alerted(monkeypatch, captured_resumes):
    """Past _CRASH_RESUME_MAX_AGE the bot was down, and a crash DM that late is noise."""
    resumed, verdicts = captured_resumes
    old = wh._CRASH_RESUME_MAX_AGE + 60
    _pending(monkeypatch, _row(42, old, {"id": 42, "type": "crash"}))

    await wh.resume_pending_crash_confirmations(bot=None)
    await asyncio.sleep(0)

    assert resumed == []
    assert verdicts == [(42, wh.VERDICT_EXPIRED)]


async def test_resume_survives_an_unreachable_database(monkeypatch, captured_resumes):
    """A DB hiccup at startup must not take the bot down with it."""
    resumed, _ = captured_resumes

    async def _boom():
        raise RuntimeError("pool not ready")

    monkeypatch.setattr(wh, "get_pending_confirmations", _boom)
    await wh.resume_pending_crash_confirmations(bot=None)
    assert resumed == []


async def test_one_unreadable_row_does_not_cost_the_others(monkeypatch, captured_resumes):
    """This runs inside on_startup, so a single bad payload must not stop the boot —
    nor the crash sitting behind it in the queue."""
    resumed, _ = captured_resumes
    _pending(monkeypatch,
             _row(1, 30, "{not json at all"),
             _row(2, 30, {"id": 2, "type": "crash"}))

    await wh.resume_pending_crash_confirmations(bot=None)
    await asyncio.sleep(0)

    assert [e["id"] for e, _a in resumed] == [2]
