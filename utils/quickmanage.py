"""Client for the QuickManage TMS API (https://api.quickmanage.com).

The source of a truck's drivers and their emails for the driver alerts. Not wired into
utils.driver_alerts yet; scripts/qm_audit.py compares it with Vitality and pti first.

Auth: POST /auth/token with the carrier's client id/secret returns a bearer token with an
expiry. A token is bound to ONE carrier, so credentials are per company
(QUICKMANAGE_CLIENT_ID_<SLUG> / QUICKMANAGE_CLIENT_SECRET_<SLUG>, data.config). The token
is cached per company and renewed ~5 minutes before it expires, or at once when a request
answers 401 (that request is then repeated once). Neither the secret nor a token is ever
logged.

Every response is an envelope ``{"error-fields": ..., "message": ..., "data": ...}``.

Failures:
  * QuickManageUnavailable -- QuickManage cannot give an answer right now: the company is
    not configured, the credentials are refused (QuickManageAuthError, 401/403), or it is
    down (network error, timeout, 429/5xx still failing after the retries).
  * QuickManageError -- it answered, but not usefully: another 4xx, a malformed body.
    Unavailable is a subclass, so ``except QuickManageError`` catches both.

Requests time out after 8 s and are retried (twice, with a short backoff) on a network
error, 429 or 5xx. A failed login is remembered briefly (5 min for refused credentials,
30 s for an outage) so a burst of events does not hammer /auth/token.

truck_drivers() finds a truck by unit number (the search is partial, so the results are
filtered to the exact normalized unit, utils.unit_key) and returns its ACTIVE drivers.
Results are cached 10 min per (company, unit); failures are not cached.
"""
import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import aiohttp

from data import config
from utils.unit_key import normalize_unit, unit_candidates

logger = logging.getLogger(__name__)

REASON_NO_TRUCK = "no_truck"
REASON_AMBIGUOUS = "ambiguous"

TOKEN_REFRESH_MARGIN = 300.0   # a token is renewed this long before it expires
TOKEN_DEFAULT_LIFETIME = 600.0  # when the expiry cannot be read
AUTH_REFUSED_TTL = 300.0       # refused credentials are not retried sooner than this
AUTH_OUTAGE_TTL = 30.0         # nor is a login that failed because QuickManage is down
TRUCK_TTL = 600.0              # truck_drivers() result, per (company, unit)

_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8, connect=4)
_RETRY_DELAYS = (0.5, 1.5)     # before the 2nd and 3rd attempt
_MAX_RETRY_AFTER = 5.0

_SEARCH_PAGE_SIZE = 50
_MAX_PAGES = 5                 # guards against a misbehaving ``count``
_DEAD_TRUCK_STATUSES = {"sold", "total_loss"}

_NAME_TOKEN_RE = re.compile(r"[^\W\d_]{2,}")   # as utils.driver_alerts (not imported: cycle)


class QuickManageError(Exception):
    """QuickManage answered, but not usefully (4xx, malformed body)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class QuickManageUnavailable(QuickManageError):
    """QuickManage cannot answer: not configured, or down after the retries."""


class QuickManageAuthError(QuickManageUnavailable):
    """The company's credentials (or its token, even after a re-login) are refused."""


@dataclass(frozen=True)
class QMDriver:
    id: str
    name: str               # "first last", spaces collapsed ("" if both are empty)
    email: str | None
    phone: str | None
    status: str | None      # active | invited | terminated (lower-cased)
    truck_unit: str | None = None   # assigned_truck.number, when QuickManage says


@dataclass(frozen=True)
class QMTruck:
    id: str
    unit: str
    status: str | None = None   # active | unassigned | sold | total_loss


@dataclass(frozen=True)
class TruckDrivers:
    """The answer for one unit: `truck` and its active `drivers` when exactly one truck
    has that unit, else `reason` (REASON_*). `matches` lists the trucks an ambiguous
    unit hit."""
    unit: str
    truck: QMTruck | None = None
    drivers: tuple[QMDriver, ...] = ()
    reason: str | None = None
    matches: tuple[QMTruck, ...] = ()

    @property
    def found(self) -> bool:
        return self.truck is not None


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


def _monotonic() -> float:
    return time.monotonic()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _key(company_slug: str | None) -> str:
    return (company_slug or "").strip().lower()


def is_configured(company_slug: str | None) -> bool:
    """Whether the company has both QuickManage credentials set."""
    return bool(config.quickmanage_client_id(company_slug)
                and config.quickmanage_client_secret(company_slug))


def _short(value, limit: int = 160) -> str:
    """One line, truncated: error pages can be full HTML."""
    return " ".join(str(value or "").split())[:limit]


