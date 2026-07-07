import asyncio
import hashlib
import hmac
import io
import json
import logging
import time
from collections import OrderedDict
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiogram import Bot
from aiogram.types import InputFile, InputMediaVideo, InputMediaPhoto
from aiogram.utils.exceptions import NetworkError, TelegramAPIError, RetryAfter, MigrateToChat

from data import config
from utils.db_api.companies import (
    get_groups_for_event,
    get_company_name,
    get_speeding_min_severity,
    get_samsara_credentials,
    get_motive_webhook_secret,
)
from utils.db_api.violations import save_violation
from utils.db_api.admins import get_subscribed_admins
from utils.samsara.client import fetch_speeding_details

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

# Exact Motive event type names → (emoji, display title)
EVENT_TYPE_MAP = {
    "hard_brake":                   ("🛑", "HARD BRAKE"),
    "crash":                        ("💥", "CRASH DETECTED"),
    "cell_phone":                   ("📵", "CELL PHONE USAGE"),
    "stop_sign_violation":          ("🛑", "STOP SIGN VIOLATION"),
    "road_facing_cam_obstruction":  ("📷", "ROAD CAMERA OBSTRUCTED"),
    "driver_facing_cam_obstruction":("📷", "DRIVER CAMERA OBSTRUCTED"),
    "forward_collision_warning":    ("⚠️", "FORWARD COLLISION WARNING"),
    "unsafe_parking":               ("🅿️", "UNSAFE PARKING"),
    "speeding":                     ("🚨", "SPEEDING OVER POSTED"),
    "seat_belt_violation":          ("🔒", "SEAT BELT VIOLATION"),
    "near_miss":                    ("⚠️", "NEAR MISS"),
    "drowsiness":                   ("😴", "DROWSINESS"),
    # Samsara harsh-event types (Motive never sends these names)
    "harsh_event":                  ("⚠️", "HARSH EVENT"),
    "harsh_acceleration":           ("🚀", "HARSH ACCELERATION"),
    "harsh_turn":                   ("↩️", "HARSH TURN"),
    "hard_cornering":               ("↩️", "HARSH TURN"),
    "inattentive_driving":          ("😵", "INATTENTIVE DRIVING"),
    "drowsy_driving":               ("😴", "POSSIBLE DROWSINESS"),
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
    """Determine canonical event type from action or type field."""
    action = (event.get("action") or "").lower()
    if action in SPEEDING_ACTIONS:
        return "speeding"
    event_type = (event.get("type") or "").lower()
    # Motive sends collisions as hard_brake with critical severity
    if event_type == "hard_brake":
        meta_sev = ((event.get("metadata") or {}).get("severity") or "").strip().lower()
        sev = meta_sev or (event.get("severity") or "").strip().lower()
        if sev == "critical":
            return "crash"
    return event_type


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


def _format_event(event: dict, company_name: str = "", samsara: dict | None = None) -> str:
    event_type = _get_event_type(event)
    emoji, title = EVENT_TYPE_MAP.get(event_type, ("🚨", event_type.upper().replace("_", " ")))

    samsara = samsara or {}
    vehicle = _get_vehicle(event)
    driver_info = event.get("driver") or event.get("current_driver") or {}
    driver = (
        driver_info.get("name")
        or f"{driver_info.get('first_name', '') or driver_info.get('FirstName', '')} "
           f"{driver_info.get('last_name', '') or driver_info.get('LastName', '')}".strip()
        or driver_info.get("username")
        or samsara.get("driver_name")
        or "Unidentified"
    )

    start_time = _to_et(event.get("start_time", ""))
    location = event.get("location", "")
    intensity = event.get("intensity", "")
    duration = event.get("duration")

    # Severity: prefer metadata.severity, then direct severity field; ignore coaching_status
    meta_sev = ((event.get("metadata") or {}).get("severity") or "").strip()
    sev_display = meta_sev or (event.get("severity") or "").strip() or (samsara.get("severity") or "").strip()

    lines = [f"{emoji} <b>{title}</b>\n"]
    if company_name and event_type == "crash":
        lines.append(company_name)
    lines.append(f"🚛 <b>Vehicle:</b> <code>{vehicle}</code>")
    lines.append(f"👤 <b>Driver:</b> {driver}")
    if sev_display and event_type not in {"driver_facing_cam_obstruction", "road_facing_cam_obstruction"}:
        sev_emoji = SEVERITY_EMOJI.get(sev_display.lower(), "⚠️")
        lines.append(f"📊 <b>Severity:</b> {sev_emoji} {sev_display.title()}")
    lines.append(f"🕐 <b>Time:</b> {start_time}")

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
            lines.append(f"📍 <b>Location:</b> {nominatim}")
    else:
        if location:
            lines.append(f"📍 <b>Location:</b> {location}")
        if event_type == "hard_brake" and intensity:
            lines.append(f"💥 <b>Intensity:</b> {intensity}")
        if duration:
            lines.append(f"⏱ <b>Duration:</b> {duration}s")

    # Tag the source only for Samsara so existing Motive alerts are unchanged.
    if event.get("_source") == "samsara":
        lines.append("\n<i>via Samsara</i>")

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
        return datetime.now()


def _event_severity(event: dict) -> str | None:
    """Normalized severity (metadata.severity → severity), lowercased, or None."""
    meta_sev = ((event.get("metadata") or {}).get("severity") or "").strip().lower()
    return meta_sev or (event.get("severity") or "").strip().lower() or None


def _format_crash_initial(event: dict, company_name: str = "") -> str:
    """First crash alert: the FULL details, sent the instant the crash is detected —
    before the video uploads. Everyone (groups and DMs) gets this, so the complete
    record is delivered even when no video ever resolves."""
    return _format_event(event, company_name) + "\n\n📹 <i>Video pending…</i>"


def _format_crash_video_caption(event: dict) -> str:
    """Short caption for the crash video follow-up — the full details already went out
    in the first alert, so this just labels the clip."""
    return f"💥 <b>CRASH</b> — <code>{_get_vehicle(event)}</code>"


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
                if harsh_type == "Obstructed Camera":
                    logger.info("[samsara] Obstructed Camera — skipping event")
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


async def _handle_event(bot: Bot, event: dict, company_slug: str = "gurman",
                        samsara_api_key: str | None = None):
    """Filter → format → send to Telegram (URLs sent directly, no download).

    For Samsara harsh events (those carrying `_samsara_vehicle_id`), first poll the
    harsh-event API to resolve the real type, location and video before continuing.
    Motive events skip that branch entirely and behave exactly as before."""
    try:
        company_display = await get_company_name(company_slug) or company_slug.title()

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
                await save_violation(
                    company_slug=company_slug,
                    vehicle_number=_get_vehicle(first_event),
                    event_type=rtype,
                    event_id=_event_id_to_bigint(event.get("id")),
                    occurred_at=_parse_occurred(first_event),
                    severity=_event_severity(first_event),
                )
                persisted_type = rtype
                logger.info(f"[samsara] Persisted {rtype} early (id={event.get('id')}) before media resolved")
                if rtype == "crash":
                    # Crash alerts go to subscribed DMs only — never to groups.
                    targets = await get_subscribed_admins("crash", company_slug)
                    text = _format_crash_initial(first_event, company_display)
                    for cid in targets:
                        await _send_with_retry(bot, cid, text)
                    crash_card_sent = True
                    crash_first_had_location = bool(first_loc)
                    logger.info(f"[samsara] Crash full alert → {len(targets)} DM target(s) id={event.get('id')}")

            harsh_data = await _fetch_samsara_harsh_event(
                event["_samsara_vehicle_id"], event["_samsara_timestamp_ms"],
                samsara_api_key, on_first=_on_first,
            )
            if harsh_data is None:
                logger.info(f"[samsara] API returned no data — skipping event id={event.get('id')}")
                return
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

        if event_type == "speeding":
            meta_sev = ((event.get("metadata") or {}).get("severity") or "").strip().lower()
            sev = meta_sev or (event.get("severity") or "").strip().lower()
            min_sev = await get_speeding_min_severity(company_slug)
            min_idx = SEVERITY_ORDER.index(min_sev) if min_sev in SEVERITY_ORDER else 2
            allowed_severities = set(SEVERITY_ORDER[min_idx:])
            if sev and sev not in allowed_severities:
                logger.info(f"Ignored speeding event {event_id} severity='{sev}' (below threshold for {company_slug})")
                return

        logger.info(f"Processing event {event_id} type={event_type}")

        # Persist the violation, unless the first-poll hook already saved it. Same
        # helpers as the hook so the row is identical either way.
        if persisted_type is None:
            await save_violation(
                company_slug=company_slug,
                vehicle_number=_get_vehicle(event),
                event_type=event_type,
                event_id=_event_id_to_bigint(event.get("id")),
                occurred_at=_parse_occurred(event),
                severity=_event_severity(event),
            )

        group_ids = await get_groups_for_event(company_slug, event_type)
        dm_ids = await get_subscribed_admins(event_type, company_slug)
        if event_type == "crash":
            # Crash alerts go to subscribed DMs only — never to groups.
            group_ids = []
        if not group_ids and not dm_ids:
            logger.info(f"No targets for company='{company_slug}' event='{event_type}' — skipping")
            return

        video_urls, image_urls = _get_camera_media_info(event)

        if crash_card_sent:
            # The full details already went out at first detection (to everyone). The
            # follow-up is ONLY the video. With no media there's no clip to send — but
            # the first alert's "Video pending…" line leaves recipients waiting, so
            # send a short closure note to the same crash targets.
            if not video_urls and not image_urls:
                logger.info(f"[samsara] Crash had no media — sending no-video closure note id={event_id}")
                for chat_id in [*group_ids, *dm_ids]:
                    await _send_with_retry(bot, chat_id, "📹 <i>No video available for this crash.</i>")
                return
            if not crash_first_had_location and event.get("location"):
                # Location wasn't ready for the first alert but is now — send the full
                # card as the video caption so recipients still get it.
                logger.info(f"[samsara] Location resolved after first crash alert — using full caption id={event_id}")
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
            text = _format_event(event, company_display, samsara_details)
            if not video_urls and not image_urls and event.get("camera_media") is None and event_type != "speeding":
                text += "\n\n📷 <i>No camera media available</i>"

        # Download the media ONCE up front and reuse the bytes for every recipient,
        # rather than re-downloading (potentially large crash clips) per chat.
        media, is_video = await _download_media(video_urls, image_urls)
        for chat_id in [*group_ids, *dm_ids]:
            await _send_with_retry(bot, chat_id, text, media, is_video)

    except Exception as e:
        logger.error(f"Event handling error: {e}", exc_info=True)


async def _migrate_group(old_id: int, new_id: int) -> None:
    from utils.db_api import db
    await db.execute(
        "UPDATE company_groups SET telegram_group_id = $1 WHERE telegram_group_id = $2",
        new_id, old_id,
    )
    logger.info(f"DB updated: group {old_id} → {new_id}")


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


async def _send_with_retry(bot: Bot, chat_id: int, text: str, media: list[bytes] = None,
                           is_video: bool = False, retries: int = 3, delay: float = 5.0):
    """Send one alert to one chat. `media` is bytes already downloaded once for all
    recipients (see _download_media); falls back to text only if the media send fails."""
    if media:
        ext = "mp4" if is_video else "jpg"
        MediaType = InputMediaVideo if is_video else InputMediaPhoto

        async def _try_send_group(sources):
            """sources: list of bytes; each is wrapped in a fresh stream per send."""
            if len(sources) == 1:
                m = InputFile(io.BytesIO(sources[0]), filename=f"media_1.{ext}")
                send_fn = bot.send_video if is_video else bot.send_photo
                await send_fn(chat_id, m, caption=text, parse_mode="HTML")
            else:
                def _make(i, src):
                    m = InputFile(io.BytesIO(src), filename=f"media_{i+1}.{ext}")
                    return MediaType(m, caption=text if i == 0 else None, parse_mode="HTML" if i == 0 else None)
                await bot.send_media_group(chat_id, [_make(i, s) for i, s in enumerate(sources)])

        for attempt in range(3):
            try:
                await _try_send_group(media)
                return
            except MigrateToChat as e:
                logger.warning(f"Group {chat_id} migrated to supergroup {e.migrate_to_chat_id} — updating DB and retrying")
                await _migrate_group(chat_id, e.migrate_to_chat_id)
                chat_id = e.migrate_to_chat_id
            except RetryAfter as e:
                logger.warning(f"Flood control (media) for {chat_id}, waiting {e.timeout}s")
                await asyncio.sleep(e.timeout + 1)
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
        except NetworkError as e:
            if attempt < retries:
                logger.warning(f"NetworkError sending to {chat_id} (attempt {attempt}/{retries}): {e} — retrying in {delay}s")
                await asyncio.sleep(delay)
            else:
                logger.error(f"NetworkError sending to {chat_id} after {retries} attempts: {e}")


async def samsara_webhook(request: web.Request) -> web.Response:
    """Receive a Samsara webhook POST for a specific company, respond 200 immediately,
    process async. The company comes from the URL (/webhook/samsara/{company}); its
    Samsara API key + webhook secret are looked up per-company in the DB."""
    try:
        company_slug = request.match_info.get("company", "")
        body_bytes = await request.read()
        bot: Bot = request.app["bot"]

        api_key, secret = await get_samsara_credentials(company_slug)

        if secret:
            # Samsara v1 scheme: header "X-Samsara-Signature: v1=<hex>", signed message
            # is "v1:<timestamp>:<body>" (not the raw body alone).
            sig_header = request.headers.get("X-Samsara-Signature", "")
            provided = sig_header.split("=", 1)[1] if "=" in sig_header else sig_header
            timestamp = request.headers.get("X-Samsara-Timestamp", "")
            signed_payload = _samsara_signed_payload(timestamp, body_bytes)
            if not _verify_hmac(secret, signed_payload, provided):
                logger.warning(f"[samsara] Invalid HMAC signature for company='{company_slug}' from {request.remote}")
                return web.Response(text="Forbidden", status=403)
        else:
            logger.warning(f"[samsara] No webhook secret for company='{company_slug}' — skipping signature check")

        body = json.loads(body_bytes)
        event_id = body.get("eventId") or ""
        if _is_duplicate(event_id):
            logger.info(f"[samsara] Duplicate eventId={event_id} — skipping")
            return web.Response(text="OK", status=200)

        event_type, normalized = _parse_samsara(body)
        if not event_type or event_type not in ALLOWED_TYPES:
            logger.info(f"[samsara] Ignored eventType='{body.get('eventType')}' resolved='{event_type}'")
            return web.Response(text="OK", status=200)

        asyncio.create_task(_handle_event(bot, normalized, company_slug, api_key))
        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"[samsara] Webhook error: {e}", exc_info=True)
        return web.Response(text="Error", status=500)


