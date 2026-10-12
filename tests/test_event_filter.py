from data.event_catalog import (
    GROUP_FILTER_TYPE_SET, GROUP_FILTER_TYPES, complete_rows, next_event_filter,
)


def test_from_all_toggling_one_off_materializes_rest():
    # Empty = "all". Toggling one OFF should yield every other type.
    result = next_event_filter(set(), "speeding")
    assert result == GROUP_FILTER_TYPE_SET - {"speeding"}
    assert "speeding" not in result


def test_toggle_off_a_type_from_allowlist():
    current = {"speeding", "hard_brake", "cell_phone"}
    result = next_event_filter(current, "hard_brake")
    assert result == {"speeding", "cell_phone"}


def test_toggle_on_a_type():
    current = {"speeding"}
    result = next_event_filter(current, "cell_phone")
    assert result == {"speeding", "cell_phone"}


def test_completing_the_set_collapses_to_all():
    # Re-adding the last missing type covers everything → collapse back to empty ("all").
    current = GROUP_FILTER_TYPE_SET - {"unsafe_parking"}
    result = next_event_filter(current, "unsafe_parking")
    assert result == set()


def test_readding_after_materialize_returns_to_all():
    # all -> drop speeding -> add speeding back should return to "all" (empty).
    dropped = next_event_filter(set(), "speeding")
    restored = next_event_filter(dropped, "speeding")
    assert restored == set()


# ── the harsh-turn row moves both underlying types as one ───────────────────────

TURN_TYPES = {"harsh_turn", "hard_cornering"}


def test_toggling_the_turn_row_off_drops_both():
    result = next_event_filter(set(), "harsh_turn")
    assert not (TURN_TYPES & result)
    assert result == GROUP_FILTER_TYPE_SET - TURN_TYPES


def test_toggling_the_turn_row_on_adds_both():
    result = next_event_filter({"speeding"}, "harsh_turn")
    assert result == {"speeding"} | TURN_TYPES


def test_toggling_the_turn_row_off_again_removes_both():
    result = next_event_filter({"speeding"} | TURN_TYPES, "harsh_turn")
    assert result == {"speeding"}


def test_a_half_present_turn_row_toggles_fully_on():
    """The filter carried over from the old schema lists harsh_turn alone. Tapping the
    chip must not leave it in that ambiguous state."""
    result = next_event_filter({"speeding", "harsh_turn"}, "harsh_turn")
    assert result == {"speeding"} | TURN_TYPES


def test_harsh_turn_is_a_single_toggle_row():
    """Samsara calls it harsh_turn and Motive hard_cornering; the admin sees one chip."""
    rows = [types for types, _, _ in GROUP_FILTER_TYPES if TURN_TYPES & set(types)]
    assert len(rows) == 1
    assert set(rows[0]) == TURN_TYPES
    # The row's token is Samsara's spelling: it is the one HF's fleet actually sends.
    assert rows[0][0] == "harsh_turn"


# ── an allowlist from before rows existed is widened to whole rows ──────────────

def test_complete_rows_fills_in_the_other_half_of_a_row():
    assert complete_rows(["speeding", "harsh_turn"]) == {"speeding"} | TURN_TYPES


def test_complete_rows_leaves_single_type_rows_and_strangers_alone():
    """A type the catalog has never heard of is kept, not dropped: it is in the old
    table because somebody put it there, and losing it silently is not this function's
    decision to make."""
    assert complete_rows(["crash", "tailgating"]) == {"crash", "tailgating"}


def test_complete_rows_of_nothing_is_still_the_all_types_state():
    assert complete_rows([]) == set()


def test_the_filter_hf_was_seeded_with_draws_every_one_of_its_chips_on():
    """seed.sql's ten types, as ensure_group carries them over. Each of them belongs to a
    row, and after widening every one of those rows is covered whole — so the panel shows
    ten chips lit, not nine and a half."""
    seeded = ["hard_brake", "harsh_acceleration", "harsh_turn", "cell_phone",
              "drowsy_driving", "no_seat_belt", "speeding", "crash",
              "stop_sign_violation", "forward_collision_warning"]
    carried = complete_rows(seeded)
    for types, _, _ in GROUP_FILTER_TYPES:
        touched = set(types) & carried
        assert not touched or touched == set(types), types
    # And it stays a custom filter: the types seed.sql left out are still left out.
    assert carried < GROUP_FILTER_TYPE_SET
    assert not {"unsafe_parking", "inattentive_driving", "harsh_event"} & carried


# ── every type the bot can actually deliver is filterable ──────────────────────
#
# A group with any filter at all receives ONLY the types on its allowlist, and that
# allowlist is materialized from this catalog. So a deliverable type missing from the
# catalog is not merely absent from the toggle screen: the first time anyone touches the
# filter, that type stops arriving and nothing on screen says so.

def test_the_catalog_and_the_alert_types_are_the_same_set():
    """Both directions. A deliverable type that is not filterable goes quiet the first
    time the filter is touched; a filterable type that is never delivered is a chip that
    does nothing."""
    from utils.webhook_handler import ALLOWED_TYPES

    assert GROUP_FILTER_TYPE_SET == ALLOWED_TYPES


def test_crash_is_filterable_here():
    """Unlike single-company, where a crash never reaches a group and so has no chip.
    Here the group is where a crash is reported, and whether it is, is a choice."""
    assert "crash" in GROUP_FILTER_TYPE_SET
    assert "crash" not in next_event_filter(set(), "crash")


def test_no_event_type_sits_in_two_rows():
    seen: list[str] = [t for types, _, _ in GROUP_FILTER_TYPES for t in types]
    assert len(seen) == len(set(seen))


def test_a_generic_harsh_event_survives_a_custom_filter():
    """The fallback type an unmapped Samsara harsh event lands on. Turning speeding off
    must not take it with them."""
    result = next_event_filter(set(), "speeding")
    assert "harsh_event" in result
