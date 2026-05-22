import asyncio
import hashlib
import hmac
import html
import io
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiogram import Bot
from aiogram.types import InputFile, InputMediaVideo, InputMediaPhoto
from aiogram.utils.exceptions import (
    BadRequest,
    BotBlocked,
    BotKicked,
    CantInitiateConversation,
    ChatNotFound,
    MigrateToChat,
    NetworkError,
    RetryAfter,
    TelegramAPIError,
    UserDeactivated,
)

# Telegram errors that mean the chat is permanently unreachable — never retry these.
_UNREACHABLE_ERRORS = (BotBlocked, BotKicked, UserDeactivated, CantInitiateConversation, ChatNotFound)

# Telegram bot upload size limits (downloaded media larger than this is rejected by the API).
_MAX_VIDEO_BYTES = 50 * 1024 * 1024
_MAX_PHOTO_BYTES = 10 * 1024 * 1024

from data import config
from utils.db_api.companies import get_groups_for_event, update_group_chat_id
from utils.db_api.violations import save_violation
from utils.db_api.admins import get_subscribed_admins

logger = logging.getLogger(__name__)

_download_timeout = aiohttp.ClientTimeout(total=300)
_http_session: aiohttp.ClientSession | None = None

# In-memory deduplication: eventId -> processed timestamp
import time as _time
_seen_event_ids: dict[str, float] = {}
_DEDUP_TTL = 300  # seconds

def _is_duplicate(event_id: str) -> bool:
    """Return True if this eventId was already processed within the last 5 minutes."""
    if not event_id:
        return False
    now = _time.monotonic()
    for k in list(_seen_event_ids):
        if now - _seen_event_ids[k] > _DEDUP_TTL:
            del _seen_event_ids[k]
    if event_id in _seen_event_ids:
        return True
    _seen_event_ids[event_id] = now
    return False


def _event_id_to_bigint(raw) -> int | None:
    """Map a provider event id onto the violations.event_id BIGINT UNIQUE column.

    Motive ids are numeric and pass through unchanged. Samsara ids are UUID strings
    which won't fit a BIGINT, so they were previously stored as NULL — and NULLs never
    conflict, so ON CONFLICT (event_id) DO NOTHING gave Samsara no persistent dedup at
    all (only the ~5-min in-memory window). Hashing the string into a stable signed
    64-bit int makes the UNIQUE constraint work for Samsara too, surviving restarts."""
    s = str(raw or "")
    if not s:
        return None
    if s.isdigit():
        return int(s)
    digest = hashlib.blake2b(s.encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def _download(url: str) -> bytes | None:
    try:
        async with _http_session.get(url) as r:
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
}

# Exact Motive event type names → (emoji, display title)
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
SPEEDING_ACTIONS = {"speeding_event_created"}

# Samsara legacy harsh event API harshEventType string → internal event type
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

# These event types are captured by the inward (driver-facing) camera only — there is
# never a forward clip — so polling can return as soon as the inward URL is ready
# instead of waiting the full window for a forward URL that never arrives.
_INWARD_ONLY_TYPES: frozenset[str] = frozenset(
    {"cell_phone", "drowsy_driving", "no_seat_belt", "inattentive_driving"}
)


def _kph_to_mph(kph: float) -> float:
    return kph * 0.621371


def _verify_hmac(secret: str, body: bytes, provided: str) -> bool:
    """Constant-time HMAC-SHA256 verification. Tries raw string key and base64-decoded key, hex and base64 output."""
    import base64

    def _check(key: bytes) -> bool:
        mac = hmac.new(key, body, hashlib.sha256)
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


