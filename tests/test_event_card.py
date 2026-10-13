"""Layout of the alert card built by utils.webhook_handler._format_event.

One card for every company on this branch: the title, a blank line, then one field per
row. They are meant to stay visibly unlike single-company's, which are ruled into blocks.
The wording of the measurements is covered elsewhere (test_speeding_enrichment,
test_motive_crash, test_samsara); what these lock down is the shape — the order of the
rows, how the unit is written, which rows are left out when there is nothing to put in
them — and the two things about a card that depend on the provider: what the event is
called, and whether the card says where it came from.
"""
import pytest

from data import config
from utils.webhook_handler import (
    _clean_vehicle, _format_crash_initial, _format_crash_video_caption, _format_event,
)


SPEEDING = {
    "action": "speeding_event_created",
    "id": 7,
    "avg_vehicle_speed": 114.0,
    "duration": 42,
    "start_time": "2026-04-08T11:29:59Z",
    "current_vehicle": {"Number": "375"},
    "driver": {"name": "John Smith"},
    "metadata": {"severity": "high"},
    "nominatim_location": "I-80 near Toledo, OH",
}


def _rows(text: str) -> list[str]:
    return text.split("\n")


def _row(text: str, label: str) -> int:
    return next(i for i, r in enumerate(_rows(text)) if label in r)


@pytest.fixture
def two_providers(monkeypatch):
    """A fleet that runs Samsara and Motive side by side, as MZ CARGO does."""
    monkeypatch.setattr(config, "SHOW_PROVIDER", True)


def test_title_is_followed_by_a_blank_line_not_a_rule():
    rows = _rows(_format_event(SPEEDING))
    assert rows[0] == "🚨 <b>SPEEDING OVER POSTED</b>"
    assert rows[1] == ""
    assert "─" not in _format_event(SPEEDING)


def test_row_order_is_unit_driver_severity_time_then_the_measurements():
    out = _format_event(SPEEDING)
    order = [_row(out, label) for label in
             ("Vehicle:", "Driver:", "Severity:", "Time:", "Average Speed:", "Location:")]
    assert order == sorted(order)


def test_severity_row_carries_its_label_emoji_and_its_level_emoji():
    assert "📊 <b>Severity:</b> 🔴 High" in _format_event(SPEEDING)


# ── what the event is called ───────────────────────────────────────────────────

@pytest.mark.parametrize("event_type, samsara, motive", [
    ("hard_brake", "🛑 <b>HARSH BRAKE</b>", "🛑 <b>HARD BRAKE</b>"),
    ("stop_sign_violation", "🛑 <b>ROLLING STOP</b>", "🛑 <b>STOP SIGN VIOLATION</b>"),
])
def test_an_event_is_titled_in_the_words_of_the_provider_that_reported_it(
        event_type, samsara, motive):
    """Samsara's dashboard says "Harsh Brake", Motive's says "Hard Brake". A card is
    where an admin starts before opening one of them, so it uses that one's word — on a
    fleet with both, two hard brakes minutes apart can rightly be titled differently."""
    event = {"type": event_type, "vehicle": {"number": "2460"}}
    assert _rows(_format_event({**event, "_source": "samsara"}))[0] == samsara
    assert _rows(_format_event(event))[0] == motive


def test_the_title_goes_by_the_event_not_by_how_the_fleet_is_set_up(monkeypatch):
    """config.EVENT_WORDING is for reports and the panel, which have no single event to
    ask. A card has one."""
    for fleet in ("samsara", "motive"):
        monkeypatch.setattr(config, "EVENT_WORDING", fleet)
        assert "HARD BRAKE" in _format_event({"type": "hard_brake"})
        assert "HARSH BRAKE" in _format_event({"type": "hard_brake", "_source": "samsara"})


def test_a_type_both_providers_name_alike_reads_the_same_from_either():
    for source in ({}, {"_source": "samsara"}):
        out = _format_event({"type": "cell_phone", "vehicle": {"number": "1"}, **source})
        assert _rows(out)[0] == "📵 <b>CELL PHONE USAGE</b>"


def test_a_type_the_build_has_never_heard_of_still_gets_a_title():
    out = _format_event({"type": "wrong_way_driving", "vehicle": {"number": "1"}})
    assert _rows(out)[0] == "🚨 <b>WRONG WAY DRIVING</b>"


# ── who and when ───────────────────────────────────────────────────────────────

def test_an_unidentified_driver_gets_no_row_at_all():
    """Samsara's webhooks never name the driver. "Driver: Unidentified" on every card
    would be a row that says nothing, so the row is simply left out."""
    out = _format_event({"type": "hard_brake", "vehicle": {"number": "2460"},
                         "driver": {"name": ""}})
    assert "Driver:" not in out
    assert "Unidentified" not in out


def test_a_driver_found_by_the_speeding_lookup_is_shown():
    out = _format_event({"type": "speeding", "vehicle": {"number": "2460"},
                         "driver": {"name": ""}}, samsara={"driver_name": "Ann Lee"})
    assert "👤 <b>Driver:</b> Ann Lee" in out


def test_the_time_row_is_always_there():
    out = _format_event({"type": "hard_brake", "vehicle": {"number": "2460"}})
    assert "🕐 <b>Time:</b>" in out


# ── the unit ───────────────────────────────────────────────────────────────────

def test_a_bare_unit_number_is_one_copyable_span():
    out = _format_event({"type": "hard_brake", "vehicle": {"number": "2460"}})
    assert "🚛 <b>Vehicle:</b> <code>2460</code>" in _rows(out)


