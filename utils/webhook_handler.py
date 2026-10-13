import asyncio
import hashlib
import hmac
import io
import json
import logging
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiogram import Bot
from aiogram.types import InputFile, InputMediaVideo, InputMediaPhoto
from aiogram.utils.exceptions import (
    NetworkError, TelegramAPIError, RetryAfter, MigrateToChat,
    BotKicked, BotBlocked, ChatNotFound, UserDeactivated, CantInitiateConversation,
)

from data import config
from utils.db_api.groups import get_alert_target, migrate_group
from utils.db_api.violations import save_violation
from utils.db_api.admins import get_subscribed_admins
from utils.db_api.crash_confirmations import (
    get_pending_confirmations,
    get_verdict_counts,
    record_pending,
    record_verdict,
    VERDICT_CONFIRMED,
    VERDICT_EXPIRED,
    VERDICT_UNKNOWN,
    VERDICT_WITHDRAWN,
)
from utils.motive import crash_still_listed, find_performance_event
from utils.samsara.client import fetch_speeding_details
from utils.tg_text import esc

logger = logging.getLogger(__name__)

_download_timeout = aiohttp.ClientTimeout(total=300)
_samsara_poll_timeout = aiohttp.ClientTimeout(total=30)

# Single shared aiohttp session reused for every outbound request (media downloads +
# Samsara polls). Opening a ClientSession per request also stands up a fresh
# connection pool each time and can't reuse keep-alive connections; one long-lived
# session avoids that. Timeouts are passed per-request rather than on the session.
# Created lazily inside the running loop; closed on server shutdown.
_http_session: aiohttp.ClientSession | None = None


def _get_http_session() -> aiohttp.ClientSession:
    """Return the shared session, creating it on first use (or if it was closed)."""
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def _close_http_session(*_a) -> None:
    """Close the shared session. Wired into the aiohttp app's on_cleanup."""
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
    _http_session = None


# In-memory deduplication: event_id -> processed monotonic timestamp. Samsara (and
# batched Motive) can redeliver, so a short TTL window suppresses immediate repeats;
# the violations.event_id UNIQUE constraint is the durable backstop. An OrderedDict
# keeps entries in first-seen order, so expired ones cluster at the oldest end and a
# single front-eviction pass is amortized O(1) per webhook (vs. scanning every key).
_seen_event_ids: "OrderedDict[str, float]" = OrderedDict()
_DEDUP_TTL = 300  # seconds


def _is_duplicate(event_id: str) -> bool:
    """True if this event_id was processed within the last _DEDUP_TTL seconds."""
    if not event_id:
        return False
    now = time.monotonic()
    # Evict expired entries from the oldest end. Insertion order == time order, so we
    # can stop at the first entry still inside the window.
    while _seen_event_ids:
        oldest = next(iter(_seen_event_ids))
        if now - _seen_event_ids[oldest] > _DEDUP_TTL:
            del _seen_event_ids[oldest]
        else:
            break
    if event_id in _seen_event_ids:
        return True
    _seen_event_ids[event_id] = now
    return False


