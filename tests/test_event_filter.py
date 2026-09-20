from data.event_catalog import GROUP_FILTER_TYPE_SET, GROUP_FILTER_TYPES, next_event_filter


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
    current = GROUP_FILTER_TYPE_SET - {"near_miss"}
    result = next_event_filter(current, "near_miss")
    assert result == set()


def test_readding_after_materialize_returns_to_all():
    # all -> drop speeding -> add speeding back should return to "all" (empty).
    dropped = next_event_filter(set(), "speeding")
    restored = next_event_filter(dropped, "speeding")
    assert restored == set()


# ── the camera-obstructed row moves all three underlying types as one ───────────

CAMERA_TYPES = {"road_facing_cam_obstruction", "driver_facing_cam_obstruction", "obstructed_camera"}


def test_toggling_the_camera_row_off_drops_all_three():
    result = next_event_filter(set(), "road_facing_cam_obstruction")
    assert not (CAMERA_TYPES & result)
    assert result == GROUP_FILTER_TYPE_SET - CAMERA_TYPES


def test_toggling_the_camera_row_on_adds_all_three():
    current = {"speeding"}
    result = next_event_filter(current, "road_facing_cam_obstruction")
    assert result == {"speeding"} | CAMERA_TYPES


def test_toggling_the_camera_row_off_again_removes_all_three():
    current = {"speeding"} | CAMERA_TYPES
    result = next_event_filter(current, "road_facing_cam_obstruction")
    assert result == {"speeding"}


def test_a_partially_present_camera_group_toggles_fully_on():
    """Old data (from before this row merged) might have only one of the three set —
    tapping the chip must not leave it in that ambiguous state."""
    current = {"speeding", "obstructed_camera"}
    result = next_event_filter(current, "road_facing_cam_obstruction")
    assert result == {"speeding"} | CAMERA_TYPES


def test_camera_obstruction_is_a_single_toggle_row():
    """The filter list once showed three near-identical camera chips (road-facing,
    driver-facing, Samsara's generic one). They now render as one."""
    rows_touching_camera = [types for types, _, _ in GROUP_FILTER_TYPES if CAMERA_TYPES & set(types)]
    assert len(rows_touching_camera) == 1
    assert set(rows_touching_camera[0]) == CAMERA_TYPES


# ── every type the bot can actually deliver is filterable ──────────────────────
#
# A group with any filter at all receives ONLY the types on its allowlist, and that
# allowlist is materialized from this catalog. So a deliverable type missing from the
# catalog is not merely absent from the toggle screen: the first time anyone touches the
# filter, that type stops arriving and nothing on screen says so. Drowsiness (Motive
# spells it drowsiness, Samsara drowsy_driving) and Samsara's generic harsh_event were
# both missing, and both went quiet.

def test_every_deliverable_event_type_can_be_filtered():
    from utils.webhook_handler import ALLOWED_TYPES

    # Crash is the one deliberate exception: it bypasses group filtering entirely.
    deliverable = ALLOWED_TYPES - {"crash", "hard_cornering"}
    assert deliverable <= GROUP_FILTER_TYPE_SET


def test_both_spellings_of_drowsiness_travel_together():
    """Motive says drowsiness, Samsara says drowsy_driving, the admin sees one chip."""
    result = next_event_filter(set(), "drowsy_driving")
    assert "drowsiness" not in result
    assert "drowsy_driving" not in result


def test_a_generic_harsh_event_survives_a_custom_filter():
    """The fallback type an unmapped Samsara harsh event lands on. Turning speeding off
    must not take it with them."""
    result = next_event_filter(set(), "speeding")
    assert "harsh_event" in result
