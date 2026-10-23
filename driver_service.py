"""driver-alerts: the service that tells drivers about their own cell phone use.

A separate Railway service built from the same repo and branch as `web`, sharing its
Postgres. `web` does the company alerts and forwards every cell_phone event here
(utils/driver_forward.py); this process runs utils/driver_alerts.notify_driver on it —
Vitality lookup, the Samsara coaching-state wait, the name check against the group
title, the daily count, the driver-bot post and email, DRY RUN, and idempotency through
driver_notifications — exactly as web used to inline.

Start command (Procfile-style; the Procfile itself only runs `web`):

    driver-alerts: python driver_service.py

Endpoints:
    POST /events  — X-Forward-Secret must equal DRIVER_ALERTS_FORWARD_SECRET (401
                    otherwise). A valid payload is answered 202 at once and processed
                    in the background; a malformed one gets 400.
    GET  /health  — 200.

Listens on PORT (default 8080) on 0.0.0.0 and :: (Railway's private network is IPv6).
Refuses to start without DRIVER_ALERTS_FORWARD_SECRET. data.config still requires the
main bot's variables (BOT_TOKEN, DATABASE_URL, ...) at import, but the main bot is never
polled and its webhook is never touched here: only the send-only driver bot is used.
"""
import asyncio
import hmac
import importlib
import logging
import os
import socket
import sys
from datetime import datetime, timezone

from aiohttp import web

from data import config
from utils import driver_alerts
from utils.db_api import init_pool, close_pool
from utils.db_api.companies import get_samsara_credentials
from utils.driver_alerts import notify_driver
from utils.driver_forward import SECRET_HEADER
from utils.webhook_handler import (
    _close_http_session as close_download_session,
    _download_media as download_media,
)

logger = logging.getLogger("driver_service")

DEFAULT_PORT = 8080
_SOURCES = ("motive", "samsara")
# How long a Motive delivery that has no clip yet waits for the delivery that carries it
# (usually 10-60 s later, sometimes ~5 min) before the driver gets a text-only alert.
MEDIA_WAIT_S = 600
_sleep = asyncio.sleep     # tests patch this
# The forwarded event dict can carry Motive's GPS/speed arrays; well above that.
_MAX_BODY = 4 * 1024 * 1024

# In-flight notifications, kept referenced until done (the loop holds only weak refs).
_tasks: set[asyncio.Task] = set()


class PayloadError(ValueError):
    pass


def _str_list(value, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(u, str) for u in value):
        raise PayloadError(f"{name} must be a list of strings")
    return [u for u in value if u]


def validate_payload(data) -> dict:
    """Check a forwarded body and return the notify_driver inputs. Raises PayloadError."""
    if not isinstance(data, dict):
        raise PayloadError("body must be a JSON object")
    company_slug = data.get("company_slug")
    if not isinstance(company_slug, str) or not company_slug.strip():
        raise PayloadError("company_slug is required")
    alert_source = data.get("alert_source")
    if alert_source not in _SOURCES:
        raise PayloadError(f"alert_source must be one of {_SOURCES}")
    event = data.get("event")
    if not isinstance(event, dict):
        raise PayloadError("event must be an object")
    event_id = data.get("event_id")
    if event_id is not None and (isinstance(event_id, bool) or not isinstance(event_id, int)):
        raise PayloadError("event_id must be an integer or null")
    vehicle = data.get("vehicle") or ""
    base_text = data.get("base_text") or ""
    if not isinstance(vehicle, str) or not isinstance(base_text, str):
        raise PayloadError("vehicle and base_text must be strings")
    occurred_raw = data.get("occurred_at")
    occurred_at = None
    if occurred_raw is not None:
        if not isinstance(occurred_raw, str):
            raise PayloadError("occurred_at must be an ISO-8601 string or null")
        try:
            occurred_at = datetime.fromisoformat(occurred_raw.replace("Z", "+00:00"))
        except ValueError:
            raise PayloadError("occurred_at is not ISO-8601")
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    return {
        "company_slug": company_slug.strip(),
        "alert_source": alert_source,
        "event": event,
        "event_id": event_id,
        "raw_event_id": data.get("raw_event_id"),
        "vehicle": vehicle,
        "occurred_at": occurred_at,
        "base_text": base_text,
        "video_urls": _str_list(data.get("video_urls"), "video_urls"),
        "image_urls": _str_list(data.get("image_urls"), "image_urls"),
    }


def _media_pending(p: dict) -> bool:
    """A Motive delivery with no media URL. Motive often sends the event first without
    camera_media at all (not only with available=true and null URLs) and redelivers it
    with the clip once transcoded, so any media-less Motive delivery waits for that one;
    a truck with no camera just gets its text-only alert MEDIA_WAIT_S later."""
    return p["alert_source"] == "motive" and not p["video_urls"] and not p["image_urls"]


