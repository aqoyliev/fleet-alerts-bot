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

from data import config
from utils.db_api.companies import get_groups_for_event
from utils.db_api.violations import save_violation
from utils.samsara.client import SamsaraClient

logger = logging.getLogger(__name__)

_samsara_token = getattr(config, "SAMSARA_API_TOKEN", "")
_samsara = (
    SamsaraClient(_samsara_token, getattr(config, "SAMSARA_API_URL", "https://api.samsara.com"))
    if _samsara_token
    else None
)

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
    # Samsara speeding severity levels
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
}

# Only process these event types — everything else is ignored
ALLOWED_TYPES = set(EVENT_TYPE_MAP.keys())

# Motive speeding webhook uses "action" field with these values
SPEEDING_ACTIONS = {"speeding_event_created", "speeding_event_updated"}


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


def _format_event(event: dict, samsara: dict | None = None) -> str:
    event_type = _get_event_type(event)
    emoji, title = EVENT_TYPE_MAP.get(event_type, ("🚨", event_type.upper().replace("_", " ")))

    samsara = samsara or {}
    vehicle = _get_vehicle(event)
    driver_info = event.get("driver") or {}
    driver = (
        driver_info.get("name")
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

        logger.info(f"Processing event {event_id} type={event_type}")

        # Parse occurred_at
        occurred_at_str = event.get("start_time") or event.get("created_at") or ""
        try:
            occurred_at = datetime.fromisoformat(occurred_at_str.replace("Z", "+00:00"))
        except Exception:
            occurred_at = datetime.now()

        vehicle_number = _get_vehicle(event)
        await save_violation(
            company_slug=company_slug,
            vehicle_number=vehicle_number,
            event_type=event_type,
            event_id=event.get("id"),
            occurred_at=occurred_at,
        )

        group_ids = await get_groups_for_event(company_slug, event_type)
        if not group_ids:
            logger.info(f"No groups configured for company='{company_slug}' event='{event_type}' — skipping")
            return

        # Samsara alert webhooks carry no speed data — pull the matching
        # speeding interval from the Samsara API before formatting.
        samsara_details = None
        if (
            event_type == "speeding"
            and _samsara
            and not event.get("avg_vehicle_speed")
            and not event.get("max_over_speed_in_kph")
        ):
            samsara_details = await _samsara.get_speeding_details(vehicle_number, occurred_at)
            if samsara_details:
                logger.info(f"Samsara enrichment for event {event_id}: {samsara_details}")

        text = _format_event(event, samsara_details)
        video_urls, image_urls = _get_camera_media_info(event)

        if event.get("camera_media") is None and event_type != "speeding":
            text += "\n\n📷 <i>No camera media available</i>"

        videos = [d for d in [await _download(u) for u in video_urls] if d]

        for chat_id in group_ids:
            if videos:
                if len(videos) == 1:
                    await bot.send_video(
                        chat_id,
                        InputFile(io.BytesIO(videos[0]), filename="alert.mp4"),
                        caption=text, parse_mode="HTML",
                    )
                else:
                    media = [
                        InputMediaVideo(InputFile(io.BytesIO(videos[0]), filename="video_1.mp4"), caption=text, parse_mode="HTML")
                    ] + [
                        InputMediaVideo(InputFile(io.BytesIO(v), filename=f"video_{i+2}.mp4"))
                        for i, v in enumerate(videos[1:])
                    ]
                    await bot.send_media_group(chat_id, media)
            elif image_urls:
                images = [d for d in [await _download(u) for u in image_urls] if d]
                if len(images) == 1:
                    await bot.send_photo(
                        chat_id,
                        InputFile(io.BytesIO(images[0]), filename="alert.jpg"),
                        caption=text, parse_mode="HTML",
                    )
                elif images:
                    media = [
                        InputMediaPhoto(InputFile(io.BytesIO(images[0]), filename="photo_1.jpg"), caption=text, parse_mode="HTML")
                    ] + [
                        InputMediaPhoto(InputFile(io.BytesIO(img), filename=f"photo_{i+2}.jpg"))
                        for i, img in enumerate(images[1:])
                    ]
                    await bot.send_media_group(chat_id, media)
                else:
                    await bot.send_message(chat_id, text, parse_mode="HTML")
            else:
                await bot.send_message(chat_id, text, parse_mode="HTML")

    except Exception as e:
        logger.error(f"Event handling error: {e}", exc_info=True)


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
                logger.info(f"Unhandled event type='{event_type}' keys={list(event.keys())} payload={json.dumps(event, default=str)[:500]}")
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

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Motive webhook server listening on port {port}")
