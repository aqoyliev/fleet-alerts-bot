"""Is the driver-alerts bot in every usable pti-checker-bot group of a company?

    python -m scripts.audit_driver_bot --company jrd

Reads the company's usable groups from its pti database (PTI_DATABASE_URL_<SLUG>, the
same query and read-only transaction as utils/pti_groups.py) and asks Telegram, per
group, getChatMember for the driver bot itself (DRIVER_ALERTS_BOT_TOKEN). Prints the
counts (member / admin / restricted / not member / error) and the units of the groups
where the bot cannot post. Read-only: nothing is written to either side, nothing is
sent. Titles (they carry driver names) are never printed.

Exit status: 0 = the bot can post everywhere, 1 = some groups need attention,
2 = configuration or database error.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
from dataclasses import dataclass

from environs import Env

# Load .env first, then fill in what data.config demands at import but this script never
# uses (the main bot and nexus' own DB are not contacted), as tests/conftest.py does.
Env().read_env()
for _name, _placeholder in (("BOT_TOKEN", "0:unused"), ("ADMINS", "0"), ("ip", "127.0.0.1"),
                            ("GROUP_CHAT_ID", "0"), ("DATABASE_URL", "postgresql://unused")):
    os.environ.setdefault(_name, _placeholder)

from data import config  # noqa: E402
from utils import bot_membership, pti_groups  # noqa: E402

PAUSE = 0.05       # between getChatMember calls, well under Telegram's rate limits
FLOOD_RETRIES = 2
_sleep = asyncio.sleep   # tests patch this

# Membership.status -> report bucket
_BUCKETS = {
    "member": "member",
    "administrator": "admin",
    "creator": "admin",
    "restricted": "restricted",
    "left": "not member",
    "kicked": "not member",
    "not_found": "not member",
    "migrated": "error",
    "error": "error",
}
BUCKET_ORDER = ("member", "admin", "restricted", "not member", "error")


@dataclass
class Row:
    group: pti_groups.PtiGroup
    membership: bot_membership.Membership

    @property
    def bucket(self) -> str:
        return _BUCKETS.get(self.membership.status, "error")


async def _check(bot, group: pti_groups.PtiGroup) -> bot_membership.Membership:
    from aiogram.utils.exceptions import RetryAfter
    for _ in range(FLOOD_RETRIES + 1):
        try:
            member = await bot.get_chat_member(group.group_id, bot.id)
        except RetryAfter as e:
            await _sleep(e.timeout + 1)
            continue
        except Exception as e:
            return bot_membership.from_error(e)[0]
        return bot_membership.classify(member)
    return bot_membership.Membership("error", bot_membership.REASON_CHECK_FAILED, "flood control")


async def audit(bot, groups) -> list[Row]:
    rows = []
    for g in groups:
        rows.append(Row(g, await _check(bot, g)))
        await _sleep(PAUSE)
    return rows


def format_report(company: str, rows: list[Row]) -> str:
    counts = Counter(r.bucket for r in rows)
    lines = [f"Driver bot in {company} pti groups: {len(rows)} usable groups"]
    lines += [f"  {b:<11} {counts.get(b, 0)}" for b in BUCKET_ORDER]
    problems = sorted((r for r in rows if not r.membership.can_post),
                      key=lambda r: (r.bucket, r.group.unit_number))
    if problems:
        lines.append("")
        lines.append(f"Cannot post ({len(problems)}):")
        for r in problems:
            m = r.membership
            note = f" - {m.detail}" if m.detail else ""
            lines.append(f"  unit {r.group.unit_number:<10} group {r.group.group_id:<16} "
                         f"{m.status} ({m.reason}){note}")
    else:
        lines.append("")
        lines.append("The driver bot can post in every usable group.")
    return "\n".join(lines)


def _make_bot(token: str):
    from aiogram import Bot
    return Bot(token=token)


async def run(company: str) -> int:
    token = os.environ.get("DRIVER_ALERTS_BOT_TOKEN", "").strip()
    if not token:
        print("DRIVER_ALERTS_BOT_TOKEN is not set", file=sys.stderr)
        return 2
    if not pti_groups.is_configured(company):
        print(f"{config.PTI_DATABASE_URL_PREFIX}<SLUG> is not set for {company!r}", file=sys.stderr)
        return 2
    try:
        groups = await pti_groups.fetch_usable_groups(company)
    except Exception as e:
        print(f"Could not read the {company} pti groups: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    finally:
        await pti_groups.close()

    bot = _make_bot(token)
    try:
        rows = await audit(bot, groups)
    finally:
        session = await bot.get_session()
        await session.close()
    print(format_report(company, rows))
    return 0 if all(r.membership.can_post for r in rows) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--company", required=True, help="company slug, e.g. jrd")
    args = parser.parse_args(argv)
    return asyncio.run(run(args.company.strip().lower()))


if __name__ == "__main__":
    sys.exit(main())
