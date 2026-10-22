"""Samsara safety events: the coaching state that gates driver cell-phone alerts.

A Samsara cell_phone event reaches the driver only while Samsara's own safety event for
it still says it needs attention (ALLOWED_COACHING_STATES). Anything else — coached,
dismissed, auto-dismissed, reviewed, an unknown value — and the driver is not told. The
safety event can show up in GET /fleet/safety-events a few minutes after the webhook, so
the lookup is retried on a schedule (wait_for_safety_event) rather than tried once.

Nothing here raises: a failed or empty lookup comes back as a result saying so, and the
caller fails closed on it.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import aiohttp

logger = logging.getLogger(__name__)

SAFETY_EVENTS_URL = "https://api.samsara.com/fleet/safety-events"
MOBILE_USAGE_LABEL = "mobileUsage"

# Samsara coachingState values for which the driver IS notified. Every other value,
# including ones Samsara adds later, means no driver notification.
ALLOWED_COACHING_STATES = frozenset({"needsReview", "needsCoaching", "needsRecognition"})

# Stand-ins written to coaching_state when Samsara gave no state. snake_case so they can
# never collide with Samsara's camelCase values; none is allowed, so such a row is
# neither notified, counted toward the daily ordinal, nor charged.
STATE_PENDING = "lookup_pending"    # the wait is running (left behind if a restart cut it)
STATE_NOT_FOUND = "not_found"       # no matching safety event after every attempt
STATE_LOOKUP_ERROR = "lookup_error" # the API failed, or there was nothing to look up with

# Seconds after the wait starts at which the lookup is tried (~8 minutes in all).
POLL_MARKS = (30, 60, 120, 240, 480)
# Pages followed per lookup at most; a 4-minute window for one truck is one page in practice.
_MAX_PAGES = 20

_timeout = aiohttp.ClientTimeout(total=30)
_sleep = asyncio.sleep  # patched by tests


def _session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(timeout=_timeout)


def is_allowed(state: str | None) -> bool:
    return state in ALLOWED_COACHING_STATES


def _rfc3339(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _as_utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


def _parse_time(raw) -> datetime | None:
    try:
        return _as_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    except Exception:
        return None


def _labels(item: dict) -> list[str]:
    out = []
    for entry in item.get("behaviorLabels") or []:
        if isinstance(entry, dict) and entry.get("label"):
            out.append(str(entry["label"]))
    return out


@dataclass
class SafetyLookup:
    """Outcome of looking for one safety event.
    status: "found" | "not_found" | "error"."""
    status: str
    event: dict | None = None
    attempts: int = 0
    labels_seen: list[str] = field(default_factory=list)

    @property
    def coaching_state(self) -> str | None:
        if self.event is None:
            return None
        state = self.event.get("coachingState")
        return str(state) if state not in (None, "") else None


async def _lookup(api_key: str, vehicle_id: str, at: datetime, label: str,
                  window_s: int) -> SafetyLookup:
    """One look-up: the item carrying `label` closest in time to `at`, within
    [at - window_s, at + window_s] for this vehicle."""
    at = _as_utc(at)
    params = {
        "startTime": _rfc3339(at - timedelta(seconds=window_s)),
        "endTime": _rfc3339(at + timedelta(seconds=window_s)),
        "vehicleIds": str(vehicle_id),
    }
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    best, best_gap = None, None
    seen: list[str] = []
    try:
        async with _session() as s:
            for _page in range(_MAX_PAGES):
                async with s.get(SAFETY_EVENTS_URL, params=dict(params), headers=headers) as r:
                    if r.status != 200:
                        body = (await r.text())[:300]
                        logger.warning(f"[samsara-safety] GET safety-events HTTP {r.status} "
                                       f"for vehicle {vehicle_id}: {body}")
                        return SafetyLookup("error", labels_seen=seen)
                    data = await r.json()
                for item in (data or {}).get("data") or []:
                    if not isinstance(item, dict):
                        continue
                    labels = _labels(item)
                    seen.extend(labels)
                    vid = str((item.get("vehicle") or {}).get("id") or "")
                    if vid and vid != str(vehicle_id):
                        continue
                    if label not in labels:
                        continue
                    ts = _parse_time(item.get("time"))
                    if ts is None:
                        continue
                    gap = abs(ts - at)
                    if best_gap is None or gap < best_gap:
                        best, best_gap = item, gap
                page = (data or {}).get("pagination") or {}
                cursor = page.get("endCursor")
                if not page.get("hasNextPage") or not cursor:
                    break
                params["after"] = cursor
            else:
                logger.warning(f"[samsara-safety] Stopped after {_MAX_PAGES} pages for vehicle "
                               f"{vehicle_id} — deciding on what was read")
    except Exception as e:
        logger.error(f"[samsara-safety] safety-events lookup failed for vehicle {vehicle_id}: "
                     f"{type(e).__name__}: {e}")
        return SafetyLookup("error", labels_seen=seen)
    if best is None:
        return SafetyLookup("not_found", labels_seen=seen)
    return SafetyLookup("found", event=best, labels_seen=seen)


async def find_safety_event(api_key: str, vehicle_id: str, at: datetime,
                            label: str = MOBILE_USAGE_LABEL, window_s: int = 120) -> dict | None:
    """The safety event of this vehicle carrying `label` closest in time to `at`
    (within ±window_s), or None. Never raises; the log says whether None means
    "not there" or "could not ask"."""
    if not api_key or not vehicle_id:
        logger.warning("[samsara-safety] No API key or vehicle id — cannot look up the safety event")
        return None
    result = await _lookup(api_key, vehicle_id, at, label, window_s)
    if result.status == "not_found":
        logger.info(f"[samsara-safety] No {label!r} safety event for vehicle {vehicle_id} near "
                    f"{_rfc3339(_as_utc(at))}; labels seen: {sorted(set(result.labels_seen)) or 'none'}")
    return result.event


async def wait_for_safety_event(api_key: str, vehicle_id: str, at: datetime,
                                label: str = MOBILE_USAGE_LABEL, window_s: int = 120,
                                marks: tuple[int, ...] | None = None) -> SafetyLookup:
    """Retry the lookup at each of `marks` (seconds after the call) until the safety
    event turns up. The result's status is the last attempt's: "found", "not_found"
    or "error". Never raises."""
    marks = tuple(POLL_MARKS if marks is None else marks)
    if not api_key or not vehicle_id:
        return SafetyLookup("error")
    elapsed, result, attempts = 0, SafetyLookup("not_found"), 0
    for mark in marks:
        if mark > elapsed:
            await _sleep(mark - elapsed)
            elapsed = mark
        attempts += 1
        try:
            result = await _lookup(api_key, vehicle_id, at, label, window_s)
        except Exception as e:  # _lookup does not raise; belt and braces
            logger.error(f"[samsara-safety] lookup crashed: {e}", exc_info=True)
            result = SafetyLookup("error")
        result.attempts = attempts
        if result.status == "found":
            logger.info(f"[samsara-safety] Vehicle {vehicle_id}: safety event "
                        f"{result.event.get('id')} found at +{elapsed}s "
                        f"(coachingState={result.coaching_state})")
            return result
        more = f" — retrying at +{marks[attempts]}s" if attempts < len(marks) else ""
        if result.status == "error":
            logger.warning(f"[samsara-safety] Vehicle {vehicle_id}: lookup error at "
                           f"+{elapsed}s{more}")
        else:
            logger.info(f"[samsara-safety] Vehicle {vehicle_id}: no {label!r} safety event yet at "
                        f"+{elapsed}s (labels seen: "
                        f"{sorted(set(result.labels_seen)) or 'none'}){more}")
    return result
