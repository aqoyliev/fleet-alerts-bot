"""Layout of the alert card built by utils.webhook_handler._format_event.

This build's cards are ruled into blocks — title / unit / event / notes — and are
meant to stay visibly unlike the other deployments'. The wording and the fields are
covered elsewhere (test_speeding_enrichment, test_motive_crash, test_samsara); what
these lock down is the shape: which block a field lands in, and that a block which
comes out empty takes its divider with it instead of leaving one dangling.
"""

from utils.webhook_handler import _RULE, _format_crash_initial, _format_event


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


def test_title_is_followed_by_a_rule_not_a_blank_line():
    rows = _rows(_format_event(SPEEDING))
    assert rows[0].startswith("🚨 <b>")
    assert rows[1] == _RULE


def test_block_order_unit_then_event():
    rows = _rows(_format_event(SPEEDING))
    vehicle = next(i for i, r in enumerate(rows) if "Vehicle:" in r)
    driver = next(i for i, r in enumerate(rows) if "Driver:" in r)
    time = next(i for i, r in enumerate(rows) if "Time:" in r)
    severity = next(i for i, r in enumerate(rows) if "Severity:" in r)
    location = next(i for i, r in enumerate(rows) if "Location:" in r)
    assert vehicle < driver < time < severity < location
    # the event block is fenced off from the unit block
    assert rows[time + 1] == _RULE


def test_severity_row_leads_with_its_own_emoji():
    out = _format_event(SPEEDING)
    assert "🔴 <b>Severity:</b> High" in out
    assert "📊" not in out


def test_missing_timestamp_drops_the_time_row():
    out = _format_event({"type": "hard_brake", "vehicle": {"number": "2460"}})
    assert "Time:" not in out


def test_card_never_ends_on_a_dangling_rule():
    """An obstructed-camera event has no severity, no measurement and often no
    location — the event block is empty, so its rule must not be emitted."""
    out = _format_event({"type": "road_facing_cam_obstruction",
                         "vehicle": {"number": "2460"},
                         "severity": "high",
                         "start_time": "2026-04-08T11:29:59Z"})
    assert not out.endswith(_RULE)
    assert out.count(_RULE) == 1


def test_footer_notes_share_one_rule():
    out = _format_crash_initial(
        {"type": "crash", "vehicle": {"number": "2460"}, "location": "I-95 N",
         "severity": "critical", "_source": "samsara", "_crash_unconfirmed": True},
        "U-Home Logistics",
    )
    rows = _rows(out)
    unconfirmed = next(i for i, r in enumerate(rows) if "Unconfirmed" in r)
    pending = next(i for i, r in enumerate(rows) if "Video pending" in r)
    via = next(i for i, r in enumerate(rows) if "via Samsara" in r)
    # consecutive: one rule above them, none in between
    assert (unconfirmed, pending, via) == (via - 2, via - 1, via)
    assert rows[unconfirmed - 1] == _RULE
    assert out.count(_RULE) == 3  # title | unit | event | notes


def test_no_notes_means_no_footer_rule():
    out = _format_event(SPEEDING)
    assert out.count(_RULE) == 2  # title | unit | event


def test_company_name_is_a_subtitle_on_crash_only():
    crash = {"type": "crash", "vehicle": {"number": "2460"}}
    assert _rows(_format_event(crash, "U-Home Logistics"))[1] == "<i>U-Home Logistics</i>"
    assert "U-Home Logistics" not in _format_event(SPEEDING, "U-Home Logistics")
