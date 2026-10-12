"""The report texts: the nightly digest, /report, /top, and the top-10 header.

These are HF's own, kept as they were when the branch took single-company as its base —
the wording differs from that branch's on purpose ("Unit 1196", "Harsh Brake", "Rolling
Stop"), and a merge from it must not quietly bring its wording back.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from handlers.users.report import _format_top_report, _report_date_range
from handlers.users.violations import _date_range_label, _format_top10_text
from utils.daily_report import _EVENT_LABEL, _format_daily_report
from utils.webhook_handler import ALLOWED_TYPES

ET = ZoneInfo("America/New_York")


# ── _format_daily_report ───────────────────────────────────────────────────────

def test_daily_report_no_violations():
    result = _format_daily_report("HAULAGE FREIGHT LLC", [], "May 07, 2026")
    assert "HAULAGE FREIGHT LLC" in result
    assert "May 07, 2026" in result
    assert "No violations" in result

def test_daily_report_speeding_appears_first():
    rows = [
        {"event_type": "hard_brake",  "vehicle_number": "1196", "total": 10},
        {"event_type": "speeding",    "vehicle_number": "1196", "total": 1},
        {"event_type": "cell_phone",  "vehicle_number": "1006", "total": 8},
    ]
    result = _format_daily_report("HAULAGE FREIGHT LLC", rows, "May 07, 2026")
    assert result.index("Speeding") < result.index("Harsh Brake")

def test_daily_report_other_types_are_ordered_by_their_totals():
    rows = [
        {"event_type": "cell_phone",  "vehicle_number": "1006", "total": 2},
        {"event_type": "hard_brake",  "vehicle_number": "1196", "total": 10},
    ]
    result = _format_daily_report("HAULAGE FREIGHT LLC", rows, "May 07, 2026")
    assert result.index("Harsh Brake") < result.index("Cell Phone Usage")

def test_daily_report_shows_vehicle_and_count():
    rows = [{"event_type": "speeding", "vehicle_number": "1196 - JOHN", "total": 7}]
    result = _format_daily_report("HAULAGE FREIGHT LLC", rows, "May 07, 2026")
    assert "🚛 Unit 1196 - JOHN — 7" in result

def test_daily_report_multiple_vehicles_sorted_by_count():
    rows = [
        {"event_type": "speeding", "vehicle_number": "low",  "total": 1},
        {"event_type": "speeding", "vehicle_number": "high", "total": 9},
    ]
    result = _format_daily_report("HAULAGE FREIGHT LLC", rows, "May 07, 2026")
    assert result.index("high") < result.index("low")

def test_daily_report_html_bold_company_name():
    result = _format_daily_report("HAULAGE FREIGHT LLC", [], "May 07, 2026")
    assert "<b>HAULAGE FREIGHT LLC</b>" in result

def test_daily_report_uses_the_reports_own_wording_for_a_type():
    """Not the alert card's headline: "SPEEDING OVER POSTED" over a list of units reads
    as if every unit under it were a separate announcement."""
    rows = [{"event_type": "stop_sign_violation", "vehicle_number": "1196", "total": 1},
            {"event_type": "speeding", "vehicle_number": "1196", "total": 1}]
    result = _format_daily_report("HAULAGE FREIGHT LLC", rows, "May 07, 2026")
    assert "<b>Rolling Stop</b>" in result
    assert "<b>Speeding</b>" in result
    assert "OVER POSTED" not in result

def test_daily_report_has_wording_for_every_type_the_bot_alerts_on():
    """A type without a label still prints — as its raw name, title-cased — which is how
    "Hard Cornering" would turn up next to "Harsh Turn" for the same kind of event."""
    assert ALLOWED_TYPES <= set(_EVENT_LABEL)

def test_daily_report_survives_a_type_it_has_no_wording_for():
    """An old row, or a type added to the alerts and forgotten here."""
    rows = [{"event_type": "near_miss", "vehicle_number": "1196", "total": 1}]
    result = _format_daily_report("HAULAGE FREIGHT LLC", rows, "May 07, 2026")
    assert "<b>Near Miss</b>" in result


# ── _format_top_report ─────────────────────────────────────────────────────────

def test_top_report_no_violations():
    result = _format_top_report("HAULAGE FREIGHT LLC", [], "May 07, 2026", 10)
    assert "No violations" in result
    assert "HAULAGE FREIGHT LLC" in result

def test_top_report_numbered_ranking():
    rows = [
        {"vehicle_number": "1196", "total": 10},
        {"vehicle_number": "1006", "total": 5},
    ]
    result = _format_top_report("HAULAGE FREIGHT LLC", rows, "May 07, 2026", 10)
    assert "1. 🚛 Unit 1196 — 10" in result
    assert "2. 🚛 Unit 1006 — 5" in result

def test_top_report_order_preserved():
    rows = [
        {"vehicle_number": "first",  "total": 10},
        {"vehicle_number": "second", "total": 5},
    ]
    result = _format_top_report("HAULAGE FREIGHT LLC", rows, "May 07, 2026", 10)
    assert result.index("first") < result.index("second")

def test_top_report_shows_count():
    rows = [{"vehicle_number": "1196", "total": 42}]
    result = _format_top_report("HAULAGE FREIGHT LLC", rows, "May 07, 2026", 10)
    assert "42" in result

def test_top_report_escapes_the_unit():
    rows = [{"vehicle_number": "A&B <1>", "total": 1}]
    result = _format_top_report("HAULAGE FREIGHT LLC", rows, "May 07, 2026", 10)
    assert "Unit A&amp;B &lt;1&gt;" in result


# ── _report_date_range ─────────────────────────────────────────────────────────

def test_date_range_today_label():
    _, _, date_str = _report_date_range("today")
    assert "(so far)" in date_str

def test_date_range_yesterday_no_so_far():
    _, _, date_str = _report_date_range("yesterday")
    assert "(so far)" not in date_str

def test_date_range_since_before_until():
    since, until, _ = _report_date_range("today")
    assert since <= until
    since, until, _ = _report_date_range("yesterday")
    assert since < until

def test_date_range_yesterday_is_one_calendar_day():
    since, until, _ = _report_date_range("yesterday")
    assert (until.date() - since.date()).days == 1
    assert (since.hour, since.minute, until.hour, until.minute) == (0, 0, 0, 0)


# ── the top-10 screen's header ─────────────────────────────────────────────────

def test_top10_header_names_the_dates_the_period_covers():
    """ "Last Week" says nothing on the Monday it is read; the dates do. `until` is
    exclusive, so the last day shown is the one before it."""
    since = datetime(2026, 5, 4, tzinfo=ET)
    until = datetime(2026, 5, 11, tzinfo=ET)
    assert _date_range_label(since, until) == "May 04 – May 10, 2026"

def test_top10_header_carries_both_years_across_a_new_year():
    since = datetime(2025, 12, 29, tzinfo=ET)
    until = datetime(2026, 1, 5, tzinfo=ET)
    assert _date_range_label(since, until) == "Dec 29, 2025 – Jan 04, 2026"

def test_top10_speeding_lists_every_event_with_no_three_a_day_note():
    """single-company's download only lists days with three or more speeding events and
    says so in the header. HF dropped that rule: every event is listed, so the note
    would describe a filter that is not applied."""
    text = _format_top10_text([{"vehicle_number": "1196", "total": 4}],
                              "May 04 – May 10, 2026", "HAULAGE FREIGHT LLC", "speeding")
    assert "Speeding only" in text
    assert "3+" not in text
    assert "1. 🚛 Unit 1196 — 4" in text

def test_top10_empty():
    text = _format_top10_text([], "May 04 – May 10, 2026", "HAULAGE FREIGHT LLC", "other")
    assert "No violations found" in text
