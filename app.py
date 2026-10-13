from aiogram import executor

from loader import dp, bot
import middlewares, filters, handlers
from utils.notify_admins import on_startup_notify
from utils.set_bot_commands import set_default_commands
from utils.webhook_handler import start_webhook_server, resume_pending_crash_confirmations
from utils.db_api import init_pool, close_pool, run_migrations
from utils.db_api.groups import ensure_group
from utils.db_api.legacy import adopt_legacy_company
from utils.db_api.admins import seed_super_admins
from utils.daily_report import schedule_daily_reports
from data import config
from data.config import ADMINS

# Hold the scheduled loop for the process's lifetime. asyncio only keeps a weak
# reference to a running task, so a bare ensure_future can be collected mid-await and
# the nightly report simply stops happening.
_daily_report_task = None


def _admin_ids() -> list[int]:
    ids = []
    for a in ADMINS:
        try:
            ids.append(int(a))
        except (TypeError, ValueError):
            pass
    return ids


def _log_alert_rules() -> None:
    """Say, once per boot, which company this is and how its alerts are ruled.

    The same code runs every company on this branch and what differs between them is a
    handful of variables. This line is how a deploy is checked against the company it
    was meant for — and where a mistyped EXTRA_EVENT_TYPES shows up, since a name that
    matches nothing is otherwise simply a type that never alerts.
    """
    import logging
    from data.event_catalog import ALERTED_TYPES, OPTIONAL_EVENT_TYPES, unknown_extra_types

    log = logging.getLogger(__name__)
    providers = [name for name, on in (("Samsara", config.SAMSARA_ENABLED),
                                       ("Motive", config.MOTIVE_ENABLED)) if on]
    extras = sorted(ALERTED_TYPES & OPTIONAL_EVENT_TYPES)
    log.info(
        f"{config.COMPANY_NAME} ({config.COMPANY_SLUG}): providers "
        f"{' + '.join(providers) or 'none configured'}; speeding from "
        f"'{config.SPEEDING_MIN_SEVERITY}'; {len(ALERTED_TYPES)} event types"
        f"{' incl. ' + ', '.join(extras) if extras else ' (core list only)'}"
    )
    unknown = unknown_extra_types()
    if unknown:
        log.warning(f"EXTRA_EVENT_TYPES names types this build does not know, ignored: "
                    f"{', '.join(unknown)}")


async def on_startup(dispatcher):
    await init_pool()
    await run_migrations()
    await adopt_legacy_company()
    await ensure_group()
    _log_alert_rules()
    await seed_super_admins(_admin_ids())
    await set_default_commands(dispatcher)
    await on_startup_notify(dispatcher)
    await start_webhook_server(bot, port=config.WEBHOOK_PORT)
    # Crash detections whose confirmation wait the last shutdown cut short.
    await resume_pending_crash_confirmations(bot)

    import asyncio
    global _daily_report_task
    _daily_report_task = asyncio.ensure_future(schedule_daily_reports(bot))


async def on_shutdown(_):
    await close_pool()


# Explicitly request the update types the bot handles. Without this, getUpdates inherits
# whatever allowed_updates was last remembered on the token — which can silently exclude
# callback_query (inline buttons) and my_chat_member (added-to-group detection). Passing
# them explicitly makes delivery deterministic and re-arms them on Telegram's side.
ALLOWED_UPDATES = [
    "message",
    "edited_message",
    "callback_query",
    "my_chat_member",
    "chat_member",
]


if __name__ == '__main__':
    executor.start_polling(dp, on_startup=on_startup, on_shutdown=on_shutdown,
                           skip_updates=True, relax=0.5, timeout=60,
                           allowed_updates=ALLOWED_UPDATES)