async def _fetch_samsara_harsh_event(vehicle_id: str, timestamp_ms: int, on_first=None) -> dict | None:
    """Poll harsh event API waiting for video URLs. Every attempt waits 20s first.

    Standard harsh events: up to 3 attempts (~60s).
    Crashes: once detected, the window extends to 15 attempts (~5 min) with no
    early-bail — crash clips are large and take a few minutes to upload.
    Inward-only types short-circuit as soon as the inward clip is ready.

    `on_first`, if given, is an async callback awaited once with the first response
    that carries a real harshEventType (i.e. the type is now known, before media is
    ready). The caller uses it to persist the violation row and fire a 'pending'
    alert, so the event survives a mid-poll restart and crashes notify immediately."""
    url = f"https://api.samsara.com/v1/fleet/vehicles/{vehicle_id}/safety/harsh_event"
    headers = {"Authorization": f"Bearer {config.SAMSARA_API_KEY}"}
    last_data = None
    ever_got_url = False
    is_crash = False
    notified = False
    max_attempts = 3  # bumped to 15 once a Crash is detected
    attempt = 0
    while attempt < max_attempts:
        attempt += 1
        await asyncio.sleep(20)
        try:
            async with _http_session.get(url, headers=headers, params={"timestamp": timestamp_ms}) as r:
                if r.status == 200:
                    data = await r.json()
                    last_data = data
                    harsh_type = data.get("harshEventType") or ""
                    if harsh_type == "Obstructed Camera":
                        logger.info(f"[samsara] Obstructed Camera — skipping event")
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
                    # Crashes wait the full window; others bail on the last attempt if nothing came.
                    if not is_crash and attempt == max_attempts and not ever_got_url:
                        logger.warning(f"[samsara] No URLs after {attempt} attempts — sending with no media")
                        return last_data
                    tail = " — retrying in 20s" if attempt < max_attempts else ""
                    logger.info(f"[samsara] Attempt {attempt}/{max_attempts} type={resolved_type or 'unknown'}: fwd={bool(fwd)} inward={bool(inward)}{tail}")
                else:
                    body_text = await r.text()
                    logger.error(f"[samsara] harsh_event API HTTP {r.status} on attempt {attempt} — giving up")
                    return None
        except Exception as e:
            logger.error(f"[samsara] harsh_event API error attempt {attempt}: {e}")
    logger.warning(f"[samsara] Gave up after {attempt} attempts (crash={is_crash}) — sending with available URLs")
    return last_data


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


def _strip_unit_prefix(v: str) -> str:
    """Strip a leading unit/UNIT/unit:/unit#/unit- prefix (any case). Single source of
    truth so the saved vehicle_number and the displayed vehicle always agree."""
    return _UNIT_PREFIX_RE.sub('', (v or "").strip()).strip()


def _get_unit_num(event: dict) -> str:
    v = _strip_unit_prefix(_get_vehicle(event))
    return v.split()[0] if v else "?"


def _clean_vehicle(event: dict) -> str:
    """Return full vehicle string with leading unit prefix stripped, truncated to 50 chars."""
    return _strip_unit_prefix(_get_vehicle(event))[:50]


def _vehicle_code_line(vehicle: str) -> str:
    """Render a vehicle string as Telegram <code> spans — unit-prefix stripped and
    HTML-escaped. Shared by the full alert and the crash 'pending' alert so they
    always format the vehicle identically."""
    v = _strip_unit_prefix(vehicle)
    if not v:
        return "—"
    if " " in v:
        num, rest = v.split(" ", 1)
        return f"<code>{html.escape(num)}</code> <code>{html.escape(rest)}</code>"
    return f"<code>{html.escape(v)}</code>"


