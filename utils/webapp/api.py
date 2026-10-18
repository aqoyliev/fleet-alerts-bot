"""JSON endpoints behind the admin Mini App.

Every handler here is wrapped in @require_admin or @require_super, so by the time one
runs the caller is a verified Telegram user who is an active admin, and their id is at
request["telegram_id"].

Two rules the whole file follows:

Reuse the bot's decisions, don't restate them. A unit is checked by the same resolve_unit
the /setunit command uses; an event filter is toggled by the same next_event_filter the
inline keyboard uses; admins are hidden by the same visible_admins the 👥 panel uses. The
panel must not become a softer path to the same tables — wherever it enforces less than
the bot does, that gap IS the bug.

Values from the browser are bound parameters, never interpolated into SQL, and never
trusted as validation. The client picking a unit from a roster dropdown is a convenience;
the server still asks the provider.
"""

import io
import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from aiohttp import web

from data import config
from data.event_catalog import GROUP_FILTER_TYPES, expand_tokens, next_event_filter
from loader import bot
from utils.db_api.admins import (
    add_admin, delete_admin, demote_from_super, get_admin_by_id, get_admin_subscriptions,
    get_all_admins, is_maintainer, is_super_admin, promote_to_super, set_admin_active,
    set_subscription, visible_admins,
)
from utils.db_api.groups import (
    get_group, get_group_event_types, get_groups_overview, remove_group,
    set_group_enabled, set_group_event_types, set_group_unit,
)
from utils.db_api.users import ensure_user
from utils import group_texts
from utils.db_api.violations import (
    get_counts_by_vehicle, get_daily_counts, get_recent_events, get_totals,
    get_top_violators, get_type_counts,
)
from utils.units import (
    clean_unit, list_units_any, resolve_unit, roster_names, suggest_units_any,
)
from utils.violation_report import (
    PERIOD_LABELS, build_report_file, date_range_label, period_label,
    period_range, report_filename,
)
from utils.webapp.auth import require_admin, require_super

logger = logging.getLogger(__name__)

# Every timestamp the panel shows is Eastern, because every timestamp the bot shows is
# (utils/daily_report.py, handlers/users/violations.py, the alert cards themselves). A
# panel on a different clock than the group messages it describes is worse than a panel
# on the wrong clock.
ET = ZoneInfo("America/New_York")

# Said whenever a mutation lands on an account named in the deployment's ADMINS. That
# list is the floor under this whole panel: seed_super_admins re-asserts it on every
# boot, so a change made here would be undone by the next restart and the panel would
# have lied about it.
_OWNER_ROW = ("That account is set in the deployment's own config. Change it there, "
              "not here.")


# ── plumbing ────────────────────────────────────────────────────────────────────

def _json_default(value):
    """asyncpg hands back datetime, date and Decimal, none of which json knows.

    Centralized here rather than converted per-endpoint: the first aggregate someone adds
    with an AVG in it would otherwise 500 in production and nowhere else.
    """
    if isinstance(value, datetime):
        return value.astimezone(ET).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _ok(data) -> web.Response:
    return web.json_response(data, dumps=lambda d: json.dumps(d, default=_json_default))


def _fail(code: str, message: str, status: int = 400, **extra) -> web.Response:
    """Uniform error shape. `message` is shown to the dispatcher verbatim, so it is
    written as a sentence someone can act on, not as a status name."""
    return web.json_response({"error": code, "message": message, **extra}, status=status)


async def _body(request: web.Request) -> dict:
    try:
        payload = await request.json()
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _int_param(request: web.Request, name: str) -> int | None:
    """Path params reach asyncpg as query arguments; a non-numeric one raises there and
    surfaces as a 500. Parsed up front so it is a 400 instead."""
    try:
        return int(request.match_info[name])
    except (KeyError, TypeError, ValueError):
        return None