def _retry_delay(attempt: int, resp) -> float:
    delay = _RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)]
    try:
        after = float(resp.headers.get("Retry-After")) if resp is not None else 0.0
    except (TypeError, ValueError, AttributeError):
        after = 0.0
    return max(delay, min(after, _MAX_RETRY_AFTER))


async def _send(method: str, path: str, headers: dict, body: dict | None) -> tuple[int, object]:
    """One logical request: (HTTP status, parsed JSON or None). A network error, timeout,
    429 or 5xx is retried (3 attempts in all); when still failing it raises
    QuickManageUnavailable. Every other status is returned for the caller to judge."""
    url = config.QUICKMANAGE_BASE_URL.rstrip("/") + path
    attempts = len(_RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        last = attempt == attempts
        try:
            session = _get_http_session()
            async with session.request(method, url, headers=headers, json=body,
                                       timeout=_REQUEST_TIMEOUT) as resp:
                status = resp.status
                retryable = status == 429 or status >= 500
                if retryable and not last:
                    logger.info(f"[quickmanage] {method} {path} HTTP {status}, retrying "
                                f"({attempt}/{attempts - 1})")
                    delay = _retry_delay(attempt, resp)
                else:
                    delay = None
                    try:
                        data = await resp.json(content_type=None)
                    except ValueError:
                        data = None
                    if retryable:
                        message = data.get("message") if isinstance(data, dict) else ""
                        raise QuickManageUnavailable(
                            f"{method} {path} HTTP {status} after {attempts} attempts: "
                            f"{_short(message)}", status)
                    return status, data
        except QuickManageError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if last:
                raise QuickManageUnavailable(
                    f"{method} {path} {type(e).__name__}: {e} (after {attempts} attempts)") from e
            logger.info(f"[quickmanage] {method} {path} {type(e).__name__}: {e}, retrying "
                        f"({attempt}/{attempts - 1})")
            delay = _retry_delay(attempt, None)
        await _sleep(delay)
    raise QuickManageUnavailable(f"{method} {path} failed")  # not reached


def _fail(method: str, path: str, status: int, data) -> QuickManageError:
    message = data.get("message") if isinstance(data, dict) else ""
    return QuickManageError(f"{method} {path} HTTP {status}: {_short(message)}", status)


# ── Token ────────────────────────────────────────────────────────────────────────

@dataclass
class _Token:
    value: str
    refresh_at: float   # monotonic


_tokens: dict[str, _Token] = {}
# company -> (until monotonic, exception): a recent login failure, re-raised meanwhile
_auth_failures: dict[str, tuple[float, QuickManageUnavailable]] = {}
_token_locks: dict[str, asyncio.Lock] = {}


def _parse_expire(value) -> datetime | None:
    """"2025-05-09T19:59:06-04:00" -> tz-aware (a naive value is read as UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("[quickmanage] Could not parse the token expiry")
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _lifetime(expire) -> float:
    """Seconds to keep a token before renewing it: until MARGIN before its expiry (but
    at least half of what it has left, so a short-lived token is not renewed on every
    request)."""
    dt = _parse_expire(expire)
    if dt is None:
        return TOKEN_DEFAULT_LIFETIME
    remaining = (dt - _utcnow()).total_seconds()
    if remaining <= 0:
        return 0.0
    return max(remaining - TOKEN_REFRESH_MARGIN, remaining / 2)


async def _authenticate(company_slug: str) -> _Token:
    client_id = config.quickmanage_client_id(company_slug)
    client_secret = config.quickmanage_client_secret(company_slug)
    if not (client_id and client_secret):
        raise QuickManageUnavailable(f"QuickManage is not configured for {company_slug!r}")
    status, data = await _send("POST", "/auth/token", {"accept": "application/json"},
                               {"client_id": client_id, "client_secret": client_secret})
    if status in (400, 401, 403):
        raise QuickManageAuthError(
            f"POST /auth/token HTTP {status}: credentials refused for {company_slug!r} "
            f"({_short(data.get('message') if isinstance(data, dict) else '', 80)})", status)
    if status != 200:
        raise _fail("POST", "/auth/token", status, data)
    payload = data.get("data") if isinstance(data, dict) else None
    token = str(payload.get("access_token") or "") if isinstance(payload, dict) else ""
    if not token:
        raise QuickManageError("POST /auth/token: no access_token in the response", status)
    return _Token(token, _monotonic() + _lifetime(payload.get("expire")))


async def _get_token(company_slug: str) -> _Token:
    key = _key(company_slug)
    tok = _tokens.get(key)
    if tok is not None and _monotonic() < tok.refresh_at:
        return tok
    lock = _token_locks.setdefault(key, asyncio.Lock())
    async with lock:
        tok = _tokens.get(key)
        if tok is not None and _monotonic() < tok.refresh_at:
            return tok
        failure = _auth_failures.get(key)
        if failure is not None:
            if _monotonic() < failure[0]:
                raise type(failure[1])(str(failure[1]), failure[1].status)
            del _auth_failures[key]
        try:
            tok = await _authenticate(company_slug)
        except QuickManageAuthError as e:
            _tokens.pop(key, None)
            _auth_failures[key] = (_monotonic() + AUTH_REFUSED_TTL, e)
            raise
        except QuickManageUnavailable as e:
            if is_configured(company_slug):
                _auth_failures[key] = (_monotonic() + AUTH_OUTAGE_TTL, e)
            raise
        _tokens[key] = tok
        return tok


# ── Requests ─────────────────────────────────────────────────────────────────────

async def _call(company_slug: str, method: str, path: str, body: dict | None = None):
    """An authenticated request; returns the envelope's ``data``. A 401 drops the cached
    token, logs in again and repeats the request once."""
    key = _key(company_slug)
    token = await _get_token(company_slug)
    relogged = False
    while True:
        headers = {"accept": "application/json", "Authorization": f"Bearer {token.value}"}
        status, data = await _send(method, path, headers, body)
        if status == 401:
            if relogged:
                raise QuickManageAuthError(f"{method} {path} HTTP 401 even after a new login",
                                           status)
            relogged = True
            if _tokens.get(key) is token:   # a concurrent request may have renewed it already
                del _tokens[key]
            logger.info(f"[quickmanage] {key}: token refused (401), logging in again")
            token = await _get_token(company_slug)
            continue
        if status == 403:
            raise QuickManageAuthError(f"{method} {path} HTTP 403", status)
        if not 200 <= status < 300:
            raise _fail(method, path, status, data)
        if not isinstance(data, dict) or "data" not in data:
            raise QuickManageError(f"{method} {path}: unexpected response body", status)
        return data["data"]


async def _search(company_slug: str, path: str, filters: list[dict]) -> list[dict]:
    """Every item matching ``filters`` (pages of 50, at most 5 pages)."""
    items: list[dict] = []
    for page in range(_MAX_PAGES):
        data = await _call(company_slug, "POST", path, {
            "query": "", "filters": filters, "page": page, "page_size": _SEARCH_PAGE_SIZE})
        if not isinstance(data, dict):
            raise QuickManageError(f"POST {path}: unexpected data")
        page_items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
        items.extend(page_items)
        try:
            count = int(data.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if len(page_items) < _SEARCH_PAGE_SIZE or len(items) >= count:
            break
    return items


# ── Parsing ──────────────────────────────────────────────────────────────────────

def _clean(value) -> str:
    return " ".join(str(value or "").split())


def _driver_from(raw: dict) -> QMDriver:
    assigned = raw.get("assigned_truck")
    return QMDriver(
        id=_clean(raw.get("id")),
        name=_clean(f"{raw.get('first_name') or ''} {raw.get('last_name') or ''}"),
        email=_clean(raw.get("email")) or None,
        phone=_clean(raw.get("phone")) or None,
        status=_clean(raw.get("status")).lower() or None,
        truck_unit=(_clean(assigned.get("number")) or None) if isinstance(assigned, dict) else None,
    )


def _truck_from(raw: dict) -> QMTruck:
    # The detail record says "unit"; the narrow search item may say unit_number.
    unit = raw.get("unit") or raw.get("unit_number") or raw.get("number")
    return QMTruck(id=_clean(raw.get("id")), unit=_clean(unit),
                   status=_clean(raw.get("status")).lower() or None)


def _search_values(unit: str) -> list[str]:
    """What to put in the unit_number filter, in order: the unit as given, its leading
    token (Motive names carry suffixes like "4063 O/O"), then the normalized keys (a stored
    "T120" is not found by "T-120"), then the digits alone."""
    raw = (unit or "").strip()
    tokens = raw.split()
    values = [raw]
    if len(tokens) > 1:
        values.append(tokens[0])
    candidates = unit_candidates(unit)
    values.extend(candidates)
    # Last resort, for spellings that differ in punctuation or spacing ("f 9121" vs
    # "F9121"): the longest run of digits; the exact test below still decides.
    runs = re.findall(r"\d+", candidates[0]) if candidates else []
    if runs:
        values.append(max(runs, key=len))
    return list(dict.fromkeys(v for v in values if v))


def _pick_exact(trucks: list[QMTruck], unit: str) -> list[QMTruck]:
    """The trucks whose unit equals the asked one once normalized: the full key first,
    else the leading-token fallback (unit_candidates). Of several, those sold / written
    off are dropped when a live one remains (a retired number can be reused)."""
    for cand in unit_candidates(unit):
        hits = list({t.id: t for t in trucks if normalize_unit(t.unit) == cand}.values())
        if hits:
            live = [t for t in hits if t.status not in _DEAD_TRUCK_STATUSES]
            return live or hits
    return []


# ── Truck -> drivers ─────────────────────────────────────────────────────────────

# (company, normalized unit) -> (expires_at monotonic, result)
_truck_cache: dict[tuple[str, str], tuple[float, TruckDrivers]] = {}
_truck_locks: dict[tuple[str, str], asyncio.Lock] = {}


async def _find_trucks(company_slug: str, unit: str) -> list[QMTruck]:
    seen: dict[str, QMTruck] = {}
    for value in _search_values(unit):
        filters = [{"field": "unit_number", "operator": "match", "value": value}]
        for raw in await _search(company_slug, "/x/trucks/search", filters):
            truck = _truck_from(raw)
            if truck.id:
                seen.setdefault(truck.id, truck)
        hits = _pick_exact(list(seen.values()), unit)
        if hits:
            return hits
    return []


async def _fetch_truck_drivers(company_slug: str, unit: str) -> TruckDrivers:
    hits = await _find_trucks(company_slug, unit)
    if not hits:
        return TruckDrivers(unit=unit, reason=REASON_NO_TRUCK)
    if len(hits) > 1:
        return TruckDrivers(unit=unit, reason=REASON_AMBIGUOUS, matches=tuple(hits))
    data = await _call(company_slug, "GET", f"/x/trucks/{hits[0].id}")
    if not isinstance(data, dict):
        raise QuickManageError("GET /x/trucks/{id}: unexpected data")
    truck = _truck_from(data)
    if not truck.id:
        truck = hits[0]
    drivers = tuple(d for d in (_driver_from(r) for r in (data.get("drivers") or [])
                                if isinstance(r, dict))
                    if d.status == "active")
    return TruckDrivers(unit=unit, truck=truck, drivers=drivers)


async def truck_drivers(company_slug: str, unit: str) -> TruckDrivers:
    """The truck with this unit number and its ACTIVE drivers. Exactly one truck whose
    normalized unit equals the asked one -> `found`; none -> REASON_NO_TRUCK; several ->
    REASON_AMBIGUOUS. Cached 10 min per (company, unit); concurrent callers share one
    fetch. Raises QuickManageUnavailable (also when the company is not configured) or
    QuickManageError; failures are not cached."""
    if not is_configured(company_slug):
        raise QuickManageUnavailable(f"QuickManage is not configured for {company_slug!r}")
    ck = (_key(company_slug), normalize_unit(unit))
    if not ck[1]:
        return TruckDrivers(unit=unit or "", reason=REASON_NO_TRUCK)
    cached = _truck_cache.get(ck)
    if cached and _monotonic() < cached[0]:
        return cached[1]
    lock = _truck_locks.setdefault(ck, asyncio.Lock())
    async with lock:
        cached = _truck_cache.get(ck)
        if cached and _monotonic() < cached[0]:
            return cached[1]
        result = await _fetch_truck_drivers(company_slug, unit)
        _truck_cache[ck] = (_monotonic() + TRUCK_TTL, result)
        return result


# ── Driver search ────────────────────────────────────────────────────────────────

def _name_tokens(text: str | None) -> set[str]:
    return set(_NAME_TOKEN_RE.findall((text or "").lower()))


async def search_drivers(company_slug: str, name: str) -> list[QMDriver]:
    """Drivers (any status) whose first/last name contains every name part ("John Smith"
    or "Smith, John"). Searches first_name/last_name with the first and last part in
    either order (one part: as either), then filters the hits locally on all the parts, so it does not depend
    on how the server combines filters. Not cached. [] for a name with no usable part."""
    if not is_configured(company_slug):
        raise QuickManageUnavailable(f"QuickManage is not configured for {company_slug!r}")
    parts = re.findall(r"[^\W\d_]{2,}", name or "")
    if not parts:
        return []
    wanted = {p.lower() for p in parts}
    a, b = parts[0], parts[-1]
    if len(parts) == 1:
        filter_sets = [[("first_name", a)], [("last_name", a)]]
    else:
        filter_sets = [[("first_name", a), ("last_name", b)],
                       [("first_name", b), ("last_name", a)]]
    found: dict[str, QMDriver] = {}
    for fs in filter_sets:
        filters = [{"field": f, "operator": "match", "value": v} for f, v in fs]
        for raw in await _search(company_slug, "/x/drivers/search", filters):
            driver = _driver_from(raw)
            if driver.id and wanted <= _name_tokens(
                    f"{raw.get('first_name') or ''} {raw.get('middle_name') or ''} "
                    f"{raw.get('last_name') or ''}"):
                found.setdefault(driver.id, driver)
        if found and len(parts) > 1:   # one part may be a first or a last name: try both
            break
    return list(found.values())


def _reset_caches() -> None:
    """Tests only."""
    _tokens.clear()
    _auth_failures.clear()
    _token_locks.clear()
    _truck_cache.clear()
    _truck_locks.clear()
