import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot

from utils.db_api.companies import get_all_companies
from utils.db_api.violations import get_violations_by_type
from utils.db_api.db import fetch
from utils.webhook_handler import EVENT_TYPE_MAP

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


async def _get_company_groups(company_slug: str) -> list[int]:
    # Samsara-only groups are excluded: they exist to receive nothing but Samsara
    # alerts, and the daily report aggregates violations from both providers. The
    # report keeps going to the original (motive / unrestricted) groups as before.
    rows = await fetch(
        """
        SELECT cg.telegram_group_id
        FROM company_groups cg
        JOIN companies c ON c.id = cg.company_id
        WHERE c.slug = $1
          AND (cg.alert_source IS NULL OR cg.alert_source <> 'samsara')
        """,
        company_slug,
    )
    return [r["telegram_group_id"] for r in rows]


def _format_daily_report(company_name: str, rows: list[dict], date_str: str) -> str:
    header = f"📊 <b>Daily Violations Report</b>\n<b>{company_name}</b> — {date_str}\n"
    if not rows:
        return header + "\n✅ No violations today."

    # Group by event_type
    by_type: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_type[row["event_type"]].append(row)

    # Speeding first, then other types sorted by total count desc
    def _type_sort_key(et):
        if et == "speeding":
            return (0, 0)
        return (1, -sum(v["total"] for v in by_type[et]))

    lines = [header]
    for event_type in sorted(by_type, key=_type_sort_key):
        emoji, title = EVENT_TYPE_MAP.get(event_type, ("⚠️", event_type.replace("_", " ").title()))
        lines.append(f"\n{emoji} <b>{title}</b>")
        for v in by_type[event_type]:
            lines.append(f"  🚛 {v['vehicle_number']} — {v['total']}")

    return "\n".join(lines)


async def send_daily_reports(bot: Bot):
    now_et = datetime.now(tz=ET)
    today_start = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start = today_start - timedelta(days=1)
    date_str = yesterday_start.strftime("%b %d, %Y")

    companies = await get_all_companies()
    for company in companies:
        try:
            rows = await get_violations_by_type(company["slug"], since=yesterday_start, until=today_start)
            text = _format_daily_report(company["name"], rows, date_str)
            group_ids = await _get_company_groups(company["slug"])
            for chat_id in group_ids:
                await bot.send_message(chat_id, text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Daily report error for {company['slug']}: {e}", exc_info=True)


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
