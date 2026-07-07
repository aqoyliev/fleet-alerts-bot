"""Samsara speeding enrichment: formatter output and client helpers."""

from utils.webhook_handler import _format_event
from utils.samsara.client import _format_location, _normalize


SAMSARA_SPEEDING = {
    "id": "e4f1a9c2-0000-0000-0000-000000000000",
    "type": "speeding",
    "vehicle": {"number": "6676   O/O"},
    "driver": {"name": ""},
    "start_time": "2026-07-07T06:49:00Z",
    "location": "",
    "nominatim_location": "",
    "severity": "critical",
    "_source": "samsara",
    "_samsara_vehicle_id": "281475003824064",
}

DETAILS = {
    "max_speed_mph": 78.918,
    "posted_limit_mph": 65.244,
    "severity": "severe",
    "duration_seconds": 82,
    "location": "I 10, Eminence, TX, 77597",
    "driver_name": None,
}

GPS_ONLY = {
    "max_speed_mph": 78.918,
    "posted_limit_mph": None,
    "severity": None,
    "duration_seconds": None,
    "location": "I 10, Eminence, TX, 77597",
    "driver_name": None,
}


def test_enriched_interval():
    out = _format_event(SAMSARA_SPEEDING, "Gurman", DETAILS)
    assert "Max Speed:</b> 78.9 mph" in out
    assert "Speed Limit:</b> 65.2 mph" in out
    assert "Over Posted:</b> 13.7 mph" in out
    assert "Duration:</b> 82s" in out
    assert "I 10, Eminence" in out
    assert "🆘 Critical" in out  # payload severity wins over interval severity
    assert "via Samsara" in out


def test_enriched_gps_only():
    out = _format_event(SAMSARA_SPEEDING, "Gurman", GPS_ONLY)
    assert "Max Speed:</b> 78.9 mph" in out
    assert "Speed Limit" not in out
    assert "Over Posted" not in out
    assert "Duration" not in out
    assert "I 10, Eminence" in out


def test_no_enrichment_unchanged():
    out = _format_event(SAMSARA_SPEEDING, "Gurman", None)
    assert "Max Speed" not in out
    assert "Location" not in out
    assert "via Samsara" in out


def test_severity_fallback_from_interval():
    event = {k: v for k, v in SAMSARA_SPEEDING.items() if k != "severity"}
    out = _format_event(event, "Gurman", DETAILS)
    assert "🆘 Severe" in out


def test_driver_fallback_from_interval():
    out = _format_event(SAMSARA_SPEEDING, "Gurman", {**DETAILS, "driver_name": "John Smith"})
    assert "Driver:</b> John Smith" in out


def test_motive_speeding_untouched():
    motive = {
        "action": "speeding_event_created",
        "id": 1,
        "avg_vehicle_speed": 114.0,
        "min_posted_speed_limit_in_kph": 88.5,
        "max_over_speed_in_kph": 26.8,
        "duration": 255,
        "start_time": "2026-04-08T11:29:59Z",
        "current_vehicle": {"ID": 1, "Number": "20286"},
        "metadata": {"severity": "critical"},
        "nominatim_location": "Pennsylvania Tpk, PA",
    }
    out = _format_event(motive)
    assert "Average Speed:</b> 70.8 mph" in out
    assert out.count("Duration") == 1
    assert "Max Speed" not in out
    assert "via Samsara" not in out


def test_client_helpers():
    assert _normalize("6676   O/O") == "6676 o/o"
    assert _format_location({"formattedLocation": "X"}) == "X"
    assert _format_location({"address": {"city": "York", "state": "PA"}}) == "York, PA"
    assert _format_location({"latitude": 40.1, "longitude": -78.2}) == "40.10000, -78.20000"
    assert _format_location({}) == ""
