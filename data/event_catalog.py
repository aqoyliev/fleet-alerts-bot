"""Canonical, user-facing event types offered in the alerts group's event filter.

EVENT_TYPE_MAP (in utils.webhook_handler) carries provider aliases and duplicates; this
is the curated, de-duplicated list an admin actually toggles in the panel. It covers
every type this deployment alerts on, so the two have to move together — a type added to
EVENT_TYPE_MAP and left out of here is one a filtered group can never be given
(tests/test_event_filter.py holds them to that).

Crash is a row like any other on this branch. On single-company it is absent, because
there a crash bypasses the groups entirely; here the one group is where a crash is
reported, so whether it receives them is the same kind of choice as for any other type.

The group's filter is stored in alert_group_event_types as an ALLOWLIST: no rows means
"all types", otherwise only the listed types are delivered. next_event_filter() encodes
the toggle rule that keeps the UI and that convention in sync (including collapsing a
full allowlist back to the empty "all" state).

Most rows are one real event type. "Harsh Turn" is two: harsh_turn is Samsara's name and
hard_cornering is Motive's, and an admin picking what the group receives doesn't think
in providers. So that row carries both and toggles them as one unit; the Alerts feed and
every stored violation still keep the specific type the provider reported. The first
type in a row is also that row's token in the panel's API — the others only ever travel
together with it, never alone.
"""

# (event_types, emoji, label) — order is the order shown in the panel. A row's first
# event_type doubles as its token wherever the UI needs one string per row.
GROUP_FILTER_TYPES = [
    (("speeding",),                     "🚨", "Speeding"),
    (("hard_brake",),                   "🛑", "Harsh Brake"),
    (("harsh_acceleration",),           "🚀", "Harsh Acceleration"),
    (("harsh_turn", "hard_cornering"),  "↩️", "Harsh Turn"),
    (("forward_collision_warning",),    "⚠️", "Forward Collision"),
    (("stop_sign_violation",),          "🛑", "Rolling Stop"),
    (("cell_phone",),                   "📵", "Cell Phone Usage"),
    (("no_seat_belt",),                 "🚫", "No Seat Belt"),
    (("inattentive_driving",),          "😵", "Inattentive Driving"),
    (("drowsy_driving",),               "😴", "Drowsiness"),
    (("unsafe_parking",),               "🅿️", "Unsafe Parking"),
    (("crash",),                        "💥", "Crash"),
    # Samsara's generic harsh event: what an alert falls back to when its specific type
    # is one we don't map. Listed so a group that sets any filter at all can keep
    # receiving it — leaving it out would make "everything except speeding" quietly mean
    # "everything except speeding and any harsh event we couldn't name".
    (("harsh_event",),                  "⚠️", "Other Harsh Event"),
]

GROUP_FILTER_TYPE_SET = {t for types, _, _ in GROUP_FILTER_TYPES for t in types}

# token (a row's first event_type) → every event_type that row's toggle covers.
_TOKEN_GROUPS: dict[str, frozenset[str]] = {
    types[0]: frozenset(types) for types, _, _ in GROUP_FILTER_TYPES
}

# any event_type → every event_type sharing its row.
_ROW_OF: dict[str, frozenset[str]] = {
    t: frozenset(types) for types, _, _ in GROUP_FILTER_TYPES for t in types
}


def complete_rows(event_types) -> set[str]:
    """Widen an allowlist so that every row it touches is covered whole.

    The panel draws one chip per row and shows it on only when all of that row's types
    are allowed. An allowlist written before rows existed — the one carried over from the
    old schema, where harsh_turn was listed and hard_cornering was not — would otherwise
    draw that chip as off while half of it was being delivered.
    """
    out: set[str] = set()
    for event_type in event_types:
        out |= _ROW_OF.get(event_type, {event_type})
    return out


def next_event_filter(current: set[str], toggled: str) -> set[str]:
    """Return the new allowlist after toggling one event type in the group filter UI.

    `toggled` is a row's token; for a multi-type row it stands in for all of that row's
    real types, which are added or removed together so the chip's on/off state stays a
    single, unambiguous fact.

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
