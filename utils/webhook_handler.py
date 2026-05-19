import asyncio
import io
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiogram import Bot
from aiogram.types import InputFile, InputMediaVideo, InputMediaPhoto
from aiogram.utils.exceptions import NetworkError, TelegramAPIError, RetryAfter, MigrateToChat

from data import config
from utils.db_api.companies import get_groups_for_event, get_company_name, get_speeding_min_severity
from utils.db_api.violations import save_violation
from utils.db_api.admins import get_subscribed_admins

logger = logging.getLogger(__name__)

_download_timeout = aiohttp.ClientTimeout(total=300)


async def _download(url: str) -> bytes | None:
    try:
        async with aiohttp.ClientSession(timeout=_download_timeout) as s:
            async with s.get(url) as r:
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
    "road_facing_cam_obstruction":  ("📷", "ROAD CAMERA OBSTRUCTED"),
    "driver_facing_cam_obstruction":("📷", "DRIVER CAMERA OBSTRUCTED"),
    "forward_collision_warning":    ("⚠️", "FORWARD COLLISION WARNING"),
    "unsafe_parking":               ("🅿️", "UNSAFE PARKING"),
    "speeding":                     ("🚨", "SPEEDING OVER POSTED"),
    "seat_belt_violation":          ("🔒", "SEAT BELT VIOLATION"),
    "aggregated_lane_swerving":     ("🔀", "LANE SWERVING"),
    "near_miss":                    ("⚠️", "NEAR MISS"),
    "drowsiness":                   ("😴", "DROWSINESS"),
}

# Only process these event types — everything else is ignored
ALLOWED_TYPES = set(EVENT_TYPE_MAP.keys())

# Motive speeding webhook uses "action" field with these values
SPEEDING_ACTIONS = {"speeding_event_created", "speeding_event_updated"}

# Severity ordering for threshold filtering
SEVERITY_ORDER = ["low", "medium", "high", "critical"]


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


def _format_event(event: dict, company_name: str = "") -> str:
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
        nominatim = event.get("nominatim_location", "")
        if nominatim:
            lines.append(f"📍 <b>Location:</b> {nominatim}")
    else:
        if location:
            lines.append(f"📍 <b>Location:</b> {location}")
        if event_type == "hard_brake" and intensity:
            lines.append(f"💥 <b>Intensity:</b> {intensity}")
        if duration:
            lines.append(f"⏱ <b>Duration:</b> {duration}s")

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


async def _handle_event(bot: Bot, event: dict, company_slug: str = "gurman"):
    """Filter → format → send to Telegram (URLs sent directly, no download)."""
    try:
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

        # Parse occurred_at
        occurred_at_str = event.get("start_time") or event.get("created_at") or ""
        try:
            occurred_at = datetime.fromisoformat(occurred_at_str.replace("Z", "+00:00"))
        except Exception:
            occurred_at = datetime.now()

        vehicle_number = _get_vehicle(event)
        meta_sev = ((event.get("metadata") or {}).get("severity") or "").strip().lower()
        severity = meta_sev or (event.get("severity") or "").strip().lower() or None
        await save_violation(
            company_slug=company_slug,
            vehicle_number=vehicle_number,
            event_type=event_type,
            event_id=event.get("id"),
            occurred_at=occurred_at,
            severity=severity,
        )

        group_ids = await get_groups_for_event(company_slug, event_type)
        if not group_ids:
            logger.info(f"No groups configured for company='{company_slug}' event='{event_type}' — skipping")
            return

        company_display = await get_company_name(company_slug) or company_slug.title()
        text = _format_event(event, company_display)
        video_urls, image_urls = _get_camera_media_info(event)

        if not video_urls and not image_urls and event.get("camera_media") is None and event_type != "speeding":
            text += "\n\n📷 <i>No camera media available</i>"

        for chat_id in group_ids:
            await _send_with_retry(bot, chat_id, text, video_urls, image_urls)

        dm_ids = await get_subscribed_admins(event_type, company_slug)
        for telegram_id in dm_ids:
            await _send_with_retry(bot, telegram_id, text, video_urls, image_urls)

    except Exception as e:
        logger.error(f"Event handling error: {e}", exc_info=True)


async def _migrate_group(old_id: int, new_id: int) -> None:
    from utils.db_api import db
    await db.execute(
        "UPDATE company_groups SET telegram_group_id = $1 WHERE telegram_group_id = $2",
        new_id, old_id,
    )
    logger.info(f"DB updated: group {old_id} → {new_id}")


async def _send_with_retry(bot: Bot, chat_id: int, text: str, video_urls: list[str] = None,
                           image_urls: list[str] = None, retries: int = 3, delay: float = 5.0):
    """Send alert to a single chat. Tries videos/images by URL; falls back to text+links on failure."""
    media_urls = video_urls or image_urls or []
    is_video = bool(video_urls)

    # Try sending media as a group by URL first
    if media_urls:
        ext = "mp4" if is_video else "jpg"
        MediaType = InputMediaVideo if is_video else InputMediaPhoto

        async def _try_send_group(sources):
            """sources: list of str (URL) or bytes (downloaded)."""
            if len(sources) == 1:
                src = sources[0]
                media = InputFile(io.BytesIO(src), filename=f"media_1.{ext}") if isinstance(src, bytes) else src
                send_fn = bot.send_video if is_video else bot.send_photo
                await send_fn(chat_id, media, caption=text, parse_mode="HTML")
            else:
                def _make(i, src):
                    m = InputFile(io.BytesIO(src), filename=f"media_{i+1}.{ext}") if isinstance(src, bytes) else src
                    return MediaType(m, caption=text if i == 0 else None, parse_mode="HTML" if i == 0 else None)
                await bot.send_media_group(chat_id, [_make(i, s) for i, s in enumerate(sources)])

        # Download all files, then send as a group
        downloaded = []
        for i, url in enumerate(media_urls):
            data = await _download(url)
            if data:
                logger.info(f"Downloaded {len(data)} bytes for media_{i+1}")
                downloaded.append(data)
            else:
                logger.error(f"Download failed for {url}")

        if downloaded:
            for attempt in range(3):
                try:
                    await _try_send_group(downloaded)
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


async def motive_webhook(request: web.Request) -> web.Response:
    """Receive Motive webhook POST, respond 200 immediately, process async."""
    try:
        company_slug = request.match_info.get("company", "gurman")
        body = await request.json()

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
    app.router.add_post("/webhook/{company}", motive_webhook)
    app.router.add_get("/health", lambda r: web.Response(text="OK"))

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Motive webhook server listening on port {port}")
