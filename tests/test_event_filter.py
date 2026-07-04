from data.event_catalog import GROUP_FILTER_TYPE_SET, next_event_filter


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
