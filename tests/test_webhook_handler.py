from utils.webhook_handler import (
    ALLOWED_TYPES,
    _clean_vehicle,
    _get_event_type,
    _get_vehicle,
)


# ── _get_event_type ────────────────────────────────────────────────────────────

def test_event_type_speeding_via_action():
    assert _get_event_type({"action": "speeding_event_created"}) == "speeding"

def test_event_type_driver_performance_uses_type_field():
    assert _get_event_type({"action": "driver_performance_event_created", "type": "hard_brake"}) == "hard_brake"
    assert _get_event_type({"action": "driver_performance_event_created", "type": "cell_phone"}) == "cell_phone"
    assert _get_event_type({"action": "driver_performance_event_created", "type": "no_seat_belt"}) == "no_seat_belt"

def test_event_type_samsara_harsh_event():
    assert _get_event_type({"type": "harsh_event"}) == "harsh_event"

def test_event_type_empty_falls_back_to_empty_string():
    assert _get_event_type({}) == ""


# ── _get_vehicle ───────────────────────────────────────────────────────────────

def test_get_vehicle_motive_uppercase_keys():
    # Motive speeding events use uppercase ID/Number
    event = {"current_vehicle": {"ID": 123, "Number": "1196"}}
    assert _get_vehicle(event) == "1196"

def test_get_vehicle_driver_performance_lowercase_keys():
    # driver_performance events use lowercase id/number
    event = {"current_vehicle": {"id": 456, "number": "1199 - JOHN DOE"}}
    assert _get_vehicle(event) == "1199 - JOHN DOE"

def test_get_vehicle_missing_returns_fallback():
    assert _get_vehicle({}) == "—"
    assert _get_vehicle({"current_vehicle": {}}) == "—"


# ── _clean_vehicle ─────────────────────────────────────────────────────────────

_UNIT_STRIP_CASES = [
    ("unit 1196 - JOHN",         "1196 - JOHN"),
    ("Unit 1196 - JOHN",         "1196 - JOHN"),
    ("UNIT 1196 - JOHN",         "1196 - JOHN"),
    ("unit#1196",                "1196"),
    ("unit# 1196",               "1196"),
    ("unit:1196",                "1196"),
    ("unit: 1196",               "1196"),
    ("unit-1196",                "1196"),
    ("unit  1196",               "1196"),
    ("uNIT 1196",                "1196"),
    ("1196 - JOHN",              "1196 - JOHN"),   # no prefix — unchanged
    ("UNIT 1985 - ISAAC BRAVE",  "1985 - ISAAC BRAVE"),
]

def test_clean_vehicle_strips_unit_prefix():
    for raw, expected in _UNIT_STRIP_CASES:
        event = {"current_vehicle": {"id": 1, "number": raw}}
        result = _clean_vehicle(event)
        assert result == expected, f"raw={raw!r}: expected {expected!r}, got {result!r}"

def test_clean_vehicle_truncates_at_50_chars():
    long_name = "unit " + "A" * 60
    event = {"current_vehicle": {"id": 1, "number": long_name}}
    assert len(_clean_vehicle(event)) <= 50

def test_clean_vehicle_no_vehicle_returns_fallback():
    assert _clean_vehicle({}) == "—"


# ── ALLOWED_TYPES ──────────────────────────────────────────────────────────────

def test_allowed_types_includes_core_events():
    for t in ["speeding", "hard_brake", "cell_phone", "no_seat_belt",
              "stop_sign_violation", "drowsy_driving", "crash"]:
        assert t in ALLOWED_TYPES, f"{t!r} missing from ALLOWED_TYPES"

def test_allowed_types_excludes_tailgating():
    assert "tailgating" not in ALLOWED_TYPES
