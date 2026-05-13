import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot

from data import config
from utils.db_api.companies import get_all_companies, get_company_groups
from utils.db_api.violations import get_violations_by_type

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

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
    "no_seat_belt":                  "🚫",
}

_EVENT_LABEL = {
    "hard_brake":                    "Hard Brake",
    "crash":                         "Crash",
    "cell_phone":                    "Cell Phone Usage",
    "stop_sign_violation":           "Rolling Stop",
    "road_facing_cam_obstruction":   "Road Camera Obstructed",
    "driver_facing_cam_obstruction": "Driver Camera Obstructed",
    "forward_collision_warning":     "Forward Collision Warning",
    "unsafe_parking":                "Unsafe Parking",
    "speeding":                      "Speeding",
    "harsh_event":                   "Harsh Event",
    "inattentive_driving":           "Inattentive Driving",
    "drowsy_driving":                "Drowsy Driving",
    "harsh_acceleration":            "Harsh Acceleration",
    "no_seat_belt":                  "No Seat Belt",
}


def _format_daily_report(company_name: str, rows: list[dict], date_str: str) -> str:
    header = f"📊 <b>Daily Violations Report</b>\n<b>{company_name}</b> — {date_str}"
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
            lines.append(f"  🚛 Unit {v['vehicle_number']} — {v['total']}")

    return "\n".join(lines)


async def send_daily_reports(bot: Bot):
    now_et = datetime.now(tz=ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    date_str = yesterday_start.strftime("%b %d, %Y")

    companies = await get_all_companies()
    # Restrict to this bot's company so a shared DB doesn't cause cross-tenant sends.
    companies = [c for c in companies if c["slug"] == config.COMPANY_SLUG]
    for company in companies:
        try:
            rows = await get_violations_by_type(company["slug"], since=yesterday_start, until=today_start)
            text = _format_daily_report(company["name"], rows, date_str)
            group_ids = await get_company_groups(company["slug"])
            for chat_id in group_ids:
                await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Daily report error for {company['slug']}: {e}", exc_info=True)


async def schedule_daily_reports(bot: Bot):
    """Runs forever, sending the daily report at midnight ET each day."""
    while True:
        now_et = datetime.now(tz=ET)
        next_midnight = (now_et + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait_seconds = (next_midnight - now_et).total_seconds()
        logger.info(f"Daily report scheduled in {wait_seconds:.0f}s (at {next_midnight.strftime('%Y-%m-%d %H:%M %Z')})")
        await asyncio.sleep(wait_seconds)
        await send_daily_reports(bot)
