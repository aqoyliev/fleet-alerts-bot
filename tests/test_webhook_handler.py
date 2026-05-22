import base64
import hashlib
import hmac

from utils.webhook_handler import (
    ALLOWED_TYPES,
    _INWARD_ONLY_TYPES,
    _SAMSARA_HARSH_TYPE_MAP,
    _clean_vehicle,
    _event_id_to_bigint,
    _format_crash_initial,
    _format_crash_video_caption,
    _format_event,
    _get_event_type,
    _get_vehicle,
    _samsara_signed_payload,
    _vehicle_code_line,
    _verify_hmac,
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


# ── _event_id_to_bigint ──────────────────────────────────────────────────────────

def test_event_id_bigint_numeric_passthrough():
    # Motive ids are numeric and must map to themselves so dedup matches the raw id.
    assert _event_id_to_bigint("123456789") == 123456789
    assert _event_id_to_bigint(123456789) == 123456789

def test_event_id_bigint_uuid_is_stable():
    # Samsara UUIDs must hash to the same int every call so the UNIQUE constraint dedups.
    u = "86f91905-f4b7-4a32-8d63-26bece8b7cb2"
    assert _event_id_to_bigint(u) == _event_id_to_bigint(u)

def test_event_id_bigint_uuid_fits_signed_64bit():
    # Must fit a Postgres BIGINT (signed 64-bit) or the INSERT raises NumericValueOutOfRange.
    val = _event_id_to_bigint("86f91905-f4b7-4a32-8d63-26bece8b7cb2")
    assert isinstance(val, int)
    assert -(2 ** 63) <= val < 2 ** 63

def test_event_id_bigint_distinct_uuids_differ():
    a = _event_id_to_bigint("86f91905-f4b7-4a32-8d63-26bece8b7cb2")
    b = _event_id_to_bigint("00000000-0000-0000-0000-000000000000")
    assert a != b

def test_event_id_bigint_empty_returns_none():
    assert _event_id_to_bigint("") is None
    assert _event_id_to_bigint(None) is None


# ── _format_event HTML escaping ───────────────────────────────────────────────────

def test_format_event_escapes_driver_and_location():
    # Unescaped &, <, > would make Telegram reject the whole message (parse_mode=HTML),
    # so dynamic values must be escaped while the layout's own tags stay intact.
    event = {
        "action": "driver_performance_event_created",
        "type": "hard_brake",
        "driver": {"name": "Tom & <Jerry>"},
        "location": "5th & Main <St>",
    }
    out = _format_event(event)
    assert "Tom &amp; &lt;Jerry&gt;" in out
    assert "5th &amp; Main &lt;St&gt;" in out
    # the raw, unescaped forms must not survive into the message
    assert "Tom & <Jerry>" not in out
    assert "5th & Main <St>" not in out
    # layout tags are still real HTML
    assert "<b>" in out and "</b>" in out


# ── Samsara v1 webhook HMAC verification ──────────────────────────────────────────

_SECRET = "supersecret"
_TS = "1714382131519"
_BODY = b'{"eventId":"86f91905-f4b7-4a32-8d63-26bece8b7cb2"}'


def _samsara_sign(secret: str, timestamp: str, body: bytes) -> str:
    """Reproduce what Samsara puts in X-Samsara-Signature: hex HMAC over v1:<ts>:<body>."""
    msg = _samsara_signed_payload(timestamp, body)
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()

def test_samsara_signed_payload_format():
    assert _samsara_signed_payload("123", b"abc") == b"v1:123:abc"

def test_samsara_valid_signature_verifies():
    sig = _samsara_sign(_SECRET, _TS, _BODY)
    assert _verify_hmac(_SECRET, _samsara_signed_payload(_TS, _BODY), sig) is True

def test_samsara_base64_encoded_signature_verifies():
    # Samsara may send the digest base64-encoded; _verify_hmac must accept that too.
    msg = _samsara_signed_payload(_TS, _BODY)
    sig_b64 = base64.b64encode(hmac.new(_SECRET.encode(), msg, hashlib.sha256).digest()).decode()
    assert _verify_hmac(_SECRET, msg, sig_b64) is True

def test_samsara_tampered_body_fails():
    sig = _samsara_sign(_SECRET, _TS, _BODY)
    assert _verify_hmac(_SECRET, _samsara_signed_payload(_TS, _BODY + b"x"), sig) is False

def test_samsara_wrong_timestamp_fails():
    # Replaying the body under a different timestamp must not validate.
    sig = _samsara_sign(_SECRET, _TS, _BODY)
    assert _verify_hmac(_SECRET, _samsara_signed_payload("9999999999999", _BODY), sig) is False

def test_samsara_raw_body_without_v1_prefix_fails():
    # Guards the regression: signing the raw body (the old scheme) must not pass.
    sig = _samsara_sign(_SECRET, _TS, _BODY)
    assert _verify_hmac(_SECRET, _BODY, sig) is False


# ── Samsara harsh-event polling config ────────────────────────────────────────────

def test_inward_only_types_are_producible_by_harsh_type_map():
    # The inward-only short-circuit only fires when _SAMSARA_HARSH_TYPE_MAP resolves a
    # Samsara harshEventType to one of these. If a type here can't be produced by the
    # map, its branch is dead — keep them in sync.
    producible = set(_SAMSARA_HARSH_TYPE_MAP.values())
    missing = _INWARD_ONLY_TYPES - producible
    assert not missing, f"inward-only types unreachable via harsh-type map: {missing}"

def test_crash_maps_to_crash_so_poll_extension_triggers():
    # _fetch_samsara_harsh_event extends the window on harshEventType == "Crash";
    # the resolved type must be 'crash' for downstream routing to agree.
    assert _SAMSARA_HARSH_TYPE_MAP["Crash"] == "crash"

def test_inward_only_excludes_forward_facing_types():
    # Forward-camera events must NOT short-circuit on the inward clip alone.
    assert "hard_brake" not in _INWARD_ONLY_TYPES
    assert "crash" not in _INWARD_ONLY_TYPES
    assert "harsh_acceleration" not in _INWARD_ONLY_TYPES


# ── _vehicle_code_line / _format_crash_pending ────────────────────────────────────

def test_vehicle_code_line_strips_prefix_and_splits():
    assert _vehicle_code_line("Unit 528609 DEYBI") == "<code>528609</code> <code>DEYBI</code>"

def test_vehicle_code_line_single_token():
    assert _vehicle_code_line("528609") == "<code>528609</code>"

def test_vehicle_code_line_escapes():
    assert _vehicle_code_line("A&B <c>") == "<code>A&amp;B</code> <code>&lt;c&gt;</code>"

def test_vehicle_code_line_empty_is_dash():
    assert _vehicle_code_line("") == "—"

def test_format_crash_initial_is_full_card_plus_video_note():
    # The first crash message carries the FULL details (so no-media crashes still
    # inform everyone), plus a hint that the video may follow.
    event = {"type": "crash", "_source": "samsara",
             "vehicle": {"number": "Unit 528609 D & J"},
             "location": "I-95 N & Exit 12",
             "start_time": "2026-04-29T09:15:31Z"}
    out = _format_crash_initial(event)
    assert "💥 <b>CRASH DETECTED</b>" in out      # full card title
    assert "<code>528609</code>" in out
    assert "📍 <b>Location:</b> I-95 N &amp; Exit 12" in out
    assert "D &amp; J" in out                      # escaped
    assert "Video pending" in out

def test_format_crash_video_caption_is_short():
    event = {"vehicle": {"number": "Unit 528609 D & J"}}
    out = _format_crash_video_caption(event)
    assert out == "💥 <b>CRASH</b> — <code>528609</code> <code>D &amp; J</code>"
    assert "Location" not in out                   # the detail lives in the first alert
