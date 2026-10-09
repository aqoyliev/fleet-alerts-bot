"""Motive's vehicle roster, for checking a unit number against it.

The counterpart to utils/samsara/client.py's roster half, and deliberately the same
shape: lookup_unit / list_units / suggest_units, plus an exception that separates "no
such truck" from "couldn't ask". utils/units.py consults both and does not care which
one answered.

Why this exists at all: a fleet running both providers has trucks that only one of them
knows about, and until now the only roster the bot could read was Samsara's. A
Motive-only truck was therefore refused by /setunit and missing from the admin panel's
unit picker, so the group it belongs to could not be paired on either surface -- while
Motive went on sending that truck's alerts, which then matched no group.

`number` is the field, not `name`: it is what the webhook payloads carry as
current_vehicle.number, and alert routing compares the stored unit against exactly that
by strict equality. Matching the roster on anything else would resolve a unit the alerts
could never route to.
"""

import logging
from datetime import datetime, timedelta, timezone

import aiohttp

from utils import unit_names

logger = logging.getLogger(__name__)

MOTIVE_API_V1 = "https://api.gomotive.com/v1"

_timeout = aiohttp.ClientTimeout(total=30)
_STALE_AFTER = timedelta(minutes=30)

# Pages of 100. The cap is a runaway guard, not a fleet-size limit: at 100 per page it
# allows 10,000 trucks, and the biggest fleet on this bot has about forty.
_PER_PAGE = 100
_MAX_PAGES = 100


class MotiveUnavailable(Exception):
    """The roster could not be fetched, so presence could not be determined.

    The same distinction SamsaraUnavailable draws, for the same reason: a caller
    validating user input must not tell a dispatcher their unit doesn't exist because
    Motive happened to be down.
    """


class MotiveRoster:
    """One per API key, caching the roster for half an hour.

    The cache is what makes a unit picker and a "did you mean" hint affordable -- both
    would otherwise walk the whole fleet on every keystroke-sized request.
    """

    def __init__(self, api_key: str, base_url: str = MOTIVE_API_V1):
        self._headers = {"X-Api-Key": api_key, "Accept": "application/json"}
        self._base = base_url.rstrip("/")
        self._names: dict[str, str] = {}   # normalized number -> Motive's own spelling
        self._fetched_at: datetime | None = None

    # ── fetching ────────────────────────────────────────────────────────────────

    async def _get(self, path: str, params: dict) -> dict | None:
        try:
            async with aiohttp.ClientSession(headers=self._headers, timeout=_timeout) as s:
                async with s.get(f"{self._base}{path}", params=params) as r:
                    if r.status != 200:
                        body = (await r.text())[:300]
                        logger.warning(f"[motive] GET {path} HTTP {r.status}: {body}")
                        return None
                    return await r.json()
        except Exception as e:
            logger.error(f"[motive] GET {path} error: {e}")
            return None

    @staticmethod
    def _rows(payload: dict) -> list[dict]:
        """The vehicle rows out of one page, whichever way they are wrapped.

        Motive returns {"vehicles": [{"vehicle": {...}}]} and unwrapping it is the same
        dance find_performance_event already does for events. Both the envelope key and
        the per-row wrapper are tolerated as absent, because this code has never been run
        against a live Motive org -- no API key exists for one yet -- and a roster that
        silently reads as empty would refuse every unit in the fleet.
        """
        rows = payload.get("vehicles")
        if not isinstance(rows, list):
            rows = payload.get("data")
        if not isinstance(rows, list):
            return []
        return [(row.get("vehicle") or row) if isinstance(row, dict) else {}
                for row in rows]

    async def _refresh(self) -> bool:
        """Reload the roster. Returns True if Motive actually answered, so the caller can
        tell an empty fleet apart from a failed fetch."""
        names: dict[str, str] = {}
        answered = False
        page = 1
        while page <= _MAX_PAGES:
            payload = await self._get("/vehicles", {"per_page": str(_PER_PAGE),
                                                    "page_no": str(page)})
            if not payload:
                break
            answered = True
            rows = self._rows(payload)
            for vehicle in rows:
                raw = str(vehicle.get("number") or "").strip()
                if raw:
                    names[unit_names.normalize(raw)] = raw
            total = (payload.get("pagination") or {}).get("total")
            if not rows or (total is not None and page * _PER_PAGE >= total):
                break
            page += 1

        if names:
            self._names = names
            self._fetched_at = datetime.now(timezone.utc)
            logger.info(f"[motive] roster: {len(names)} vehicles")
        elif answered:
            # Answered with nothing. Recorded as a successful fetch so callers stop
            # retrying, but the cache is left alone: an org that returns an empty page
            # once must not wipe a roster that was readable a minute ago.
            self._fetched_at = self._fetched_at or datetime.now(timezone.utc)
            logger.warning("[motive] roster came back empty")
        return answered

    async def _ensure_fresh(self, *, required: bool) -> None:
        """Refresh if the cache is cold or stale.

        `required` is whether the caller needs a roster to answer at all. A stale roster
        still answers "does this unit exist" correctly almost always, so a failed refresh
        is only fatal when there is nothing cached to fall back on.
        """
        stale = (self._fetched_at is None
                 or datetime.now(timezone.utc) - self._fetched_at > _STALE_AFTER)
        if not stale:
            return
        answered = await self._refresh()
        if required and not answered and self._fetched_at is None:
            raise MotiveUnavailable("could not fetch the vehicle roster")

    # ── the three questions ─────────────────────────────────────────────────────

    async def find_number(self, unit: str) -> str | None:
        """Motive's own spelling of this unit, or None if no vehicle carries it."""
        key = unit_names.normalize(unit)
        if not key:
            return None
        if key not in self._names:
            await self._ensure_fresh(required=True)
        return unit_names.match(unit, self._names, provider="Motive")

    async def all_numbers(self) -> list[str]:
        await self._ensure_fresh(required=True)
        return sorted(self._names.values())

    async def nearby_numbers(self, unit: str, limit: int = 5) -> list[str]:
        """Never refreshes and never raises -- it only decorates an error message."""
        return unit_names.nearby(unit, self._names, limit)


# One roster per key, so the cache survives between requests.
_rosters: dict[str, MotiveRoster] = {}


def _roster_for(api_key: str) -> MotiveRoster:
    roster = _rosters.get(api_key)
    if roster is None:
        roster = _rosters[api_key] = MotiveRoster(api_key)
    return roster


async def lookup_unit(api_key: str, unit: str) -> str | None:
    """Is `unit` a real vehicle in this Motive org? Returns its number exactly as Motive
    spells it, or None if no such vehicle exists.

    Raises MotiveUnavailable if the roster could not be read at all."""
    if not api_key:
        raise MotiveUnavailable("no Motive API key configured")
    return await _roster_for(api_key).find_number(unit)


async def list_units(api_key: str) -> list[str]:
    """Every vehicle number in the org, as Motive spells them, sorted.

    Raises MotiveUnavailable rather than returning [], because a picker showing zero
    trucks and a picker that couldn't load are different things to a dispatcher.
    """
    if not api_key:
        raise MotiveUnavailable("no Motive API key configured")
    return await _roster_for(api_key).all_numbers()


async def suggest_units(api_key: str, unit: str) -> list[str]:
    """'Did you mean' candidates for a rejected unit. Never raises."""
    if not api_key:
        return []
    try:
        return await _roster_for(api_key).nearby_numbers(unit)
    except Exception:
        return []
