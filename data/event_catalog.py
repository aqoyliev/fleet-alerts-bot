"""Canonical, user-facing event types offered in the per-group event filter.

EVENT_TYPE_MAP (in utils.webhook_handler) carries provider aliases and duplicates; this
is the curated, de-duplicated list a group admin actually toggles. Crash is intentionally
absent — crash alerts bypass this filter entirely, going to subscribed admin DMs plus the
deployment's CRASH_GROUP_ID chat and no other group, so there is nothing here to filter.

A group's filter is stored in group_event_types as an ALLOWLIST: no rows means "all types",
otherwise only the listed types are delivered. next_event_filter() encodes the toggle rule
that keeps the UI and that convention in sync (including collapsing a full allowlist back to
the empty "all" state).

Most rows are one real event type. "Camera Obstructed" is three: Motive reports road- and
driver-facing obstruction separately and Samsara reports one generic obstruction, and an
admin picking what a group receives doesn't think in providers — they think "is the camera
blocked." So that row carries all three underlying types and toggles them as one unit; the
Alerts feed and every stored violation still keep the real, specific type Motive or Samsara
reported. The first type in a row is also that row's token in callback data and the panel's
API — the other two only ever travel together with it, never alone.
"""

# (event_types, emoji, label) — order is the order shown in the toggle keyboard. A row's
# first event_type doubles as its token wherever the UI needs one string per row.
GROUP_FILTER_TYPES = [
    (("speeding",),                  "🚨", "Speeding"),
    (("hard_brake",),                "🛑", "Hard Brake"),
    (("harsh_acceleration",),        "🚀", "Harsh Acceleration"),
    (("harsh_turn",),                "↩️", "Harsh Turn"),
    (("forward_collision_warning",), "⚠️", "Forward Collision"),
    (("stop_sign_violation",),       "🛑", "Stop Sign Violation"),
    (("cell_phone",),                "📵", "Cell Phone Usage"),
    (("seat_belt_violation",),       "🔒", "Seat Belt Violation"),
    (("no_seat_belt",),              "🚫", "No Seat Belt"),
    (("inattentive_driving",),       "😵", "Inattentive Driving"),
    # Samsara calls it drowsy_driving, Motive calls it drowsiness. One row, both types:
    # a filter that listed only Samsara's spelling silently dropped every Motive
    # drowsiness alert for that group.
    (("drowsy_driving", "drowsiness"), "😴", "Drowsiness"),
    (("road_facing_cam_obstruction", "driver_facing_cam_obstruction", "obstructed_camera"),
     "📷", "Camera Obstructed"),
    (("unsafe_parking",),            "🅿️", "Unsafe Parking"),
    (("near_miss",),                 "⚠️", "Near Miss"),
    # Samsara's generic harsh event: what an alert falls back to when its specific type
    # is one we don't map. Listed so a group that sets any filter at all keeps receiving
    # it — leaving it out made "everything except speeding" quietly mean "everything
    # except speeding and any harsh event we couldn't name".
    (("harsh_event",),               "⚠️", "Other Harsh Event"),
]

GROUP_FILTER_TYPE_SET = {t for types, _, _ in GROUP_FILTER_TYPES for t in types}

# token (a row's first event_type) → every event_type that row's toggle covers.
_TOKEN_GROUPS: dict[str, frozenset[str]] = {
    types[0]: frozenset(types) for types, _, _ in GROUP_FILTER_TYPES
}


def next_event_filter(current: set[str], toggled: str) -> set[str]:
    """Return the new allowlist after toggling one event type in the group filter UI.

    `toggled` is a row's token; for the multi-type "Camera Obstructed" row it stands in
    for all three of that row's real types, which are added or removed together so the
    chip's on/off state stays a single, unambiguous fact.

    - From the empty "all" state, toggling a type OFF materializes the allowlist to
      everything except that group.
    - Otherwise the group is added/removed from the current allowlist as a whole.
    - If the result covers every filterable type, it collapses back to the empty "all"
      state so the group stays on "receive everything" (including future new types).
    """
    group = _TOKEN_GROUPS.get(toggled, {toggled})
    if not current:
        result = set(GROUP_FILTER_TYPE_SET) - group
    elif group <= current:
        result = current - group
    else:
        result = current | group

    if result >= GROUP_FILTER_TYPE_SET:
        return set()
    return result
