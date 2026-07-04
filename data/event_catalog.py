"""Canonical, user-facing event types offered in the per-group event filter.

EVENT_TYPE_MAP (in utils.webhook_handler) carries provider aliases and duplicates; this
is the curated, de-duplicated list a group admin actually toggles. Crash is intentionally
absent — crash alerts go to admin DMs only, never to any group, so there is nothing to
filter.

A group's filter is stored in group_event_types as an ALLOWLIST: no rows means "all types",
otherwise only the listed types are delivered. next_event_filter() encodes the toggle rule
that keeps the UI and that convention in sync (including collapsing a full allowlist back to
the empty "all" state).
"""

# (event_type, emoji, label) — order is the order shown in the toggle keyboard.
GROUP_FILTER_TYPES = [
    ("speeding",                      "🚨", "Speeding"),
    ("hard_brake",                    "🛑", "Hard Brake"),
    ("harsh_acceleration",            "🚀", "Harsh Acceleration"),
    ("harsh_turn",                    "↩️", "Harsh Turn"),
    ("forward_collision_warning",     "⚠️", "Forward Collision"),
    ("stop_sign_violation",           "🛑", "Stop Sign Violation"),
    ("cell_phone",                    "📵", "Cell Phone Usage"),
    ("seat_belt_violation",           "🔒", "Seat Belt Violation"),
    ("no_seat_belt",                  "🚫", "No Seat Belt"),
    ("inattentive_driving",           "😵", "Inattentive Driving"),
    ("drowsy_driving",                "😴", "Drowsiness"),
    ("road_facing_cam_obstruction",   "📷", "Road Camera Obstructed"),
    ("driver_facing_cam_obstruction", "📷", "Driver Camera Obstructed"),
    ("unsafe_parking",                "🅿️", "Unsafe Parking"),
    ("near_miss",                     "⚠️", "Near Miss"),
]

GROUP_FILTER_TYPE_SET = {t for t, _, _ in GROUP_FILTER_TYPES}


def next_event_filter(current: set[str], toggled: str) -> set[str]:
    """Return the new allowlist after toggling one event type in the group filter UI.

    - From the empty "all" state, toggling a type OFF materializes the allowlist to
      everything except that type.
    - Otherwise the type is added/removed from the current allowlist.
    - If the result covers every filterable type, it collapses back to the empty "all"
      state so the group stays on "receive everything" (including future new types).
    """
    if not current:
        result = set(GROUP_FILTER_TYPE_SET) - {toggled}
    elif toggled in current:
        result = current - {toggled}
    else:
        result = current | {toggled}

    if result >= GROUP_FILTER_TYPE_SET:
        return set()
    return result
