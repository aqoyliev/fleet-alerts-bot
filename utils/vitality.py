"""Client for the Vitality ELD fleet API (DriveHOS "Global Integration API v2").

Used by utils.driver_lookup to find who is driving a truck when a cell_phone event
comes in. Modelled on the ELD anomaly bot's client (nexus-hos utils/eld/vitalityeld.py).

Every request carries two keys:
  * ``X-API-Provider-Key`` -- this bot's key (config.VITALITY_PROVIDER_KEY). It is
    rate-limited and shared with the ELD anomaly bot, which polls every 5 minutes, so
    everything here is cached and retried at most once.
  * ``X-API-Company-Key``  -- the carrier's key (companies.vitality_company_key).

``latest-vehicle-status`` is a CURRENT snapshot (who is assigned to the truck now, and
when it last reported); there is no history. The caller decides whether the snapshot is
fresh enough to say who was driving at the time of the event.

Some carriers' keys are valid but their data stopped flowing (``data: null``), and some
get HTTP 403. Both raise VitalityUnavailable, which is cached for a few minutes so a
burst of events does not keep spending the provider key on a dead feed.
"""
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp

from data import config
from utils.unit_key import normalize_unit, unit_candidates

logger = logging.getLogger(__name__)

# latest-vehicle-status caps ``limit`` at 200, drivers at 1,000. The page cap guards
# against a misbehaving ``total_pages``.
_VEHICLE_PAGE_SIZE = 200
_DRIVER_PAGE_SIZE = 1000
_MAX_PAGES = 20

SNAPSHOT_TTL = 45.0        # several cell_phone events in a burst share one snapshot
ERROR_TTL = 45.0           # a failed snapshot is not retried sooner than this
UNAVAILABLE_TTL = 300.0    # data null / 403: the feed is down, look again in 5 min
STATUS_TTL = 45.0
ROSTER_TTL = 3600.0
ROSTER_RETRY = 300.0       # a failed roster fetch is retried after this

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=6, connect=3)
_RETRY_DELAY = 1.0


class VitalityError(Exception):
    """A request failed (HTTP error, bad envelope, network)."""


class VitalityUnavailable(VitalityError):
    """Vitality has nothing for this company: data null/empty, or the key is refused."""


@dataclass
class VitalityUnit:
    number: str
    driver_id: str | None
    timestamp: datetime | None  # last report, tz-aware UTC
    status: str | None = None   # IN_MOTION / STATIONARY / OFFLINE


# ── HTTP ─────────────────────────────────────────────────────────────────────────

_http_session: aiohttp.ClientSession | None = None


def _get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def close() -> None:
    """Close the shared HTTP session (app shutdown)."""
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
    _http_session = None


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def is_configured() -> bool:
    return bool(config.VITALITY_PROVIDER_KEY)


def _headers(company_key: str, provider_key: str | None = None) -> dict:
    return {
        "accept": "application/json",
        "X-API-Provider-Key": provider_key or config.VITALITY_PROVIDER_KEY,
        "X-API-Company-Key": company_key,
    }


# Which provider key last worked for a company key (Vitality vs Vitality Plus).
_provider_for: dict[str, str] = {}


def _provider_keys(company_key: str) -> list[str]:
    """Provider keys to try, the one that last worked for this carrier first."""
    keys = [k for k in (config.VITALITY_PROVIDER_KEY, config.VITALITY_PLUS_PROVIDER_KEY) if k]
    keys = list(dict.fromkeys(keys))
    known = _provider_for.get(company_key)
    if known in keys:
        keys.remove(known)
        keys.insert(0, known)
    return keys


async def _get_any(path: str, company_key: str, params: dict | None = None) -> dict:
    """_get with the Vitality Plus fallback: carriers that moved to Plus keep their
    company key, but the first provider key answers 403 or an empty first page for them
    — then the other provider key is tried, and whichever worked is remembered."""
    keys = _provider_keys(company_key)
    last: Exception | None = None
    for i, provider in enumerate(keys):
        try:
            env = await _get(path, company_key, params, provider)
        except VitalityUnavailable as e:
            last = e
            continue
        first_page = (params or {}).get("page", 1) == 1
        if env.get("data") in (None, []) and first_page and "page" in (params or {}) \
                and i < len(keys) - 1:
            last = VitalityUnavailable(f"{path} returned no data")
            continue
        if _provider_for.get(company_key) != provider:
            _provider_for[company_key] = provider
            if i:
                logger.info(f"[vitality] {path}: using the "
                            f"{'Plus' if provider == config.VITALITY_PLUS_PROVIDER_KEY else 'primary'} "
                            f"provider key for this carrier")
        return env
    raise last or VitalityUnavailable(f"{path}: no provider key")


