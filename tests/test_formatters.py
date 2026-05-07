import importlib.util
import os

from utils.daily_report import _format_daily_report

# Load report.py directly to avoid importing the entire handlers package
_spec = importlib.util.spec_from_file_location(
    "report_module",
    os.path.join(os.path.dirname(__file__), "..", "handlers", "users", "report.py"),
)
_report_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_report_mod)
_format_top_report = _report_mod._format_top_report
_report_date_range = _report_mod._report_date_range


# ── _format_daily_report ───────────────────────────────────────────────────────

def test_daily_report_no_violations():
    result = _format_daily_report("MZ CARGO INC", [], "May 07, 2026")
    assert "MZ CARGO INC" in result
    assert "May 07, 2026" in result
    assert "No violations" in result

def test_daily_report_speeding_appears_first():
    rows = [
        {"event_type": "hard_brake",  "vehicle_number": "1196", "total": 10},
        {"event_type": "speeding",    "vehicle_number": "1196", "total": 1},
        {"event_type": "cell_phone",  "vehicle_number": "1006", "total": 8},
    ]
    result = _format_daily_report("MZ CARGO INC", rows, "May 07, 2026")
    assert result.index("Speeding") < result.index("Hard Brake")

def test_daily_report_shows_vehicle_and_count():
    rows = [{"event_type": "speeding", "vehicle_number": "1196 - JOHN", "total": 7}]
    result = _format_daily_report("MZ CARGO INC", rows, "May 07, 2026")
    assert "1196 - JOHN" in result
    assert "7" in result

def test_daily_report_multiple_vehicles_sorted_by_count():
    rows = [
        {"event_type": "speeding", "vehicle_number": "low",  "total": 1},
        {"event_type": "speeding", "vehicle_number": "high", "total": 9},
    ]
    result = _format_daily_report("MZ CARGO INC", rows, "May 07, 2026")
    assert result.index("high") < result.index("low")

def test_daily_report_html_bold_company_name():
    result = _format_daily_report("MZ CARGO INC", [], "May 07, 2026")
    assert "<b>MZ CARGO INC</b>" in result


# ── _format_top_report ─────────────────────────────────────────────────────────

def test_top_report_no_violations():
    result = _format_top_report("MZ CARGO INC", [], "May 07, 2026", 10)
    assert "No violations" in result
    assert "MZ CARGO INC" in result

def test_top_report_numbered_ranking():
    rows = [
        {"vehicle_number": "1196", "total": 10},
        {"vehicle_number": "1006", "total": 5},
    ]
    result = _format_top_report("MZ CARGO INC", rows, "May 07, 2026", 10)
    assert "1. " in result
    assert "2. " in result

def test_top_report_order_preserved():
    rows = [
        {"vehicle_number": "first",  "total": 10},
        {"vehicle_number": "second", "total": 5},
    ]
    result = _format_top_report("MZ CARGO INC", rows, "May 07, 2026", 10)
    assert result.index("first") < result.index("second")

def test_top_report_shows_count():
    rows = [{"vehicle_number": "1196", "total": 42}]
    result = _format_top_report("MZ CARGO INC", rows, "May 07, 2026", 10)
    assert "42" in result


# ── _report_date_range ─────────────────────────────────────────────────────────

def test_date_range_today_label():
    _, _, date_str = _report_date_range("today")
    assert "(so far)" in date_str

def test_date_range_yesterday_no_so_far():
    _, _, date_str = _report_date_range("yesterday")
    assert "(so far)" not in date_str

def test_date_range_since_before_until():
    since, until, _ = _report_date_range("today")
    assert since < until
    since, until, _ = _report_date_range("yesterday")
    assert since < until

def test_date_range_yesterday_is_one_day():
    since, until, _ = _report_date_range("yesterday")
    assert (until - since).days == 1
