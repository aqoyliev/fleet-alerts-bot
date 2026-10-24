"""Add the driver-alerts bot to a company's pti groups it is missing from, via the userbot.

    python -m scripts.add_driver_bot --company jrd            # dry run: report only
    python -m scripts.add_driver_bot --company jrd --apply    # add, one group per --pace s

For every usable group in the company's pti-checker-bot DB (utils.pti_groups), the
driver bot's membership is read with getChatMember. Where it is missing and the userbot
is in the group (and may add members), --apply adds the bot, then re-checks it. Groups
the userbot is not in are listed for HOS. Prints unit numbers and group ids only.

Env: PTI_DATABASE_URL_<SLUG>, DRIVER_ALERTS_BOT_TOKEN, USERBOT_API_ID, USERBOT_API_HASH,
USERBOT_SESSION. The main bot's settings get dummies (as in scripts.audit_driver_bot).
The Telethon helpers are utils.userbot's. Do not run this while the driver-alerts
service uses the same session (USERBOT_ENABLED=true there): Telegram may revoke a
session used from two places at once (AUTH_KEY_DUPLICATED).
"""
import argparse
import asyncio
import os
import sys

for _k, _v in {"BOT_TOKEN": "1:x", "ADMINS": "1", "ip": "x", "GROUP_CHAT_ID": "1",
               "DATABASE_URL": "postgresql://unused@localhost/unused"}.items():
    os.environ.setdefault(_k, _v)

import aiohttp  # noqa: E402
from telethon import errors  # noqa: E402
from telethon.tl.types import Channel  # noqa: E402

from utils import pti_groups, userbot  # noqa: E402
from utils.unit_key import normalize_unit  # noqa: E402

IN_GROUP = ("creator", "administrator", "member")
MAX_FAILURES_IN_A_ROW = 3


async def _bot_api(session, token, method, **params):
    async with session.get(f"https://api.telegram.org/bot{token}/{method}", params=params) as r:
        return await r.json()


async def bot_status(session, token, chat_id, bot_id) -> str:
    b = await _bot_api(session, token, "getChatMember", chat_id=chat_id, user_id=bot_id)
    return b["result"]["status"] if b.get("ok") else "not_found"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--pace", type=float, default=60.0, help="seconds between adds")
    ap.add_argument("--limit", type=int, default=0, help="add at most this many (0 = all)")
    ap.add_argument("--skip-units", default="", help="comma list of units to leave alone")
    args = ap.parse_args()
    skip = {normalize_unit(u) for u in args.skip_units.split(",") if u.strip()}
    e = os.environ

    groups = await pti_groups.fetch_usable_groups(args.company)
    await pti_groups.close()
    token = e["DRIVER_ALERTS_BOT_TOKEN"]
    bot_id = int(token.split(":")[0])

    async with aiohttp.ClientSession() as http:
        me = await _bot_api(http, token, "getMe")
        bot_username = me["result"]["username"]
        missing = []
        for g in groups:
            if await bot_status(http, token, g.group_id, bot_id) not in IN_GROUP:
                missing.append(g)
            await asyncio.sleep(0.05)
        print(f"Company {args.company}: {len(groups)} usable groups, @{bot_username} missing "
              f"in {len(missing)}. Mode: {'APPLY' if args.apply else 'DRY RUN'}\n")

        client = userbot.make_client(e["USERBOT_SESSION"], int(e["USERBOT_API_ID"]),
                                     e["USERBOT_API_HASH"])
        await client.connect()
        if not await client.is_user_authorized():
            print("Userbot session is not authorized")
            return 2
        dialogs = await userbot.group_dialogs(client)
        bot = await client.get_input_entity(bot_username)

        plan, for_hos, blocked = [], [], []
        for g in sorted(missing, key=lambda g: g.unit_number):
            if normalize_unit(g.unit_number) in skip:
                print(f"  unit {g.unit_number:<10} skipped (--skip-units)")
                continue
            ent = dialogs.get(g.group_id)
            if ent is None:
                for_hos.append(g)
                continue
            if userbot.stale_basic_group(ent):
                blocked.append((g, "basic group migrated/deactivated: pti has a stale id"))
                continue
            perms = await client.get_permissions(ent, "me")
            if not userbot.can_add(ent, perms):
                blocked.append((g, "userbot has no right to add members"))
                continue
            plan.append((g, ent))

        print(f"Would add the bot ({len(plan)}):")
        for g, ent in plan:
            kind = "supergroup" if isinstance(ent, Channel) else "group"
            print(f"  unit {g.unit_number:<10} {g.group_id:<16} {kind}")
        print(f"\nUserbot not in the group -> HOS ({len(for_hos)}):")
        for g in for_hos:
            print(f"  unit {g.unit_number:<10} {g.group_id}")
        print(f"\nCannot add -> admin ({len(blocked)}):")
        for g, why in blocked:
            print(f"  unit {g.unit_number:<10} {g.group_id:<16} {why}")

        if not args.apply:
            await client.disconnect()
            return 0

        todo = plan[:args.limit] if args.limit else plan
        added, failed, in_a_row = 0, 0, 0
        print(f"\nAdding to {len(todo)} group(s), one every {args.pace:.0f}s")
        for i, (g, ent) in enumerate(todo):
            if i:
                await asyncio.sleep(args.pace)
            try:
                await userbot.add_member(client, ent, bot)
            except errors.FloodWaitError as ex:
                print(f"  FLOOD WAIT {ex.seconds}s at unit {g.unit_number} - stopping; rerun later")
                break
            except Exception as ex:
                failed += 1
                in_a_row += 1
                print(f"  unit {g.unit_number:<10} FAILED {type(ex).__name__}: {ex}")
                if in_a_row >= MAX_FAILURES_IN_A_ROW:
                    print("  too many failures in a row - stopping")
                    break
                continue
            in_a_row = 0
            await asyncio.sleep(2)
            status = await bot_status(http, token, g.group_id, bot_id)
            ok = status in IN_GROUP
            added += ok
            print(f"  unit {g.unit_number:<10} added -> bot status {status}{'' if ok else '  (CHECK)'}")
        await client.disconnect()
        print(f"\nDone: {added} added, {failed} failed")
        return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