def _format_event(event: dict) -> str:
    event_type = _get_event_type(event)
    emoji, title = EVENT_TYPE_MAP.get(event_type, ("🚨", event_type.upper().replace("_", " ")))

    vehicle = _get_vehicle(event)
    driver_info = event.get("driver") or event.get("current_driver") or {}
    driver = (
        driver_info.get("name")
        or f"{driver_info.get('first_name', '') or driver_info.get('FirstName', '')} "
           f"{driver_info.get('last_name', '') or driver_info.get('LastName', '')}".strip()
        or driver_info.get("username")
        or "Unidentified"
    )

    start_time = _to_et(event.get("start_time", ""))
    location = event.get("location", "")
    intensity = event.get("intensity", "")
    duration = event.get("duration")

    # Severity: prefer metadata.severity, then direct severity field; ignore coaching_status
    meta_sev = ((event.get("metadata") or {}).get("severity") or "").strip()
    sev_display = meta_sev or (event.get("severity") or "").strip()

    # All dynamic values are HTML-escaped: parse_mode="HTML" means an unescaped
    # '&', '<' or '>' in a driver name or location makes Telegram reject the whole
    # message ("can't parse entities") and the alert is lost.
    lines = [f"{emoji} <b>{html.escape(title)}</b>\n"]
    lines.append(_vehicle_code_line(vehicle))
    if driver and driver != "Unidentified":
        lines.append(f"👤 <b>Driver:</b> {html.escape(driver)}")
    if sev_display and event_type not in {"driver_facing_cam_obstruction", "road_facing_cam_obstruction"}:
        sev_emoji = SEVERITY_EMOJI.get(sev_display.lower(), "⚠️")
        lines.append(f"📊 <b>Severity:</b> {sev_emoji} {html.escape(sev_display.title())}")
    lines.append(f"🕐 <b>Time:</b> {html.escape(start_time)}")

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
        nominatim = event.get("nominatim_location", "")
        if nominatim:
            lines.append(f"📍 <b>Location:</b> {html.escape(nominatim)}")
    else:
        if location:
            lines.append(f"📍 <b>Location:</b> {html.escape(location)}")
        if event_type == "hard_brake" and intensity:
            lines.append(f"💥 <b>Intensity:</b> {html.escape(str(intensity))}")
        if duration:
            lines.append(f"⏱ <b>Duration:</b> {duration}s")

    source = "Samsara" if event.get("_source") == "samsara" else "Motive"
    lines.append(f"\n<i>via {source}</i>")
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


def _format_crash_initial(event: dict) -> str:
    """First crash alert: the FULL details, sent the instant the crash is detected —
    before the video uploads. Everyone (groups and DMs) gets this, so the complete
    record is delivered even when no video ever resolves."""
    return _format_event(event) + "\n\n📹 <i>Video pending…</i>"


def _format_crash_video_caption(event: dict) -> str:
    """Short caption for the crash video follow-up — the full details already went out
    in the first alert, so this just labels the clip."""
    return f"💥 <b>CRASH</b> — {_vehicle_code_line(_get_vehicle(event))}"


