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
the server still asks Samsara.
"""

import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from aiohttp import web

from data import config
from data.event_catalog import GROUP_FILTER_TYPES, next_event_filter
from utils.db_api.admins import (
    add_admin, delete_admin, get_admin_by_id, get_all_admins, is_maintainer,
    is_super_admin, promote_to_super, set_admin_active, visible_admins,
)
from utils.db_api.groups import (
    get_group, get_group_event_types, get_groups_overview, remove_group,
    set_group_enabled, set_group_event_types, set_group_unit,
)
from utils.db_api.users import ensure_user
from utils.db_api.violations import (
    get_counts_by_vehicle, get_daily_counts, get_recent_events, get_totals,
    get_top_violators, get_type_counts,
)
from utils.samsara.client import list_units, suggest_units
from utils.units import resolve_unit
from utils.webapp.auth import require_admin, require_super

logger = logging.getLogger(__name__)

# Every timestamp the panel shows is Eastern, because every timestamp the bot shows is
# (utils/daily_report.py, handlers/users/violations.py, the alert cards themselves). A
# panel on a different clock than the group messages it describes is worse than a panel
# on the wrong clock.
ET = ZoneInfo("America/New_York")


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


def _period_range(period: str) -> tuple[datetime, datetime, str]:
    """(since, until, label) for the dashboard's three windows, in Eastern time.

    Deliberately not the same set as the DM report's today/last_week/last_month
    (handlers/users/violations.py): "last week" on a Monday morning is not the question a
    dispatcher opening a live panel is asking. Rolling windows are.
    """
    now = datetime.now(tz=ET)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "30d":
        return midnight - timedelta(days=29), now, "Last 30 days"
    if period == "7d":
        return midnight - timedelta(days=6), now, "Last 7 days"
    return midnight, now, "Today"


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
        },
        "company": {"name": config.COMPANY_NAME, "slug": config.COMPANY_SLUG},
        "event_types": [
            {"type": types[0], "emoji": emoji, "label": label}
            for types, emoji, label in GROUP_FILTER_TYPES
        ],
        "main_group_id": config.MAIN_GROUP_ID,
        "crash_group_id": config.CRASH_GROUP_ID,
        "samsara_enabled": bool(config.SAMSARA_API_KEY),
    })


@require_admin
async def stats(request: web.Request) -> web.Response:
    since, until, label = _period_range(request.query.get("period", "today"))
    totals = await get_totals(since, until)
    by_type = await get_type_counts(since, until)
    top_units = await get_top_violators(since=since, until=until, limit=5)
    by_day = await get_daily_counts(since, until) if since.date() != until.date() else []

    groups = await get_groups_overview()
    muted = [g for g in groups if not g["enabled"]]

    return _ok({
        "period": {"key": request.query.get("period", "today"), "label": label},
        "totals": dict(totals),
        "by_type": [
            {**row, "emoji": _event_label(row["event_type"])[0],
             "label": _event_label(row["event_type"])[1]}
            for row in by_type
        ],
        "by_day": by_day,
        "top_units": top_units,
        "groups": {"total": len(groups), "muted": len(muted),
                   "muted_titles": [g["title"] or str(g["telegram_group_id"]) for g in muted[:5]]},
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
            "is_main": unit is None,
            # Zero rows in group_event_types means "every type", not "no types". Naming
            # that here keeps the UI from rendering an empty list as "receives nothing".
            "filter_mode": "all" if not row["event_types"] else "custom",
            "alerts_7d": counts.get(unit, 0) if unit else sum(counts.values()),
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

    rows = await get_recent_events(
        limit=limit, before_ts=before_ts, before_id=before_id,
        event_type=request.query.get("type") or None,
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
    """The Samsara roster, so changing a unit is a pick rather than a guess.

    Degrades to an empty list with a reason rather than failing the screen: a Motive-only
    fleet has no roster at all, and a Samsara outage must not make the group detail
    unopenable.
    """
    if not config.SAMSARA_API_KEY:
        return _ok({"units": [], "available": False,
                    "reason": "This deployment has no Samsara fleet."})
    try:
        names = await list_units(config.SAMSARA_API_KEY)
    except Exception as e:
        logger.warning(f"[webapp] roster unavailable: {e}")
        return _ok({"units": [], "available": False,
                    "reason": "Samsara couldn't be reached just now."})

    taken = {g["vehicle_number"] for g in await get_groups_overview() if g["vehicle_number"]}
    return _ok({
        "units": [{"name": n, "linked": n in taken} for n in names],
        "available": True,
    })


@require_admin
async def admins(request: web.Request) -> web.Response:
    """Maintainers are filtered out server-side. The panel must not become the place that
    leaks what the bot deliberately hides (see data/config.py on HIDDEN_ADMIN_IDS)."""
    everyone = await get_all_admins()
    return _ok(visible_admins(everyone, request["telegram_id"]))


# ── group mutations ─────────────────────────────────────────────────────────────

async def _load_editable_group(request: web.Request) -> tuple[dict | None, web.Response | None]:
    """Resolve the {tgid} path param to a group the panel may edit, or an error."""
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
    if err:
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
    unit that Samsara can't confirm is refused rather than stored, because a stored guess
    produces a group that looks configured and silently receives nothing — see
    utils/units.py for the whole argument.
    """
    group, err = await _load_editable_group(request)
    if err:
        return err

    unit = str((await _body(request)).get("unit", "")).strip().lstrip("#").strip()
    # Same input rules as /setunit, including the 50-char cap that matches the column.
    if not unit or not any(c.isdigit() for c in unit) or len(unit) > 50:
        return _fail("bad_unit", "Enter a unit number, e.g. 1234.")

    status, resolved = await resolve_unit(unit)

    if status == "missing":
        return _fail("unit_not_found", f"No unit “{unit}” in Samsara.", status=409,
                     suggestions=await suggest_units(config.SAMSARA_API_KEY, unit))
    if status == "unavailable":
        return _fail(
            "roster_unavailable",
            "Samsara couldn't be reached, so the unit wasn't saved — the spelling has to "
            "match the roster exactly or this group would receive nothing. Try again in a "
            "few minutes.",
            status=503,
        )

    await set_group_unit(group["telegram_group_id"], resolved)
    logger.info(f"[webapp] {request['telegram_id']} set group "
                f"{group['telegram_group_id']} to unit {resolved}")
    return _ok({
        "ok": True,
        "vehicle_number": resolved,
        # Surfaced so the dispatcher sees that "571" became "unit571" rather than
        # wondering why the field doesn't show what they typed.
        "note": f"Matched Samsara's “{resolved}”." if resolved != unit else None,
        "unverified": status == "no_roster",
    })