# Every window the numbers screen offers, rolling and calendar in one table.
#
# The panel used to have only the three rolling windows and the report only the two
# calendar ones, on the reasoning that a live panel answers "how are we doing right now"
# and a report answers "what happened last week". Both are true, and while the two lived
# on separate screens the split was invisible. They are one screen now, and a dispatcher
# who reads "Top units, last 7 days" and then downloads "last week" gets two different
# answers to one question -- so the panel offers both windows explicitly and labels which
# is which, instead of choosing silently.
_ROLLING = {"today": (0, "Today"), "7d": (6, "Last 7 days"), "30d": (29, "Last 30 days")}
# "yesterday" is a calendar day, not a rolling one: the full day that just ended, which is
# the window somebody opens the panel on a morning to ask about.
PANEL_PERIODS = ("today", "yesterday", "7d", "30d", "last_week", "last_month")


def _period_range(period: str) -> tuple[datetime, datetime, str]:
    """(since, until, label) for one of the panel's windows, in Eastern time."""
    if period in ("last_week", "last_month"):
        since, until = period_range(period)
        return since, until, period_label(period)
    now = datetime.now(tz=ET)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "yesterday":
        return midnight - timedelta(days=1), midnight, "Yesterday"
    days, label = _ROLLING.get(period, _ROLLING["today"])
    return (midnight - timedelta(days=days) if days else midnight), now, label


def _range_text(period: str, since: datetime, until: datetime) -> str:
    """The dates a window covers, as a person would write them.

    The calendar windows end at the midnight that *starts* the next one, so printing
    `until` as-is says the week of Oct 5 ran to Oct 12. The last day shown is the last
    day inside the window.
    """
    if period in ("yesterday", "last_week", "last_month"):
        until = until - timedelta(seconds=1)
        if until.date() == since.date():
            return since.strftime("%b %d, %Y")
    return date_range_label(since, until)


def _chip_token(event_type: str) -> str:
    """The catalog row a stored event type belongs to, or itself.

    The feed filters by row now (see alerts), so a By-type row on the numbers screen has
    to link with the row's token rather than with the exact type it counted -- otherwise
    tapping "Camera Obstructed · 14" on a Samsara fleet asks the feed for
    obstructed_camera, which is a type the chips do not know.
    """
    for types, _, _ in GROUP_FILTER_TYPES:
        if event_type in types:
            return types[0]
    return event_type


def _event_label(event_type: str) -> tuple[str, str]:
    """Emoji and title for an event type, from the same map the alert cards use."""
    from utils.webhook_handler import EVENT_TYPE_MAP
    return EVENT_TYPE_MAP.get(event_type, ("⚠️", event_type.replace("_", " ").title()))


def _is_reserved(telegram_group_id: int) -> bool:
    """The two chats configured in .env rather than registered in alert_groups.

    The main group is recreated by ensure_main_group on every boot, so removing it from
    the panel would be a lie with a deploy-shaped expiry. The crash group is deliberately
    absent from the table entirely (data/config.py) — a row there with a NULL unit *is*
    the main group in this schema. Neither is the panel's to edit.
    """
    return telegram_group_id in {config.MAIN_GROUP_ID, config.CRASH_GROUP_ID}


# ── reads ───────────────────────────────────────────────────────────────────────

_bot_username: str | None = None


async def _username() -> str:
    """The bot's @name, for t.me/<bot>?startgroup=true.

    Adding the bot to a chat is the one setup step the panel genuinely cannot perform --
    Telegram has no API for a bot to put itself in a group. It does have a deep link that
    opens the user's own chat picker and does it in one tap, and that link needs the
    username. Asked once and kept: it changes about as often as the bot token.

    A failure is not cached, so a panel opened during a Telegram outage is not stuck
    without the link for the life of the process.
    """
    global _bot_username
    if _bot_username is None:
        try:
            _bot_username = (await bot.get_me()).username or ""
        except Exception as e:
            logger.warning(f"[webapp] get_me failed, no group link this time: {e}")
            return ""
    return _bot_username


