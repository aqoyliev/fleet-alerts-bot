import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import aiohttp

logger = logging.getLogger(__name__)

_timeout = aiohttp.ClientTimeout(total=30)


class SamsaraUnavailable(Exception):
    """The vehicle roster could not be fetched, so presence could not be determined.

    Distinct from "no such unit": a caller validating user input must not tell a
    dispatcher their unit doesn't exist because Samsara happened to be down.
    """


def _normalize(name: str) -> str:
    return " ".join(name.split()).lower()


# A leading "UNIT"/"TRUCK" label, with any :#- separator. Fleets spell the same truck as
# "unit571" in Samsara and "UNIT: 571" on the Telegram group, so the label is dropped
# before comparing. CPT's roster is literally unit001/unit571/unit2007.
_UNIT_LABEL_RE = re.compile(r"^(?:unit|truck)\s*[:#\-]*\s*", re.IGNORECASE)


def _core(name: str) -> str:
    """Comparison key for a unit: normalized, with a leading UNIT/TRUCK label removed."""
    return _UNIT_LABEL_RE.sub("", _normalize(name)).strip()


# Last-resort key: the unit's digit run. Rosters decorate names in ways no label rule can
# anticipate — "unit1234 (lease)", "1234 - Freightliner", "TRK-1234" — and dropping a
# leading UNIT/TRUCK label does not reach any of those. In a fleet the digits are what
# actually identify the truck, so they are the final thing compared.
_DIGITS_RE = re.compile(r"\d{3,7}")


def _digits(name: str) -> str:
    """The unit's identifying digit run, or "" if the name carries none."""
    m = _DIGITS_RE.search(_normalize(name))
    return m.group(0) if m else ""


def _kph_to_mph(kph: float) -> float:
    return kph * 0.621371


# One client per API key: each company is a separate Samsara org with its own
# key (stored in the DB), and the vehicle name→id cache is per-org.
_clients: dict[str, "SamsaraClient"] = {}


async def fetch_speeding_details(api_key: str, vehicle_name: str, event_time: datetime,
                                 vehicle_id: str = "") -> dict | None:
    """Module-level entry point used by the webhook handler."""
    if not api_key:
        return None
    client = _clients.get(api_key)
    if client is None:
        client = _clients[api_key] = SamsaraClient(api_key)
    return await client.get_speeding_details(vehicle_name, event_time, vehicle_id=vehicle_id)


async def lookup_unit(api_key: str, unit: str) -> str | None:
    """Is `unit` a real vehicle in this org? Returns its name exactly as Samsara spells
    it (so the caller can store the canonical form), or None if no such vehicle exists.

    Raises SamsaraUnavailable if the roster could not be read at all."""
    if not api_key:
        raise SamsaraUnavailable("no Samsara API key configured")
    return await _client_for(api_key).find_vehicle_name(unit)


async def suggest_units(api_key: str, unit: str) -> list[str]:
    """'Did you mean' candidates for a rejected unit. Never raises."""
    try:
        return await _client_for(api_key).nearby_units(unit)
    except Exception:
        return []


def _client_for(api_key: str) -> "SamsaraClient":
    client = _clients.get(api_key)
    if client is None:
        client = _clients[api_key] = SamsaraClient(api_key)
    return client


def _parse_time(iso: str) -> datetime | None:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except Exception:
        return None


def _format_location(loc: dict) -> str:
    if not isinstance(loc, dict):
        return ""
    for key in ("formattedLocation", "formattedAddress"):
        if loc.get(key):
            return loc[key]
    addr = loc.get("address")
    if isinstance(addr, str):
        return addr
    if isinstance(addr, dict):
        parts = [addr.get(k) for k in ("street", "city", "state", "postalCode") if addr.get(k)]
        if parts:
            return ", ".join(parts)
    lat, lon = loc.get("latitude"), loc.get("longitude")
    if lat is not None and lon is not None:
        return f"{lat:.5f}, {lon:.5f}"
    return ""


