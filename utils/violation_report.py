"""The violations report, independent of which surface asked for it.

The Mini App's Report screen asks for these numbers; the period windows, the ranking and
the downloadable PDF live here, away from the HTTP handlers. This module gathers the
numbers into a report_pdf.Report and report_pdf draws it.

Free of aiogram, so the formatting can be tested without a bot token, the same reason
utils/group_texts.py is its own module.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from data.event_catalog import GROUP_FILTER_TYPES
from utils import report_pdf
from utils.db_api.violations import (
    get_top_violators, get_totals, get_type_counts, get_vehicle_events,
)

# Eastern, like every other timestamp this bot prints. See utils/webapp/api.py.
ET = ZoneInfo("America/New_York")

# The windows a report can be built for. Every report covers every event type: there used
# to be a speeding-only and an other-only variant, and a dispatcher asking for last week's
# violations got to choose which half of them to see.
PERIOD_LABELS = {
    "today": "Today",
    "last_week": "Last Week",
    "last_month": "Last Month",
}


def period_label(period: str) -> str:
    return PERIOD_LABELS.get(period, PERIOD_LABELS["last_week"])


def period_range(period: str) -> tuple[datetime, datetime]:
    """(since, until) for one of the three report windows, in Eastern time.

    Calendar windows, not rolling ones: "last week" is the week that ended, which is the
    period a dispatcher is asked about on a Monday. The panel's dashboard deliberately
    uses rolling windows instead — see _period_range in utils/webapp/api.py.
    """
    now = datetime.now(tz=ET)
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0), now
    if period == "last_month":
        first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return (first_of_this_month - timedelta(days=1)).replace(day=1), first_of_this_month
    start_of_this_week = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return start_of_this_week - timedelta(weeks=1), start_of_this_week


def date_range_label(since: datetime, until: datetime) -> str:
    return f"{since.strftime('%b %d')} — {until.strftime('%b %d, %Y')}"


# The report is the worst of the fleet, not all of it: a hundred-truck company would
# otherwise get a document nobody reads past the second page. The totals above the list
# still count every unit, so the cut is visible rather than silent.
TOP_UNITS = 10

# Hue per kind of event, the same grouping as the panel's EVENT_STYLE in static/app.js: red
# is anything that can hurt somebody, orange is how the truck is driven, purple the driver,
# teal the hardware, blue the rest. Crash is darker than the red it shares with them.
_RED, _ORANGE, _PURPLE, _TEAL, _BLUE = "#C93442", "#D9480F", "#7C3AED", "#0F766E", "#2F5BEA"
_TONES = {
    "speeding": _RED, "forward_collision_warning": _RED, "near_miss": _RED,
    "stop_sign_violation": _RED, "crash": report_pdf.CRASH,
    "hard_brake": _ORANGE, "harsh_acceleration": _ORANGE, "harsh_turn": _ORANGE,
    "harsh_event": _ORANGE,
    "cell_phone": _PURPLE, "seat_belt_violation": _PURPLE, "no_seat_belt": _PURPLE,
    "inattentive_driving": _PURPLE, "drowsy_driving": _PURPLE,
    "road_facing_cam_obstruction": _TEAL,
}


def _row_of(event_type: str) -> tuple[str, str]:
    """(label, token) of the catalog row an event type belongs to.

    Rows, not raw types, because one thing an admin calls "Drowsiness" is two stored
    spellings and "Camera Obstructed" is three; counted separately they would show up as
    near-duplicate bars.
    """
    for types, _, label in GROUP_FILTER_TYPES:
        if event_type in types:
            return label, types[0]
    from utils.webhook_handler import EVENT_TYPE_MAP
    title = EVENT_TYPE_MAP.get(event_type, ("", event_type.replace("_", " ")))[1]
    if event_type == "crash":
        title = "Crash"
    return title.capitalize() if title.isupper() else title, event_type


def _tone(token: str) -> str:
    return _TONES.get(token, _BLUE)


def range_text(period: str, since: datetime, until: datetime) -> str:
    """The window as a person reads it. Calendar windows end at the midnight that begins
    the next one, so the last day *inside* them is a second earlier."""
    if period in ("last_week", "last_month"):
        until = until - timedelta(seconds=1)
    if since.date() == until.date():
        return since.strftime("%b %d, %Y")
    return date_range_label(since, until)


async def collect_report(period: str, company_name: str, *,
                         show_severity: bool = False) -> report_pdf.Report:
    """Everything the PDF says, as plain data: fleet-wide figures, then the worst units
    with every event under each.

    `show_severity` is the super-admin extra: a speeding event's severity is only
    annotated for them. Passed in rather than looked up here so this module stays free of
    the admin tables.
    """
    since, until = period_range(period)
    totals = await get_totals(since, until)

    merged: dict[str, list] = {}
    for row in await get_type_counts(since, until):
        label, token = _row_of(row["event_type"])
        entry = merged.setdefault(token, [label, 0])
        entry[1] += row["total"]
    types = sorted(((label, _tone(token), n) for token, (label, n) in merged.items()),
                   key=lambda t: -t[2])

    units = []
    for row in await get_top_violators(since, until=until, limit=TOP_UNITS):
        events = []
        for e in await get_vehicle_events(row["vehicle_number"], since, until=until):
            label, token = _row_of(e["event_type"])
            if show_severity and token == "speeding" and e.get("severity"):
                label += f" ({e['severity'].title()})"
            events.append(report_pdf.Event(e["occurred_at"].astimezone(ET), label, _tone(token)))
        units.append(report_pdf.UnitBlock(row["vehicle_number"], row["total"], events))

    return report_pdf.Report(
        company=company_name,
        period=period_label(period),
        range_text=range_text(period, since, until),
        generated=datetime.now(tz=ET),
        alerts=totals["total"],
        units_with_alerts=totals["units"],
        speeding=totals["speeding"],
        crashes=totals["crashes"],
        types=types,
        units=units,
    )


async def build_report_file(period: str, company_name: str, *,
                            show_severity: bool = False) -> bytes:
    """The finished PDF."""
    return report_pdf.render(
        await collect_report(period, company_name, show_severity=show_severity))


def report_filename(slug: str, period: str) -> str:
    return f"{slug}_{period}_report.pdf"
