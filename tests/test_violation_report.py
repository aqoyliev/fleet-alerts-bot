"""The report both surfaces share.

It used to live inside the DM callback handler, which meant the Mini App could only have
had a second copy of it. These tests pin the parts that were easy to get subtly wrong in
such a copy: which days a window covers, and that one report holds every violation of every
type, speeding included, rather than a separate file per kind.
"""

from datetime import datetime, timedelta

import pytest

from utils import violation_report as vr


def _at(day: str, hour: int = 12, minute: int = 0) -> datetime:
    return datetime.fromisoformat(f"{day}T{hour:02d}:{minute:02d}:00").replace(tzinfo=vr.ET)


@pytest.fixture
def rows(monkeypatch):
    """The numbers the database would give back, which the test sets."""
    store = {
        "totals": {"total": 0, "speeding": 0, "crashes": 0, "units": 0},
        "types": [], "top": [], "events": {},
    }

    async def _totals(since, until):
        return store["totals"]

    async def _types(since, until):
        return store["types"]

    async def _top(since, until=None, limit=10):
        store["limit"] = limit
        return store["top"]

    async def _events(vehicle_number, since, until=None):
        return store["events"].get(vehicle_number, [])

    monkeypatch.setattr(vr, "get_totals", _totals)
    monkeypatch.setattr(vr, "get_type_counts", _types)
    monkeypatch.setattr(vr, "get_top_violators", _top)
    monkeypatch.setattr(vr, "get_vehicle_events", _events)
    return store


def _pages(pdf: bytes) -> int:
    import re
    return len(re.findall(rb"/Type /Page(?!s)", pdf))


def _text(pdf: bytes) -> str:
    """The strings the PDF draws, in order. Pages are written uncompressed, so this is a
    plain scan for the text operators rather than a PDF parser."""
    import re
    return " | ".join(s.decode("cp1252") for s in re.findall(rb"\((.*?)\) Tj", pdf))


# ── the windows ─────────────────────────────────────────────────────────────────

def test_today_runs_from_midnight_to_now():
    since, until = vr.period_range("today")
    assert (since.hour, since.minute, since.second) == (0, 0, 0)
    assert since.date() == until.date()


def test_last_week_is_the_week_that_ended_not_the_last_seven_days():
    """A dispatcher asked about "last week" on a Monday morning means the week that just
    finished, not the rolling seven days — which on that morning would be almost all of
    it plus today. The panel's dashboard is the one that uses rolling windows."""
    since, until = vr.period_range("last_week")
    assert until - since == timedelta(weeks=1)
    assert since.weekday() == 0 and until.weekday() == 0
    assert (until.hour, until.minute) == (0, 0)


def test_last_month_is_the_previous_calendar_month():
    since, until = vr.period_range("last_month")
    assert since.day == 1 and until.day == 1
    assert since < until
    # Exactly one month apart, whatever its length.
    assert (until.year - since.year) * 12 + (until.month - since.month) == 1


def test_an_unknown_period_is_labelled_as_the_default():
    assert vr.period_label("nonsense") == "Last Week"
    assert vr.period_label("today") == "Today"


# ── the report ──────────────────────────────────────────────────────────────────

async def test_the_report_is_cut_to_the_ten_worst_units(rows):
    """A hundred-truck fleet gets the same document as a ten-truck one; what shows the cut
    is that the totals still count every unit."""
    rows["totals"] = {"total": 400, "speeding": 250, "crashes": 2, "units": 96}
    await vr.collect_report("last_week", "Test Co")

    assert rows["limit"] == 10


async def test_every_type_of_violation_is_in_the_report_together(rows):
    """Speeding used to be a file of its own, filtered to days with three or more, and the
    rest another. A dispatcher asking for last week gets all of it in one."""
    rows["top"] = [{"vehicle_number": "1269", "total": 3}]
    rows["events"]["1269"] = [
        {"event_type": "speeding", "occurred_at": _at("2026-10-06", 8), "severity": None},
        {"event_type": "hard_brake", "occurred_at": _at("2026-10-06", 9), "severity": None},
        {"event_type": "tailgating", "occurred_at": _at("2026-10-07", 9), "severity": None},
    ]
    report = await vr.collect_report("last_week", "Test Co")

    assert [e.label for e in report.units[0].events] == ["Speeding", "Hard Brake", "Tailgating"]
    assert report.units[0].total == 3


