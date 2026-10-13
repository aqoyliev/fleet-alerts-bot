"""What tells one company's deployment from another's.

One branch runs HF, MZ CARGO and JTR. The code is the same for all three; what differs
is which optional event types a deployment alerts on, where its speeding threshold sits,
and which provider's names it uses — all of it read from that service's variables. These
tests go through the catalog's functions with the arrangement passed in, so they hold
whatever the suite's own environment happens to be.
"""
import importlib
import os

import pytest

from data import config
from data.event_catalog import (
    CORE_EVENT_TYPES, EVENT_TYPES, OPTIONAL_EVENT_TYPES, alerted_types, event_label,
    event_title, filter_rows, unknown_extra_types,
)

JTR_EXTRAS = {"road_facing_cam_obstruction", "driver_facing_cam_obstruction",
              "obstructed_camera", "seat_belt_violation", "near_miss", "drowsiness"}


def _reloaded(**env):
    """data.config as it would load with these variables, restored afterwards."""
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        cfg = importlib.reload(config)
        return {name: getattr(cfg, name) for name in dir(cfg) if name.isupper()}
    finally:
        for k, v in saved.items():
            os.environ[k] = v if v is not None else ""
        importlib.reload(config)


# ── which types a deployment alerts on ─────────────────────────────────────────

def test_with_nothing_set_a_deployment_alerts_on_the_core_list():
    """HF and MZ CARGO. The list they had before the branches were made one."""
    assert alerted_types(set()) == CORE_EVENT_TYPES
    assert CORE_EVENT_TYPES == {
        "hard_brake", "crash", "cell_phone", "stop_sign_violation",
        "forward_collision_warning", "unsafe_parking", "speeding", "harsh_event",
        "inattentive_driving", "drowsy_driving", "harsh_acceleration", "harsh_turn",
        "hard_cornering", "no_seat_belt",
    }


def test_the_optional_types_are_opt_in_one_by_one():
    assert alerted_types({"near_miss"}) == CORE_EVENT_TYPES | {"near_miss"}
    assert "tailgating" not in alerted_types({"near_miss"})


def test_jtrs_variables_give_it_the_list_its_old_build_had():
    """The single-group template's EVENT_TYPE_MAP, which is what JTR has been alerted on
    since it was set up: the core and six more, tailgating not among them."""
    assert alerted_types(JTR_EXTRAS) == CORE_EVENT_TYPES | JTR_EXTRAS
    assert JTR_EXTRAS == OPTIONAL_EVENT_TYPES - {"tailgating"}


def test_a_mistyped_extra_costs_that_type_and_nothing_else():
    extra = {"near_miss", "near-miss", "seatbelt"}
    assert alerted_types(extra) == CORE_EVENT_TYPES | {"near_miss"}
    assert unknown_extra_types(extra) == ["near-miss", "seatbelt"]


def test_naming_a_core_type_as_an_extra_is_neither_an_error_nor_a_typo():
    assert alerted_types({"speeding"}) == CORE_EVENT_TYPES
    assert unknown_extra_types({"speeding"}) == []


def test_extra_event_types_is_read_as_a_comma_separated_list():
    loaded = _reloaded(EXTRA_EVENT_TYPES=" Near_Miss , drowsiness,,")
    assert loaded["EXTRA_EVENT_TYPES"] == {"near_miss", "drowsiness"}
    assert _reloaded(EXTRA_EVENT_TYPES="")["EXTRA_EVENT_TYPES"] == frozenset()


# ── what the panel's Group screen offers ───────────────────────────────────────

def test_the_filter_offers_a_row_for_each_alerted_type_and_no_other():
    for extra in (set(), JTR_EXTRAS, OPTIONAL_EVENT_TYPES):
        alerted = alerted_types(extra)
        offered = {t for types, _, _ in filter_rows(alerted) for t in types}
        assert offered == alerted