async def _get(path: str, company_key: str, params: dict | None = None,
               provider_key: str | None = None) -> dict:
    """One GET, returning the success envelope. At most one retry, and only on 5xx or
    a network error/timeout. 401/403 raise VitalityUnavailable; anything else that is
    not a success raises VitalityError."""
    url = config.VITALITY_BASE_URL.rstrip("/") + path
    for attempt in (1, 2):
        retry = attempt == 1
        try:
            session = _get_http_session()
            async with session.get(url, headers=_headers(company_key, provider_key), params=params,
                                   timeout=_REQUEST_TIMEOUT) as resp:
                status = resp.status
                if status in (401, 403):
                    raise VitalityUnavailable(f"{path} HTTP {status}")
                if status != 200:
                    # Truncated: error pages can be full HTML.
                    body = " ".join((await resp.text()).split())[:160]
                    if status >= 500 and retry:
                        logger.info(f"[vitality] {path} HTTP {status}, retrying once")
                        await _sleep(_RETRY_DELAY)
                        continue
                    raise VitalityError(f"{path} HTTP {status}: {body}")
                data = await resp.json(content_type=None)
        except VitalityError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if retry:
                logger.info(f"[vitality] {path} {type(e).__name__}: {e}, retrying once")
                await _sleep(_RETRY_DELAY)
                continue
            raise VitalityError(f"{path} {type(e).__name__}: {e}") from e
        except ValueError as e:  # body is not JSON
            raise VitalityError(f"{path} bad JSON: {e}") from e

        if not isinstance(data, dict):
            raise VitalityError(f"{path} unexpected body: {type(data).__name__}")
        if data.get("status_code") in (401, 403):
            raise VitalityUnavailable(f"{path} status_code {data.get('status_code')}")
        if data.get("description") != "success":
            raise VitalityError(f"{path} error: {str(data.get('description'))[:160]}")
        return data
    raise VitalityError(f"{path} failed")  # not reached


async def _fetch_pages(path: str, company_key: str, page_size: int,
                       extra: dict | None = None) -> list[dict]:
    """``data`` across every page of a page/limit endpoint. ``data: null`` on the first
    page raises VitalityUnavailable (the carrier's feed stopped)."""
    items: list[dict] = []
    page = 1
    while page <= _MAX_PAGES:
        params = {"limit": page_size, "page": page, **(extra or {})}
        env = await _get_any(path, company_key, params)
        data = env.get("data")
        if data is None:
            if page == 1:
                raise VitalityUnavailable(f"{path} returned data: null")
            break
        if isinstance(data, dict):
            data = [data]
        items.extend(d for d in data if isinstance(d, dict))
        try:
            total_pages = int(env.get("total_pages") or 1)
        except (TypeError, ValueError):
            total_pages = 1
        if page >= total_pages:
            break
        page += 1
    return items