async def test_a_single_speeding_event_is_not_dropped(rows):
    """The old speeding file listed only days a unit sped three or more times, so a lone
    event vanished from the report even though the panel counted it."""
    rows["top"] = [{"vehicle_number": "1269", "total": 1}]
    rows["events"]["1269"] = [
        {"event_type": "speeding", "occurred_at": _at("2026-10-06", 8), "severity": None},
    ]
    pdf = await vr.build_report_file("last_week", "Test Co")

    assert "Unit 1269" in _text(pdf) and "Speeding" in _text(pdf)


async def test_two_spellings_of_one_event_are_one_bar(rows):
    """Motive says drowsiness and Samsara says drowsy_driving; the group-filter row is
    one thing to an admin, so it is one bar with both counts in it."""
    rows["types"] = [{"event_type": "drowsiness", "total": 3},
                     {"event_type": "drowsy_driving", "total": 2},
                     {"event_type": "speeding", "total": 9}]
    report = await vr.collect_report("last_week", "Test Co")

    assert [(label, n) for label, _, n in report.types] == [("Speeding", 9), ("Drowsiness", 5)]


async def test_severity_is_a_super_admin_detail(rows):
    rows["top"] = [{"vehicle_number": "1269", "total": 1}]
    rows["events"]["1269"] = [
        {"event_type": "speeding", "occurred_at": _at("2026-10-06", 8), "severity": "high"},
    ]

    plain = await vr.collect_report("last_week", "Test Co")
    detailed = await vr.collect_report("last_week", "Test Co", show_severity=True)

    assert plain.units[0].events[0].label == "Speeding"
    assert detailed.units[0].events[0].label == "Speeding (High)"


async def test_the_header_carries_the_company_and_the_dates(rows):
    pdf = await vr.build_report_file("today", "NVS Logistics LLC")

    assert pdf.startswith(b"%PDF")
    text = _text(pdf)
    assert "NVS Logistics LLC" in text and "Generated" in text and "Page 1 of 1" in text


def test_a_calendar_window_ends_on_its_last_day_not_the_midnight_after():
    since = datetime(2026, 10, 5, tzinfo=vr.ET)
    until = datetime(2026, 10, 12, tzinfo=vr.ET)

    assert vr.range_text("last_week", since, until) == "Oct 05 — Oct 11, 2026"


async def test_a_long_report_runs_onto_more_pages_and_says_so(rows):
    """Forty events a unit is far more than a page. The unit carries on over the page
    break under a "(continued)" heading, and every footer knows the real page count."""
    rows["top"] = [{"vehicle_number": f"{n}", "total": 40} for n in range(1, 4)]
    for n in range(1, 4):
        rows["events"][f"{n}"] = [
            {"event_type": "speeding", "occurred_at": _at("2026-10-06", 8, i), "severity": None}
            for i in range(40)
        ]
    pdf = await vr.build_report_file("last_week", "Test Co")
    pages = _pages(pdf)

    assert pages > 2
    assert "continued" in _text(pdf)
    assert f"Page 1 of {pages}" in _text(pdf) and f"Page {pages} of {pages}" in _text(pdf)


async def test_an_unprintable_name_does_not_stop_the_report(rows):
    """The built-in font cannot draw every script. One roster name in Cyrillic must not
    cost a dispatcher the whole report."""
    rows["top"] = [{"vehicle_number": "Грузовик 7", "total": 1}]
    rows["events"]["Грузовик 7"] = [
        {"event_type": "speeding", "occurred_at": _at("2026-10-06", 8), "severity": None}]

    assert (await vr.build_report_file("last_week", "Test Co")).startswith(b"%PDF")


def test_the_filename_carries_the_slug_and_the_period():
    assert vr.report_filename("nvs", "last_month") == "nvs_last_month_report.pdf"
