"""Tests for the Samsara unit check behind /setunit.

A group registered to a unit that doesn't exist looks configured and silently never
receives anything, so /setunit verifies the number against the org's vehicle roster
first. These drive SamsaraClient.find_vehicle_name through a faked _get so nothing
touches the network.
"""
import pytest

from utils.samsara.client import SamsaraClient, SamsaraUnavailable


def _client(*pages, fail=False):
    """A client whose roster fetch returns `pages`, or fails outright."""
    c = SamsaraClient("test-key")
    calls = []

    async def _fake_get(path, params=None):
        calls.append((path, params))
        if fail:
            return None
        i = len(calls) - 1
        return pages[i] if i < len(pages) else None

    c._get = _fake_get
    c._calls = calls
    return c


def _page(*names, has_next=False, cursor=None):
    return {
        "data": [{"name": n, "id": f"id-{n}"} for n in names],
        "pagination": {"hasNextPage": has_next, "endCursor": cursor},
    }


# ── the unit exists ────────────────────────────────────────────────────────────

async def test_known_unit_returns_samsaras_own_spelling():
    """Routing matches the stored unit against the vehicle name by strict equality, so
    the canonical spelling is what must be stored — not what the dispatcher typed."""
    c = _client(_page("1274"))
    assert await c.find_vehicle_name("1274") == "1274"


async def test_lookup_is_case_and_whitespace_insensitive_but_returns_canonical():
    c = _client(_page("Unit 1274"))
    assert await c.find_vehicle_name("  unit   1274 ") == "Unit 1274"


async def test_unit_found_on_a_later_page():
    c = _client(
        _page("1111", has_next=True, cursor="c1"),
        _page("2222"),
    )
    assert await c.find_vehicle_name("2222") == "2222"


# ── the unit does not exist ────────────────────────────────────────────────────

async def test_unknown_unit_returns_none():
    c = _client(_page("1274", "5678"))
    assert await c.find_vehicle_name("9999") is None


async def test_no_prefix_fuzz_unlike_get_vehicle_id():
    """get_vehicle_id falls back to prefix matching; this must not. Accepting '127' for
    vehicle '1274' would register a group that never matches an actual alert."""
    c = _client(_page("1274"))
    assert await c.find_vehicle_name("127") is None
    assert await c.find_vehicle_name("12740") is None


# ── bridging the UNIT label gap ────────────────────────────────────────────────

async def test_bare_number_finds_a_unit_prefixed_vehicle():
    """CPT's real roster: Samsara says 'unit571', the Telegram group says 'UNIT: 571',
    and the group parser yields '571'. These have to meet."""
    c = _client(_page("unit571"))
    assert await c.find_vehicle_name("571") == "unit571"


async def test_labelled_input_finds_a_bare_vehicle():
    c = _client(_page("1274"))
    assert await c.find_vehicle_name("UNIT: 1274") == "1274"
    assert await c.find_vehicle_name("TRUCK# 1274") == "1274"


async def test_label_match_still_returns_samsaras_spelling():
    c = _client(_page("Unit 786"))
    assert await c.find_vehicle_name("786") == "Unit 786"


async def test_leading_zeros_are_not_collapsed():
    """unit001 and unit1 would be different trucks; guessing between them is worse than
    reporting not-found."""
    c = _client(_page("unit001"))
    assert await c.find_vehicle_name("1") is None
    assert await c.find_vehicle_name("001") == "unit001"


async def test_ambiguous_match_is_refused_not_guessed():
    c = _client(_page("571", "unit571"))
    assert await c.find_vehicle_name("truck571") is None


async def test_blank_unit_is_not_a_lookup():
    c = _client(_page("1274"))
    assert await c.find_vehicle_name("   ") is None
    assert c._calls == []  # never hit the API


# ── the check could not be made ────────────────────────────────────────────────

async def test_unreachable_roster_raises_rather_than_reporting_absent():
    """The caller must be able to tell 'no such unit' from 'couldn't check' — telling a
    dispatcher their unit doesn't exist because Samsara was down is the bad outcome."""
    c = _client(fail=True)
    with pytest.raises(SamsaraUnavailable):
        await c.find_vehicle_name("1274")


async def test_stale_cache_answers_when_a_refresh_fails():
    """One failed refresh shouldn't turn a working check into an outage."""
    c = _client(_page("1274"))
    assert await c.find_vehicle_name("1274") == "1274"

    async def _fail(path, params=None):
        return None

    c._get = _fail
    # Unknown unit forces a refresh, which fails — but the cached roster still answers.
    assert await c.find_vehicle_name("9999") is None
    assert await c.find_vehicle_name("1274") == "1274"


async def test_empty_fleet_is_not_treated_as_unavailable():
    """An org with no vehicles answers 'not found', not 'couldn't check'."""
    c = _client(_page())
    assert await c.find_vehicle_name("1274") is None
