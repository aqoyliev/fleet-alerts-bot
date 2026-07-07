import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp

logger = logging.getLogger(__name__)

_timeout = aiohttp.ClientTimeout(total=30)


def _normalize(name: str) -> str:
    return " ".join(name.split()).lower()


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

    async def _refresh_vehicles(self):
        vehicles: dict[str, str] = {}
        after = None
        while True:
            params = {"limit": 512}
            if after:
                params["after"] = after
            data = await self._get("/fleet/vehicles", params)
            if not data:
                break
            for v in data.get("data", []):
                name = _normalize(v.get("name") or "")
                if name and v.get("id") is not None:
                    vehicles[name] = str(v["id"])
            page = data.get("pagination") or {}
            after = page.get("endCursor")
            if not page.get("hasNextPage") or not after:
                break
        if vehicles:
            self._vehicles = vehicles
            self._vehicles_at = datetime.now(timezone.utc)

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