async def _handle_event(bot: Bot, event: dict, company_slug: str = ""):
    """Filter → format → send to Telegram."""
    company_slug = company_slug or config.COMPANY_SLUG
    try:
        # Set once we persist the row at first poll response, so the post-poll save
        # below is skipped (and to record what type we already committed).
        persisted_type = None
        # Set once a crash's full details have been sent at first detection, so the
        # main path knows to follow up with only the video (short caption).
        crash_card_sent = False
        # Whether that first crash alert already carried the location. If not (it can
        # lag the type), the video follow-up upgrades to the full caption so the
        # location still reaches recipients.
        crash_first_had_location = False

        # Enrich Samsara AlertIncident events: fetch harsh event data (type, location, video)
        if event.get("_samsara_vehicle_id") and event.get("_samsara_timestamp_ms"):
            async def _on_first(data: dict):
                """Runs on the first poll response that carries a real type — i.e. ~20s
                in, well before the video uploads. Persists the violation row now so a
                mid-poll restart can't lose it. For crashes it ALSO sends the full
                details immediately (text), to groups and DMs alike, so everyone has the
                complete record even if no video ever resolves; the video then follows
                from the main path as a short-captioned clip."""
                nonlocal persisted_type, crash_card_sent, crash_first_had_location
                rtype = _SAMSARA_HARSH_TYPE_MAP.get(data.get("harshEventType") or "", "harsh_event")
                if rtype not in ALLOWED_TYPES:
                    return  # type we'd filter out anyway — don't persist or alert
                # Enrich from the first response (type + location are known now; video
                # isn't) so the full card is complete apart from the clip.
                first_loc = (data.get("location") or {}).get("address") or ""
                first_event = {**event, "type": rtype,
                               "location": first_loc, "camera_media": None}
                await save_violation(
                    company_slug=company_slug,
                    vehicle_number=_clean_vehicle(first_event),
                    event_type=rtype,
                    event_id=_event_id_to_bigint(event.get("id")),
                    occurred_at=_parse_occurred(first_event),
                    severity=_event_severity(first_event),
                )
                persisted_type = rtype
                logger.info(f"[samsara] Persisted {rtype} early (id={event.get('id')}) before media resolved")
                if rtype == "crash":
                    targets = [*await get_groups_for_event(company_slug, "crash"),
                               *await get_subscribed_admins("crash", company_slug)]
                    text = _format_crash_initial(first_event)
                    for cid in targets:
                        await _send_text(bot, cid, text, retries=3, delay=5.0)
                    crash_card_sent = True
                    crash_first_had_location = bool(first_loc)
                    logger.info(f"[samsara] Crash full alert → {len(targets)} target(s) id={event.get('id')}")

            harsh_data = await _fetch_samsara_harsh_event(
                event["_samsara_vehicle_id"], event["_samsara_timestamp_ms"], on_first=_on_first
            )
            if harsh_data is None:
                logger.info(f"[samsara] API returned no data — skipping event id={event.get('id')}")
                return
            if harsh_data:
                harsh_type = harsh_data.get("harshEventType") or ""
                resolved_type = _SAMSARA_HARSH_TYPE_MAP.get(harsh_type, "harsh_event")
                loc = harsh_data.get("location") or {}
                fwd_url = harsh_data.get("downloadForwardVideoUrl") or ""
                inward_url = harsh_data.get("downloadInwardVideoUrl") or ""
                fwd_img = harsh_data.get("downloadForwardImageUrl") or ""
                inward_img = harsh_data.get("downloadInwardImageUrl") or ""
                has_video = bool(fwd_url or inward_url)
                has_image = bool(fwd_img or inward_img)
                if has_video:
                    camera_media = {
                        "available": True,
                        "downloadable_videos": {
                            "front_facing_plain_url": fwd_url,
                            "driver_facing_plain_url": inward_url,
                        },
                        "downloadable_images": {},
                    }
                elif has_image:
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
            if sev and sev not in {"critical", "high", "medium"}:
                logger.info(f"Ignored speeding event {event_id} severity='{sev}' (below threshold)")
                return

        logger.info(f"Processing unit={_get_unit_num(event)} type={event_type} id={event_id} video={'yes' if event.get('camera_media') else 'no'}")

        # Persist the violation, unless the first-poll hook already saved it. Same
        # helpers as the hook so the row is identical either way.
        if persisted_type is None:
            await save_violation(
                company_slug=company_slug,
                vehicle_number=_clean_vehicle(event),
                event_type=event_type,
                event_id=_event_id_to_bigint(event.get("id")),
                occurred_at=_parse_occurred(event),
                severity=_event_severity(event),
            )

        group_ids = await get_groups_for_event(company_slug, event_type)
        dm_ids = await get_subscribed_admins(event_type, company_slug)
        if not group_ids and not dm_ids:
            # Previously this returned when there were no groups, even if admins were
            # subscribed for DMs — so DM-only configs got nothing. Check both.
            logger.info(f"No targets for company='{company_slug}' event='{event_type}' — skipping")
            return

        video_urls, image_urls = _get_camera_media_info(event)
        is_video = bool(video_urls)
        media_urls = video_urls or image_urls or []
        size_limit = _MAX_VIDEO_BYTES if is_video else _MAX_PHOTO_BYTES

        media = []
        for i, url in enumerate(media_urls):
            data = await _download(url)
            if not data:
                logger.error(f"Download failed for media_{i+1}: {url}")
                continue
            if len(data) > size_limit:
                logger.warning(f"media_{i+1} is {len(data)} bytes (> {size_limit} limit) — "
                               f"Telegram would reject it; skipping, alert will be text-only")
                continue
            logger.info(f"Downloaded {len(data)} bytes for media_{i+1}")
            media.append(data)

        if crash_card_sent:
            # The full details already went out at first detection (to everyone). The
            # follow-up is ONLY the video, with a short caption. With no media there's
            # no clip to send — but the first alert's "Video pending…" line leaves
            # recipients waiting, so send a short closure note so they know none is
            # coming, to the same crash targets.
            if not media:
                logger.info(f"[samsara] Crash had no media — sending no-video closure note id={event_id}")
                for chat_id in [*group_ids, *dm_ids]:
                    await _send_text(bot, chat_id, "📹 <i>No video available for this crash.</i>",
                                     retries=3, delay=5.0)
                return
            if not crash_first_had_location and event.get("location"):
                # The location wasn't ready for the first alert but is now — send the
                # full card as the video caption so recipients still get it.
                logger.info(f"[samsara] Location resolved after first crash alert — using full caption id={event_id}")
                text = _format_event(event)
            else:
                text = _format_crash_video_caption(event)
        else:
            text = _format_event(event)
            if not video_urls and not image_urls and event.get("camera_media") is None and event_type != "speeding":
                text += "\n\n📷 <i>No camera media available</i>"

        # Upload the media once, then reuse the returned Telegram file_id for every other
        # recipient instead of re-uploading the same (potentially large) clip N times.
        sent_file_ids = None
        for chat_id in [*group_ids, *dm_ids]:
            payload = sent_file_ids if sent_file_ids is not None else media
            result = await _send_with_retry(bot, chat_id, text, is_video, payload)
            if result and sent_file_ids is None:
                sent_file_ids = result

    except Exception as e:
        logger.error(f"Event handling error: {e}", exc_info=True)


