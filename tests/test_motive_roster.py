"""Tests for the Motive vehicle roster and the two-provider unit check.

Until now the only roster the bot could read was Samsara's, so on a fleet running both
providers a truck that only Motive knew about was refused by /setunit and missing from
the admin panel's picker -- unpairable on either surface, while Motive went on sending
that truck's alerts, which then matched no group at all.

So both rosters are consulted, and the message names the one(s) actually searched. The
distinction these tests care most about is "missing" versus "unavailable": absence from
one roster is not absence from the fleet, and refusing a unit outright because the other
provider happened to be down would be refusing a truck that exists.

Nothing here touches the network -- MotiveRoster is driven through a faked _get, the way
tests/test_unit_lookup.py drives the Samsara client.
"""
import pytest

from data import config
from utils import unit_names, units
from utils.motive.vehicles import MotiveRoster, MotiveUnavailable
from utils.samsara.client import SamsaraUnavailable


# ── the shared matcher ─────────────────────────────────────────────────────────
#
# Both clients compare names through utils/unit_names.py. A Motive copy of these rules
# that drifted from Samsara's would resolve a unit differently depending on which
# provider answered, which is a group pointed at the wrong truck.

def _roster(*names):
    return {unit_names.normalize(n): n for n in names}


def test_an_exact_name_matches_itself():
    assert unit_names.match("unit571", _roster("unit571", "unit2007")) == "unit571"


def test_the_label_is_dropped_from_both_sides():
    """A dispatcher types 571; the roster says unit571."""
    assert unit_names.match("571", _roster("unit571")) == "unit571"
    assert unit_names.match("UNIT: 571", _roster("571")) == "571"


def test_the_digit_run_is_the_last_resort():
    assert unit_names.match("571", _roster("571 - Freightliner")) == "571 - Freightliner"


def test_two_trucks_answering_to_one_label_is_not_a_match():
    """Guessing would route one truck's alerts to another truck's group."""
    assert unit_names.match("571", _roster("unit571", "truck571")) is None


def test_two_trucks_sharing_a_digit_run_is_not_a_match():
    """The looser the pass, the more it can tie -- and a tie stops the search rather
    than falling through to a pass that can only widen it."""
    assert unit_names.match("571", _roster("571 - Freightliner", "unit571 (lease)")) is None


def test_a_name_that_is_in_no_roster_is_not_invented():
    assert unit_names.match("999", _roster("unit571")) is None


# ── the Motive roster client ───────────────────────────────────────────────────

def _client(*pages, fail=False):
    """A roster whose fetch returns `pages` in order, or fails outright."""
    roster = MotiveRoster("test-key")
    calls = []

    async def _fake_get(path, params):
        calls.append((path, params))
        if fail:
            return None
        i = len(calls) - 1
        return pages[i] if i < len(pages) else None

    roster._get = _fake_get
    roster.calls = calls
    return roster


def _page(*numbers, total=None):
    """Motive's own envelope: a list of rows, each wrapping one vehicle."""
    return {
        "vehicles": [{"vehicle": {"id": i, "number": n}} for i, n in enumerate(numbers)],
        "pagination": {"per_page": 100, "page_no": 1,
                       "total": total if total is not None else len(numbers)},
    }


async def test_a_known_number_comes_back_as_motive_spells_it():
    """Routing compares the stored unit against current_vehicle.number by strict
    equality, so the roster's own spelling is the only useful answer."""
    roster = _client(_page("1269", "1270"))
    assert await roster.find_number("1269") == "1269"


async def test_the_label_a_group_title_carries_still_finds_the_truck():
    roster = _client(_page("1269"))
    assert await roster.find_number("UNIT: 1269") == "1269"


async def test_a_number_motive_does_not_have_is_none_not_an_error():
    roster = _client(_page("1269"))
    assert await roster.find_number("9999") is None