def test_a_row_shared_by_two_spellings_shows_only_the_ones_in_use():
    """Drowsiness is drowsy_driving at Samsara and drowsiness at Motive. A deployment
    that takes only the first has a one-type row, not a chip that is half inert."""
    core_rows = {types[0]: types for types, _, _ in filter_rows(alerted_types(set()))}
    jtr_rows = {types[0]: types for types, _, _ in filter_rows(alerted_types(JTR_EXTRAS))}
    assert core_rows["drowsy_driving"] == ("drowsy_driving",)
    assert jtr_rows["drowsy_driving"] == ("drowsy_driving", "drowsiness")
    assert "road_facing_cam_obstruction" not in core_rows
    assert set(jtr_rows["road_facing_cam_obstruction"]) == {
        "road_facing_cam_obstruction", "driver_facing_cam_obstruction", "obstructed_camera"}


def test_every_known_type_has_a_row_to_be_filtered_by():
    offered = {t for types, _, _ in filter_rows(frozenset(EVENT_TYPES)) for t in types}
    assert offered == set(EVENT_TYPES)


# ── what things are called ─────────────────────────────────────────────────────

@pytest.mark.parametrize("event_type, samsara, motive", [
    ("hard_brake", "Harsh Brake", "Hard Brake"),
    ("stop_sign_violation", "Rolling Stop", "Stop Sign Violation"),
])
def test_the_two_types_the_providers_name_differently(event_type, samsara, motive):
    assert event_label(event_type, motive, "samsara") == samsara
    assert event_label(event_type, motive, "motive") == motive
    assert event_title(event_type, "samsara")[1] == samsara.upper()
    assert event_title(event_type, "motive")[1] == motive.upper()
    rows = lambda w: {types[0]: label for types, _, label in
                      filter_rows(CORE_EVENT_TYPES, w)}
    assert rows("samsara")[event_type] == samsara
    assert rows("motive")[event_type] == motive


def test_every_other_type_is_called_the_same_by_both():
    for event_type in set(EVENT_TYPES) - {"hard_brake", "stop_sign_violation"}:
        assert event_title(event_type, "samsara") == event_title(event_type, "motive")


def test_a_fleet_with_any_motive_in_it_uses_motives_names():
    """HF is Samsara only; JTR is Motive only; MZ CARGO is both, and was already saying
    "Hard Brake" on its reports before the branches were made one."""
    assert _reloaded(SAMSARA_API_KEY="k")["EVENT_WORDING"] == "samsara"
    assert _reloaded(MOTIVE_WEBHOOK_SECRET="s")["EVENT_WORDING"] == "motive"
    assert _reloaded(SAMSARA_API_KEY="k", MOTIVE_API_KEY="k")["EVENT_WORDING"] == "motive"


# ── the speeding threshold, and who the company is ─────────────────────────────

def test_speeding_alerts_from_medium_unless_the_deployment_says_otherwise():
    assert _reloaded(SPEEDING_MIN_SEVERITY="")["SPEEDING_MIN_SEVERITY"] == "medium"
    assert _reloaded(SPEEDING_MIN_SEVERITY=" High ")["SPEEDING_MIN_SEVERITY"] == "high"


def test_no_company_is_the_default_one():
    """The branch is nobody's in particular. A deployment that names no company is not
    quietly given HF's name on its reports."""
    loaded = _reloaded(COMPANY_SLUG="", COMPANY_NAME="")
    assert loaded["COMPANY_SLUG"] == "fleet"
    assert loaded["COMPANY_NAME"] == "FLEET"
    assert loaded["COMPANY_NAME_FROM_ENV"] is False


def test_a_slug_alone_names_the_company_until_something_better_does():
    loaded = _reloaded(COMPANY_SLUG="jtr", COMPANY_NAME="")
    assert (loaded["COMPANY_SLUG"], loaded["COMPANY_NAME"]) == ("jtr", "JTR")
    loaded = _reloaded(COMPANY_SLUG="hf", COMPANY_NAME="HAULAGE FREIGHT LLC")
    assert loaded["COMPANY_NAME"] == "HAULAGE FREIGHT LLC"
    assert loaded["COMPANY_NAME_FROM_ENV"] is True