@require_admin
async def bootstrap(request: web.Request) -> web.Response:
    """Everything the shell needs to render before it knows anything else.

    One call rather than four so the panel doesn't waterfall on a truck-stop LTE
    connection, and the event catalog ships from here so data/event_catalog.py stays the
    single source of truth instead of being copied into JavaScript.
    """
    telegram_id = request["telegram_id"]
    return _ok({
        "me": {
            "telegram_id": telegram_id,
            "is_super": await is_super_admin(telegram_id),
            # The deployment's own account (config.ADMINS), which outranks a super admin
            # the company promoted: only it may take the super-admin role back off
            # someone. Says nothing about anyone else -- a viewer learns their own tier
            # and no one else's, which is what HIDDEN_ADMIN_IDS exists to protect.
            "is_owner": is_maintainer(telegram_id),
        },
        "company": {"name": config.COMPANY_NAME, "slug": config.COMPANY_SLUG},
        "bot_username": await _username(),
        "event_types": [
            # `types` as well as the token: a stored event type has to be mapped back to
            # the row it belongs to in a couple of places on the client -- a group's
            # filter count, and the feed link under a By-type row -- and without the full
            # membership the client would have to keep its own copy of the aliases.
            {"type": types[0], "types": list(types), "emoji": emoji, "label": label}
            for types, emoji, label in GROUP_FILTER_TYPES
        ],
        "main_group_id": config.MAIN_GROUP_ID,
        "crash_group_id": config.CRASH_GROUP_ID,
        "samsara_enabled": bool(config.SAMSARA_API_KEY),
    })


@require_admin
async def stats(request: web.Request) -> web.Response:
    """Everything the numbers screen shows, for one window.

    One endpoint rather than two. The panel used to read this for its dashboard and a
    second endpoint for its report, which is how it ended up showing two rankings of the
    same thing with different numbers on one scroll.
    """
    period = request.query.get("period", "today")
    if period not in PANEL_PERIODS:
        period = "today"

    since, until, label = _period_range(period)
    totals = await get_totals(since, until)
    by_type = await get_type_counts(since, until)
    top_units = await get_top_violators(since=since, until=until, limit=10)
    by_day = (await get_daily_counts(since, until)
              if since.date() != until.date() else [])

    groups = await get_groups_overview()
    muted = [g for g in groups if not g["enabled"]]
    # Coverage, from the list this handler already has in hand -- no second query. It is
    # on the numbers screen because "18 of 20 groups are pointed at a truck" is a number,
    # and because the two that are not are the reason some of the others read zero.
    unpaired = [g for g in groups if not g["is_main"] and not g["vehicle_number"]]

    return _ok({
        "period": {"key": period, "label": label,
                   "range": _range_text(period, since, until),
                   "downloadable": period in PERIOD_LABELS},
        "totals": dict(totals),
        "by_type": [
            {**row, "emoji": _event_label(row["event_type"])[0],
             "label": _event_label(row["event_type"])[1],
             "token": _chip_token(row["event_type"])}
            for row in by_type
        ],
        "by_day": by_day,
        "top_units": top_units,
        "groups": {
            "total": len(groups),
            "muted": len(muted),
            "muted_titles": [g["title"] or str(g["telegram_group_id"]) for g in muted[:5]],
            "unpaired": len(unpaired),
            "unpaired_titles": [g["title"] or str(g["telegram_group_id"])
                                for g in unpaired[:5]],
            "with_unit": len(groups) - len(unpaired),
        },
    })


@require_admin
async def groups(request: web.Request) -> web.Response:
    """The Groups tab. Alert counts are attached in Python — see get_counts_by_vehicle
    for why they aren't joined."""
    since = datetime.now(tz=ET).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
    rows = await get_groups_overview()
    counts = await get_counts_by_vehicle(since)

    out = []
    for row in rows:
        unit = row["vehicle_number"]
        out.append({
            **row,
            # Straight from the row now. Inferring it from a missing unit would label
            # every just-added group the all-fleet one and hide the unit picker on the
            # single screen that exists to use it.
            "is_main": row["is_main"],
            # Zero rows in group_event_types means "every type", not "no types". Naming
            # that here keeps the UI from rendering an empty list as "receives nothing".
            "filter_mode": "all" if not row["event_types"] else "custom",
            # The whole fleet's week for the all-fleet group, this truck's for a driver
            # group, and zero for one with no unit yet — which receives nothing, and would
            # otherwise be shown the fleet-wide figure as if it had been getting it.
            "alerts_7d": (sum(counts.values()) if row["is_main"]
                          else counts.get(unit, 0) if unit else 0),
        })
    return _ok(out)