class SamsaraClient:
    """Minimal Samsara API client used to enrich speeding alerts.

    Samsara alert webhooks don't carry speed data, so we look up the
    speeding interval (speed, posted limit, location, driver) via
    GET /speeding-intervals/stream after the alert arrives.
    """

    def __init__(self, api_token: str, base_url: str = "https://api.samsara.com"):
        self._headers = {"Authorization": f"Bearer {api_token}", "Accept": "application/json"}
        self._base = base_url.rstrip("/")
        self._vehicles: dict[str, str] = {}  # normalized name -> asset id
        self._vehicle_names: dict[str, str] = {}  # normalized name -> name as Samsara spells it
        self._vehicles_at: datetime | None = None

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        try:
            async with aiohttp.ClientSession(headers=self._headers, timeout=_timeout) as s:
                async with s.get(f"{self._base}{path}", params=params or {}) as r:
                    if r.status != 200:
                        body = (await r.text())[:300]
                        logger.warning(f"Samsara GET {path} HTTP {r.status}: {body}")
                        return None
                    return await r.json()
        except Exception as e:
            logger.error(f"Samsara GET {path} error: {e}")
            return None

    async def _refresh_vehicles(self) -> bool:
        """Reload the vehicle roster. Returns True if the API actually answered, so a
        caller can tell an empty fleet apart from a failed lookup."""
        vehicles: dict[str, str] = {}
        names: dict[str, str] = {}
        after = None
        answered = False
        while True:
            params = {"limit": 512}
            if after:
                params["after"] = after
            data = await self._get("/fleet/vehicles", params)
            if not data:
                break
            answered = True
            for v in data.get("data", []):
                raw = (v.get("name") or "").strip()
                name = _normalize(raw)
                if name and v.get("id") is not None:
                    vehicles[name] = str(v["id"])
                    names[name] = raw
            page = data.get("pagination") or {}
            after = page.get("endCursor")
            if not page.get("hasNextPage") or not after:
                break
        if vehicles:
            self._vehicles = vehicles
            self._vehicle_names = names
            self._vehicles_at = datetime.now(timezone.utc)
        return answered

    async def get_vehicle_id(self, vehicle_name: str) -> str | None:
        key = _normalize(vehicle_name)
        if not key:
            return None
        stale = (
            self._vehicles_at is None
            or datetime.now(timezone.utc) - self._vehicles_at > timedelta(minutes=30)
        )
        if stale or key not in self._vehicles:
            await self._refresh_vehicles()
        if key in self._vehicles:
            return self._vehicles[key]
        for name, vid in self._vehicles.items():
            if name.startswith(key) or key.startswith(name):
                return vid
        logger.warning(f"Samsara vehicle not found for name '{vehicle_name}'")
        return None

    async def find_vehicle_name(self, unit: str) -> str | None:
        """Return the vehicle's name exactly as Samsara spells it, or None if no vehicle
        in the org carries that name.

        Three matching passes, each looser than the last, all case- and
        whitespace-insensitive:
          1. the name exactly as given;
          2. the name with a leading UNIT/TRUCK label dropped from BOTH sides, so a
             dispatcher typing 571 finds Samsara's "unit571" and vice versa;
          3. the identifying digit run alone, which is the only thing that survives a
             roster that decorates names ("unit571 (lease)", "571 - Freightliner",
             "TRK-571").

        Passes 2 and 3 only resolve when exactly ONE vehicle matches; a tie stops the
        search rather than falling through to a looser pass that can only widen it, and
        an ambiguous input is treated as not found rather than guessed at. Whichever pass hits, the
        return value is the roster's own spelling, because alert routing compares the
        stored unit against the vehicle name by strict SQL equality. Returning what the
        user typed would register a group that then silently never receives anything —
        the worst outcome, since it looks configured.

        Raises SamsaraUnavailable when the roster can't be read and nothing is cached,
        so a caller can tell "no such unit" apart from "couldn't check".
        """
        key = _normalize(unit)
        if not key:
            return None
        stale = (
            self._vehicles_at is None
            or datetime.now(timezone.utc) - self._vehicles_at > timedelta(minutes=30)
        )
        if stale or key not in self._vehicle_names:
            answered = await self._refresh_vehicles()
            # A failed refresh is only fatal with no roster to fall back on; a stale one
            # still answers "does this unit exist" correctly almost always.
            if not answered and self._vehicles_at is None:
                raise SamsaraUnavailable("could not fetch the vehicle roster")

        if key in self._vehicle_names:
            return self._vehicle_names[key]

        for key_of in (_core, _digits):
            wanted = key_of(unit)
            if not wanted:
                continue
            hits = {n for k, n in self._vehicle_names.items() if key_of(k) == wanted}
            if len(hits) == 1:
                return hits.pop()
            if hits:
                # Two trucks answer to this — guessing would route a group's alerts to
                # the wrong unit, so stop here rather than fall through to a looser pass
                # that can only widen the tie.
                logger.warning(f"Samsara unit '{unit}' is ambiguous: {sorted(hits)}")
                return None
        return None

    async def nearby_units(self, unit: str, limit: int = 5) -> list[str]:
        """Roster names that look like `unit`, for a 'did you mean' hint. Best-effort:
        never refreshes and never raises, since it only decorates an error message."""
        wanted = _core(unit)
        if not wanted:
            return []
        hits = [n for k, n in self._vehicle_names.items() if wanted in k or _core(k) in wanted]
        return sorted(hits)[:limit]

    async def get_driver_name(self, driver_id: str) -> str | None:
        data = await self._get(f"/fleet/drivers/{driver_id}")
        return ((data or {}).get("data") or {}).get("name")

    async def get_speeding_details(
        self,
        vehicle_name: str,
        event_time: datetime,
        vehicle_id: str = "",
        retries: int = 2,
        retry_delay: int = 20,
        match_window_minutes: int = 45,
    ) -> dict | None:
        """Speed details for the alert: interval if available, else GPS snapshot.

        Speeding intervals carry the posted limit and severity but are only
        written at/after trip end, so for a live alert the GPS history
        (near-real-time ECU speed + reverse-geocoded location) is the usual
        source. `vehicle_id` (the Samsara asset id, when the webhook carried it)
        skips the name→id lookup. Returns dict(max_speed_mph, posted_limit_mph,
        severity, duration_seconds, location, driver_name) or None.
        """
        vehicle_id = vehicle_id or await self.get_vehicle_id(vehicle_name)
        if not vehicle_id:
            return None

        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)

        for attempt in range(retries):
            interval, driver_id = await self._find_interval(vehicle_id, event_time, match_window_minutes)
            if interval:
                return await self._build_details(interval, driver_id)
            gps = await self._gps_snapshot(vehicle_id, event_time)
            if gps:
                return gps
            if attempt < retries - 1:
                await asyncio.sleep(retry_delay)
        logger.info(f"No Samsara speeding data for '{vehicle_name}' near {event_time.isoformat()}")
        return None

    async def _gps_snapshot(self, vehicle_id: str, event_time: datetime, window_seconds: int = 150) -> dict | None:
        """Max ECU speed and location around event_time from GPS history."""
        params = {
            "vehicleIds": vehicle_id,
            "types": "gps",
            "startTime": (event_time - timedelta(seconds=window_seconds)).isoformat(),
            "endTime": (event_time + timedelta(seconds=window_seconds)).isoformat(),
        }
        data = await self._get("/fleet/vehicles/stats/history", params)
        vehicles = (data or {}).get("data") or []
        points = vehicles[0].get("gps") or [] if vehicles else []
        best = None
        for p in points:
            speed = p.get("speedMilesPerHour")
            if speed is None:
                continue
            if best is None or speed > best.get("speedMilesPerHour", 0):
                best = p
        if not best:
            return None
        location = ((best.get("reverseGeo") or {}).get("formattedLocation")) or ""
        return {
            "max_speed_mph": best["speedMilesPerHour"],
            "posted_limit_mph": None,
            "severity": None,
            "duration_seconds": None,
            "location": location,
            "driver_name": None,
        }

    async def _find_interval(self, vehicle_id: str, event_time: datetime, window_minutes: int):
        params = {
            "assetIds": vehicle_id,
            "startTime": (event_time - timedelta(hours=1)).isoformat(),
            "endTime": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(),
            "includeDriverId": "true",
        }
        best = None
        best_driver = None
        best_gap = timedelta(minutes=window_minutes)
        after = None
        while True:
            if after:
                params["after"] = after
            data = await self._get("/speeding-intervals/stream", params)
            if not data:
                break
            for trip in data.get("data", []):
                for iv in trip.get("intervals") or []:
                    start = _parse_time(iv.get("startTime") or "")
                    if not start:
                        continue
                    gap = abs(start - event_time)
                    if gap <= best_gap:
                        best, best_driver, best_gap = iv, trip.get("driverId"), gap
            page = data.get("pagination") or {}
            after = page.get("endCursor")
            if not page.get("hasNextPage") or not after:
                break
        return best, best_driver

    async def _build_details(self, interval: dict, driver_id) -> dict:
        start = _parse_time(interval.get("startTime") or "")
        end = _parse_time(interval.get("endTime") or "")
        duration = int((end - start).total_seconds()) if start and end else None
        # Samsara returns the literal string "null" for unassigned drivers
        has_driver = driver_id and str(driver_id).lower() not in ("null", "none")
        driver_name = await self.get_driver_name(str(driver_id)) if has_driver else None
        max_kph = interval.get("maxSpeedKilometersPerHour")
        limit_kph = interval.get("postedSpeedLimitKilometersPerHour")
        return {
            "max_speed_mph": _kph_to_mph(max_kph) if max_kph else None,
            "posted_limit_mph": _kph_to_mph(limit_kph) if limit_kph else None,
            "severity": interval.get("severityLevel"),
            "duration_seconds": duration,
            "location": _format_location(interval.get("location") or {}),
            "driver_name": driver_name,
        }