async def test_a_flat_row_without_the_wrapper_is_still_read():
    """The envelope shape has never been exercised against a live Motive org -- no API
    key exists for one yet -- and a roster that silently read as empty would refuse
    every unit in the fleet."""
    roster = _client({"vehicles": [{"number": "1269"}], "pagination": {"total": 1}})
    assert await roster.find_number("1269") == "1269"


async def test_the_roster_pages_until_it_has_everything():
    roster = _client(_page(*(str(1000 + i) for i in range(100)), total=150),
                     _page(*(str(1100 + i) for i in range(50)), total=150))
    names = await roster.all_numbers()
    assert len(names) == 150
    assert len(roster.calls) == 2


async def test_an_unreadable_roster_raises_rather_than_reading_as_empty():
    """An empty list would be indistinguishable from a fleet with no trucks, and the
    caller has to tell those apart to decide whether to refuse a unit."""
    roster = _client(fail=True)
    with pytest.raises(MotiveUnavailable):
        await roster.find_number("1269")


async def test_the_roster_is_cached_between_lookups():
    roster = _client(_page("1269", "1270"))
    await roster.find_number("1269")
    await roster.find_number("1270")
    assert len(roster.calls) == 1


async def test_an_empty_answer_does_not_wipe_a_roster_that_was_readable():
    roster = _client(_page("1269"), {"vehicles": [], "pagination": {"total": 0}})
    await roster.find_number("1269")
    roster._fetched_at = None          # force the next lookup to refetch
    assert await roster.find_number("1269") == "1269"


# ── which roster the messages name ─────────────────────────────────────────────

@pytest.fixture
def keys(monkeypatch):
    """Sets the two provider keys. Nothing is configured by default in tests."""
    def _set(samsara="", motive=""):
        monkeypatch.setattr(config, "SAMSARA_API_KEY", samsara)
        monkeypatch.setattr(config, "MOTIVE_API_KEY", motive)
    return _set


@pytest.mark.parametrize("samsara, motive, expected", [
    ("k", "",  "Samsara"),
    ("",  "k", "Motive"),
    ("k", "k", "Samsara or Motive"),
    ("",  "",  ""),
])
def test_the_roster_is_named_from_the_configured_keys(keys, samsara, motive, expected):
    """Telling a Motive fleet their truck "isn't in Samsara" names a system they do not
    use; telling a dual-provider fleet the same thing sends them looking in the wrong
    place for a truck that is in the other one."""
    keys(samsara, motive)
    assert units.roster_names() == expected


# ── resolve_unit across both providers ─────────────────────────────────────────

@pytest.fixture
def rosters(monkeypatch):
    """Scripts what each provider answers. A value is a name, None for "no such truck",
    or an exception instance to raise."""
    def _set(samsara=None, motive=None):
        async def _one(answer):
            if isinstance(answer, Exception):
                raise answer
            return answer

        async def _samsara(_key, unit):
            state["asked"].append("samsara")
            return await _one(samsara)

        async def _motive(_key, unit):
            state["asked"].append("motive")
            return await _one(motive)

        monkeypatch.setattr(units, "lookup_unit", _samsara)
        monkeypatch.setattr(units.motive, "lookup_unit", _motive)
        return state

    state = {"asked": []}
    return _set


async def test_with_no_provider_there_is_nothing_to_check(keys, rosters):
    keys()
    rosters()
    assert await units.resolve_unit("1269") == ("no_roster", "1269")


async def test_a_truck_samsara_knows_resolves_to_samsaras_spelling(keys, rosters):
    keys("k", "k")
    state = rosters(samsara="unit1269", motive="1269")
    assert await units.resolve_unit("1269") == ("ok", "unit1269")
    # Motive is not even asked: the first roster answered.
    assert state["asked"] == ["samsara"]


