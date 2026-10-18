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
    """One unit, whose events the test sets."""
    store = {"top": [], "events": []}

    async def _top(since, until=None, limit=10):
        return store["top"]

    async def _events(vehicle_number, since, until=None):
        return store["events"]

    monkeypatch.setattr(vr, "get_top_violators", _top)
    monkeypatch.setattr(vr, "get_vehicle_events", _events)
    return store


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


# ── the file ────────────────────────────────────────────────────────────────────

async def test_one_report_holds_every_type_of_violation_together(rows):
    """Speeding used to be a file of its own, filtered to days with three or more, and the
    rest another. A dispatcher asking for last week gets all of it in one."""
    rows["top"] = [{"vehicle_number": "1269", "total": 3}]
    rows["events"] = [
        {"event_type": "speeding", "occurred_at": _at("2026-10-06", 8), "severity": None},
        {"event_type": "hard_brake", "occurred_at": _at("2026-10-06", 9), "severity": None},
        {"event_type": "tailgating", "occurred_at": _at("2026-10-07", 9), "severity": None},
    ]
    text = (await vr.build_report_file("last_week", "Test Co")).decode()

    assert "1. Unit 1269 — 3 events" in text
    assert "SPEEDING" in text.upper() and "HARD BRAKE" in text and "TAILGATING" in text


async def test_a_single_speeding_event_is_not_dropped(rows):
    """The old speeding file listed only days a unit sped three or more times, so a lone
    event vanished from the report even though the panel counted it."""
    rows["top"] = [{"vehicle_number": "1269", "total": 1}]
    rows["events"] = [
        {"event_type": "speeding", "occurred_at": _at("2026-10-06", 8), "severity": None},
    ]
    text = (await vr.build_report_file("last_week", "Test Co")).decode()

    assert "1. Unit 1269 — 1 event\n" in text
    assert "Oct 06" in text
    assert "3+" not in text


async def test_severity_is_a_super_admin_detail(rows):
    rows["top"] = [{"vehicle_number": "1269", "total": 1}]
    rows["events"] = [
        {"event_type": "speeding", "occurred_at": _at("2026-10-06", 8), "severity": "high"},
    ]

    plain = (await vr.build_report_file("last_week", "Test Co")).decode()
    detailed = (await vr.build_report_file("last_week", "Test Co",
                                           show_severity=True)).decode()
    assert "High" not in plain
    assert "(High)" in detailed


async def test_the_header_carries_the_company_and_the_dates(rows):
    rows["top"] = []
    text = (await vr.build_report_file("today", "NVS Logistics LLC")).decode()

    assert text.startswith("NVS Logistics LLC — Today (")
    assert "Generated:" in text


def test_the_filename_carries_the_slug_and_the_period():
    assert vr.report_filename("nvs", "last_month") == "nvs_last_month_report.txt"
