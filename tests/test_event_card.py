"""Layout of the alert card built by utils.webhook_handler._format_event.

MZ's cards are the ones its group has always been shown: the title, a blank line, the
unit on a row of its own, one field per row, and under a blank line which provider
reported it. hf's label the unit and leave the provider off; single-company's are ruled
into blocks. The wording of the measurements is covered elsewhere
(test_speeding_enrichment, test_motive_crash, test_samsara); what these lock down is the
shape — the order of the rows, how the unit is written, which rows are left out when
there is nothing to put in them, and what comes after the signature.
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
             ("<code>375</code>", "Driver:", "Severity:", "Time:", "Average Speed:",
              "Location:", "via ")]
    assert order == sorted(order)


def test_severity_row_carries_its_label_emoji_and_its_level_emoji():
    assert "📊 <b>Severity:</b> 🔴 High" in _format_event(SPEEDING)


def test_a_hard_brake_keeps_motives_name_for_it():
    """hf titles this HARSH BRAKE, after Samsara. MZ's group has read HARD BRAKE since
    the day it was created, and half of what it reads comes from Motive."""
    out = _format_event({"type": "hard_brake", "vehicle": {"number": "2460"}})
    assert _rows(out)[0] == "🛑 <b>HARD BRAKE</b>"


def test_a_stop_sign_violation_is_called_that():
    out = _format_event({"type": "stop_sign_violation", "vehicle": {"number": "2460"}})
    assert _rows(out)[0] == "🛑 <b>STOP SIGN VIOLATION</b>"


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

def test_the_unit_is_a_row_of_its_own_with_no_label():
    out = _format_event({"type": "hard_brake", "vehicle": {"number": "2460"}})
    assert _rows(out)[2] == "<code>2460</code>"
    assert "Vehicle:" not in out


def test_the_number_and_what_follows_it_are_separate_spans():
    """So that a tap copies the number alone, which is what gets pasted into the
    provider's dashboard."""
    out = _format_event({"type": "hard_brake", "vehicle": {"number": "1196 - JOHN"}})
    assert _rows(out)[2] == "<code>1196</code> <code>- JOHN</code>"


def test_a_leading_unit_label_is_dropped_however_it_is_spelled():
    for spelled in ("Unit 1196", "unit:1196", "UNIT #1196", "unit-1196", "Unit # 1196"):
        out = _format_event({"type": "hard_brake", "vehicle": {"number": spelled}})
        assert _rows(out)[2] == "<code>1196</code>", spelled


def test_the_card_and_the_stored_row_agree_on_the_unit():
    """One rule for both. The report groups by the stored string, and an admin who reads
    "1196" on a card has to find "1196" in the report."""
    event = {"type": "hard_brake", "vehicle": {"number": "Unit #1196 JOHN"}}
    assert _clean_vehicle(event) == "1196 JOHN"
    assert "<code>1196</code> <code>JOHN</code>" in _format_event(event)


def test_a_missing_unit_is_a_dash_not_an_empty_span():
    out = _format_event({"type": "hard_brake", "vehicle": {"number": ""}})
    assert _rows(out)[2] == "—"
    assert "<code></code>" not in out


def test_the_stored_unit_fits_the_column():
    assert len(_clean_vehicle({"vehicle": {"number": "9" * 80}})) == 50


def test_the_crash_video_caption_uses_the_same_unit_markup():
    caption = _format_crash_video_caption({"type": "crash",
                                           "vehicle": {"number": "Unit 2460 ANN"}})
    assert caption == "💥 <b>CRASH</b> — <code>2460</code> <code>ANN</code>"


# ── the signature, and what follows it ─────────────────────────────────────────

def test_every_card_says_which_provider_reported_it():
    """MZ runs Samsara and Motive side by side into one group. Which of them saw an
    event decides where an admin goes to look at it, and nothing else on the card says."""
    samsara = _format_event({"type": "hard_brake", "vehicle": {"number": "2460"},
                             "_source": "samsara"})
    motive = _format_event({"type": "hard_brake", "vehicle": {"number": "2460"}})
    assert _rows(samsara)[-2:] == ["", "<i>via Samsara</i>"]
    assert _rows(motive)[-2:] == ["", "<i>via Motive</i>"]


def test_a_card_with_no_notes_ends_on_its_signature():
    rows = _rows(_format_event(SPEEDING))
    assert rows[-1] == "<i>via Motive</i>"
    # The title's blank line and the signature's, and no third.
    assert rows.count("") == 2


def test_an_unconfirmed_crash_says_so_above_the_signature():
    out = _format_event({"type": "crash", "vehicle": {"number": "2460"},
                         "_crash_unconfirmed": True})
    rows = _rows(out)
    unconfirmed = _row(out, "Unconfirmed")
    assert rows[unconfirmed - 1] == ""
    assert rows[unconfirmed + 1:] == ["", "<i>via Motive</i>"]


def test_notes_come_last_under_a_blank_line_of_their_own():
    """"Video pending…" is about the message, not about the event, so it sits below the
    signature the way it always has in this group."""
    out = _format_crash_initial(
        {"type": "crash", "vehicle": {"number": "2460"}, "location": "I-95 N",
         "severity": "critical", "_source": "samsara"},
    )
    assert _rows(out)[-4:] == ["", "<i>via Samsara</i>", "", "📹 <i>Video pending…</i>"]


def test_the_company_is_not_named_on_any_card():
    """Every card in this group is this company's. hf prints the name under a crash's
    title; MZ took it off its cards in May and it stays off."""
    crash = {"type": "crash", "vehicle": {"number": "2460"}}
    assert "MZ CARGO INC" not in _format_event(crash, "MZ CARGO INC")
    assert _rows(_format_event(crash, "MZ CARGO INC"))[2] == "<code>2460</code>"
    assert "MZ CARGO INC" not in _format_event(SPEEDING, "MZ CARGO INC")
