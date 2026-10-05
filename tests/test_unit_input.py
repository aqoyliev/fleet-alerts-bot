"""What counts as a unit worth looking up.

The input rule used to require a digit, on the theory that a unit is a number. Samsara
disagrees: this fleet has trucks on the roster named after their driver and after their
VIN tail. The panel's picker was offering those names and the server was refusing them,
so a group could be pointed at a truck from the list and told to type a number instead.

The rule now only drops what cannot be looked up at all. The roster, through
resolve_unit, decides the rest -- which is the check that actually keeps a group from
following a truck that does not exist.
"""

import pytest

from utils.units import MAX_UNIT_LEN, clean_unit


# -- what survives the tidy-up ---------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("1234", "1234"),
    ("  1234  ", "1234"),
    ("#1234", "1234"),
    ("# 1234", "1234"),
    ("unit571", "unit571"),
])
def test_a_number_still_comes_through_the_same_way(raw, expected):
    assert clean_unit(raw) == expected


@pytest.mark.parametrize("name", [
    "Ruhoallah Assadi",   # named after its driver
    "GFXH-ARH-SHW",       # named after its VIN tail
    "C-NVS",
])
def test_a_roster_name_with_no_digit_in_it_is_accepted(name):
    """The regression this file exists for. These are real spellings off the vehicle
    roster, offered by the panel's own picker; refusing them contradicted the list the
    dispatcher was picking from."""
    assert clean_unit(name) == name


# -- what does not ---------------------------------------------------------------

@pytest.mark.parametrize("raw", ["", "   ", "#", " # ", None])
def test_nothing_to_look_up_is_rejected(raw):
    assert clean_unit(raw) is None


def test_longer_than_the_column_is_rejected():
    """Stored unverified it would be truncated, and a truncated unit matches nothing."""
    assert clean_unit("x" * MAX_UNIT_LEN) == "x" * MAX_UNIT_LEN
    assert clean_unit("x" * (MAX_UNIT_LEN + 1)) is None


def test_both_surfaces_share_one_rule():
    """units.py's docstring promises the command and the panel agree exactly. They can
    only drift if one of them stops calling this."""
    import inspect

    from handlers.groups import group_events
    from utils.webapp import api

    assert "clean_unit" in inspect.getsource(group_events.cmd_setunit)
    assert "clean_unit" in inspect.getsource(api.set_unit)
    assert "isdigit" not in inspect.getsource(api.set_unit)
    assert "isdigit" not in inspect.getsource(group_events.cmd_setunit)
