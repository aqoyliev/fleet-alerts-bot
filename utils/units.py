"""Deciding whether a unit number is real, and what this deployment calls it.

This is deployment policy, not provider API mechanics, which is why it lives here and
not in either client: it reads the configured keys and it encodes the four-state answer
the *bot* needs ("ok" / "missing" / "unavailable" / "no_roster"). The clients below it
only know how to ask one provider a question.

Both rosters are consulted, because a fleet running both has trucks only one of them
knows about. Checking Samsara alone meant a Motive-only truck was refused by /setunit
and absent from the panel's picker, so its group could not be paired on either surface
-- while Motive kept sending that truck's alerts, which then matched no group at all.

Two callers share this -- the /setunit command and the admin panel's unit picker -- and
they must agree exactly. A unit accepted by one and refused by the other would be a
group that looks configured on one surface and not the other.
"""

import logging

from data import config
from utils.motive.vehicles import MotiveUnavailable
from utils.motive import vehicles as motive
from utils.samsara.client import (
    list_units as samsara_list_units, lookup_unit, suggest_units, SamsaraUnavailable,
)

logger = logging.getLogger(__name__)

MAX_UNIT_LEN = 50  # the width of the vehicle_number column


def clean_unit(raw: str) -> str | None:
    """Tidy what a dispatcher typed or picked into something worth looking up.

    Returns None when there is nothing to look up at all: empty, or longer than the
    column it would be stored in.

    What is deliberately not checked is whether it contains a digit. It used to be --
    "unit" sounds like a number, and requiring one caught a stray word before it cost a
    round trip. But the roster is the authority on how a truck is named, and this fleet
    names some of them after their driver ("Ruhoallah Assadi") or their VIN tail
    ("GFXH-ARH-SHW"). The rule was refusing units that the panel's own picker was
    offering, taken straight off the roster -- a guess about what a unit looks like,
    overruling the roster it was supposed to protect. resolve_unit below refuses anything
    no provider can confirm, and that is the check that actually keeps a group from being
    pointed at a truck that does not exist.
    """
    unit = (raw or "").strip().lstrip("#").strip()
    if not unit or len(unit) > MAX_UNIT_LEN:
        return None
    return unit


def roster_names() -> str:
    """What to call the roster in a message: "Samsara", "Motive", "Samsara or Motive".

    Written from the configured keys rather than hard-coded, because the name in the
    message has to be the name of the roster that was actually searched. Telling a
    Motive fleet their truck "isn't in Samsara" names a system they do not use; telling
    a dual-provider fleet the same thing is worse, because the truck may well be in
    Motive and the message sends them looking in the wrong place.

    Empty string when neither is configured -- callers have a "no_roster" answer for
    that case and should not be naming anything.
    """
    names = [n for n, key in (("Samsara", config.SAMSARA_API_KEY),
                              ("Motive", config.MOTIVE_API_KEY)) if key]
    return " or ".join(names)


async def resolve_unit(unit: str) -> tuple[str, str]:
    """Check a unit against the configured vehicle rosters and canonicalize it.

    Returns (status, value):
      ("ok", <name as its provider spells it>) — exists; store this, not what was typed
      ("missing", unit)       — every configured roster answered, none has this truck
      ("unavailable", unit)   — there is a roster but it could not be read
      ("no_roster", unit)     — this deployment has neither provider configured

    Storing the provider's own spelling is the point. Alert routing matches the stored
    unit against the vehicle name by strict equality, and this fleet names trucks
    "unit571" while its Telegram groups say "UNIT: 571" — so the two only ever meet if
    the roster's version is what goes in the database.

    That is why "unavailable" is kept apart from "no_roster". With a key configured
    there IS a canonical spelling; registering an unverified guess against it produces a
    group that looks configured and never receives anything, which is the failure this
    whole function exists to prevent — so the caller refuses and asks for a retry.
    Without any key there is no canonical spelling to disagree with, so what the
    dispatcher typed is all there is and registration proceeds.

    "missing" is only returned when every configured roster answered and none of them
    had the truck. A provider that failed to answer makes the result "unavailable"
    instead, even if the other one said no: absence from one roster is not absence from
    the fleet, and refusing the unit outright would be refusing a truck that exists.

    Samsara is asked first, and wins when both know the truck. Not because it is more
    authoritative but because every group paired before this function could read Motive
    was stored with Samsara's spelling; preferring Motive's would silently re-point
    those groups the next time somebody re-saved one. A truck the two providers spell
    differently still only routes for the provider whose spelling is stored -- see the
    note in utils/webhook_handler.py on what routing compares.
    """
    checked_any = False
    unreachable = False

    if config.SAMSARA_API_KEY:
        checked_any = True
        try:
            canonical = await lookup_unit(config.SAMSARA_API_KEY, unit)
            if canonical is not None:
                if canonical != unit:
                    logger.info(f"Unit '{unit}' resolved to Samsara's '{canonical}'")
                return "ok", canonical
        except SamsaraUnavailable as e:
            logger.warning(f"Samsara unit check unavailable for '{unit}': {e}")
            unreachable = True

    if config.MOTIVE_API_KEY:
        checked_any = True
        try:
            canonical = await motive.lookup_unit(config.MOTIVE_API_KEY, unit)
            if canonical is not None:
                if canonical != unit:
                    logger.info(f"Unit '{unit}' resolved to Motive's '{canonical}'")
                return "ok", canonical
        except MotiveUnavailable as e:
            logger.warning(f"Motive unit check unavailable for '{unit}': {e}")
            unreachable = True

    if not checked_any:
        return "no_roster", unit
    if unreachable:
        return "unavailable", unit
    return "missing", unit


async def suggest_units_any(unit: str) -> list[str]:
    """"Did you mean" candidates from every configured roster, for a rejected unit.

    Merged rather than per-provider: the dispatcher who typo'd a unit number is not
    asking which system it lives in, and a hint split in two would make them answer that
    question before they could use it. Never raises -- it only decorates an error.
    """
    found: list[str] = []
    if config.SAMSARA_API_KEY:
        found += await suggest_units(config.SAMSARA_API_KEY, unit)
    if config.MOTIVE_API_KEY:
        found += await motive.suggest_units(config.MOTIVE_API_KEY, unit)
    # Deduplicated because a truck in both systems, spelled the same way, would otherwise
    # be offered twice — which reads as two different trucks.
    return sorted(set(found))[:5]


async def list_units_any() -> tuple[list[str], bool]:
    """Every unit the deployment can name, and whether any roster actually answered.

    Returns (names, available). available is False when nothing could be read, which the
    panel shows as "the roster is unavailable" rather than as a fleet of no trucks --
    they are different things to a dispatcher, and only one of them is their problem.

    A provider that fails while the other answers does not make the list unavailable: a
    partial picker is worth far more than an error, and the server re-checks whatever is
    picked anyway.
    """
    names: list[str] = []
    available = False
    for key, fetch, label in (
        (config.SAMSARA_API_KEY, samsara_list_units, "Samsara"),
        (config.MOTIVE_API_KEY, motive.list_units, "Motive"),
    ):
        if not key:
            continue
        try:
            names += await fetch(key)
            available = True
        except Exception as e:
            logger.warning(f"[units] {label} roster unavailable: {e}")
    return sorted(set(names)), available