@require_admin
async def alerts(request: web.Request) -> web.Response:
    """The feed. Keyset-paginated: the cursor is the last row's (occurred_at, id)."""
    try:
        limit = max(1, min(int(request.query.get("limit", 50)), 200))
    except ValueError:
        limit = 50

    before_ts = request.query.get("before_ts") or None
    if before_ts:
        try:
            before_ts = datetime.fromisoformat(before_ts)
        except ValueError:
            return _fail("bad_cursor", "That page cursor isn't valid.")
    try:
        before_id = int(request.query["before_id"]) if request.query.get("before_id") else None
    except ValueError:
        return _fail("bad_cursor", "That page cursor isn't valid.")

    # One chip, every spelling behind it. The filter chips are the catalog's rows and a
    # row can carry several real event types; expand_tokens is the same function the group
    # filter uses, so the feed and the filter agree about what a label means. A token that
    # is not a catalog row -- crash is deliberately not one -- stands for itself.
    token = request.query.get("type") or None
    event_types = sorted(expand_tokens([token])) or [token] if token else None

    rows = await get_recent_events(
        limit=limit, before_ts=before_ts, before_id=before_id,
        event_types=event_types,
        vehicle_number=request.query.get("unit") or None,
    )
    items = [
        {**row, "emoji": _event_label(row["event_type"])[0],
         "label": _event_label(row["event_type"])[1]}
        for row in rows
    ]
    # A full page implies there may be more; a short one is the end. Cheaper than a
    # COUNT(*) over a table that only grows.
    nxt = None
    if len(rows) == limit:
        last = rows[-1]
        nxt = {"before_ts": last["occurred_at"], "before_id": last["id"]}
    return _ok({"items": items, "next": nxt})


@require_admin
async def units(request: web.Request) -> web.Response:
    """The fleet roster, so changing a unit is a pick rather than a guess.

    Every configured provider, merged by list_units_any. That merge is the whole point of
    this endpoint now: the picker is the only way this screen can set a unit, and while
    it offered Samsara's vehicles alone, a truck that only Motive knew about could not be
    paired here at all — nor in the chat, since /setunit refused it too.

    Degrades to an empty list with a reason rather than failing the screen: a deployment
    with no provider configured has no roster, and one provider's outage must not make
    the group detail unopenable.
    """
    roster = roster_names()
    if not roster:
        return _ok({"units": [], "available": False,
                    "reason": "This deployment has no vehicle roster configured."})
    names, available = await list_units_any()
    if not available:
        return _ok({"units": [], "available": False,
                    "reason": f"{roster} couldn't be reached just now."})

    taken = {g["vehicle_number"] for g in await get_groups_overview() if g["vehicle_number"]}
    return _ok({
        "units": [{"name": n, "linked": n in taken} for n in names],
        "available": True,
    })


def _subscription_group(token: str) -> tuple[str, ...]:
    """Every event type one notification switch covers.

    The same rule the group filter follows (data/event_catalog.py): a row like "Camera
    Obstructed" is three provider spellings, and get_subscribed_admins matches the stored
    type exactly. Subscribing to the token alone would silence whichever provider spells
    it differently -- which is the bug that made Motive's drowsiness alerts vanish from
    group filters, and it must not be reintroduced here.
    """
    if token == "crash":
        return ("crash",)
    for types_, _, _ in GROUP_FILTER_TYPES:
        if types_[0] == token:
            return types_
    return ()