def _extract_file_ids(messages: list, is_video: bool) -> list | None:
    """Pull the Telegram file_id(s) out of sent message(s) so they can be reused
    for the remaining recipients without re-uploading."""
    ids = []
    for m in messages:
        if is_video and getattr(m, "video", None):
            ids.append(m.video.file_id)
        elif not is_video and getattr(m, "photo", None):
            ids.append(m.photo[-1].file_id)
    return ids or None


async def _send_text(bot: Bot, chat_id: int, text: str, retries: int, delay: float) -> None:
    """Send a text-only message with flood-control / network retries. Permanent errors
    (chat unreachable, bad request) are not retried — the same message would fail again."""
    for attempt in range(1, retries + 1):
        try:
            await bot.send_message(chat_id, text, parse_mode="HTML", disable_web_page_preview=True)
            return
        except RetryAfter as e:
            logger.warning(f"Flood control (text) for {chat_id}, waiting {e.timeout}s")
            await asyncio.sleep(e.timeout + 1)
        except MigrateToChat as e:
            logger.warning(f"Chat {chat_id} migrated to supergroup {e.migrate_to_chat_id} — updating DB")
            await update_group_chat_id(chat_id, e.migrate_to_chat_id)
            chat_id = e.migrate_to_chat_id
        except _UNREACHABLE_ERRORS as e:
            logger.error(f"Chat {chat_id} unreachable ({type(e).__name__}: {e}) — giving up")
            return
        except BadRequest as e:
            logger.error(f"Text rejected for {chat_id} ({e}) — giving up (retrying identical text won't help)")
            return
        except NetworkError as e:
            if attempt < retries:
                logger.warning(f"NetworkError sending to {chat_id} (attempt {attempt}/{retries}): {e} — retrying in {delay}s")
                await asyncio.sleep(delay)
            else:
                logger.error(f"NetworkError sending to {chat_id} after {retries} attempts: {e}")