async def test_samsara_wins_a_disagreement_on_purpose(keys, rosters):
    """Every group paired before this function could read Motive was stored with
    Samsara's spelling. Preferring Motive's would silently re-point those groups the
    next time somebody re-saved one."""
    keys("k", "k")
    rosters(samsara="unit1269", motive="1269")
    status, resolved = await units.resolve_unit("1269")
    assert (status, resolved) == ("ok", "unit1269")


async def test_a_motive_only_truck_now_resolves(keys, rosters):
    """The whole point. This used to be ("missing", …) -- the group could not be paired
    from the panel or the chat, while Motive kept sending that truck's alerts."""
    keys("k", "k")
    state = rosters(samsara=None, motive="1269")
    assert await units.resolve_unit("1269") == ("ok", "1269")
    assert state["asked"] == ["samsara", "motive"]


async def test_a_truck_in_neither_roster_is_missing(keys, rosters):
    keys("k", "k")
    rosters(samsara=None, motive=None)
    assert await units.resolve_unit("9999") == ("missing", "9999")


async def test_one_provider_down_makes_the_answer_unavailable_not_missing(keys, rosters):
    """Absence from one roster is not absence from the fleet. Refusing the unit here
    would refuse a truck that may well be in the roster that didn't answer."""
    keys("k", "k")
    rosters(samsara=None, motive=MotiveUnavailable("down"))
    assert await units.resolve_unit("1269") == ("unavailable", "1269")


async def test_the_other_provider_still_answers_when_one_is_down(keys, rosters):
    keys("k", "k")
    rosters(samsara=SamsaraUnavailable("down"), motive="1269")
    assert await units.resolve_unit("1269") == ("ok", "1269")


async def test_a_motive_only_deployment_no_longer_skips_the_check(keys, rosters):
    """With a Motive key there IS a canonical spelling, so "no_roster" -- which stores
    whatever was typed, unverified -- is the wrong answer for this deployment now."""
    keys("", "k")
    rosters(motive=None)
    assert await units.resolve_unit("1269") == ("missing", "1269")


# ── the merged helpers the two surfaces share ──────────────────────────────────

@pytest.fixture
def listings(monkeypatch):
    def _set(samsara=None, motive=None):
        async def _one(answer):
            if isinstance(answer, Exception):
                raise answer
            return answer or []

        async def _samsara(_key):
            return await _one(samsara)

        async def _motive(_key):
            return await _one(motive)

        monkeypatch.setattr(units, "samsara_list_units", _samsara)
        monkeypatch.setattr(units.motive, "list_units", _motive)
    return _set


async def test_the_picker_offers_both_fleets(keys, listings):
    keys("k", "k")
    listings(samsara=["unit1269"], motive=["1270", "1271"])
    assert await units.list_units_any() == (["1270", "1271", "unit1269"], True)


async def test_one_provider_down_still_gives_a_picker(keys, listings):
    """A partial picker is worth far more than an error, and the server re-checks
    whatever is picked anyway."""
    keys("k", "k")
    listings(samsara=SamsaraUnavailable("down"), motive=["1270"])
    assert await units.list_units_any() == (["1270"], True)


async def test_both_providers_down_is_unavailable_not_an_empty_fleet(keys, listings):
    keys("k", "k")
    listings(samsara=SamsaraUnavailable("down"), motive=MotiveUnavailable("down"))
    assert await units.list_units_any() == ([], False)


async def test_did_you_mean_merges_and_deduplicates(keys, monkeypatch):
    """A truck in both systems, spelled the same way, would otherwise be offered twice
    -- which reads as two different trucks."""
    keys("k", "k")

    async def _samsara(_key, _unit):
        return ["unit1269", "1270"]

    async def _motive(_key, _unit):
        return ["1270", "1271"]

    monkeypatch.setattr(units, "suggest_units", _samsara)
    monkeypatch.setattr(units.motive, "suggest_units", _motive)

    assert await units.suggest_units_any("127") == ["1270", "1271", "unit1269"]