def _parse_time(value) -> datetime | None:
    """"2026-09-17T15:03:40Z" -> tz-aware UTC (a naive value is read as UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        logger.warning(f"[vitality] Could not parse time {value!r}")
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clean_name(value) -> str:
    """Roster names carry stray spaces ("ELISE ", "PRINCE  JAMES"); collapse them."""
    return " ".join(str(value or "").split())


def _row_name(row: dict) -> str:
    full = _clean_name(f"{row.get('first_name') or ''} {row.get('last_name') or ''}")
    return full or _clean_name(row.get("username"))


# ── Fleet snapshot ───────────────────────────────────────────────────────────────

# company_key -> (expires_at monotonic, units or None, exception or None)
_snapshots: dict[str, tuple[float, dict | None, Exception | None]] = {}
_snapshot_locks: dict[str, asyncio.Lock] = {}


def _raise_cached(exc: Exception | None) -> None:
    """Re-raise a cached failure as a fresh exception (no growing traceback)."""
    if exc is not None:
        raise type(exc)(str(exc))


def _index_units(raw_units: list[dict]) -> dict[str, VitalityUnit]:
    units: dict[str, VitalityUnit] = {}
    for raw in raw_units:
        number = str(raw.get("number") or "").strip()
        key = normalize_unit(number)
        if not key:
            continue
        unit = VitalityUnit(
            number=number,
            driver_id=(str(raw["driver_id"]).strip() or None) if raw.get("driver_id") else None,
            timestamp=_parse_time(raw.get("timestamp")),
            status=str(raw["status"]).upper() if raw.get("status") else None,
        )
        units.setdefault(key, unit)
        units.setdefault(key.lstrip("0") or key, unit)
    return units


async def fleet_snapshot(company_key: str) -> dict[str, VitalityUnit]:
    """The carrier's latest-vehicle-status, keyed by normalize_unit(number) and that key
    without leading zeros; look up with :func:`lookup`. Cached ~45 s per company key;
    concurrent callers share one fetch. A failure is cached too (VitalityUnavailable for
    5 min, other errors for 45 s) and re-raised from the cache."""
    now = time.monotonic()
    cached = _snapshots.get(company_key)
    if cached and now < cached[0]:
        _raise_cached(cached[2])
        return cached[1]
    lock = _snapshot_locks.setdefault(company_key, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _snapshots.get(company_key)
        if cached and now < cached[0]:
            _raise_cached(cached[2])
            return cached[1]
        try:
            raw = await _fetch_pages("/v2/latest-vehicle-status", company_key, _VEHICLE_PAGE_SIZE)
            if not raw:
                raise VitalityUnavailable("/v2/latest-vehicle-status returned no vehicles")
        except VitalityUnavailable as e:
            _snapshots[company_key] = (time.monotonic() + UNAVAILABLE_TTL, None, e)
            raise
        except VitalityError as e:
            _snapshots[company_key] = (time.monotonic() + ERROR_TTL, None, e)
            raise
        units = _index_units(raw)
        _snapshots[company_key] = (time.monotonic() + SNAPSHOT_TTL, units, None)
        return units


def lookup(units: dict[str, VitalityUnit], unit_number: str | None) -> VitalityUnit | None:
    """Find a truck (as Motive/Samsara spell it) in a :func:`fleet_snapshot`. Tries the
    full name first, then the bare unit before a suffix like "O/O" (unit_candidates)."""
    for key in unit_candidates(unit_number):
        unit = units.get(key) or units.get(key.lstrip("0") or key)
        if unit:
            return unit
    return None


# ── Driver status ────────────────────────────────────────────────────────────────

# (company_key, driver_id) -> (expires_at, item or None)
_statuses: dict[tuple[str, str], tuple[float, dict | None]] = {}


async def driver_status(company_key: str, driver_id: str) -> dict | None:
    """The driver's latest-driver-status item ({current_status, username, drive, ...}),
    or None if Vitality has none. Cached ~45 s. Raises VitalityError on failure."""
    ck = (company_key, driver_id)
    cached = _statuses.get(ck)
    if cached and time.monotonic() < cached[0]:
        return cached[1]
    env = await _get_any("/v2/latest-driver-status", company_key, {"driver_id": driver_id})
    data = env.get("data")
    items = [data] if isinstance(data, dict) else [d for d in (data or []) if isinstance(d, dict)]
    item = next((d for d in items if str(d.get("driver_id") or "") == driver_id), None)
    if item is None and len(items) == 1 and not items[0].get("driver_id"):
        item = items[0]
    _statuses[ck] = (time.monotonic() + STATUS_TTL, item)
    return item


# ── Driver names ─────────────────────────────────────────────────────────────────

# company_key -> (expires_at, {driver_id: name})
_rosters: dict[str, tuple[float, dict[str, str]]] = {}


async def _roster(company_key: str) -> dict[str, str]:
    cached = _rosters.get(company_key)
    if cached and time.monotonic() < cached[0]:
        return cached[1]
    try:
        rows = await _fetch_pages("/v2/drivers", company_key, _DRIVER_PAGE_SIZE)
    except VitalityError as e:
        logger.warning(f"[vitality] Driver roster fetch failed: {e}")
        names = cached[1] if cached else {}
        _rosters[company_key] = (time.monotonic() + ROSTER_RETRY, names)
        return names
    names = {}
    for r in rows:
        did = str(r.get("driver_id") or "").strip()
        name = _row_name(r)
        if did and name:
            names[did] = name
    _rosters[company_key] = (time.monotonic() + ROSTER_TTL, names)
    return names


async def driver_name(company_key: str, driver_id: str) -> str:
    """The driver's name ("First Last", spaces collapsed), "" if unknown. The whole
    roster is fetched once an hour; a driver missing from it is fetched alone
    (``/v2/driver/{id}``) and remembered. Raises VitalityError only from that fetch."""
    names = await _roster(company_key)
    if driver_id in names:
        return names[driver_id]
    env = await _get_any(f"/v2/driver/{driver_id}", company_key)
    data = env.get("data")
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict)), None)
    name = _row_name(data) if isinstance(data, dict) else ""
    names[driver_id] = name  # "" too: an unnamed driver is not fetched again this hour
    return name


def _reset_caches() -> None:
    """Tests only."""
    _snapshots.clear()
    _snapshot_locks.clear()
    _statuses.clear()
    _rosters.clear()
    _provider_for.clear()