def _event_id_to_bigint(raw) -> int | None:
    """Map a provider event id onto the violations.event_id BIGINT UNIQUE column.

    Motive ids are numeric and pass through unchanged. Samsara ids are UUID strings
    which won't fit a BIGINT, so without this they'd be stored as NULL — and NULLs
    never conflict, so ON CONFLICT (event_id) DO NOTHING gives Samsara no persistent
    dedup. Hashing the string into a stable signed 64-bit int makes the UNIQUE
    constraint work for Samsara too, surviving restarts."""
    s = str(raw or "")
    if not s:
        return None
    if s.isdigit():
        return int(s)
    digest = hashlib.blake2b(s.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def _download(url: str) -> bytes | None:
    try:
        session = _get_http_session()
        async with session.get(url, timeout=_download_timeout) as r:
            if r.status == 200:
                return await r.read()
            logger.error(f"Download failed HTTP {r.status}: {url}")
    except Exception as e:
        logger.error(f"Download error: {e}")
    return None


SEVERITY_EMOJI = {
    "low": "🟢",
    "medium": "🟡",
    "high": "🔴",
    "critical": "🆘",
    # Samsara speeding-interval severity levels (used when the interval supplies
    # the severity because the webhook payload carried none)
    "light": "🟢",
    "moderate": "🟡",
    "heavy": "🔴",
    "severe": "🆘",
}

# Event type → (emoji, display title). This is MZ's list, and it is shorter than
# single-company's on purpose: a type that is not here is not alerted on at all (see
# ALLOWED_TYPES below), and MZ does not want camera obstructions or tailgating in its
# group. The Samsara alias table further down still names those types — that is how an
# event of one of them is recognised and dropped, rather than reported as a generic
# harsh event.
#
# The titles are the ones MZ's group has always been shown. hf, which this branch
# follows, words two of them the way Samsara does ("HARSH BRAKE", "ROLLING STOP"); MZ's
# fleet is half Motive, whose names for the same things are these.
EVENT_TYPE_MAP = {
    "hard_brake":                   ("🛑", "HARD BRAKE"),
    "crash":                        ("💥", "CRASH DETECTED"),
    "cell_phone":                   ("📵", "CELL PHONE USAGE"),
    "stop_sign_violation":          ("🛑", "STOP SIGN VIOLATION"),
    "forward_collision_warning":    ("⚠️", "FORWARD COLLISION WARNING"),
    "unsafe_parking":               ("🅿️", "UNSAFE PARKING"),
    "speeding":                     ("🚨", "SPEEDING OVER POSTED"),
    "harsh_event":                  ("⚠️", "HARSH EVENT"),
    "inattentive_driving":          ("😵", "INATTENTIVE DRIVING"),
    "drowsy_driving":               ("😴", "POSSIBLE DROWSINESS"),
    "harsh_acceleration":           ("🚀", "HARSH ACCELERATION"),
    "harsh_turn":                   ("↩️", "HARSH TURN"),
    "hard_cornering":               ("↩️", "HARSH TURN"),
    "no_seat_belt":                 ("🚫", "NO SEAT BELT"),
}

# Only process these event types — everything else is ignored
ALLOWED_TYPES = set(EVENT_TYPE_MAP.keys())

# Motive speeding webhook uses "action" field with these values
SPEEDING_ACTIONS = {"speeding_event_created", "speeding_event_updated"}

# Severity ordering for threshold filtering
SEVERITY_ORDER = ["low", "medium", "high", "critical"]

# Samsara legacy harsh-event API harshEventType string → internal event type
_SAMSARA_HARSH_TYPE_MAP: dict[str, str] = {
    "Harsh Braking":       "hard_brake",
    "Harsh Brake":         "hard_brake",
    "Harsh Acceleration":  "harsh_acceleration",
    "Crash":               "crash",
    "Mobile Usage":        "cell_phone",
    "Inattentive Driving": "inattentive_driving",
    "Inattentive":         "inattentive_driving",
    "Drowsy Driving":      "drowsy_driving",
    "Drowsy":              "drowsy_driving",
    "Obstructed Camera":   "obstructed_camera",
    "No Seat Belt":        "no_seat_belt",
    "Generic Distraction": "inattentive_driving",
    "Tailgating":          "tailgating",
    "Harsh Turn":          "harsh_turn",
    "Rolling Stop":        "stop_sign_violation",
    "Did Not Stop":        "stop_sign_violation",
    "Near Collision":      "forward_collision_warning",
}

# Captured by the inward (driver-facing) camera only — there is never a forward clip,
# so the poll can return as soon as the inward URL is ready rather than waiting the
# full window for a forward URL that never arrives.
_INWARD_ONLY_TYPES: frozenset[str] = frozenset(
    {"cell_phone", "drowsy_driving", "no_seat_belt", "inattentive_driving"}
)

# Samsara SpeedingEventStarted severityLevel → our severity
_SAMSARA_SPEED_SEV: dict[str, str] = {
    "Light":    "low",
    "Moderate": "medium",
    "Heavy":    "high",
    "Severe":   "critical",
}


def _kph_to_mph(kph: float) -> float:
    return kph * 0.621371


def _to_et(utc_iso: str) -> str:
    try:
        dt = datetime.fromisoformat(utc_iso.replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%b %d, %I:%M %p %Z")
    except Exception:
        return utc_iso


def _get_event_type(event: dict) -> str:
    """Determine canonical event type from action or type field.

    A hard brake is a hard brake here, whatever its severity. hf and single-company
    read a Motive hard_brake of critical severity as a crash and put it through the
    confirmation wait; MZ never has, and on this branch that reading is left out. MZ is
    the one fleet actually receiving Motive's deliveries, Motive files its crashes under
    type `crash`, and a critical hard brake promoted to one would spend three minutes
    held back only to be found absent from the crash listing and sent as the hard brake
    it was — or, with Motive's API unreachable, be announced as an unconfirmed CRASH.
    """
    # Set when Motive's API says a crash detection was withdrawn (see
    # _motive_crash_is_real). Checked first so the downgrade sticks everywhere the type
    # is re-derived — _format_event does that internally.
    override = event.get("_type_override")
    if override:
        return override
    action = (event.get("action") or "").lower()
    if action in SPEEDING_ACTIONS:
        return "speeding"
    return (event.get("type") or "").lower()


def _get_vehicle(event: dict) -> str:
    """Extract vehicle number from any event payload structure."""
    # current_vehicle present in both speeding (capital keys) and driver_performance (lowercase)
    current = event.get("current_vehicle") or {}
    if current.get("Number") or current.get("number"):
        return current.get("Number") or current.get("number")

    vehicle_info = event.get("vehicle") or {}
    return (
        vehicle_info.get("number")
        or vehicle_info.get("truck_number")
        or event.get("vehicle_number")
        or event.get("truck_number")
        or str(event.get("vehicle_id") or "")
        or "—"
    )


_UNIT_PREFIX_RE = re.compile(r'^unit[\s:#-]+', re.IGNORECASE)


def _clean_vehicle(event: dict) -> str:
    """The vehicle as it is stored and counted: without a leading "unit" label, and no
    longer than the column.

    Samsara's roster spells the same truck "1196", "Unit 1196" and "unit #1196" depending
    on who typed it in, and a report groups by this string — so the label has to come off
    before the row is written or one truck shows up as three.
    """
    v = _get_vehicle(event).strip()
    return _UNIT_PREFIX_RE.sub('', v).strip()[:50]


def _get_unit_num(event: dict) -> str:
    """Just the unit's number, for a log line."""
    v = _clean_vehicle(event)
    return v.split()[0] if v else "?"


def _vehicle_markup(event: dict) -> str:
    """The vehicle as an alert card shows it.

    The number and whatever follows it are separate <code> spans, so a tap copies the
    number alone — which is what gets pasted into Samsara — instead of the number with
    the driver's name trailing behind it.
    """
    v = _clean_vehicle(event)
    # _get_vehicle answers "—" for a payload that names no vehicle; that is a
    # placeholder, not a unit number, and there is nothing in it to copy.
    if not v or v == "—":
        return "—"
    num, _, rest = v.partition(" ")
    if rest:
        return f"<code>{esc(num)}</code> <code>{esc(rest)}</code>"
    return f"<code>{esc(num)}</code>"


def _format_event(
    event: dict,
    company_name: str = "",
    samsara: dict | None = None,
    *,
    notes: list[str] | None = None,
) -> str:
    """Render one alert card.

    The title on its own line, then the unit, who and when, then what the event
    measured, and under a blank line which provider reported it. This is MZ's layout,
    kept as its group has always seen it, and it differs from hf's in three places:

      * the unit stands on a line of its own with no "Vehicle:" label in front of it;
      * the card is signed "via Samsara" or "via Motive" — MZ runs both, so which of
        them saw the event is something the reader cannot work out for themselves;
      * the company's name is not printed, on a crash or on anything else. Every card
        in this group is this company's. `company_name` is still accepted, so the
        callers shared with hf need no changes, and is not used.

    `notes` are lines for the very end — "video pending", "no camera media" — set off
    from the signature by one blank line however many there are.
    """
    event_type = _get_event_type(event)
    emoji, title = EVENT_TYPE_MAP.get(event_type, ("🚨", event_type.upper().replace("_", " ")))

    samsara = samsara or {}
    driver_info = event.get("driver") or event.get("current_driver") or {}
    driver = (
        driver_info.get("name")
        or f"{driver_info.get('first_name', '') or driver_info.get('FirstName', '')} "
           f"{driver_info.get('last_name', '') or driver_info.get('LastName', '')}".strip()
        or driver_info.get("username")
        or samsara.get("driver_name")
        or ""
    )

    start_time = _to_et(event.get("start_time", ""))
    location = event.get("location", "")
    intensity = event.get("intensity", "")
    duration = event.get("duration")

    # Severity: prefer metadata.severity, then direct severity field; ignore coaching_status
    meta_sev = ((event.get("metadata") or {}).get("severity") or "").strip()
    sev_display = meta_sev or (event.get("severity") or "").strip() or (samsara.get("severity") or "").strip()

    # Everything below is escaped as it goes in: a driver named "A & B" or an address
    # with a "<" in it would otherwise make Telegram reject the whole card, and a
    # rejected alert is a dropped alert.
    lines = [f"{emoji} <b>{title}</b>\n"]
    lines.append(_vehicle_markup(event))
    # No line at all for an unidentified driver. Samsara's webhooks never name one, so
    # "Driver: Unidentified" would be on nearly every card and say nothing on any.
    if driver:
        lines.append(f"👤 <b>Driver:</b> {esc(driver)}")
    if sev_display:
        sev_emoji = SEVERITY_EMOJI.get(sev_display.lower(), "⚠️")
        lines.append(f"📊 <b>Severity:</b> {sev_emoji} {esc(sev_display.title())}")
    lines.append(f"🕐 <b>Time:</b> {esc(start_time)}")

    if event_type == "speeding":
        avg = event.get("avg_vehicle_speed")
        limit = event.get("min_posted_speed_limit_in_kph")
        over = event.get("max_over_speed_in_kph")
        if avg:
            lines.append(f"💨 <b>Average Speed:</b> {_kph_to_mph(avg):.1f} mph")
        if limit:
            lines.append(f"🚦 <b>Speed Limit:</b> {_kph_to_mph(limit):.1f} mph")
        if over:
            lines.append(f"📈 <b>Max Over Posted:</b> {_kph_to_mph(over):.1f} mph")
        if duration:
            lines.append(f"⏱ <b>Duration:</b> {duration}s")
        # Samsara enrichment — fetched only when the webhook payload had no speed data
        max_mph = samsara.get("max_speed_mph")
        limit_mph = samsara.get("posted_limit_mph")
        if max_mph:
            lines.append(f"💨 <b>Max Speed:</b> {max_mph:.1f} mph")
        if limit_mph:
            lines.append(f"🚦 <b>Speed Limit:</b> {limit_mph:.1f} mph")
        if max_mph and limit_mph:
            lines.append(f"📈 <b>Over Posted:</b> {max_mph - limit_mph:.1f} mph")
        if not duration and samsara.get("duration_seconds"):
            lines.append(f"⏱ <b>Duration:</b> {samsara['duration_seconds']}s")
        nominatim = event.get("nominatim_location", "") or samsara.get("location", "")
        if nominatim:
            lines.append(f"📍 <b>Location:</b> {esc(nominatim)}")
    else:
        if location:
            lines.append(f"📍 <b>Location:</b> {esc(location)}")
        if event_type == "hard_brake" and intensity:
            lines.append(f"💥 <b>Intensity:</b> {esc(intensity)}")
        if duration:
            lines.append(f"⏱ <b>Duration:</b> {duration}s")

    # Say so when a crash alert went out without Motive's confirmation, so an
    # unverified crash is never mistaken for a verified one.
    if event_type == "crash" and event.get("_crash_unconfirmed"):
        lines.append("")
        lines.append("⚠️ <i>Unconfirmed — Motive's review could not be reached.</i>")

    source = "Samsara" if event.get("_source") == "samsara" else "Motive"
    lines.append("")
    lines.append(f"<i>via {source}</i>")

    if notes:
        lines.append("")
        lines.extend(notes)

    return "\n".join(lines)


def _get_camera_media_info(event: dict) -> tuple[list[str], list[str]]:
    """Returns (video_urls, image_urls) from camera_media."""
    camera_media = event.get("camera_media") or {}
    if not camera_media.get("available"):
        return [], []
    dl = camera_media.get("downloadable_videos") or {}
    video_urls = [u for u in [dl.get("front_facing_plain_url"), dl.get("driver_facing_plain_url")] if u]
    imgs = camera_media.get("downloadable_images") or {}
    image_urls = [u for u in [imgs.get("front_facing_jpg_url"), imgs.get("driver_facing_jpg_url")] if u]
    return video_urls, image_urls


def _parse_occurred(event: dict) -> datetime:
    """Best-effort UTC occurrence time; falls back to now() on a bad/absent timestamp.
    Shared so the early (first-poll) save and any later save agree on the row."""
    s = event.get("start_time") or event.get("created_at") or ""
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def _event_severity(event: dict) -> str | None:
    """Normalized severity (metadata.severity → severity), lowercased, or None."""
    meta_sev = ((event.get("metadata") or {}).get("severity") or "").strip().lower()
    return meta_sev or (event.get("severity") or "").strip().lower() or None


async def _persist_once(event: dict, event_type: str) -> bool:
    """Store the violation and say whether it is the first sighting of this event.

    True means "new, go ahead and alert". False means the provider has sent this
    event_id before — a redelivery, a retry after a slow 200, or Motive's
    speeding_event_updated following its _created twin — and the alert already went out.
    Stopping here is what keeps the UNIQUE constraint from being merely a table
    protection: without it the row is silently skipped and the message sent anyway.

    A database error answers True: the alert is the product and the row is bookkeeping,
    so an outage may cost a duplicate but must never cost the alert itself.
    """
    try:
        return await save_violation(
            vehicle_number=_clean_vehicle(event),
            event_type=event_type,
            event_id=_event_id_to_bigint(event.get("id")),
            occurred_at=_parse_occurred(event),
            severity=_event_severity(event),
        )
    except Exception as e:
        logger.error(f"Could not persist violation id={event.get('id')}: {e} — alerting anyway")
        return True


# Strong references to the fire-and-forget tasks started below. asyncio keeps only a
# weak one, so a task nobody holds can be garbage-collected mid-await — and these are
# long-lived: a Samsara media poll runs for minutes and a Motive crash confirmation
# sleeps three of them.
_background_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """Run `coro` in the background, keeping a reference until it finishes."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _format_crash_initial(event: dict, company_name: str = "") -> str:
    """First crash alert: the FULL details, sent the instant the crash is detected —
    before the video uploads. Everyone (groups and DMs) gets this, so the complete
    record is delivered even when no video ever resolves."""
    return _format_event(event, company_name, notes=["📹 <i>Video pending…</i>"])


def _format_crash_video_caption(event: dict) -> str:
    """Short caption for the crash video follow-up — the full details already went out
    in the first alert, so this just labels the clip."""
    return f"💥 <b>CRASH</b> — {_vehicle_markup(event)}"


# ── Samsara webhook auth ───────────────────────────────────────────────────────

def _verify_hmac(secret: str, body: bytes, provided: str, digestmod=hashlib.sha256) -> bool:
    """Constant-time HMAC verification. Tries raw string key and base64-decoded key,
    hex and base64 output. digestmod selects the hash: Samsara signs with SHA256,
    Motive (née KeepTruckin) with SHA1."""
    import base64

    if not provided:
        return False

    def _check(key: bytes) -> bool:
        mac = hmac.new(key, body, digestmod)
        return (
            hmac.compare_digest(mac.hexdigest(), provided)
            or hmac.compare_digest(base64.b64encode(mac.digest()).decode(), provided)
        )

    if _check(secret.encode()):
        return True
    try:
        return _check(base64.b64decode(secret))
    except Exception:
        return False


def _unsigned_allowed(provider: str, remote) -> bool:
    """Whether to process a delivery that arrived with no secret configured to check it.

    Refusing is the default: the endpoint is public, so without a signature anyone who
    finds the URL can post a crash straight into the admins' chats. A deployment that
    truly cannot sign sets ALLOW_UNSIGNED_WEBHOOKS and gets a warning per request
    instead of a refusal.
    """
    if config.ALLOW_UNSIGNED_WEBHOOKS:
        logger.warning(f"[{provider}] No webhook secret configured — accepting unsigned "
                       f"delivery from {remote} because ALLOW_UNSIGNED_WEBHOOKS is set")
        return True
    logger.error(f"[{provider}] Refused a delivery from {remote}: no webhook secret is "
                 f"configured, so its signature cannot be checked. Set "
                 f"{provider.upper()}_WEBHOOK_SECRET, or ALLOW_UNSIGNED_WEBHOOKS=true "
                 f"to accept unsigned deliveries.")
    return False


def _timestamp_is_fresh(raw: str, max_skew: int = 300) -> bool:
    """Is this delivery's own timestamp close enough to now to not be a replay?

    Samsara signs the timestamp along with the body, so an attacker cannot adjust it —
    which makes checking it the whole defence against a captured delivery being sent
    again later. Seconds and milliseconds are both accepted because the header's unit is
    not worth a production surprise: anything far too large for seconds is milliseconds.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return False
    if value > 1e11:  # milliseconds
        value /= 1000.0
    return abs(time.time() - value) <= max_skew


def _samsara_signed_payload(timestamp: str, body: bytes) -> bytes:
    """The exact message Samsara signs for its v1 webhook signature: the literal
    'v1:', the X-Samsara-Timestamp header, and the raw request body, colon-joined."""
    return b"v1:" + timestamp.encode() + b":" + body


async def _fetch_samsara_harsh_event(vehicle_id: str, timestamp_ms: int, api_key: str,
                                     on_first=None) -> dict | None:
    """Poll the legacy harsh-event API waiting for video URLs. Every attempt waits 20s first.

    Standard harsh events: up to 3 attempts (~60s).
    Crashes: once detected, the window extends to 15 attempts (~5 min) with no early
    bail — crash clips are large and take a few minutes to upload.
    Inward-only types short-circuit as soon as the inward clip is ready.
    A type this deployment does not alert on ends the poll at once, with None: there is
    no card to attach a clip to, so nothing is gained by waiting for one.

    `api_key` is the company's own Samsara bearer token (each company is a separate
    Samsara org). `on_first`, if given, is an async callback awaited once with the
    first response that carries a real harshEventType (type known, before media is
    ready) — the caller uses it to persist the violation and fire a 'pending' alert,
    so the event survives a mid-poll restart and crashes notify immediately."""
    url = f"https://api.samsara.com/v1/fleet/vehicles/{vehicle_id}/safety/harsh_event"
    headers = {"Authorization": f"Bearer {api_key}"}
    last_data = None
    ever_got_url = False
    is_crash = False
    notified = False
    max_attempts = 3  # bumped to 15 once a Crash is detected
    attempt = 0
    session = _get_http_session()
    while attempt < max_attempts:
        attempt += 1
        await asyncio.sleep(20)
        try:
            async with session.get(url, headers=headers, params={"timestamp": timestamp_ms},
                                   timeout=_samsara_poll_timeout) as r:
                if r.status != 200:
                    logger.error(f"[samsara] harsh_event API HTTP {r.status} on attempt {attempt} — giving up")
                    return None
                data = await r.json()
                last_data = data
                harsh_type = data.get("harshEventType") or ""
                # Named, and named as something outside EVENT_TYPE_MAP — an obstructed
                # camera, for one. An unnamed type is a different thing: Samsara has not
                # filled the record in yet, and that still has to be waited out.
                if harsh_type and _SAMSARA_HARSH_TYPE_MAP.get(harsh_type, "harsh_event") not in ALLOWED_TYPES:
                    logger.info(f"[samsara] {harsh_type} is not alerted on — skipping event")
                    return None
                if harsh_type == "Crash" and not is_crash:
                    is_crash = True
                    max_attempts = 15  # ~5 min total at 20s intervals
                    logger.info(f"[samsara] Crash detected — extending poll window to {max_attempts} attempts (~5 min)")
                # First response with a known type: let the caller persist + alert
                # before we keep waiting on the (possibly slow) video upload.
                if on_first is not None and not notified and harsh_type:
                    notified = True
                    try:
                        await on_first(data)
                    except Exception as e:
                        logger.error(f"[samsara] on_first hook failed: {e}", exc_info=True)
                fwd = data.get("downloadForwardVideoUrl") or ""
                inward = data.get("downloadInwardVideoUrl") or ""
                resolved_type = _SAMSARA_HARSH_TYPE_MAP.get(harsh_type, "")
                if fwd and inward:
                    logger.info(f"[samsara] Both video URLs ready on attempt {attempt} (type={harsh_type})")
                    return data
                if resolved_type in _INWARD_ONLY_TYPES and inward:
                    logger.info(f"[samsara] Inward-only type '{resolved_type}' ready on attempt {attempt} — short-circuit")
                    return data
                if fwd or inward:
                    ever_got_url = True
                if not is_crash and attempt == max_attempts and not ever_got_url:
                    logger.warning(f"[samsara] No URLs after {attempt} attempts — sending with no media")
                    return last_data
                tail = " — retrying in 20s" if attempt < max_attempts else ""
                logger.info(f"[samsara] Attempt {attempt}/{max_attempts} type={resolved_type or 'unknown'}: fwd={bool(fwd)} inward={bool(inward)}{tail}")
        except Exception as e:
            logger.error(f"[samsara] harsh_event API error attempt {attempt}: {e}")
    logger.warning(f"[samsara] Gave up after {attempt} attempts (crash={is_crash}) — sending with available URLs")
    return last_data


def _parse_samsara(body: dict) -> tuple[str, dict]:
    """Normalize a Samsara v2 webhook payload to an internal event dict.
    Returns (event_type, normalized)."""
    event_type_raw = body.get("eventType") or ""

    if event_type_raw == "SpeedingEventStarted":
        d = body.get("data") or {}
        vehicle = d.get("vehicle") or {}
        severity = _SAMSARA_SPEED_SEV.get(d.get("severityLevel") or "", "")
        normalized = {
            "id":                 body.get("eventId"),
            "type":               "speeding",
            "vehicle":            {"number": vehicle.get("name") or vehicle.get("id") or ""},
            "driver":             {"name": ""},
            "start_time":         d.get("startTime") or body.get("eventTime") or "",
            "location":           "",
            "nominatim_location": "",
            "severity":           severity,
            "_source":            "samsara",
            "_samsara_vehicle_id": vehicle.get("id") or "",
        }
        return "speeding", normalized

    if event_type_raw == "SevereSpeedingStarted":
        # SevereSpeedingStarted nests its payload one level deeper: data.data
        d = (body.get("data") or {}).get("data") or {}
        vehicle = d.get("vehicle") or {}
        normalized = {
            "id":                 body.get("eventId"),
            "type":               "speeding",
            "vehicle":            {"number": vehicle.get("name") or vehicle.get("id") or ""},
            "driver":             {"name": ""},
            "start_time":         d.get("startTime") or body.get("eventTime") or "",
            "location":           "",
            "nominatim_location": "",
            "severity":           "critical",
            "_source":            "samsara",
            "_samsara_vehicle_id": vehicle.get("id") or "",
        }
        return "speeding", normalized

    if event_type_raw == "AlertIncident":
        data = body.get("data") or {}
        conditions = data.get("conditions") or []
        if not conditions:
            return "", {}
        details = (conditions[0].get("details") or {})

        if "harshEvent" in details:
            harsh = details["harshEvent"]
            vehicle_obj = harsh.get("vehicle") or {}
            vehicle_id = vehicle_obj.get("id") or ""
            event_time = data.get("happenedAtTime") or body.get("eventTime") or ""

            # Extract timestamp (ms) from the last segment of incidentUrl
            timestamp_ms = 0
            parts = (data.get("incidentUrl") or "").rstrip("/").split("/")
            if parts and parts[-1].isdigit():
                timestamp_ms = int(parts[-1])

            # Video fetching is deferred to _handle_event (background task) so the
            # webhook can respond 200 immediately — Samsara has a short delivery timeout.
            normalized = {
                "id":                    body.get("eventId"),
                "type":                  "harsh_event",
                "_samsara_vehicle_id":   vehicle_id,
                "_samsara_timestamp_ms": timestamp_ms,
                "vehicle":               {"number": vehicle_obj.get("name") or vehicle_id},
                "driver":                {"name": ""},
                "start_time":            event_time,
                "location":              "",
                "nominatim_location":    "",
                "severity":              "",
                "camera_media":          None,
                "_source":               "samsara",
            }
            return "harsh_event", normalized

        return "", {}

    logger.info(f"[samsara] Unhandled eventType='{event_type_raw}' — ignoring")
    return "", {}


# How long Motive's review is given before we decide. Its verdict landed 60-85s after
# the first delivery in every case observed, so 180s leaves real margin. The cost is
# that a genuine crash alert arrives ~3 minutes late; that beats another false CRASH.
_CRASH_CONFIRM_DELAY = 180
# Extra look-ups taken on the way there. Logged only, never acted on — they record how
# long withdrawal actually takes so the delay above can be tuned from traffic rather
# than guessed at again.
_CRASH_CONFIRM_PROBES = (60, 120)


# When to go back and ask Motive for the clip. Motive sends the webhook the moment the
# detector trips, which is before the clip has been transcoded: the delivery carries
# camera_media.available = true with every URL still null and auto_transcode_status
# "not started". Motive re-delivers the event once the clip is ready, but under the same
# id — which _persist_once has already recorded, so that delivery is correctly refused as
# a duplicate and the video used never to arrive at all.
#
# So the clip is fetched, and the alert waits for it: one message carrying the card and
# the footage, rather than a card now and a clip later. Splitting them was the first
# attempt here and it reads wrong — the video is part of the alert, not a second alert
# about the same thing — so the cost is paid on latency instead, up to the last mark
# below. The one exception is a crash, which on this branch sends its card as soon as it
# is confirmed and its clip afterwards (see _handle_event): it has waited once already.
#
# These marks are a guess at how long transcoding takes, in the shape of
# _CRASH_CONFIRM_PROBES: each attempt logs what it found, so the real timing can be read
# off a week of logs rather than guessed at twice. A restart during the wait loses the
# alert, the same way it would during a crash confirmation — minus the pending row that
# buys one back, which is not worth building for a dashcam clip.
_MOTIVE_VIDEO_PROBES = (120, 300, 600)


def _motive_clip_is_coming(event: dict) -> bool:
    """Does Motive hold a clip for this event that it has not finished preparing?

    True only for the one state worth going back for: a Motive delivery whose
    camera_media says a clip exists while none of its URLs are filled in yet. The
    neighbouring states are deliberately not polled — available=false is Motive saying
    there is no footage, an absent camera_media block is a payload that never mentions
    cameras (a speeding event, which also carries no `type` to look itself up by), and
    a delivery that already has its URLs needs nothing. Without the API key there is
    nothing to ask, and a Samsara event has its own poll.

    Nor is a crash that Motive withdrew. It goes out as a hard brake (_type_override),
    and it would qualify on every other count — but being withdrawn means the event is
    gone from the very listing the clip is fetched from, so each probe would come back
    absent and the alert would sit out the whole wait to arrive with nothing. Withdrawn
    detections are most of what Motive's crash webhook sends, so that is not a corner.
    """
    if event.get("_source") == "samsara" or not config.MOTIVE_API_KEY:
        return False
    if event.get("_type_override"):
        return False
    if not (event.get("type") or "").strip():
        return False
    camera = event.get("camera_media")
    if not isinstance(camera, dict) or not camera.get("available"):
        return False
    video_urls, image_urls = _get_camera_media_info(event)
    return not video_urls and not image_urls


async def _wait_for_motive_clip(event: dict) -> tuple[list[str], list[str]]:
    """Ask Motive for the clip until it has one, and hand back its URLs.

    Returns ([], []) if it never turned up, which is the caller carrying on with a card
    and no footage — the same card it would have sent anyway, just later.
    """
    event_id = event.get("id", "?")
    raw_type = (event.get("type") or "").strip().lower()
    occurred = _parse_occurred(event)
    elapsed = 0.0
    for mark in _MOTIVE_VIDEO_PROBES:
        await asyncio.sleep(mark - elapsed)
        elapsed = mark
        status, row = await find_performance_event(
            config.MOTIVE_API_KEY, event_id, occurred, raw_type)
        if status != "found":
            # "absent" is not a verdict here the way it is for a crash: the window is
            # keyed on the event's own day and type, so a miss is far likelier to be a
            # lag in Motive's index than a withdrawal. Keep asking.
            logger.info(f"[motive] clip for {event_id} at +{mark}s: lookup {status}")
            continue
        # available=true is forced rather than read: _motive_clip_is_coming already
        # established that a clip exists from the delivery itself, so the only question
        # left for the API row is whether the URLs are filled in yet. That also keeps
        # this working if the row simply omits the flag the webhook carries.
        camera = dict(row.get("camera_media") or {})
        camera["available"] = True
        video_urls, image_urls = _get_camera_media_info({"camera_media": camera})
        if not video_urls and not image_urls:
            logger.info(f"[motive] clip for {event_id} at +{mark}s: still transcoding "
                        f"(status={camera.get('auto_transcode_status') or '?'})")
            continue
        logger.info(f"[motive] clip for {event_id} ready at +{mark}s — alerting with it")
        return video_urls, image_urls
    logger.info(f"[motive] no clip for {event_id} after {_MOTIVE_VIDEO_PROBES[-1]}s — "
                f"alerting without one")
    return [], []


async def _motive_crash_is_real(event: dict, elapsed_before: float = 0.0) -> bool | None:
    """Wait for Motive's review, then ask its API whether this crash still stands.

    Returns True (confirmed), False (withdrawn) or None (couldn't tell — no API key
    configured, or the lookup failed). None must fail open: no evidence is not evidence
    of no crash.

    `elapsed_before` is how long the detection has already been waiting — non-zero only
    when resuming one that a restart interrupted. Marks already behind us are not slept
    through again; the observations among them are simply missed, and the verdict is
    taken straight away if even the final mark has passed."""
    api_key = config.MOTIVE_API_KEY
    event_id = event.get("id")
    if not api_key:
        logger.warning(f"[motive] No MOTIVE_API_KEY configured — "
                       f"sending crash {event_id} unconfirmed")
        return None

    occurred = _parse_occurred(event)
    elapsed = elapsed_before
    for mark in (*_CRASH_CONFIRM_PROBES, _CRASH_CONFIRM_DELAY):
        final = mark == _CRASH_CONFIRM_DELAY
        if mark > elapsed:
            await asyncio.sleep(mark - elapsed)
            elapsed = mark
        elif not final:
            # Resumed past this observation point — nothing left to observe.
            continue
        listed = await crash_still_listed(api_key, event_id, occurred)
        if not final:
            logger.info(f"[motive] crash {event_id} at +{mark}s: listed={listed} (observation)")
            continue
        logger.info(f"[motive] crash {event_id} verdict at +{int(elapsed)}s: listed={listed}")
        return listed
    return None


def _verdict_name(listed: bool | None) -> str:
    if listed is True:
        return VERDICT_CONFIRMED
    return VERDICT_WITHDRAWN if listed is False else VERDICT_UNKNOWN


async def _note_crash(coro, event_id) -> None:
    """Run a confirmation bookkeeping write, swallowing anything it raises.

    These writes are an audit trail, not part of deciding. _handle_event catches
    everything, so an unguarded failure here — a database that missed the migration
    being the obvious one — would abandon the whole event and silently drop a crash
    alert. Losing the row is the acceptable failure; losing the alert is not."""
    try:
        await coro
    except Exception as e:
        logger.error(f"[motive] Could not record crash confirmation {event_id}: {e}")


async def _group_targets(event_type: str, event: dict) -> list[int]:
    """The alerts group, if it is to get this event — as the list the senders take.

    Empty when no group is configured, when it is muted, or when its event filter leaves
    this type out (get_alert_target folds all three into one answer), and for an event
    Samsara never finished describing, which is kept out of the group whatever its
    filter says.
    """
    if event.get("_incomplete"):
        return []
    group_id = await get_alert_target(event_type)
    return [group_id] if group_id else []


async def _handle_event(bot: Bot, event: dict, samsara_api_key: str | None = None,
                        crash_resume_elapsed: float | None = None):
    """Filter → format → send to Telegram (URLs sent directly, no download).

    For Samsara harsh events (those carrying `_samsara_vehicle_id`), first poll the
    harsh-event API to resolve the real type, location and video before continuing.
    Motive events skip that branch entirely and behave exactly as before.

    `crash_resume_elapsed` is set only by resume_pending_crash_confirmations, for a
    crash whose confirmation wait a restart cut short: it says how long the detection
    has already been held, and marks the event as already recorded as pending."""
    try:
        company_display = config.COMPANY_NAME

        # Set once we persist the row at the first poll response, so the post-poll
        # save below is skipped (and records which type we already committed).
        persisted_type = None
        # Set once a crash's full details have been sent at first detection, so the
        # main path knows to follow up with only the video (short caption).
        crash_card_sent = False
        # Whether that first crash alert already carried the location. If not (it can
        # lag the type), the video follow-up upgrades to the full caption.
        crash_first_had_location = False

        # Enrich Samsara harsh events: poll for type, location and video URLs.
        # (Speeding events also carry _samsara_vehicle_id but are enriched later.)
        if event.get("type") == "harsh_event" and event.get("_samsara_vehicle_id") and samsara_api_key:
            async def _on_first(data: dict):
                """Runs on the first poll response that carries a real type (~20s in,
                before the video uploads). Persists the violation now so a mid-poll
                restart can't lose it; for crashes it ALSO sends the full details
                immediately (text) to groups and DMs alike, so everyone has the
                complete record even if no video ever resolves."""
                nonlocal persisted_type, crash_card_sent, crash_first_had_location
                rtype = _SAMSARA_HARSH_TYPE_MAP.get(data.get("harshEventType") or "", "harsh_event")
                if rtype not in ALLOWED_TYPES:
                    return  # a type we'd filter out anyway — don't persist or alert
                first_loc = (data.get("location") or {}).get("address") or ""
                first_event = {**event, "type": rtype, "location": first_loc, "camera_media": None}
                if not await _persist_once(first_event, rtype):
                    # Already in the table: this detection was alerted on before (a
                    # redelivery, or a restart mid-poll). Leave persisted_type unset so
                    # the main path sees the same answer and stops there too.
                    logger.info(f"[samsara] Duplicate event id={event.get('id')} — not alerting again")
                    return
                persisted_type = rtype
                logger.info(f"[samsara] Persisted {rtype} early (id={event.get('id')}) before media resolved")
                if rtype == "crash":
                    # A crash takes the same route as every other type here — the group,
                    # if its filter lists crashes, and the admins' DMs — only sooner. The
                    # same target list is rebuilt for the video follow-up below, so both
                    # halves of a crash land in the same chats.
                    targets = [*await _group_targets("crash", first_event),
                               *await get_subscribed_admins("crash")]
                    text = _format_crash_initial(first_event, company_display)
                    # _send_all, not a bare loop: one unreachable chat must not cost the
                    # recipients behind it the one alert that matters most.
                    await _send_all(bot, targets, text)
                    crash_card_sent = True
                    crash_first_had_location = bool(first_loc)
                    logger.info(f"[samsara] Crash full alert → {len(targets)} target(s) id={event.get('id')}")

            harsh_data = await _fetch_samsara_harsh_event(
                event["_samsara_vehicle_id"], event["_samsara_timestamp_ms"],
                samsara_api_key, on_first=_on_first,
            )
            if harsh_data is None:
                logger.info(f"[samsara] Nothing to alert on — skipping event id={event.get('id')}")
                return
            if not harsh_data:
                # An empty {} after the whole poll: Samsara announced the event and then
                # never filled its record in, so there is no type, no place and no clip.
                # That is not worth the group's attention, but it is worth somebody's —
                # it goes to subscribed admins alone, marked as what it is.
                logger.warning(f"[samsara] Empty response after polling — DM-only id={event.get('id')}")
                event = {**event, "_incomplete": True}
            else:
                harsh_type = harsh_data.get("harshEventType") or ""
                resolved_type = _SAMSARA_HARSH_TYPE_MAP.get(harsh_type, "harsh_event")
                loc = harsh_data.get("location") or {}
                fwd_url = harsh_data.get("downloadForwardVideoUrl") or ""
                inward_url = harsh_data.get("downloadInwardVideoUrl") or ""
                fwd_img = harsh_data.get("downloadForwardImageUrl") or ""
                inward_img = harsh_data.get("downloadInwardImageUrl") or ""
                if fwd_url or inward_url:
                    camera_media = {
                        "available": True,
                        "downloadable_videos": {
                            "front_facing_plain_url": fwd_url,
                            "driver_facing_plain_url": inward_url,
                        },
                        "downloadable_images": {},
                    }
                elif fwd_img or inward_img:
                    camera_media = {
                        "available": True,
                        "downloadable_videos": {},
                        "downloadable_images": {
                            "front_facing_jpg_url": fwd_img,
                            "driver_facing_jpg_url": inward_img,
                        },
                    }
                else:
                    camera_media = None
                event = {
                    **event,
                    "type": resolved_type,
                    "location": loc.get("address") or "",
                    "camera_media": camera_media,
                }

        event_type = _get_event_type(event)
        event_id = event.get("id", "?")

        if event_type not in ALLOWED_TYPES:
            logger.info(f"Ignored event type='{event_type}' id={event_id}")
            return

        # Motive fires the crash webhook before its own review runs, and withdraws the
        # detections that review rejects. Hold the alert until the API can be asked.
        # Only Motive deliveries: Samsara crashes are resolved by their own poll and
        # never come through here.
        if event_type == "crash" and event.get("_source") != "samsara":
            # Written before the wait, not after, so a restart inside it leaves
            # evidence to resume from rather than losing the alert.
            crash_key = _event_id_to_bigint(event.get("id"))
            if crash_resume_elapsed is None:
                await _note_crash(record_pending(crash_key, event), crash_key)
            listed = await _motive_crash_is_real(event, crash_resume_elapsed or 0.0)
            await _note_crash(record_verdict(crash_key, _verdict_name(listed)), crash_key)
            if listed is False:
                event["_type_override"] = "hard_brake"
                event_type = "hard_brake"
                logger.info(f"[motive] Crash {event_id} withdrawn by Motive — "
                            f"routing as hard_brake instead of alerting")
            elif listed is None:
                event["_crash_unconfirmed"] = True

        if event_type == "speeding":
            meta_sev = ((event.get("metadata") or {}).get("severity") or "").strip().lower()
            sev = meta_sev or (event.get("severity") or "").strip().lower()
            min_sev = config.SPEEDING_MIN_SEVERITY
            min_idx = SEVERITY_ORDER.index(min_sev) if min_sev in SEVERITY_ORDER else 2
            allowed_severities = set(SEVERITY_ORDER[min_idx:])
            if sev and sev not in allowed_severities:
                logger.info(f"Ignored speeding event {event_id} severity='{sev}' (below threshold)")
                return

        logger.info(f"Processing unit={_get_unit_num(event)} type={event_type} id={event_id} "
                    f"video={'yes' if event.get('camera_media') else 'no'}")

        # Persist the violation, unless the first-poll hook already saved it. Same
        # helpers as the hook so the row is identical either way. A row that was already
        # there means this event has been alerted on already — stop rather than send a
        # second copy of it.
        if persisted_type is None and not await _persist_once(event, event_type):
            logger.info(f"Duplicate event id={event_id} type={event_type} — not alerting again")
            return

        # Route to the one group (every vehicle; its event filter decides the types) plus
        # the admins who asked for this type in their own DMs.
        group_ids = await _group_targets(event_type, event)
        dm_ids = await get_subscribed_admins(event_type)
        if not group_ids and not dm_ids:
            logger.info(f"No targets for event='{event_type}' — skipping")
            return

        video_urls, image_urls = _get_camera_media_info(event)
        if _motive_clip_is_coming(event):
            if event_type == "crash":
                # Every other Motive alert waits for its clip and goes out as one
                # message. A crash does not get to: it has already been held three
                # minutes for confirmation, and the clip can be ten more behind that.
                # So it takes the shape a Samsara crash has above — the whole card now,
                # to everyone, and the footage after it under a short caption. MZ's
                # crash alerts have always arrived once the confirmation was in, and
                # waiting for video is not allowed to make that later.
                await _send_all(bot, [*group_ids, *dm_ids],
                                _format_crash_initial(event, company_display))
                crash_card_sent = True
                # Motive's delivery carries the location from the start, so the clip
                # brings nothing the first card lacked and its caption stays short.
                crash_first_had_location = True
                logger.info(f"[motive] Crash card sent ahead of its clip → "
                            f"{len(group_ids) + len(dm_ids)} target(s) id={event_id}")
            video_urls, image_urls = await _wait_for_motive_clip(event)

        source_tag = event.get("_source") or "motive"
        if crash_card_sent:
            # The full details already went out at first detection (to everyone). The
            # follow-up is ONLY the video. With no media there's no clip to send — but
            # the first alert's "Video pending…" line leaves recipients waiting, so
            # send a short closure note to the same crash targets.
            if not video_urls and not image_urls:
                logger.info(f"[{source_tag}] Crash had no media — sending no-video closure note id={event_id}")
                await _send_all(bot, [*group_ids, *dm_ids], "📹 <i>No video available for this crash.</i>")
                return
            if not crash_first_had_location and event.get("location"):
                # Location wasn't ready for the first alert but is now — send the full
                # card as the video caption so recipients still get it.
                logger.info(f"[{source_tag}] Location resolved after first crash alert — using full caption id={event_id}")
                text = _format_event(event, company_display)
            else:
                text = _format_crash_video_caption(event)
        else:
            # Samsara speeding webhooks carry no speed data — pull the matching
            # speeding interval (or a live GPS snapshot) from the Samsara API.
            samsara_details = None
            if event_type == "speeding" and samsara_api_key and event.get("_source") == "samsara":
                samsara_details = await fetch_speeding_details(
                    samsara_api_key,
                    _get_vehicle(event),
                    _parse_occurred(event),
                    vehicle_id=event.get("_samsara_vehicle_id") or "",
                )
                if samsara_details:
                    logger.info(f"[samsara] Speeding enrichment id={event_id}: {samsara_details}")
            notes = []
            if (not video_urls and not image_urls
                    and event.get("camera_media") is None
                    and event_type != "speeding"):
                notes.append("📷 <i>No camera media available</i>")
            text = _format_event(event, company_display, samsara_details, notes=notes)
            if event.get("_incomplete"):
                text = "🔍 <i>Possible error — incomplete Samsara data</i>\n\n" + text

        # Download the media ONCE up front and reuse the bytes for every recipient,
        # rather than re-downloading (potentially large crash clips) per chat.
        media, is_video = await _download_media(video_urls, image_urls)
        await _send_all(bot, [*group_ids, *dm_ids], text, media, is_video)

    except Exception as e:
        logger.error(f"Event handling error: {e}", exc_info=True)


# How stale a held-back crash may be and still be worth alerting on. A restart costs
# a minute or two, so anything past this means the bot was down — and a crash DM
# arriving an hour after the fact is noise, not a warning.
_CRASH_RESUME_MAX_AGE = 3600


async def resume_pending_crash_confirmations(bot: Bot) -> None:
    """Finish crash confirmations a restart interrupted, and log the recent tally.

    The confirmation wait runs in a fire-and-forget task, so a deploy landing inside it
    used to drop the crash alert with nothing to show for it. Any row still undecided
    is exactly that, and goes back through the normal path with the time it already
    spent waiting carried across."""
    try:
        pending = await get_pending_confirmations()
        counts = await get_verdict_counts(datetime.now(timezone.utc) - timedelta(days=7))
    except Exception as e:
        logger.error(f"[motive] Could not read crash confirmations: {e}")
        return

    logger.info(f"[motive] Crash confirmations over the last 7d: "
                f"{counts or 'none'} ({len(pending)} interrupted)")

    now = datetime.now(timezone.utc)
    for row in pending:
        event_id = row["event_id"]
        # Per row, because this runs inside on_startup: one unreadable payload must not
        # cost the other resumes, let alone stop the bot from booting.
        try:
            age = (now - row["detected_at"]).total_seconds()
            if age > _CRASH_RESUME_MAX_AGE:
                logger.warning(f"[motive] Dropping crash {event_id} — held {int(age)}s, "
                               f"too stale to alert on")
                await _note_crash(record_verdict(event_id, VERDICT_EXPIRED), event_id)
                continue
            payload = row["payload"]
            # asyncpg hands back JSONB as text unless a codec is registered for it.
            if isinstance(payload, str):
                payload = json.loads(payload)
            logger.info(f"[motive] Resuming crash confirmation {event_id} ({int(age)}s held)")
            _spawn(_handle_event(bot, payload, crash_resume_elapsed=age))
        except Exception as e:
            logger.error(f"[motive] Could not resume crash {event_id}: {e}")


async def _migrate_group(old_id: int, new_id: int) -> None:
    await migrate_group(old_id, new_id)
    logger.info(f"DB updated: group {old_id} → {new_id}")


# Telegram errors that mean "this chat will never accept another message": the bot was
# kicked or blocked, the chat is gone, or the user's account is deactivated. Retrying is
# pointless — every attempt fails the same way — and these all subclass TelegramAPIError,
# so without naming them they get swept into the generic retry branch below.
_PERMANENT_SEND_ERRORS = (
    BotKicked, BotBlocked, ChatNotFound, UserDeactivated, CantInitiateConversation,
)


async def _drop_unreachable(chat_id: int, exc: Exception) -> None:
    """Give up on a chat the bot cannot post to — for this alert, and only this alert.

    Nothing is written down. single-company mutes a group the bot has been kicked from,
    because there a dead driver group would otherwise be retried on every alert for a
    truck nobody is watching, and re-adding the bot un-mutes it. Here there is one group
    and it is the whole point of the deployment: a mute that depends on a my_chat_member
    update to be lifted is a mute that sticks whenever that update is missed — and
    polling starts with skip_updates, so it is missed every time the bot is put back
    during a deploy. A send that fails while the bot is out of the chat costs one API
    call; a group left muted costs every alert until somebody notices.

    So the group is always tried, and the first alert after the bot is back gets
    through on its own. An admin who blocked the bot is likewise only logged: they keep
    their access and can unblock at any time."""
    if chat_id > 0:
        logger.warning(f"DM {chat_id} unreachable ({type(exc).__name__}) — skipping this alert")
        return
    logger.error(f"Alerts group {chat_id} unreachable ({type(exc).__name__}) — this alert "
                 f"did not reach it. Check that the bot is still in the group.")


async def _send_all(bot: Bot, chat_ids: list[int], text: str,
                    media: list[bytes] = None, is_video: bool = False) -> None:
    """Deliver one alert to every recipient. Each send is isolated: a chat that fails
    must not cost the recipients behind it in the list their alert."""
    for chat_id in chat_ids:
        try:
            await _send_with_retry(bot, chat_id, text, media, is_video)
        except Exception as e:
            logger.error(f"Send to {chat_id} failed, continuing with the rest: {e}", exc_info=True)


async def _download_media(video_urls: list[str], image_urls: list[str]) -> tuple[list[bytes], bool]:
    """Download each media URL exactly once so the bytes can be reused for every
    recipient, instead of re-downloading (potentially large) clips per chat.
    Returns (downloaded_bytes_in_order, is_video)."""
    urls = video_urls or image_urls or []
    is_video = bool(video_urls)
    downloaded: list[bytes] = []
    for i, url in enumerate(urls):
        data = await _download(url)
        if data:
            logger.info(f"Downloaded {len(data)} bytes for media_{i+1}")
            downloaded.append(data)
        else:
            logger.error(f"Download failed for {url}")
    return downloaded, is_video


async def _send_with_retry(bot: Bot, chat_id: int, text: str,
                           media: list[bytes | str] = None,
                           is_video: bool = False, retries: int = 3, delay: float = 5.0):
    """Send one alert to one chat. `media` is bytes already downloaded once for all
    recipients (see _download_media); falls back to text only if the media send fails.

    An item may also be a Telegram file_id string, which uploads nothing: Telegram already
    holds that file and re-sends it from its own storage. Alert clips are each seen once,
    so they stay bytes — it is a recurring post that would otherwise re-upload the same
    megabytes to every chat, every day. Nothing on this branch sends one; the path is
    kept as it is on single-company so the two do not drift apart here.

    Returns the Messages Telegram replied with, which is how a caller learns the file_ids
    of what it just uploaded; None when the media path was not taken or did not succeed.
    """
    if media:
        ext = "mp4" if is_video else "jpg"
        MediaType = InputMediaVideo if is_video else InputMediaPhoto

        def _source(i, src):
            """A file_id goes through as-is; bytes are wrapped in a fresh stream per send."""
            if isinstance(src, str):
                return src
            return InputFile(io.BytesIO(src), filename=f"media_{i+1}.{ext}")

        async def _try_send_group(sources):
            if len(sources) == 1:
                send_fn = bot.send_video if is_video else bot.send_photo
                return [await send_fn(chat_id, _source(0, sources[0]),
                                      caption=text, parse_mode="HTML")]
            def _make(i, src):
                return MediaType(_source(i, src), caption=text if i == 0 else None,
                                 parse_mode="HTML" if i == 0 else None)
            return await bot.send_media_group(
                chat_id, [_make(i, s) for i, s in enumerate(sources)])

        for attempt in range(3):
            try:
                return await _try_send_group(media)
            except MigrateToChat as e:
                logger.warning(f"Group {chat_id} migrated to supergroup {e.migrate_to_chat_id} — updating DB and retrying")
                await _migrate_group(chat_id, e.migrate_to_chat_id)
                chat_id = e.migrate_to_chat_id
            except RetryAfter as e:
                logger.warning(f"Flood control (media) for {chat_id}, waiting {e.timeout}s")
                await asyncio.sleep(e.timeout + 1)
            except _PERMANENT_SEND_ERRORS as e:
                # Nothing will ever reach this chat again — don't retry, and don't fall
                # through to the text-only path, which would only fail the same way.
                await _drop_unreachable(chat_id, e)
                return
            except (TimeoutError, NetworkError, TelegramAPIError) as e:
                logger.warning(f"Media send failed for {chat_id} (attempt {attempt+1}/3): {e} — retrying")
                await asyncio.sleep(5)

        # Last resort: text only so the alert is never lost
        logger.error(f"All media failed for {chat_id}, sending text only")
        for attempt in range(3):
            try:
                await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
                return
            except MigrateToChat as e:
                logger.warning(f"Group {chat_id} migrated to supergroup {e.migrate_to_chat_id} — updating DB and retrying")
                await _migrate_group(chat_id, e.migrate_to_chat_id)
                chat_id = e.migrate_to_chat_id
            except RetryAfter as e:
                logger.warning(f"Flood control (text) for {chat_id}, waiting {e.timeout}s")
                await asyncio.sleep(e.timeout + 1)
            except _PERMANENT_SEND_ERRORS as e:
                await _drop_unreachable(chat_id, e)
                return
            except (TimeoutError, NetworkError, TelegramAPIError) as e:
                logger.warning(f"Text send failed for {chat_id} (attempt {attempt+1}/3): {e} — retrying")
                await asyncio.sleep(5)
        logger.error(f"Text-only fallback exhausted for {chat_id} — alert dropped")
        return

    # No media — send text only
    for attempt in range(1, retries + 1):
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
            return
        except MigrateToChat as e:
            logger.warning(f"Group {chat_id} migrated to supergroup {e.migrate_to_chat_id} — updating DB and retrying")
            await _migrate_group(chat_id, e.migrate_to_chat_id)
            chat_id = e.migrate_to_chat_id
        except RetryAfter as e:
            logger.warning(f"Flood control (text-only) for {chat_id}, waiting {e.timeout}s")
            await asyncio.sleep(e.timeout + 1)
        except _PERMANENT_SEND_ERRORS as e:
            await _drop_unreachable(chat_id, e)
            return
        except NetworkError as e:
            if attempt < retries:
                logger.warning(f"NetworkError sending to {chat_id} (attempt {attempt}/{retries}): {e} — retrying in {delay}s")
                await asyncio.sleep(delay)
            else:
                logger.error(f"NetworkError sending to {chat_id} after {retries} attempts: {e}")


async def samsara_webhook(request: web.Request) -> web.Response:
    """Receive a Samsara webhook POST, respond 200 immediately, process async. This
    deployment serves a single company; its Samsara API key + webhook secret come from
    the environment (config.SAMSARA_*)."""
    try:
        body_bytes = await request.read()
        bot: Bot = request.app["bot"]

        api_key = config.SAMSARA_API_KEY or None
        secret = config.SAMSARA_WEBHOOK_SECRET

        if secret:
            # Samsara v1 scheme: header "X-Samsara-Signature: v1=<hex>", signed message
            # is "v1:<timestamp>:<body>" (not the raw body alone).
            sig_header = request.headers.get("X-Samsara-Signature", "")
            provided = sig_header.split("=", 1)[1] if "=" in sig_header else sig_header
            timestamp = request.headers.get("X-Samsara-Timestamp", "")
            signed_payload = _samsara_signed_payload(timestamp, body_bytes)
            if not _verify_hmac(secret, signed_payload, provided):
                logger.warning(f"[samsara] Invalid HMAC signature from {request.remote}")
                return web.Response(text="Forbidden", status=403)
            # Only meaningful once the signature holds: until then the timestamp is just
            # a number the sender chose.
            if not _timestamp_is_fresh(timestamp, config.SAMSARA_MAX_SKEW_SECONDS):
                logger.warning(f"[samsara] Stale or unreadable timestamp '{timestamp}' "
                               f"from {request.remote} — refusing as a replay")
                return web.Response(text="Forbidden", status=403)
        elif not _unsigned_allowed("samsara", request.remote):
            return web.Response(text="Forbidden", status=403)

        body = json.loads(body_bytes)
        event_id = body.get("eventId") or ""
        if _is_duplicate(event_id):
            logger.info(f"[samsara] Duplicate eventId={event_id} — skipping")
            return web.Response(text="OK", status=200)

        event_type, normalized = _parse_samsara(body)
        if not event_type or event_type not in ALLOWED_TYPES:
            logger.info(f"[samsara] Ignored eventType='{body.get('eventType')}' resolved='{event_type}'")
            return web.Response(text="OK", status=200)

        _spawn(_handle_event(bot, normalized, api_key))
        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"[samsara] Webhook error: {e}", exc_info=True)
        return web.Response(text="Error", status=500)


async def motive_webhook(request: web.Request) -> web.Response:
    """Receive Motive webhook POST, respond 200 immediately, process async."""
    try:
        body_bytes = await request.read()
        bot: Bot = request.app["bot"]

        secret = config.MOTIVE_WEBHOOK_SECRET
        if secret:
            # Motive (formerly KeepTruckin) signs with an HMAC-SHA1 hex digest of the
            # raw body, delivered in the X-KT-Webhook-Signature header. NOT SHA256, and
            # NOT an X-Motive-* header.
            sig = request.headers.get("X-KT-Webhook-Signature", "")
            if not _verify_hmac(secret, body_bytes, sig, hashlib.sha1):
                logger.warning(f"[motive] Invalid HMAC signature from {request.remote}")
                return web.Response(text="Forbidden", status=403)
        elif not _unsigned_allowed("motive", request.remote):
            return web.Response(text="Forbidden", status=403)

        body = json.loads(body_bytes)

        # Verification ping — list of event type strings
        if isinstance(body, list) and all(isinstance(i, str) for i in body):
            logger.info(f"Webhook verification ping: {body}")
            return web.Response(text="OK", status=200)

        items = body if isinstance(body, list) else [body]

        for item in items:
            if not isinstance(item, dict):
                continue
            # Unwrap if Motive wrapped the event in a key
            event = (
                item.get("driver_performance_event")
                or item.get("safety_event")
                or item.get("event")
                or item
            )
            event_type = _get_event_type(event)
            if event_type not in ALLOWED_TYPES:
                logger.debug(f"Unhandled event type='{event_type}' keys={list(event.keys())} payload={json.dumps(event, default=str)[:500]}")
            # Same short-window guard Samsara gets. Motive retries a delivery it thinks
            # went unanswered and sends speeding_event_updated under the id of the
            # _created it follows, so without this the same event is processed twice
            # concurrently — and two tasks racing means both can pass the database check
            # before either has inserted its row.
            motive_id = str(event.get("id") or "")
            if motive_id and _is_duplicate(f"motive:{motive_id}"):
                logger.info(f"[motive] Duplicate id={motive_id} — skipping")
                continue
            _spawn(_handle_event(bot, event))

        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"Webhook parse error: {e}")
        return web.Response(text="Error", status=500)


