"""Deciding whether a unit number is real, and what this deployment calls it.

This is deployment policy, not Samsara API mechanics, which is why it lives here and not
in utils/samsara/client.py: it reads config.SAMSARA_API_KEY and it encodes the four-state
answer the *bot* needs ("ok" / "missing" / "unavailable" / "no_roster"). The client below
it only knows how to ask Samsara a question.

Two callers share it — the /setunit command and the admin panel's unit picker — and they
must agree exactly. A unit accepted by one and refused by the other would be a group that
looks configured on one surface and not the other.
"""

import logging

from data import config
from utils.samsara.client import lookup_unit, SamsaraUnavailable

logger = logging.getLogger(__name__)


async def resolve_unit(unit: str) -> tuple[str, str]:
    """Check a unit against Samsara's vehicle roster and canonicalize it.

    Returns (status, value):
      ("ok", <name as Samsara spells it>)  — exists; store this, not what was typed
      ("missing", unit)                    — no such vehicle in the org
      ("unavailable", unit)                — there is a roster but it could not be read
      ("no_roster", unit)                  — this deployment has no Samsara at all

    Storing Samsara's own spelling is the point. Alert routing matches the stored unit
    against the vehicle name by strict equality, and this fleet names trucks "unit571"
    while its Telegram groups say "UNIT: 571" — so the two only ever meet if the
    roster's version is what goes in the database.

    That is why "unavailable" is kept apart from "no_roster". With a key configured
    there IS a canonical spelling; registering an unverified guess against it produces a
    group that looks configured and never receives anything, which is the failure this
    whole function exists to prevent — so the caller refuses and asks for a retry.
    Without a key there is no canonical spelling to disagree with (a Motive-only fleet),
    so what the dispatcher typed is all there is and registration proceeds.
    """
    if not config.SAMSARA_API_KEY:
        return "no_roster", unit
    try:
        canonical = await lookup_unit(config.SAMSARA_API_KEY, unit)
    except SamsaraUnavailable as e:
        logger.warning(f"Samsara unit check unavailable for '{unit}': {e}")
        return "unavailable", unit
    if canonical is None:
        return "missing", unit
    if canonical != unit:
        logger.info(f"Unit '{unit}' resolved to Samsara's '{canonical}'")
    return "ok", canonical
