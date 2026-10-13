"""Layout of the alert card built by utils.webhook_handler._format_event.

HF's cards are the plain ones: the title, a blank line, then one field per row. They are
meant to stay visibly unlike single-company's, which are ruled into blocks. The wording
of the measurements is covered elsewhere (test_speeding_enrichment, test_motive_crash,
test_samsara); what these lock down is the shape — the order of the rows, how the unit
is written, and which rows are left out when there is nothing to put in them.
"""

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


def test_hard_brake_is_titled_harsh_brake():
    out = _format_event({"type": "hard_brake", "vehicle": {"number": "2460"}})
    assert _rows(out)[0] == "🛑 <b>HARSH BRAKE</b>"


def test_stop_sign_violation_is_titled_rolling_stop():
    out = _format_event({"type": "stop_sign_violation", "vehicle": {"number": "2460"}})
    assert _rows(out)[0] == "🛑 <b>ROLLING STOP</b>"


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
    """So that a tap copies the number alone, which is what gets pasted into Samsara."""
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


def test_alerts_are_not_tagged_with_their_provider():
    """Every alert here comes from Samsara. A "via Samsara" line under each one would be
    the same four words on every card."""
    out = _format_event({"type": "hard_brake", "vehicle": {"number": "2460"},
                         "_source": "samsara"})
    assert "via Samsara" not in out


def test_company_name_is_a_subtitle_on_crash_only():
    crash = {"type": "crash", "vehicle": {"number": "2460"}}
    assert _rows(_format_event(crash, "HAULAGE FREIGHT LLC"))[2] == "HAULAGE FREIGHT LLC"
    assert "HAULAGE FREIGHT LLC" not in _format_event(SPEEDING, "HAULAGE FREIGHT LLC")