def add_webhook_routes(app: web.Application) -> None:
    """Mount the provider webhooks and the health check.

    One company per deployment, so the path needs no company in it — but the
    multi-company build this replaced put one there, and MZ's providers have been
    delivering to URLs of that form ever since. Those forms are still answered, with
    the segment ignored, so that deploying this does not also mean re-pointing the
    provider. A delivery is trusted for its signature, never for its path.

    Specific paths first: aiohttp takes the first route that matches, and
    /webhook/{company} would otherwise claim /webhook/samsara and hand a Samsara
    delivery to the Motive handler.
    """
    app.router.add_post("/webhook/samsara/{company}", samsara_webhook)
    app.router.add_post("/webhook/samsara", samsara_webhook)
    app.router.add_post("/webhook/motive/{company}", motive_webhook)
    app.router.add_post("/webhook/motive", motive_webhook)
    app.router.add_post("/webhook/{company}", motive_webhook)
    app.router.add_post("/webhook", motive_webhook)
    app.router.add_get("/health", lambda r: web.Response(text="OK"))


async def start_webhook_server(bot: Bot, port: int = 8080):
    app = web.Application()
    app["bot"] = bot
    add_webhook_routes(app)

    # The admin Mini App rides on this same server, under /panel. Imported here rather
    # than at module scope because the panel reads EVENT_TYPE_MAP from this file, and a
    # top-level import in both directions is a cycle.
    from utils.webapp import setup_routes
    setup_routes(app)
    # Close the shared aiohttp session when the server shuts down.
    app.on_cleanup.append(_close_http_session)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Webhook server listening on port {port} (Motive + Samsara)")