@require_admin
async def toggle_event(request: web.Request) -> web.Response:
    """Flip one event type in a group's filter, or reset it to "all".

    The toggle rule lives in next_event_filter and stays there. Batching the changes in
    the browser and saving once would mean reimplementing its collapse rule — "an
    allowlist covering every type becomes the empty all-types state" — in JavaScript,
    and when the two drifted, a group would quietly stop receiving newly added event
    types with nothing in the UI to show for it. So each tap is a round trip and the
    response carries the authoritative list the client re-renders from.
    """
    group, err = await _load_editable_group(request)
    if err:
        return err

    body = await _body(request)
    tgid = group["telegram_group_id"]

    if body.get("action") == "all":
        await set_group_event_types(tgid, set())
        selected: list[str] = []
    else:
        event_type = body.get("event_type")
        if event_type not in {types[0] for types, _, _ in GROUP_FILTER_TYPES}:
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
    if err:
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

    # admins.telegram_id is a foreign key into users, so the row has to exist first.
    await ensure_user(telegram_id)
    await add_admin(telegram_id, added_by=request["telegram_id"])
    logger.info(f"[webapp] {request['telegram_id']} added admin {telegram_id}")
    return _ok({"ok": True})


@require_super
async def update_admin(request: web.Request) -> web.Response:
    target, err = await _load_target_admin(request)
    if err:
        return err

    body = await _body(request)
    actor = request["telegram_id"]

    if "is_active" in body:
        active = bool(body["is_active"])
        if target["is_super"]:
            return _fail("is_super", "Super admins can't be deactivated here.", status=403)
        # A guard the bot's flow can't easily reach but a panel can: is_admin requires
        # is_active, so a super admin who deactivates their own row is locked out of both
        # surfaces with no way back that isn't a hand-written SQL statement.
        if target["telegram_id"] == actor and not active:
            return _fail("self_action", "You can't deactivate yourself.", status=403)
        await set_admin_active(target["id"], active)
        logger.info(f"[webapp] {actor} set admin {target['id']} active={active}")
        return _ok({"ok": True, "is_active": active})

    if body.get("is_super"):
        if target["is_super"]:
            return _fail("already_super", "That admin is already a super admin.")
        if not target["is_active"]:
            return _fail("inactive", "Activate this admin before promoting them.")
        await promote_to_super(target["id"])
        logger.info(f"[webapp] {actor} promoted admin {target['id']} to super")
        return _ok({"ok": True, "is_super": True})

    return _fail("bad_request", "Nothing to change.")


@require_super
async def delete_admin_route(request: web.Request) -> web.Response:
    target, err = await _load_target_admin(request)
    if err:
        return err
    if not (await _body(request)).get("confirm"):
        return _fail("confirm_required", "This needs to be confirmed.")
    if target["telegram_id"] == request["telegram_id"]:
        return _fail("self_action", "You can't remove yourself.", status=403)
    if target["is_super"]:
        return _fail("is_super", "Super admins can't be removed here.", status=403)
    await delete_admin(target["id"])
    logger.info(f"[webapp] {request['telegram_id']} removed admin {target['id']}")
    return _ok({"ok": True})
