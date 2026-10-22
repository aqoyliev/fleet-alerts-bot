"""Who was driving a truck when an event happened.

Order:
  1. Vitality ELD, when the company has a vitality_company_key and VITALITY_PROVIDER_KEY
     is set: the driver assigned to the truck in the current fleet snapshot, accepted
     only if the truck reported within 30 minutes of the event (the snapshot cannot say
     who was driving at any other time). Duty status from latest-driver-status.
  2. The driver carried on the event payload (and the Motive driver id, when present).

The email source (QuickManage) is not wired yet: email stays None.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from data import config
from utils import vitality
from utils.db_api.companies import get_vitality_company_key

logger = logging.getLogger(__name__)

# The Vitality part as a whole (DB key lookup + snapshot + status + name) gives up after
# this, so resolve_driver stays well inside ~10 s.
VITALITY_TIMEOUT = 8.0
# The truck's last report must be this close to the event for its current driver to be
# taken as the driver at the time of the event.
MAX_REPORT_GAP = timedelta(minutes=30)
# Vitality outages are logged at WARNING at most this often per company (and kind).
WARN_INTERVAL = 3600.0

_ON_DUTY = {"DS_D", "DS_ON", "DR_IND_YM"}
_OFF_DUTY = {"DS_OFF", "DS_SB", "DR_IND_PC"}

_warned_at: dict[tuple[str, str], float] = {}


@dataclass
class DriverInfo:
    name: str = ""          # display name, "" if unknown
    ref: str | None = None  # stable driver id (provider/QuickManage), None if unknown
    email: str | None = None
    source: str = "unknown" # "event" | "vitality" | "samsara" | "motive" | "quickmanage" | "unknown"
    on_duty: bool | None = None  # None = could not determine


def _event_driver(event: dict) -> dict:
    info = event.get("driver") or event.get("current_driver") or {}
    return info if isinstance(info, dict) else {}


def _name_from_event(event: dict) -> str:
    """The same fields _format_event reads, minus its "Unidentified" placeholder."""
    info = _event_driver(event)
    full = (
        f"{info.get('first_name', '') or info.get('FirstName', '')} "
        f"{info.get('last_name', '') or info.get('LastName', '')}"
    ).strip()
    return str(info.get("name") or full or info.get("username") or "").strip()


def _from_event(event: dict) -> DriverInfo:
    """The driver on the payload. A Motive event's driver id (driver.id, else the
    top-level driver_id real payloads carry) becomes the ref; Samsara events carry no
    driver id here."""
    name = _name_from_event(event)
    ref = None
    if event.get("_source") != "samsara":
        did = _event_driver(event).get("id")
        if did in (None, "", 0):
            did = event.get("driver_id")
        if did not in (None, "", 0):
            ref = f"motive:{did}"
    return DriverInfo(name=name, ref=ref, source="event" if (name or ref) else "unknown")


def duty_from_status(code: str | None) -> bool | None:
    """latest-driver-status current_status -> on duty? None when unknown."""
    code = str(code or "").strip().upper()
    if code in _ON_DUTY:
        return True
    if code in _OFF_DUTY:
        return False
    return None


def _same_name(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def _warn_throttled(company_slug: str, kind: str, message: str) -> None:
    now = time.monotonic()
    last = _warned_at.get((company_slug, kind))
    if last is not None and now - last < WARN_INTERVAL:
        logger.debug(message)
        return
    _warned_at[(company_slug, kind)] = now
    logger.warning(message + " (logged at most once an hour)")


def _as_utc(ts: datetime | None) -> datetime:
    ts = ts or datetime.now(timezone.utc)
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts


async def _from_vitality(company_slug: str, vehicle: str,
                         occurred_at: datetime | None) -> DriverInfo | None:
    """The Vitality driver, or None when Vitality is not set up or cannot tell."""
    if not vitality.is_configured():
        return None
    company_key = await get_vitality_company_key(company_slug)
    if not company_key:
        return None
    try:
        units = await vitality.fleet_snapshot(company_key)
    except vitality.VitalityUnavailable as e:
        _warn_throttled(company_slug, "unavailable",
                        f"[driver_lookup] Vitality unavailable for {company_slug} ({e}) — "
                        f"using the event's driver")
        return None

    unit = vitality.lookup(units, vehicle)
    if unit is None:
        logger.info(f"[driver_lookup] {company_slug}/{vehicle} not in the Vitality fleet")
        return None
    if not unit.driver_id:
        logger.info(f"[driver_lookup] {company_slug}/{vehicle}: no driver assigned in Vitality")
        return None
    at = _as_utc(occurred_at)
    if unit.timestamp is None or abs(unit.timestamp - at) > MAX_REPORT_GAP:
        logger.info(f"[driver_lookup] {company_slug}/{vehicle}: Vitality last report "
                    f"{unit.timestamp.isoformat() if unit.timestamp else 'unknown'} is too far "
                    f"from the event ({at.isoformat()}) to tell who was driving")
        return None

    driver_id = unit.driver_id
    status, username = None, ""
    try:
        item = await vitality.driver_status(company_key, driver_id)
        if item:
            status = item.get("current_status")
            username = " ".join(str(item.get("username") or "").split())
    except vitality.VitalityError as e:
        logger.warning(f"[driver_lookup] Vitality driver status failed for "
                       f"{company_slug}/{vehicle}: {e}")
    name = ""
    try:
        name = await vitality.driver_name(company_key, driver_id)
    except vitality.VitalityError as e:
        logger.warning(f"[driver_lookup] Vitality driver name failed for "
                       f"{company_slug}/{vehicle}: {e}")
    return DriverInfo(name=name or username, ref=f"vitality:{driver_id}", source="vitality",
                      on_duty=duty_from_status(status))


async def resolve_driver(company_slug: str, event: dict, vehicle: str,
                         occurred_at: datetime | None) -> DriverInfo:
    """Best-effort driver for this event. Never raises: anything unknown is left at
    its DriverInfo default."""
    try:
        from_event = _from_event(event or {})
    except Exception as e:
        logger.warning(f"[driver_lookup] Could not read driver for {company_slug}/{vehicle}: {e}")
        from_event = DriverInfo()

    from_vitality = None
    try:
        from_vitality = await asyncio.wait_for(
            _from_vitality(company_slug, vehicle, occurred_at), VITALITY_TIMEOUT)
    except asyncio.TimeoutError:
        _warn_throttled(company_slug, "error",
                        f"[driver_lookup] Vitality lookup for {company_slug}/{vehicle} timed out "
                        f"after {VITALITY_TIMEOUT:g}s — using the event's driver")
    except Exception as e:
        _warn_throttled(company_slug, "error",
                        f"[driver_lookup] Vitality lookup failed for {company_slug}/{vehicle}: "
                        f"{type(e).__name__}: {e} — using the event's driver")

    if from_vitality is None:
        return from_event
    try:
        if from_vitality.name and from_event.name and not _same_name(from_vitality.name,
                                                                     from_event.name):
            logger.info(f"[driver_lookup] Driver mismatch for {company_slug}/{vehicle}: "
                        f"Vitality says {from_vitality.name!r} ({from_vitality.ref}), "
                        f"the event says {from_event.name!r} — using Vitality's")
        if not from_vitality.name:
            from_vitality.name = from_event.name
    except Exception:
        pass
    return from_vitality