async def _send_with_retry(bot: Bot, chat_id: int, text: str, is_video: bool = False,
                           media: list = None, retries: int = 3, delay: float = 5.0) -> list | None:
    """Send alert to a single chat. `media` is a list of raw bytes (uploaded) or
    Telegram file_id strings (reused from a prior send — no re-upload).

    Returns the file_id(s) of the media just sent so the caller can reuse them for the
    other recipients, or None if nothing reusable was sent."""
    media = media or []

    if media:
        ext = "mp4" if is_video else "jpg"
        MediaType = InputMediaVideo if is_video else InputMediaPhoto

        def _to_file(i: int, src):
            # bytes → fresh upload; str (file_id/URL) → passed through, served by Telegram
            return InputFile(io.BytesIO(src), filename=f"media_{i+1}.{ext}") if isinstance(src, bytes) else src

        async def _try_send_group() -> list | None:
            if len(media) == 1:
                send_fn = bot.send_video if is_video else bot.send_photo
                msg = await send_fn(chat_id, _to_file(0, media[0]), caption=text, parse_mode="HTML")
                return _extract_file_ids([msg], is_video)
            items = [MediaType(_to_file(i, s), caption=text if i == 0 else None,
                               parse_mode="HTML" if i == 0 else None)
                     for i, s in enumerate(media)]
            msgs = await bot.send_media_group(chat_id, items)
            return _extract_file_ids(msgs, is_video)

        for attempt in range(1, retries + 1):
            try:
                return await _try_send_group()
            except RetryAfter as e:
                logger.warning(f"Flood control (media) for {chat_id}, waiting {e.timeout}s")
                await asyncio.sleep(e.timeout + 1)
            except MigrateToChat as e:
                logger.warning(f"Group {chat_id} migrated to supergroup {e.migrate_to_chat_id} — updating DB")
                await update_group_chat_id(chat_id, e.migrate_to_chat_id)
                chat_id = e.migrate_to_chat_id
            except _UNREACHABLE_ERRORS as e:
                logger.error(f"Chat {chat_id} unreachable ({type(e).__name__}: {e}) — giving up, not retrying")
                return None
            except BadRequest as e:
                # Permanent for this payload (caption too long, bad media, parse error) —
                # don't retry the same media; fall through to a text-only attempt.
                logger.warning(f"Media rejected for {chat_id} ({e}) — falling back to text")
                break
            except (TimeoutError, NetworkError, TelegramAPIError) as e:
                logger.warning(f"Media send failed for {chat_id} (attempt {attempt}/{retries}): {e}")
                if attempt < retries:
                    await asyncio.sleep(delay)

        # Last resort: text only so the alert is never lost
        logger.error(f"Media undeliverable for {chat_id}, sending text only")
        await _send_text(bot, chat_id, text, retries, delay)
        return None

    # No media — send text only
    await _send_text(bot, chat_id, text, retries, delay)
    return None


# ── Samsara ──────────────────────────────────────────────────────────────────

# SpeedingEventStarted severityLevel → our severity
_SAMSARA_SPEED_SEV: dict[str, str] = {
    "Light":    "low",
    "Moderate": "medium",
    "Heavy":    "high",
    "Severe":   "critical",
}


async def _parse_samsara(body: dict) -> tuple[str, dict]:
    """Normalize Samsara v2 webhook payload to internal event dict. Returns (event_type, normalized)."""
    event_type_raw = body.get("eventType") or ""

    if event_type_raw == "SpeedingEventStarted":
        d = body.get("data") or {}
        vehicle = d.get("vehicle") or {}
        severity = _SAMSARA_SPEED_SEV.get(d.get("severityLevel") or "", "")
        normalized: dict = {
            "id":                 body.get("eventId"),
            "type":               "speeding",
            "vehicle":            {"number": vehicle.get("name") or vehicle.get("id") or ""},
            "driver":             {"name": ""},
            "start_time":         d.get("startTime") or body.get("eventTime") or "",
            "location":           "",
            "nominatim_location": "",
            "severity":           severity,
            "_source":            "samsara",
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

            # Extract timestamp from last segment of incidentUrl
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


async def samsara_webhook(request: web.Request) -> web.Response:
    """Receive Samsara webhook POST, respond 200 immediately, process async."""
    try:
        company_slug = request.match_info.get("company") or config.COMPANY_SLUG
        body_bytes = await request.read()

        if config.SAMSARA_WEBHOOK_SECRET:
            # Samsara's documented v1 scheme (verified against live webhooks on the hf
            # deployment): header is "X-Samsara-Signature: v1=<hex>" and the signed
            # message is "v1:<timestamp>:<body>", not the raw body. The old code signed
            # the raw body under an X-Samsara-Hmac-Sha256 header that Samsara never sends,
            # so enabling the secret would have 403'd every legitimate webhook.
            sig_header = request.headers.get("X-Samsara-Signature", "")
            provided = sig_header.split("=", 1)[1] if "=" in sig_header else sig_header
            timestamp = request.headers.get("X-Samsara-Timestamp", "")
            signed_payload = _samsara_signed_payload(timestamp, body_bytes)
            if not _verify_hmac(config.SAMSARA_WEBHOOK_SECRET, signed_payload, provided):
                logger.warning(f"[samsara] Invalid HMAC signature from {request.remote}")
                return web.Response(text="Forbidden", status=403)

        body = json.loads(body_bytes)
        # logger.info(f"[samsara] webhook body:\n{json.dumps(body, indent=2)}")
        event_id = body.get("eventId") or ""
        if _is_duplicate(event_id):
            logger.info(f"[samsara] Duplicate eventId={event_id} — skipping")
            return web.Response(text="OK", status=200)

        bot: Bot = request.app["bot"]

        event_type, normalized = await _parse_samsara(body)
        if not event_type or event_type not in ALLOWED_TYPES:
            logger.info(f"[samsara] Ignored eventType='{body.get('eventType')}' resolved='{event_type}'")
            return web.Response(text="OK", status=200)

        asyncio.create_task(_handle_event(bot, normalized, company_slug))
        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"[samsara] Webhook error: {e}", exc_info=True)
        return web.Response(text="Error", status=500)