async def process_event(p: dict) -> None:
    """Download the evidence, look up the Samsara key if needed, notify the driver.
    A Motive delivery whose clip is still transcoding waits for the redelivery that
    carries it, then falls back to a text-only alert (a no-op if that redelivery already
    notified: notify_driver claims each event once). Never raises (notify_driver itself
    never does)."""
    event_id = p["event_id"]
    try:
        if not driver_alerts.is_enabled():
            logger.warning(f"[driver-service] Event {event_id}: DRIVER_ALERTS_BOT_TOKEN unset — "
                           f"not notifying the driver")
            return
        if _media_pending(p):
            logger.info(f"[driver-service] Event {event_id}: media pending, waiting for the "
                        f"delivery with the clip ({MEDIA_WAIT_S}s)")
            await _sleep(MEDIA_WAIT_S)
            await notify_driver(
                company_slug=p["company_slug"],
                event=p["event"],
                event_id=event_id,
                vehicle=p["vehicle"],
                occurred_at=p["occurred_at"],
                base_text=p["base_text"],
                media=None,
                is_video=False,
                video_urls=[],
                image_urls=[],
                clip_wait_over=True,
            )
            return
        media, is_video = await download_media(p["video_urls"], p["image_urls"])
        samsara_api_key = None
        if p["alert_source"] == "samsara":
            try:
                samsara_api_key, _secret = await get_samsara_credentials(p["company_slug"])
            except Exception as e:
                # notify_driver's gate fails closed on a missing key.
                logger.error(f"[driver-service] Could not read the Samsara key for "
                             f"{p['company_slug']}: {e}")
        await notify_driver(
            company_slug=p["company_slug"],
            event=p["event"],
            event_id=event_id,
            vehicle=p["vehicle"],
            occurred_at=p["occurred_at"],
            base_text=p["base_text"],
            media=media,
            is_video=is_video,
            video_urls=p["video_urls"],
            image_urls=p["image_urls"],
            samsara_api_key=samsara_api_key,
        )
    except Exception as e:
        logger.error(f"[driver-service] Event {event_id} failed: {e}", exc_info=True)


def _spawn(coro, what: str) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.error(f"[driver-service] {what} failed: {t.exception()}", exc_info=t.exception())

    task.add_done_callback(_done)
    return task


def _secret_ok(expected: str, provided: str | None) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected.encode(), provided.encode())


async def handle_events(request: web.Request) -> web.Response:
    if not _secret_ok(request.app["forward_secret"], request.headers.get(SECRET_HEADER)):
        logger.warning(f"[driver-service] Rejected /events from {request.remote}: bad or missing "
                       f"{SECRET_HEADER}")
        return web.json_response({"error": "unauthorized"}, status=401)
    try:
        data = await request.json()
    except Exception:
        return web.json_response({"error": "body is not JSON"}, status=400)
    try:
        p = validate_payload(data)
    except PayloadError as e:
        logger.warning(f"[driver-service] Rejected malformed payload: {e}")
        return web.json_response({"error": str(e)}, status=400)
    logger.info(f"[driver-service] Accepted {p['alert_source']} event {p['event_id']} "
                f"({p['company_slug']}/{p['vehicle']}, raw id {p['raw_event_id']})")
    _spawn(process_event(p), f"driver notification {p['event_id']}")
    return web.json_response({"status": "accepted"}, status=202)


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


async def _on_startup(_app: web.Application) -> None:
    await init_pool()
    logger.info(f"[driver-service] Ready — driver bot "
                f"{'configured' if driver_alerts.is_enabled() else 'NOT configured (nothing will be sent)'}, "
                f"DRY_RUN={config.DRIVER_ALERTS_DRY_RUN}, "
                f"COUNT_FROM={config.DRIVER_ALERTS_COUNT_FROM or '(day start)'}, "
                f"Vitality={'on' if config.VITALITY_PROVIDER_KEY else 'off'}, "
                f"email={'on' if config.RESEND_API_KEY and config.EMAIL_FROM else 'off'}")


async def _close_module(name: str) -> None:
    await importlib.import_module(name).close()


async def _on_cleanup(_app: web.Application) -> None:
    pending = [t for t in _tasks if not t.done()]
    if pending:
        logger.warning(f"[driver-service] Shutting down with {len(pending)} notification(s) "
                       f"in flight — cancelling")
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    try:
        from loader import driver_bot
        if driver_bot is not None:
            session = await driver_bot.get_session()
            await session.close()
    except Exception as e:
        logger.warning(f"[driver-service] Could not close the driver bot session: {e}")
    for name, closer in (("download", close_download_session),
                         ("Vitality", lambda: _close_module("utils.vitality")),
                         ("email", lambda: _close_module("utils.email_sender"))):
        try:
            await closer()
        except Exception as e:
            logger.warning(f"[driver-service] Could not close the {name} session: {e}")
    # utils.samsara.safety opens a session per look-up; nothing to close there.
    await close_pool()


def create_app(secret: str, *, manage_resources: bool = True) -> web.Application:
    """The aiohttp app. manage_resources=False (tests) skips the DB pool and the
    session cleanup."""
    if not secret:
        raise ValueError("DRIVER_ALERTS_FORWARD_SECRET is empty")
    app = web.Application(client_max_size=_MAX_BODY)
    app["forward_secret"] = secret
    app.router.add_post("/events", handle_events)
    app.router.add_get("/health", health)
    if manage_resources:
        app.on_startup.append(_on_startup)
        app.on_cleanup.append(_on_cleanup)
    return app


def _bind_hosts() -> list[str]:
    return ["0.0.0.0", "::"] if socket.has_ipv6 else ["0.0.0.0"]


def main() -> None:
    secret = config.DRIVER_ALERTS_FORWARD_SECRET
    if not secret:
        logger.critical("[driver-service] DRIVER_ALERTS_FORWARD_SECRET is empty — refusing to start")
        sys.exit(1)
    port = int(os.environ.get("PORT") or DEFAULT_PORT)
    hosts = _bind_hosts()
    logger.info(f"[driver-service] Starting on {', '.join(hosts)} port {port} (POST /events, GET /health)")
    web.run_app(create_app(secret), host=hosts, port=port, access_log=None, print=None)


if __name__ == "__main__":
    main()
