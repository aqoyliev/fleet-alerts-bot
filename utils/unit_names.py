"""Matching a typed unit number against a provider's vehicle roster.

Provider-independent on purpose. Samsara names a truck "unit571" and Motive names the
same truck "571", and both rosters have to be searched by the same rules — a deployment
where /setunit resolves differently depending on which provider answered would point a
group at the wrong truck, or at no truck, with nothing in the UI to show for it.

The two clients (utils/samsara/client.py, utils/motive/vehicles.py) know how to fetch
and cache a roster. Everything about *comparing* names lives here, once.

A roster is passed in as {normalized key: the provider's own spelling}. The provider's
spelling is what comes back out, because alert routing compares the stored unit against
the vehicle name by strict SQL equality — see resolve_unit in utils/units.py.
"""

import logging
import re

logger = logging.getLogger(__name__)


def normalize(name: str) -> str:
    """The roster key: case- and whitespace-insensitive."""
    return " ".join((name or "").split()).lower()


# A leading "UNIT"/"TRUCK" label, with any :#- separator. Fleets spell the same truck as
# "unit571" in Samsara and "UNIT: 571" on the Telegram group, so the label is dropped
# before comparing. CPT's roster is literally unit001/unit571/unit2007.
_UNIT_LABEL_RE = re.compile(r"^(?:unit|truck)\s*[:#\-]*\s*", re.IGNORECASE)


def core(name: str) -> str:
    """Comparison key for a unit: normalized, with a leading UNIT/TRUCK label removed."""
    return _UNIT_LABEL_RE.sub("", normalize(name)).strip()


# Last-resort key: the unit's digit run. Rosters decorate names in ways no label rule can
# anticipate — "unit1234 (lease)", "1234 - Freightliner", "TRK-1234" — and dropping a
# leading UNIT/TRUCK label does not reach any of those. In a fleet the digits are what
# actually identify the truck, so they are the final thing compared.
_DIGITS_RE = re.compile(r"\d{3,7}")


def digits(name: str) -> str:
    """The unit's identifying digit run, or "" if the name carries none."""
    m = _DIGITS_RE.search(normalize(name))
    return m.group(0) if m else ""


def match(unit: str, roster: dict[str, str], *, provider: str = "") -> str | None:
    """Find `unit` in `roster`, returning the provider's own spelling, or None.

    Three passes, each looser than the last, all case- and whitespace-insensitive:
      1. the name exactly as given;
      2. the name with a leading UNIT/TRUCK label dropped from BOTH sides, so a
         dispatcher typing 571 finds "unit571" and vice versa;
      3. the identifying digit run alone, which is the only thing that survives a roster
         that decorates names ("unit571 (lease)", "571 - Freightliner", "TRK-571").

    Passes 2 and 3 only resolve when exactly ONE vehicle matches. A tie stops the search
    rather than falling through to a looser pass that can only widen it, and an ambiguous
    input is treated as not found rather than guessed at: guessing would route one
    truck's alerts to another truck's group.
    """
    key = normalize(unit)
    if not key:
        return None
    if key in roster:
        return roster[key]

    for key_of in (core, digits):
        wanted = key_of(unit)
        if not wanted:
            continue
        hits = {name for k, name in roster.items() if key_of(k) == wanted}
        if len(hits) == 1:
            return hits.pop()
        if hits:
            logger.warning(f"{provider or 'roster'} unit '{unit}' is ambiguous: "
                           f"{sorted(hits)}")
            return None
    return None


def nearby(unit: str, roster: dict[str, str], limit: int = 5) -> list[str]:
    """Roster names that look like `unit`, for a "did you mean" hint.

    Best-effort and deliberately looser than match: it only decorates an error message,
    so an ambiguous answer is useful here where it is dangerous there.
    """
    wanted = core(unit)
    if not wanted:
        return []
    hits = [name for k, name in roster.items() if wanted in k or core(k) in wanted]
    return sorted(hits)[:limit]
