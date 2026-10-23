"""Hand a cell_phone event over to the driver-alerts service.

web does the company alerts only. Telling the driver (Vitality, the Samsara
coaching-state wait, the driver bot, the email — utils/driver_alerts.py) runs in a
separate Railway service, driver_service.py, which shares the Postgres. web POSTs each
cell_phone event to it over Railway's private network and does not wait for the
answer: _handle_event schedules forward_cell_phone_event as a background task.

The payload is JSON: the normalized event plus what web already worked out (the
company's alert text, the media URLs, the event id as stored on the violation). Media
bytes are not sent; the receiver downloads the URLs itself. See build_payload.

forward_cell_phone_event never raises and never blocks the company alerts: a bounded
number of quick retries on a network error or a 5xx, then a logged give-up. The shared
secret travels in the X-Forward-Secret header and is never logged.

DRIVER_ALERTS_FORWARD_URL empty = the feature is off (nothing is sent).
"""
import asyncio
import json
import logging
from datetime import datetime, timezone

import aiohttp

from data import config

logger = logging.getLogger(__name__)

SECRET_HEADER = "X-Forward-Secret"
_timeout = aiohttp.ClientTimeout(total=5)
# Retries after the first attempt, and the pause before each of them.
_RETRIES = 2
_BACKOFF = (0.5, 1.5)
_sleep = asyncio.sleep  # patched by tests

# One lazily-created session, like webhook_handler's; closed on shutdown via close().
_http_session: aiohttp.ClientSession | None = None


def _get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def close() -> None:
    """Close the forwarding session (app shutdown)."""
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
    _http_session = None


def _url() -> str:
    return (config.DRIVER_ALERTS_FORWARD_URL or "").strip()


def is_enabled() -> bool:
    """Whether cell_phone events are forwarded to the driver-alerts service."""
    return bool(_url())


def _iso_utc(ts: datetime | None) -> str | None:
    """ISO-8601 in UTC. A naive timestamp is read as UTC, as utils.driver_alerts does."""
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def build_payload(*, company_slug: str, alert_source: str, event_id: int | None,
                  raw_event_id, vehicle: str, occurred_at: datetime | None, base_text: str,
                  video_urls: list[str] | None, image_urls: list[str] | None,
                  event: dict) -> dict:
    """The forwarded body. Round-tripped through json with default=str, so whatever
    the event dict holds, the result is plain JSON (a stray datetime becomes a string)."""
    payload = {
        "company_slug": company_slug,
        "alert_source": alert_source,
        "event_id": event_id,
        "raw_event_id": None if raw_event_id is None else str(raw_event_id),
        "vehicle": vehicle or "",
        "occurred_at": _iso_utc(occurred_at),
        "base_text": base_text or "",
        "video_urls": list(video_urls or []),
        "image_urls": list(image_urls or []),
        "event": event or {},
    }
    return json.loads(json.dumps(payload, default=str))


async def forward_cell_phone_event(payload: dict) -> None:
    """POST the payload to DRIVER_ALERTS_FORWARD_URL. No-op when it is unset; never raises."""
    try:
        url = _url()
        if not url:
            return
        what = (f"event {payload.get('event_id')} "
                f"({payload.get('company_slug')}/{payload.get('vehicle')})")
        body = json.dumps(payload, default=str)
        headers = {
            "Content-Type": "application/json",
            SECRET_HEADER: config.DRIVER_ALERTS_FORWARD_SECRET or "",
        }
        for attempt in range(_RETRIES + 1):
            try:
                session = _get_http_session()
                async with session.post(url, data=body, headers=headers, timeout=_timeout) as r:
                    if r.status < 300:
                        logger.info(f"[driver-forward] Forwarded {what} → HTTP {r.status}")
                        return
                    if r.status < 500:
                        # 401 (secret mismatch) or 400 (rejected payload): a retry would
                        # get the same answer.
                        logger.error(f"[driver-forward] driver-alerts refused {what}: "
                                     f"HTTP {r.status} — not retrying")
                        return
                    reason = f"HTTP {r.status}"
            except Exception as e:
                reason = f"{type(e).__name__}: {e}"
            if attempt < _RETRIES:
                logger.warning(f"[driver-forward] Forward of {what} failed ({reason}), "
                               f"retry {attempt + 1}/{_RETRIES}")
                await _sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
            else:
                logger.error(f"[driver-forward] Gave up forwarding {what} after "
                             f"{_RETRIES + 1} attempts: {reason}")
    except Exception as e:
        logger.error(f"[driver-forward] Forward failed: {type(e).__name__}: {e}")
