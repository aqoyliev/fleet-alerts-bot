"""The violations report, independent of which surface asked for it.

The Mini App's Report screen asks for these numbers; the period windows, the ranking and
the downloadable text file live here, away from the HTTP handlers, so the formatting can
be tested on its own.

Free of aiogram, so the formatting can be tested without a bot token, the same reason
utils/group_texts.py is its own module.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from utils.db_api.violations import get_top_violators, get_vehicle_events

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


async def build_report_file(period: str, company_name: str, *,
                            show_severity: bool = False) -> bytes:
    """The full .txt report: every unit that had a violation in the window, ranked, with
    every event under it and what each one was.

    `show_severity` is the super-admin extra: a speeding event's severity is only
    annotated for them. Passed in rather than looked up here so this module stays free of
    the admin tables.
    """
    since, until = period_range(period)
    rows = await get_top_violators(since, until=until, limit=50)

    now = datetime.now(tz=ET)
    lines = [
        f"{company_name} — {period_label(period)} ({date_range_label(since, until)})",
        f"Generated: {now.strftime('%b %d, %Y %I:%M %p ET')}",
        "",
    ]

    from utils.webhook_handler import EVENT_TYPE_MAP

    for rank, row in enumerate(rows, 1):
        unit = row["vehicle_number"]
        events = await get_vehicle_events(unit, since, until=until)

        total = row["total"]
        lines.append(f"{rank}. Unit {unit} — {total} event{'s' if total != 1 else ''}")
        for e in events:
            local = e["occurred_at"].astimezone(ET)
            ts = local.strftime("%b %d, %I:%M %p ET")
            _, title = EVENT_TYPE_MAP.get(e["event_type"], ("🚨", e["event_type"].upper()))
            if show_severity and e["event_type"] == "speeding" and e.get("severity"):
                title += f" ({e['severity'].title()})"
            lines.append(f"     • {ts} — {title}")
        lines.append("")

    return "\n".join(lines).encode("utf-8")


def report_filename(slug: str, period: str) -> str:
    return f"{slug}_{period}_report.txt"
