import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot

from data import config
from data.event_catalog import event_label
from utils.db_api.groups import get_alert_target
from utils.db_api.violations import get_violations_by_type
from utils.tg_text import esc

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# The report's own wording for each type. Not EVENT_TYPE_MAP's titles: those are
# headlines for a single alert ("SPEEDING OVER POSTED", "CRASH DETECTED") and read wrong
# as the heading over a list of units. Covers every type the build knows — a deployment
# alerts on some of them, and an older row in its table may carry any.
_EVENT_EMOJI = {
    "hard_brake":                    "🛑",
    "crash":                         "💥",
    "cell_phone":                    "📵",
    "stop_sign_violation":           "🛑",
    "road_facing_cam_obstruction":   "📷",
    "driver_facing_cam_obstruction": "📷",
    "forward_collision_warning":     "⚠️",
    "unsafe_parking":                "🅿️",
    "speeding":                      "🚨",
    "harsh_event":                   "⚠️",
    "inattentive_driving":           "😵",
    "drowsy_driving":                "😴",
    "harsh_acceleration":            "🚀",
    "harsh_turn":                    "↩️",
    "hard_cornering":                "↩️",
    "no_seat_belt":                  "🚫",
    "obstructed_camera":             "📷",
    "seat_belt_violation":           "🔒",
    "near_miss":                     "⚠️",
    "drowsiness":                    "😴",
    "tailgating":                    "🚗",
}

_EVENT_LABEL = {
    "hard_brake":                    "Hard Brake",
    "crash":                         "Crash",
    "cell_phone":                    "Cell Phone Usage",
    "stop_sign_violation":           "Stop Sign Violation",
    "road_facing_cam_obstruction":   "Road Camera Obstructed",
    "driver_facing_cam_obstruction": "Driver Camera Obstructed",
    "forward_collision_warning":     "Forward Collision Warning",
    "unsafe_parking":                "Unsafe Parking",
    "speeding":                      "Speeding",
    "harsh_event":                   "Harsh Event",
    "inattentive_driving":           "Inattentive Driving",
    "drowsy_driving":                "Drowsy Driving",
    "harsh_acceleration":            "Harsh Acceleration",
    "harsh_turn":                    "Harsh Turn",
    "hard_cornering":                "Harsh Turn",
    "no_seat_belt":                  "No Seat Belt",
    "obstructed_camera":             "Camera Obstructed",
    "seat_belt_violation":           "Seat Belt Violation",
    "near_miss":                     "Near Miss",
    "drowsiness":                    "Drowsiness",
    "tailgating":                    "Tailgating",
}
# Two of these are named differently by the two providers, and a report has no single
# event to take the name from — so it uses the fleet's: Samsara's on a Samsara-only
# fleet ("Harsh Brake", "Rolling Stop"), Motive's wherever there is any Motive.
_EVENT_LABEL = {t: event_label(t, label) for t, label in _EVENT_LABEL.items()}


def _format_daily_report(company_name: str, rows: list[dict], date_str: str) -> str:
    header = f"📊 <b>Daily Violations Report</b>\n<b>{esc(company_name)}</b> — {date_str}"
    if not rows:
        return header + "\n\n✅ No violations."

    # Group rows by event_type
    by_type: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_type[row["event_type"]].append(row)

    # Total violations per type for section ordering
    type_totals = {et: sum(r["total"] for r in vs) for et, vs in by_type.items()}

    # Speeding first, then rest sorted by total desc
    ordered = []
    if "speeding" in by_type:
        ordered.append("speeding")
    for et, _ in sorted(type_totals.items(), key=lambda x: -x[1]):
        if et != "speeding":
            ordered.append(et)

    lines = [header]
    for et in ordered:
        vehicles = sorted(by_type[et], key=lambda r: -r["total"])
        emoji = _EVENT_EMOJI.get(et, "⚠️")
        label = _EVENT_LABEL.get(et, et.replace("_", " ").title())
        lines.append(f"\n{emoji} <b>{label}</b>")
        for v in vehicles:
            lines.append(f"  🚛 Unit {esc(v['vehicle_number'])} — {v['total']}")

    return "\n".join(lines)


async def send_daily_reports(bot: Bot):
    now_et = datetime.now(tz=ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    date_str = yesterday_start.strftime("%b %d, %Y")

    try:
        # Asked without an event type: the report is not an alert, so the group's event
        # filter has no say in it. A muted group still gets nothing, report included.
        chat_id = await get_alert_target()
        if not chat_id:
            logger.info("Daily report not sent: the alerts group is muted or not configured")
            return
        rows = await get_violations_by_type(since=yesterday_start, until=today_start)
        text = _format_daily_report(config.COMPANY_NAME, rows, date_str)
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Daily report error: {e}", exc_info=True)


async def schedule_daily_reports(bot: Bot):
    """Runs forever, sending the daily report at midnight ET each day."""
    while True:
        now_et = datetime.now(tz=ET)
        next_midnight = (now_et + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        wait_seconds = (next_midnight - now_et).total_seconds()
        logger.info(f"Daily report scheduled in {wait_seconds:.0f}s (at {next_midnight.strftime('%Y-%m-%d %H:%M %Z')})")
        await asyncio.sleep(wait_seconds)
        await send_daily_reports(bot)
