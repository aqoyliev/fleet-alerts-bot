"""
Manual test runner for webhook_handler formatting logic.
Run: python test_webhook.py
No bot/Telegram connection needed.
"""

import sys
import os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(__file__))

# Patch entire data package and config before any imports
import types
import unittest.mock as mock

data_pkg = types.ModuleType("data")
config_mod = types.ModuleType("data.config")
config_mod.ADMINS = []
config_mod.BOT_TOKEN = "test"
config_mod.COMPANY_SLUG = "testco"
config_mod.COMPANY_NAME = "Test Co"
config_mod.SPEEDING_MIN_SEVERITY = "high"
config_mod.SAMSARA_API_KEY = ""
config_mod.SAMSARA_WEBHOOK_SECRET = ""
config_mod.MOTIVE_WEBHOOK_SECRET = ""
data_pkg.config = config_mod
sys.modules["data"] = data_pkg
sys.modules["data.config"] = config_mod

# Stub aiogram and aiohttp so heavy deps aren't needed
sys.modules["aiogram"] = mock.MagicMock()
sys.modules["aiogram.types"] = mock.MagicMock()
sys.modules["aiohttp"] = mock.MagicMock()
sys.modules["aiohttp.web"] = mock.MagicMock()

# Stub sub-modules that pull heavy deps
sys.modules["utils.motive"] = mock.MagicMock()
sys.modules["utils.notify_admins"] = mock.MagicMock()
sys.modules["utils.set_bot_commands"] = mock.MagicMock()

# Load webhook_handler directly from file path
import importlib.util
spec = importlib.util.spec_from_file_location(
    "webhook_handler",
    os.path.join(os.path.dirname(__file__), "utils", "webhook_handler.py"),
)
wh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wh)

_get_event_type = wh._get_event_type
_get_vehicle = wh._get_vehicle
_format_event = wh._format_event
_get_direct_video_urls = wh._get_direct_video_urls
ALLOWED_TYPES = wh.ALLOWED_TYPES


# ── Sample payloads ────────────────────────────────────────────────────────────

SPEEDING = {
    "action": "speeding_event_created",
    "id": 2453258760,
    "max_over_speed_in_kph": 26.808185544,
    "avg_over_speed_in_kph": 25.495718014,
    "min_posted_speed_limit_in_kph": 88.513694763,
    "avg_vehicle_speed": 114.00941278,
    "duration": 255,
    "start_time": "2026-04-08T11:29:59Z",
    "end_time": "2026-04-08T11:34:14Z",
    "coaching_status": "coachable",
    "current_driver": None,
    "current_vehicle": {"ID": 2199435, "Number": "20286"},
    "metadata": {"severity": "critical", "trigger": "speeding"},
    "nominatim_location": "Pennsylvania Tpk, Juniata Township, Pennsylvania, 15553, United States",
}

UNSAFE_PARKING = {
    "action": "driver_performance_event_created",
    "id": 3328319009,
    "type": "unsafe_parking",
    "start_time": "2026-04-08T11:16:24Z",
    "location": "60.2 mi E of Box Elder, SD",
    "coaching_status": "coachable",
    "coachable_behaviors": ["unsafe_parking"],
    "primary_behavior": ["unsafe_parking"],
    "current_vehicle": {"id": 2224612, "number": "1199"},
    "metadata": {"severity": "medium"},
    "camera_media": {
        "id": 3328319009,
        "available": True,
        "downloadable_videos": {
            "front_facing_plain_url": "https://example.com/front.mp4",
            "driver_facing_plain_url": "https://example.com/driver.mp4",
        },
    },
}

STOP_SIGN_NO_VIDEO = {
    "action": "driver_performance_event_created",
    "id": 3328325693,
    "type": "stop_sign_violation",
    "start_time": "2026-04-08T11:28:52Z",
    "location": "Longwood, FL",
    "coaching_status": "pending_review",
    "current_vehicle": {"id": 2238865, "number": "1269"},
    "metadata": {"severity": None},
    "camera_media": {
        "id": 3328325693,
        "available": True,
        "downloadable_videos": {
            "front_facing_plain_url": None,
            "driver_facing_plain_url": None,
        },
        "auto_transcode_status": "not started",
    },
}

HARD_BRAKE = {
    "action": "driver_performance_event_created",
    "id": 9001,
    "type": "hard_brake",
    "start_time": "2026-04-08T10:00:00Z",
    "location": "Nashville, TN",
    "intensity": "0.42",
    "coaching_status": "pending_review",
    "driver": {"name": "John Doe"},
    "current_vehicle": {"id": 111, "number": "1050"},
    "metadata": {"severity": "high"},
}

CELL_PHONE = {
    "action": "driver_performance_event_created",
    "id": 9002,
    "type": "cell_phone",
    "start_time": "2026-04-08T09:30:00Z",
    "location": "Chicago, IL",
    "duration": 45,
    "coaching_status": "coachable",
    "driver": {"name": "Jane Smith"},
    "current_vehicle": {"id": 222, "number": "2020"},
    "metadata": {"severity": "low"},
}

TAILGATING = {
    "action": "driver_performance_event_created",
    "id": 9003,
    "type": "tailgating",
    "start_time": "2026-04-08T09:00:00Z",
    "current_vehicle": {"id": 333, "number": "3030"},
}

# ── Tests ──────────────────────────────────────────────────────────────────────

def sep(title):
    print(f"\n{'-'*50}")
    print(f"  {title}")
    print('-'*50)


def test_event_type():
    sep("_get_event_type")
    assert _get_event_type(SPEEDING) == "speeding", "speeding via action field"
    assert _get_event_type(UNSAFE_PARKING) == "unsafe_parking"
    assert _get_event_type(STOP_SIGN_NO_VIDEO) == "stop_sign_violation"
    assert _get_event_type(TAILGATING) == "tailgating"
    print("  PASS: event type detection")


def test_vehicle():
    sep("_get_vehicle")
    assert _get_vehicle(SPEEDING) == "20286", f"got {_get_vehicle(SPEEDING)}"
    assert _get_vehicle(UNSAFE_PARKING) == "1199", f"got {_get_vehicle(UNSAFE_PARKING)}"
    assert _get_vehicle(STOP_SIGN_NO_VIDEO) == "1269"
    print("  PASS: vehicle extraction (uppercase + lowercase current_vehicle)")


def test_allowed_types():
    sep("ALLOWED_TYPES filter")
    assert "speeding" in ALLOWED_TYPES
    assert "tailgating" not in ALLOWED_TYPES
    assert "hard_brake" in ALLOWED_TYPES
    print("  PASS: tailgating filtered, others allowed")


def test_direct_video_urls():
    sep("_get_direct_video_urls")
    urls = _get_direct_video_urls(UNSAFE_PARKING)
    assert len(urls) == 2, f"expected 2, got {len(urls)}"
    urls_none = _get_direct_video_urls(STOP_SIGN_NO_VIDEO)
    assert urls_none == [], f"expected [], got {urls_none}"
    print("  PASS: direct video URL extraction")


def test_format():
    sep("_format_event — formatted output")
    for label, payload in [
        ("SPEEDING", SPEEDING),
        ("UNSAFE PARKING", UNSAFE_PARKING),
        ("STOP SIGN (no video)", STOP_SIGN_NO_VIDEO),
        ("HARD BRAKE", HARD_BRAKE),
        ("CELL PHONE", CELL_PHONE),
    ]:
        print(f"\n[{label}]")
        print(_format_event(payload))


# ── Run ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_event_type()
    test_vehicle()
    test_allowed_types()
    test_direct_video_urls()
    test_format()
    print("\n✓ All tests passed\n")