def test_the_number_and_what_follows_it_are_separate_spans():
    """So that a tap copies the number alone, which is what gets pasted into the
    provider's dashboard."""
    out = _format_event({"type": "hard_brake", "vehicle": {"number": "1196 - JOHN"}})
    assert "🚛 <b>Vehicle:</b> <code>1196</code> <code>- JOHN</code>" in _rows(out)


def test_a_leading_unit_label_is_dropped_however_it_is_spelled():
    for spelled in ("Unit 1196", "unit:1196", "UNIT #1196", "unit-1196", "Unit # 1196"):
        out = _format_event({"type": "hard_brake", "vehicle": {"number": spelled}})
        assert "🚛 <b>Vehicle:</b> <code>1196</code>" in _rows(out), spelled


def test_the_card_and_the_stored_row_agree_on_the_unit():
    """One rule for both. The report groups by the stored string, and an admin who reads
    "1196" on a card has to find "1196" in the report."""
    event = {"type": "hard_brake", "vehicle": {"number": "Unit #1196 JOHN"}}
    assert _clean_vehicle(event) == "1196 JOHN"
    assert "<code>1196</code> <code>JOHN</code>" in _format_event(event)


def test_a_missing_unit_is_a_dash_not_an_empty_span():
    out = _format_event({"type": "hard_brake", "vehicle": {"number": ""}})
    assert "🚛 <b>Vehicle:</b> —" in _rows(out)
    assert "<code></code>" not in out


def test_the_stored_unit_fits_the_column():
    assert len(_clean_vehicle({"vehicle": {"number": "9" * 80}})) == 50


def test_the_crash_video_caption_uses_the_same_unit_markup():
    caption = _format_crash_video_caption({"type": "crash",
                                           "vehicle": {"number": "Unit 2460 ANN"}})
    assert caption == "💥 <b>CRASH</b> · <code>2460</code> <code>ANN</code>"


# ── footer notes ───────────────────────────────────────────────────────────────

def test_footer_notes_sit_together_under_one_blank_line():
    out = _format_crash_initial(
        {"type": "crash", "vehicle": {"number": "2460"}, "location": "I-95 N",
         "severity": "critical", "_source": "samsara", "_crash_unconfirmed": True},
        "HAULAGE FREIGHT LLC",
    )
    rows = _rows(out)
    unconfirmed = _row(out, "Unconfirmed")
    pending = _row(out, "Video pending")
    assert pending == unconfirmed + 1
    assert rows[unconfirmed - 1] == ""
    assert rows[-1] == rows[pending]
    # The title's blank line and the footer's, and no third.
    assert rows.count("") == 2


def test_no_notes_means_no_trailing_blank_line():
    rows = _rows(_format_event(SPEEDING))
    assert rows.count("") == 1
    assert rows[-1] != ""


def test_company_name_is_a_subtitle_on_crash_only():
    crash = {"type": "crash", "vehicle": {"number": "2460"}}
    assert _rows(_format_event(crash, "HAULAGE FREIGHT LLC"))[2] == "HAULAGE FREIGHT LLC"
    assert "HAULAGE FREIGHT LLC" not in _format_event(SPEEDING, "HAULAGE FREIGHT LLC")


# ── which provider it came from ────────────────────────────────────────────────

def test_a_one_provider_fleet_does_not_sign_its_cards():
    """Every alert at HF comes from Samsara and every one at JTR from Motive. A "via …"
    line under each would be the same three words on every card."""
    for source in ({}, {"_source": "samsara"}):
        out = _format_event({"type": "hard_brake", "vehicle": {"number": "2460"}, **source})
        assert "via " not in out


def test_a_two_provider_fleet_says_which_one_saw_the_event(two_providers):
    """MZ CARGO runs both into one group. Which of them reported an event decides where
    an admin goes to look at it, and nothing else on the card says."""
    event = {"type": "cell_phone", "vehicle": {"number": "2460"}}
    assert _rows(_format_event(event))[-2:] == ["", "<i>via Motive</i>"]
    assert _rows(_format_event({**event, "_source": "samsara"}))[-2:] == ["", "<i>via Samsara</i>"]


def test_the_provider_is_the_last_line_of_the_one_footer(two_providers):
    """Not a block of its own: it joins whatever else is under the footer's blank line,
    after it, so "Video pending…" is not pushed down the card by a signature."""
    out = _format_crash_initial(
        {"type": "crash", "vehicle": {"number": "2460"}, "_crash_unconfirmed": True})
    rows = _rows(out)
    assert rows[-3:] == [
        "⚠️ <i>Unconfirmed — Motive's review could not be reached.</i>",
        "📹 <i>Video pending…</i>",
        "<i>via Motive</i>",
    ]
    assert rows[-4] == ""
    assert rows.count("") == 2


def test_whether_cards_are_signed_follows_from_which_providers_are_configured():
    """Nothing to set: it is read off the provider keys the deployment already has."""
    import importlib

    import data.config as cfg

    def _show(**env):
        import os
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            return importlib.reload(cfg).SHOW_PROVIDER, cfg.EVENT_WORDING
        finally:
            for k, v in saved.items():
                os.environ[k] = v if v is not None else ""
            importlib.reload(cfg)

    assert _show(SAMSARA_WEBHOOK_SECRET="s", MOTIVE_WEBHOOK_SECRET="") == (False, "samsara")
    assert _show(SAMSARA_WEBHOOK_SECRET="", MOTIVE_API_KEY="m") == (False, "motive")
    assert _show(SAMSARA_API_KEY="s", MOTIVE_WEBHOOK_SECRET="m") == (True, "motive")
    assert _show(SAMSARA_API_KEY="", MOTIVE_API_KEY="") == (False, "samsara")
