"""Tests for matching a typed or parsed unit against Samsara's vehicle roster.

Dispatchers, group titles and the Samsara roster all spell the same truck differently
— "UNIT: 1234" on the Telegram group, "unit1234" in Samsara, "1234" from whoever runs
/setunit — so the lookup has to reconcile them. It must NOT reconcile two different
trucks, which is why an ambiguous input resolves to nothing.
"""
from datetime import datetime, timezone

import pytest

from utils.group_parser import extract_vehicle_number
from utils.samsara.client import SamsaraClient, _normalize


def _client(*roster: str) -> SamsaraClient:
    """A client with a fixed roster and no network."""
    c = SamsaraClient("key")
    c._vehicle_names = {_normalize(r): r for r in roster}
    c._vehicles_at = datetime.now(timezone.utc)

    async def _no_refresh():
        return True

    c._refresh_vehicles = _no_refresh
    return c


# ── the roster's own spelling is what comes back ───────────────────────────────

@pytest.mark.parametrize("typed", ["unit1234", "1234", "unit 1234", "UNIT 1234",
                                   "Unit#1234", "unit-1234", "truck 1234"])
async def test_every_spelling_resolves_to_the_roster_name(typed):
    assert await _client("unit1234").find_vehicle_name(typed) == "unit1234"


@pytest.mark.parametrize("roster_name", [
    "unit1234 (lease)",       # roster carries a suffix the label rule cannot reach
    "1234 - Freightliner",
    "TRK-1234",               # a label that is neither UNIT nor TRUCK
    "Unit 1234",
])
async def test_decorated_roster_names_match_on_the_digit_run(roster_name):
    c = _client(roster_name)
    assert await c.find_vehicle_name("1234") == roster_name
    assert await c.find_vehicle_name("unit1234") == roster_name


# ── the group title the bot reads on being added ───────────────────────────────

@pytest.mark.parametrize("title", [
    "unit 1234 - ali / ahmed",        # space between label and number
    "UNIT: 1234 ALI AHMED (CD)",
    "unit1234 ali",
    "1234 DHIDINBE, MADAR / YUSUF",
    "TRUCK# 1234 LATODDRICK BARBER",
])
async def test_group_title_registers_against_the_roster(title):
    unit = extract_vehicle_number(title, "")
    assert unit == "1234"
    assert await _client("unit1234").find_vehicle_name(unit) == "unit1234"


# ── never guess between two trucks ─────────────────────────────────────────────

async def test_two_trucks_sharing_a_number_resolve_to_nothing():
    """Routing a group's alerts to the wrong truck is worse than refusing to register."""
    assert await _client("unit1234", "truck1234").find_vehicle_name("1234") is None


async def test_a_tie_does_not_fall_through_to_a_looser_pass():
    """The digit pass must not rescue a name the label pass already found ambiguous —
    it can only widen the tie, never break it."""
    c = _client("unit1234 (lease)", "truck1234 spare")
    assert await c.find_vehicle_name("1234") is None


async def test_unknown_unit_is_not_found():
    assert await _client("unit1234", "unit5678").find_vehicle_name("9999") is None


async def test_name_without_digits_does_not_match_everything():
    assert await _client("unit1234").find_vehicle_name("spare truck") is None


# ── what lands in the database ─────────────────────────────────────────────────

@pytest.mark.parametrize("typed", ["unit1234", "1234", "unit 1234", "UNIT: 1234",
                                   "Unit#1234", "unit1234 (lease)"])
async def test_the_roster_spelling_is_what_gets_stored(typed, monkeypatch):
    """Alert routing compares the stored unit to the vehicle name by strict SQL
    equality, so the database must hold Samsara's spelling and never the caller's.
    Storing what was typed would register a group that then silently receives
    nothing — the worst outcome, because it looks configured.
    """
    import handlers.groups.group_events as ge
    from data import config

    c = _client("unit1234")
    monkeypatch.setattr(config, "SAMSARA_API_KEY", "key")
    monkeypatch.setattr(ge, "lookup_unit", lambda _k, u: c.find_vehicle_name(u))

    status, stored = await ge._resolve_unit(typed)
    assert (status, stored) == ("ok", "unit1234")


async def test_an_unknown_unit_is_rejected_not_stored(monkeypatch):
    import handlers.groups.group_events as ge
    from data import config

    c = _client("unit1234")
    monkeypatch.setattr(config, "SAMSARA_API_KEY", "key")
    monkeypatch.setattr(ge, "lookup_unit", lambda _k, u: c.find_vehicle_name(u))

    status, _ = await ge._resolve_unit("9999")
    assert status == "missing"    # the caller refuses to register on this status