async def motive_webhook(request: web.Request) -> web.Response:
    """Receive Motive webhook POST, respond 200 immediately, process async."""
    try:
        company_slug = request.match_info.get("company") or config.COMPANY_SLUG
        body_bytes = await request.read()

        if config.MOTIVE_WEBHOOK_SECRET:
            sig = request.headers.get("X-Motive-Hmac-Sha256", "")
            if not _verify_hmac(config.MOTIVE_WEBHOOK_SECRET, body_bytes, sig):
                logger.warning(f"[motive] Invalid HMAC signature from {request.remote}")
                return web.Response(text="Forbidden", status=403)

        body = json.loads(body_bytes)
        bot: Bot = request.app["bot"]

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
            # Dedup per item: a batched payload (list) carries no top-level id, so
            # deduping there missed batches entirely. Key off each event's own id.
            item_id = str(event.get("id") or item.get("id") or "")
            if _is_duplicate(item_id):
                logger.info(f"[motive] Duplicate event id={item_id} — skipping")
                continue
            event_type = _get_event_type(event)
            if event_type not in ALLOWED_TYPES:
                logger.debug(f"Unhandled event type='{event_type}' keys={list(event.keys())} payload={json.dumps(event, default=str)[:500]}")
            asyncio.create_task(_handle_event(bot, event, company_slug))

        return web.Response(text="OK", status=200)
    except Exception as e:
        logger.error(f"Webhook parse error: {e}")
        return web.Response(text="Error", status=500)


async def start_webhook_server(bot: Bot, port: int = 8080):
    global _http_session
    _http_session = aiohttp.ClientSession(timeout=_download_timeout)

    if not config.MOTIVE_WEBHOOK_SECRET:
        logger.warning("MOTIVE_WEBHOOK_SECRET not set — Motive webhook auth disabled")
    if not config.SAMSARA_WEBHOOK_SECRET:
        logger.warning("SAMSARA_WEBHOOK_SECRET not set — Samsara webhook auth disabled")

    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/webhook/samsara/{company}", samsara_webhook)
    app.router.add_post("/webhook/samsara", samsara_webhook)
    app.router.add_post("/webhook/motive/{company}", motive_webhook)
    app.router.add_post("/webhook/motive", motive_webhook)
    app.router.add_post("/webhook/{company}", motive_webhook)
    app.router.add_post("/webhook", motive_webhook)
    app.router.add_get("/health", lambda r: web.Response(text="OK"))

    async def _on_cleanup(_app):
        await _http_session.close()

    app.on_cleanup.append(_on_cleanup)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Webhook server listening on port {port}")
