"""The violations report, independent of which surface asked for it.

Two places ask for the same numbers now: the DM flow (handlers/users/violations.py) and
the Mini App's Report screen. The period windows, the ranking and the downloadable text
file live here so those two cannot drift apart — a report that says something different
depending on which button produced it is worse than having only one button.

Free of aiogram, so the formatting can be tested without a bot token, the same reason
utils/group_texts.py is its own module.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from utils.db_api.violations import get_top_violators, get_vehicle_events

# Eastern, like every other timestamp this bot prints. See utils/webapp/api.py.
ET = ZoneInfo("America/New_York")

# The DM keyboard offers two of these (keyboards/inline/violations.py owns that choice);
# the panel offers all three. Both read their labels from here.
PERIOD_LABELS = {
    "today": "Today",
    "last_week": "Last Week",
    "last_month": "Last Month",
}

# None is the third option — "every type" — and is spelled as the empty string wherever a
# value has to survive a query string or a callback payload.
EVENT_SCOPES = {
    "": "All violations",
    "speeding": "Speeding only",
    "other": "Other violations (excl. speeding)",
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


async def top_units(period: str, event_type: str | None, limit: int = 10) -> list[dict]:
    """The ranked list both surfaces show. `event_type` is '', 'speeding' or 'other'."""
    since, until = period_range(period)
    return await get_top_violators(since, until=until, event_type=event_type or None,
                                   limit=limit)


async def build_report_file(period: str, event_type: str | None, company_name: str,
                            *, show_severity: bool = False) -> bytes:
    """The full .txt report, one unit per rank with every event under it.

    `show_severity` is the super-admin extra the DM flow has always applied: a speeding
    event's severity is only annotated for them. Passed in rather than looked up here so
    this module stays free of the admin tables.
    """
    since, until = period_range(period)
    rows = await get_top_violators(since, until=until, event_type=event_type or None,
                                   limit=50)

    now = datetime.now(tz=ET)
    lines = [
        f"{company_name} — {period_label(period)} ({date_range_label(since, until)})",
        f"Generated: {now.strftime('%b %d, %Y %I:%M %p ET')}",
        "",
    ]
    if event_type == "speeding":
        lines.append("Filter: Speeding only")
        lines.append("Note: Only days with 3+ speeding events per unit are listed.")
        lines.append("")
    elif event_type == "other":
        lines.append("Filter: Other violations (excl. speeding)")
        lines.append("")

    from utils.webhook_handler import EVENT_TYPE_MAP

    rank = 1
    for row in rows:
        unit = row["vehicle_number"]
        events = await get_vehicle_events(unit, since, until=until,
                                          event_type=event_type or None)

        if event_type == "speeding":
            by_date: dict[str, list[str]] = {}
            for e in events:
                local = e["occurred_at"].astimezone(ET)
                day = local.strftime("%b %d")
                time_str = local.strftime("%I:%M %p").lstrip("0")
                if show_severity and e.get("severity"):
                    time_str += f" ({e['severity'].title()})"
                by_date.setdefault(day, []).append(time_str)
            day_lines = []
            for day, times in by_date.items():
                if len(times) >= 3:
                    day_lines.append(f"     • {day}, {', '.join(times)} — {len(times)} times")
            if not day_lines:
                continue
            total_shown = sum(len(by_date[d]) for d in by_date if len(by_date[d]) >= 3)
            lines.append(f"{rank}. Unit {unit} — {total_shown} speeding events")
            lines.extend(day_lines)
        else:
            total = row["total"]
            lines.append(f"{rank}. Unit {unit} — {total} event{'s' if total != 1 else ''}")
            for e in events:
                local = e["occurred_at"].astimezone(ET)
                ts = local.strftime("%b %d, %I:%M %p ET")
                if not event_type or event_type == "other":
                    _, title = EVENT_TYPE_MAP.get(
                        e["event_type"], ("🚨", e["event_type"].upper()))
                    lines.append(f"     • {ts} — {title}")
                else:
                    lines.append(f"     • {ts}")

        lines.append("")
        rank += 1

    return "\n".join(lines).encode("utf-8")


def report_filename(slug: str, period: str) -> str:
    return f"{slug}_{period}_report.txt"