async def motive_webhook(request: web.Request) -> web.Response:
    """Receive Motive webhook POST, respond 200 immediately, process async."""
    try:
        company_slug = request.match_info.get("company", "gurman")
        body_bytes = await request.read()
        bot: Bot = request.app["bot"]

        secret = await get_motive_webhook_secret(company_slug)
        if secret:
            # Motive (formerly KeepTruckin) signs with an HMAC-SHA1 hex digest of the
            # raw body, delivered in the X-KT-Webhook-Signature header. NOT SHA256, and
            # NOT an X-Motive-* header.
            sig = request.headers.get("X-KT-Webhook-Signature", "")
            if not _verify_hmac(secret, body_bytes, sig, hashlib.sha1):
                logger.warning(f"[motive] Invalid HMAC signature for company='{company_slug}' from {request.remote}")
                return web.Response(text="Forbidden", status=403)
        else:
            logger.warning(f"[motive] No webhook secret for company='{company_slug}' — skipping signature check")

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
            asyncio.create_task(_handle_event(bot, event, company_slug))

        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"Webhook parse error: {e}")
        return web.Response(text="Error", status=500)


async def start_webhook_server(bot: Bot, port: int = 8080):
    app = web.Application()
    app["bot"] = bot
    # Samsara is a distinct 3-segment path (/webhook/samsara/{company}); the 2-segment
    # Motive route below can't shadow it. Registered first for clarity.
    app.router.add_post("/webhook/samsara/{company}", samsara_webhook)
    app.router.add_post("/webhook/{company}", motive_webhook)
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    # Close the shared aiohttp session when the server shuts down.
    app.on_cleanup.append(_close_http_session)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Webhook server listening on port {port} (Motive + Samsara)")