@require_admin
async def settings(request: web.Request) -> web.Response:
    """Which alerts this admin gets in their own DM.

    The same switches as the bot's Settings menu, over the curated catalog rather than
    the raw EVENT_TYPE_MAP the inline keyboard walks -- that map carries provider
    aliases, so it offered the same behaviour twice under two names.
    """
    subscribed = set(await get_admin_subscriptions(request["telegram_id"]))
    rows = [{
        "type": "crash", "emoji": "\U0001f4a5", "label": "Crash",
        "on": "crash" in subscribed,
        # The one opt-OUT switch, and the only alert that reaches no group at all unless
        # the deployment has a CRASH_GROUP_ID. Worth saying on the row.
        "note": "On unless you turn it off",
    }]
    for types_, emoji, label in GROUP_FILTER_TYPES:
        rows.append({"type": types_[0], "emoji": emoji, "label": label,
                     "on": any(t in subscribed for t in types_)})
    return _ok({"notifications": rows})


@require_admin
async def admins(request: web.Request) -> web.Response:
    """Maintainers are filtered out server-side. The panel must not become the place that
    leaks what the bot deliberately hides (see data/config.py on HIDDEN_ADMIN_IDS).

    is_owner is added after that filter, never before it: for anyone but a maintainer
    every surviving row is False, because the rows it would be True for are already gone.
    A maintainer sees it on the rows only they can see, which is what lets the panel grey
    out the controls that config.ADMINS would override on the next restart instead of
    offering them and failing.
    """
    everyone = await get_all_admins()
    rows = visible_admins(everyone, request["telegram_id"])
    return _ok([{**row, "is_owner": is_maintainer(row["telegram_id"])} for row in rows])


# ── group mutations ─────────────────────────────────────────────────────────────

