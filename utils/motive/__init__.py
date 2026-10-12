import base64
import json
import logging
import urllib.parse
from datetime import datetime, timedelta

import aiohttp

logger = logging.getLogger(__name__)

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


async def find_performance_event(api_key: str, event_id, occurred_at,
                                 event_types: str) -> tuple[str, dict | None]:
    """Look one driver-performance event up in Motive's books.

    Returns ("found", <the event row>), ("absent", None) when the window held events but
    not this one, or ("error", None) when the lookup itself could not be completed. The
    three are kept apart because the callers mean different things by each: absence is a
    verdict, an error is no verdict at all.

    `event_types` narrows the query to the behaviour Motive filed the event under -- its
    own payload `type`, which is not always the type the bot routes on (a critical
    hard_brake is a crash to us and a hard_brake to Motive).
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
        "event_types": event_types,
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
                        logger.error(f"[motive] event lookup HTTP {r.status} for {event_id}")
                        return "error", None
                    data = await r.json()
                batch = data.get("driver_performance_events") or []
                if not batch:
                    return "absent", None
                for row in batch:
                    ev = row.get("driver_performance_event") or row
                    if str(ev.get("id")) == target:
                        return "found", ev
                if page * int(params["per_page"]) >= (data.get("total") or 0):
                    return "absent", None
                page += 1
            return "absent", None
    except Exception as e:
        logger.error(f"[motive] event lookup failed for {event_id}: {e}")
        return "error", None


async def crash_still_listed(api_key: str, event_id, occurred_at) -> bool | None:
    """Is this crash detection still in Motive's books as a crash?

    Motive fires the crash webhook the instant its detector trips, then runs a review.
    A detection the review rejects is WITHDRAWN -- it disappears from
    /v2/driver_performance_events entirely, not merely reclassified. Measured over
    2026-07-29..08-01: of 62 crash webhooks we alerted on, 59 were gone from the API
    under every event type, 1 had become a near_miss, and the 2 that remained
    type='crash' were the single genuine collision (jrd unit 2460).

    So presence here is the classifier, and no payload field is: 'in_progress' vs
    resolved, secondary_behaviors, coaching_status and event_intensity were all
    identical between real and false detections.

    Returns True if still listed as a crash, False if withdrawn, and None if the
    lookup itself failed -- the caller must treat None as 'unknown', not 'withdrawn'.
    """
    status, _ = await find_performance_event(api_key, event_id, occurred_at, "crash")
    if status == "error":
        return None
    return status == "found"

