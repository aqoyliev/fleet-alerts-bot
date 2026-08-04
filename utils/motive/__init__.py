import base64
import json
import logging
import urllib.parse
from datetime import datetime, timedelta

import aiohttp

logger = logging.getLogger(__name__)

MOTIVE_API = "https://api.gomotive.com/v1"
MOTIVE_API_V2 = "https://api.gomotive.com/v2"


def extract_event_id(mandrill_url: str) -> str | None:
    """Decode a Mandrill tracking URL and return the GoMotive event ID."""
    try:
        parsed = urllib.parse.urlparse(mandrill_url)
        p = urllib.parse.parse_qs(parsed.query).get("p", [None])[0]
        if not p:
            return None
        outer = json.loads(base64.b64decode(p + "==").decode())
        inner = json.loads(outer["p"])
        url = inner.get("url", "")
        # URL looks like: https://app.gomotive.com/#/safety/events/1507822145
        parts = url.rstrip("/").split("/")
        return parts[-1] if parts[-1].isdigit() else None
    except Exception as e:
        logger.warning(f"Could not extract event ID from URL: {e}")
        return None


async def crash_still_listed(api_key: str, event_id, occurred_at) -> bool | None:
    """Is this crash detection still in Motive's books as a crash?

    Motive fires the crash webhook the instant its detector trips, then runs a review.
    A detection the review rejects is WITHDRAWN — it disappears from
    /v2/driver_performance_events entirely, not merely reclassified. Measured over
    2026-07-29..08-01: of 62 crash webhooks we alerted on, 59 were gone from the API
    under every event type, 1 had become a near_miss, and the 2 that remained
    type='crash' were the single genuine collision (jrd unit 2460).

    So presence here is the classifier, and no payload field is: 'in_progress' vs
    resolved, secondary_behaviors, coaching_status and event_intensity were all
    identical between real and false detections.

    Returns True if still listed as a crash, False if withdrawn, and None if the
    lookup itself failed — the caller must treat None as 'unknown', not 'withdrawn'.
    """
    # start_date/end_date are whole days, so widen by one either side: a UTC event
    # near midnight would otherwise fall outside a same-day-only window.
    try:
        if isinstance(occurred_at, str):
            day = datetime.fromisoformat(occurred_at.replace("Z", "+00:00")).date()
        else:
            day = occurred_at.date()
    except Exception:
        day = datetime.utcnow().date()

    params = {
        "event_types": "crash",
        "start_date": (day - timedelta(days=1)).isoformat(),
        "end_date": (day + timedelta(days=1)).isoformat(),
        # Return everything rather than only what clears Motive's display thresholds,
        # so a real crash can never read as withdrawn just for being filtered out.
        "ignore_thresholds": "true",
        "per_page": "100",
    }
    target = str(event_id)
    headers = {"X-Api-Key": api_key}
    try:
        async with aiohttp.ClientSession() as s:
            page = 1
            while page <= 10:  # genuine crashes are rare; this never gets deep
                async with s.get(f"{MOTIVE_API_V2}/driver_performance_events",
                                 headers=headers, params={**params, "page_no": str(page)},
                                 timeout=aiohttp.ClientTimeout(total=30)) as r:
                    if r.status != 200:
                        logger.error(f"[motive] crash lookup HTTP {r.status} for event {event_id}")
                        return None
                    data = await r.json()
                batch = data.get("driver_performance_events") or []
                if not batch:
                    return False
                for row in batch:
                    ev = row.get("driver_performance_event") or row
                    if str(ev.get("id")) == target:
                        return True
                if page * int(params["per_page"]) >= (data.get("total") or 0):
                    return False
                page += 1
            return False
    except Exception as e:
        logger.error(f"[motive] crash lookup failed for event {event_id}: {e}")
        return None


class MotiveClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._headers = {"X-Api-Key": api_key, "Accept": "application/json"}

    async def get_event_video_url(self, event_id: str) -> str | None:
        """Fetch safety event by ID and return video clip URL if available."""
        try:
            async with aiohttp.ClientSession(headers=self._headers) as s:
                async with s.get(f"{MOTIVE_API}/safety_events/{event_id}") as r:
                    if r.status != 200:
                        logger.warning(f"Safety event {event_id} returned HTTP {r.status}")
                        return None
                    data = await r.json()
                    event = data.get("safety_event", data)
                    clip = event.get("video_clip") or {}
                    url = clip.get("url") or clip.get("download_url")
                    if url:
                        logger.info(f"Video clip found for event {event_id}")
                    return url
        except Exception as e:
            logger.error(f"get_event_video_url error: {e}")
            return None

    async def download_video(self, video_url: str) -> bytes | None:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(video_url) as resp:
                    if resp.status == 200:
                        return await resp.read()
                    logger.error(f"Video download failed: HTTP {resp.status}")
                    return None
        except Exception as e:
            logger.error(f"Video download error: {e}")
            return None