async def _load_editable_group(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """Resolve the {tgid} path param to a group the panel may edit, or an error.

    Callers must test the error with `is not None`, never for truthiness: aiohttp's
    Response defines __len__, and an empty-body response is therefore falsy — so a
    plain `if err:` skipped every refusal this function returns and let the handler
    run on a group of None.
    """
    tgid = _int_param(request, "tgid")
    if tgid is None:
        return None, _fail("bad_request", "That group id isn't valid.")
    if _is_reserved(tgid):
        return None, _fail(
            "reserved_group",
            "This chat is configured in the deployment settings, not here.", status=403,
        )
    group = await get_group(tgid)
    if group is None:
        return None, _fail("not_found", "That group isn't registered.", status=404)
    return group, None


@require_admin
async def set_enabled(request: web.Request) -> web.Response:
    group, err = await _load_editable_group(request)
    if err is not None:
        return err
    enabled = bool((await _body(request)).get("enabled"))
    await set_group_enabled(group["telegram_group_id"], enabled)
    logger.info(f"[webapp] {request['telegram_id']} "
                f"{'unmuted' if enabled else 'muted'} group {group['telegram_group_id']}")
    return _ok({"ok": True, "enabled": enabled})


@require_admin
async def set_unit(request: web.Request) -> web.Response:
    """Repoint a group at a different truck.

    The roster picker in the UI makes a bad value unlikely; this makes it impossible. A
    unit no provider can confirm is refused rather than stored, because a stored guess
    produces a group that looks configured and silently receives nothing — see
    utils/units.py for the whole argument.
    """
    group, err = await _load_editable_group(request)
    if err is not None:
        return err

    # Same input rules as /setunit -- they share clean_unit so the two surfaces cannot
    # drift apart on what counts as a unit.
    roster = roster_names()
    unit = clean_unit(str((await _body(request)).get("unit", "")))
    if unit is None:
        return _fail("bad_unit", "Pick a unit from the list, or type its name the way "
                                 f"{roster} spells it.")

    status, resolved = await resolve_unit(unit)

    if status == "missing":
        return _fail("unit_not_found", f"No unit “{unit}” in {roster}.", status=409,
                     suggestions=await suggest_units_any(unit))
    if status == "unavailable":
        return _fail(
            "roster_unavailable",
            f"Couldn't finish checking that unit against {roster}, so it wasn't saved — "
            "the spelling has to match the roster exactly or this group would receive "
            "nothing. Try again in a few minutes.",
            status=503,
        )

    await set_group_unit(group["telegram_group_id"], resolved)
    logger.info(f"[webapp] {request['telegram_id']} set group "
                f"{group['telegram_group_id']} to unit {resolved}")
    await _tell_group_its_unit(group, resolved)
    return _ok({
        "ok": True,
        "vehicle_number": resolved,
        # Surfaced so the dispatcher sees that "571" became "unit571" rather than
        # wondering why the field doesn't show what they typed. The provider that matched
        # is not named, because resolve_unit does not report it and naming the wrong one
        # would be worse than naming none.
        "note": f"Matched the roster's “{resolved}”." if resolved != unit else None,
        "unverified": status == "no_roster",
    })


async def _tell_group_its_unit(group: dict, unit: str) -> None:
    """Post the change into the driver group, and only there.

    The admins are deliberately not DMed, unlike the /setunit path: the admin who did
    this is looking at the screen they did it on. The people it is news to are in the
    chat — the bot told them on the day it joined that nothing would arrive until
    somebody set a unit, and nothing has told them since.

    Never allowed to fail the request. The unit is stored by the time this runs, and a
    group that has restricted the bot, or thrown it out without the bot hearing about it,
    must not turn a successful edit into an error the admin would then retry.
    """
    if group["vehicle_number"] == unit:
        return  # nothing changed; saying so in the chat is noise
    try:
        await bot.send_message(
            group["telegram_group_id"],
            group_texts.unit_set(unit, previous=group["vehicle_number"],
                                 muted=not group["enabled"]),
            parse_mode="HTML", disable_web_page_preview=True,
        )
    except Exception as e:
        logger.warning(f"[webapp] couldn't tell group {group['telegram_group_id']} "
                       f"about its unit: {e}")


@require_admin
async def toggle_event(request: web.Request) -> web.Response:
    """Change a group's event filter: flip one type, set the whole list, or reset to
    "all".

    Every one of those is decided in data/event_catalog.py and stays there. Batching in
    the browser and saving a computed allowlist would mean reimplementing the collapse
    rule -- "an allowlist covering every type becomes the empty all-types state" -- in
    JavaScript, and when the two drifted a group would quietly stop receiving newly
    added event types with nothing in the UI to show for it.

    So "set" sends the tokens the user picked, not a computed filter: the browser says
    what was tapped and the server says what that means. That is what makes one round
    trip safe where fifteen taps used to be fifteen. Every response carries the
    authoritative list the client re-renders from.
    """
    group, err = await _load_editable_group(request)
    if err is not None:
        return err

    body = await _body(request)
    tgid = group["telegram_group_id"]

    known = {types[0] for types, _, _ in GROUP_FILTER_TYPES}

    if body.get("action") == "all":
        await set_group_event_types(tgid, set())
        selected: list[str] = []
    elif body.get("action") == "set":
        tokens = body.get("event_types")
        if not isinstance(tokens, list) or any(t not in known for t in tokens):
            return _fail("bad_event_type", "That isn't an event type this bot sends.")
        # An empty allowlist means "every type" in this schema, so saving an empty
        # selection would turn "send nothing" into "send everything" -- the exact
        # opposite of what was asked, silently. Muting a group is a different control
        # and it is on the row above.
        if not tokens:
            return _fail("empty_filter", "Pick at least one event type — or mute the "
                                         "group if it should receive nothing.")
        updated = expand_tokens(tokens)
        await set_group_event_types(tgid, updated)
        selected = sorted(updated)
    else:
        event_type = body.get("event_type")
        if event_type not in known:
            return _fail("bad_event_type", "That isn't an event type this bot sends.")
        current = set(await get_group_event_types(tgid))
        updated = next_event_filter(current, event_type)
        await set_group_event_types(tgid, updated)
        selected = sorted(updated)

    logger.info(f"[webapp] {request['telegram_id']} changed the event filter on group {tgid}")
    return _ok({"ok": True, "event_types": selected,
                "filter_mode": "all" if not selected else "custom"})


@require_super
async def delete_group(request: web.Request) -> web.Response:
    group, err = await _load_editable_group(request)
    if err is not None:
        return err
    if not (await _body(request)).get("confirm"):
        return _fail("confirm_required", "This needs to be confirmed.")
    await remove_group(group["telegram_group_id"])
    logger.info(f"[webapp] {request['telegram_id']} removed group {group['telegram_group_id']}")
    return _ok({"ok": True})


# ── admin mutations ─────────────────────────────────────────────────────────────
#
# This is now the only surface for admin management — the old in-chat 👥 Admins flow
# was removed once the panel covered everything it did. The refusals below (no
# self-removal, no touching a super admin, maintainer concealed) are the same rules
# that flow used to enforce.

async def _load_target_admin(request: web.Request) -> tuple[dict | None, web.Response | None]:
    admin_id = _int_param(request, "admin_id")
    if admin_id is None:
        return None, _fail("bad_request", "That admin id isn't valid.")
    target = await get_admin_by_id(admin_id)
    # A maintainer answers exactly as a deleted admin would. Distinguishing the two would
    # turn the panel into an oracle for which id is the hidden bootstrap account — the
    # concealment in data/config.py exists precisely so other admins can't find it.
    if target is None or (is_maintainer(target["telegram_id"])
                          and not is_maintainer(request["telegram_id"])):
        return None, _fail("not_found", "Admin not found.", status=404)
    return target, None


@require_super
async def create_admin(request: web.Request) -> web.Response:
    try:
        telegram_id = int((await _body(request)).get("telegram_id"))
    except (TypeError, ValueError):
        return _fail("bad_request", "Enter a numeric Telegram ID.")

    # Adding a hidden maintainer reports success without doing anything, mirroring
    # _finish_add: a distinguishable "already an admin" here would leak the same secret
    # _load_target_admin protects.
    if is_maintainer(telegram_id) and not is_maintainer(request["telegram_id"]):
        return _ok({"ok": True})

    # Somebody who already is an admin has nothing to be added to. Answering "added" anyway
    # left the list unchanged and the person at the panel with no way to tell a failed add
    # from a successful one -- four identical "Admin added" toasts and a list that never
    # grew. An inactive row is different: adding them again switches them back on.
    existing = next((a for a in await get_all_admins()
                     if a["telegram_id"] == telegram_id), None)
    if existing and existing["is_active"]:
        return _fail("already_admin",
                     "That person is already an admin, so there is nothing to add.",
                     status=409)

    # admins.telegram_id is a foreign key into users, so the row has to exist first.
    await ensure_user(telegram_id)
    await add_admin(telegram_id, added_by=request["telegram_id"])
    logger.info(f"[webapp] {request['telegram_id']} added admin {telegram_id}")
    return _ok({"ok": True})


@require_super
async def update_admin(request: web.Request) -> web.Response:
    target, err = await _load_target_admin(request)
    if err is not None:
        return err

    body = await _body(request)
    actor = request["telegram_id"]
    # Promotion is a super admin's to give; taking it back is not. A company's own super
    # admins are peers -- letting one strip another is a coin-flip over who clicks first,
    # and the loser has no way back. The deployment's account (config.ADMINS) is not a
    # peer: it is the only one that can be locked out of nothing, because is_super_admin
    # derives its answer from config rather than from the row.
    owner = is_maintainer(actor)

    if "is_active" in body:
        active = bool(body["is_active"])
        # A guard the bot's flow can't easily reach but a panel can: is_admin requires
        # is_active, so an admin who deactivates their own row is locked out of both
        # surfaces with no way back that isn't a hand-written SQL statement.
        if target["telegram_id"] == actor and not active:
            return _fail("self_action", "You can't deactivate yourself.", status=403)
        if is_maintainer(target["telegram_id"]):
            return _fail("is_owner", _OWNER_ROW, status=403)
        if target["is_super"] and not owner:
            return _fail("is_super", "Only the deployment's own account can deactivate "
                                     "a super admin.", status=403)
        await set_admin_active(target["id"], active)
        logger.info(f"[webapp] {actor} set admin {target['id']} active={active}")
        return _ok({"ok": True, "is_active": active})

    if "is_super" in body:
        if body["is_super"]:
            if target["is_super"]:
                return _fail("already_super", "That admin is already a super admin.")
            if not target["is_active"]:
                return _fail("inactive", "Activate this admin before promoting them.")
            await promote_to_super(target["id"])
            logger.info(f"[webapp] {actor} promoted admin {target['id']} to super")
            return _ok({"ok": True, "is_super": True})

        if not target["is_super"]:
            return _fail("not_super", "That admin isn't a super admin.")
        if target["telegram_id"] == actor:
            # Not a safety rail that can be argued with: for the deployment's account the
            # write wouldn't even hold -- is_super_admin reads config.ADMINS and
            # seed_super_admins puts the column back on the next restart.
            return _fail("self_action", "You can't take away your own super-admin role.",
                         status=403)
        if is_maintainer(target["telegram_id"]):
            return _fail("is_owner", _OWNER_ROW, status=403)
        if not owner:
            return _fail("is_super", "Only the deployment's own account can take the "
                                     "super-admin role back.", status=403)
        await demote_from_super(target["id"])
        logger.info(f"[webapp] {actor} demoted admin {target['id']} to plain admin")
        return _ok({"ok": True, "is_super": False})

    return _fail("bad_request", "Nothing to change.")


@require_admin
async def set_notification(request: web.Request) -> web.Response:
    """Flip one of the caller's own DM switches. Nobody can change anybody else's: the
    row written is request["telegram_id"], never an id from the body."""
    body = await _body(request)
    group = _subscription_group(str(body.get("type", "")))
    if not group:
        return _fail("unknown_type", "That isn't an event type this bot reports.")

    on = bool(body.get("on"))
    for event_type in group:
        await set_subscription(request["telegram_id"], event_type, on)
    return _ok({"ok": True, "on": on})


@require_admin
async def send_report(request: web.Request) -> web.Response:
    """Build the full report and send it to the caller's own chat as a document.

    Not a download. A Mini App WebView has nowhere to put a file -- and the person
    asking for it is, by definition, sitting in a Telegram chat with this bot, where the
    file is searchable and forwardable afterwards. It goes to request["telegram_id"] and
    to no other chat: an id in the body would make this an endpoint for mailing the
    company's violations to a stranger.
    """
    body = await _body(request)
    period = body.get("period", "last_week")
    if period not in PERIOD_LABELS:
        return _fail("bad_period", "That isn't a report period.")

    telegram_id = request["telegram_id"]
    content = await build_report_file(
        period, config.COMPANY_NAME,
        show_severity=await is_super_admin(telegram_id),
    )

    from aiogram.types import InputFile
    try:
        await bot.send_document(
            telegram_id,
            InputFile(io.BytesIO(content),
                      filename=report_filename(config.COMPANY_SLUG, period)),
            caption=f"\U0001f4ca {config.COMPANY_NAME} \u2014 {period_label(period)}",
        )
    except Exception as e:
        # Almost always the same cause: the admin has never pressed /start, so the bot
        # cannot open a chat with them. Say the thing they can act on.
        logger.warning(f"[webapp] report to {telegram_id} failed: {e}")
        return _fail("send_failed",
                     "Couldn't send the file to your chat. Open the bot and press "
                     "/start, then try again.", status=502)
    logger.info(f"[webapp] sent {period} report to {telegram_id}")
    return _ok({"ok": True})


@require_super
async def delete_admin_route(request: web.Request) -> web.Response:
    target, err = await _load_target_admin(request)
    if err is not None:
        return err
    if not (await _body(request)).get("confirm"):
        return _fail("confirm_required", "This needs to be confirmed.")
    if target["telegram_id"] == request["telegram_id"]:
        return _fail("self_action", "You can't remove yourself.", status=403)
    if is_maintainer(target["telegram_id"]):
        return _fail("is_owner", _OWNER_ROW, status=403)
    if target["is_super"] and not is_maintainer(request["telegram_id"]):
        return _fail("is_super", "Only the deployment's own account can remove a super "
                                 "admin.", status=403)
    await delete_admin(target["id"])
    logger.info(f"[webapp] {request['telegram_id']} removed admin {target['id']}")
    return _ok({"ok": True})
