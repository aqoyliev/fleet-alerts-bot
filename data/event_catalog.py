"""Every event type this build knows, which of them this deployment alerts on, what
each is called, and the list an admin picks from in the alerts group's filter.

One branch runs several companies, and they do not all want the same events or call them
the same thing. So there are three layers here, and only the first is the same for
everyone:

  * EVENT_TYPES — every type the code can recognise, with its emoji and headline.
  * ALERTED_TYPES — the ones this deployment acts on: the core list, plus whichever of
    the OPTIONAL_EVENT_TYPES its EXTRA_EVENT_TYPES variable names. Anything else is
    recognised and dropped — not alerted, not stored, not counted in a report. That is
    different from the group's filter below, which only decides what reaches the group.
  * GROUP_FILTER_TYPES — the alerted types as the panel's Group screen shows them, one
    chip per row.

Crash is a row like any other. On single-company it is absent, because there a crash
bypasses the groups entirely; here the one group is where a crash is reported, so
whether it receives them is the same kind of choice as for any other type — and a
company that wants them kept out of the group has that chip off.

The group's filter is stored in alert_group_event_types as an ALLOWLIST: no rows means
"all types", otherwise only the listed types are delivered. next_event_filter() encodes
the toggle rule that keeps the UI and that convention in sync (including collapsing a
full allowlist back to the empty "all" state).

Most rows are one real event type. A few are several, because the two providers name the
same thing differently and an admin picking what the group receives doesn't think in
providers: harsh_turn is Samsara's name and hard_cornering is Motive's; drowsy_driving
and drowsiness likewise; a camera obstruction has three spellings. Such a row carries all
of its types and toggles them as one unit; the Alerts feed and every stored violation
still keep the specific type the provider reported. The first type in a row is also that
row's token in the panel's API — the others only ever travel together with it.
"""
from data import config

# Event type → (emoji, headline). The headline is the one an alert card opens with, in
# Motive's wording where the providers differ (see _SAMSARA_WORDING). Insertion order is
# the order ⚙️ Settings lists them in.
EVENT_TYPES: dict[str, tuple[str, str]] = {
    "hard_brake":                    ("🛑", "HARD BRAKE"),
    "crash":                         ("💥", "CRASH DETECTED"),
    "cell_phone":                    ("📵", "CELL PHONE USAGE"),
    "stop_sign_violation":           ("🛑", "STOP SIGN VIOLATION"),
    "forward_collision_warning":     ("⚠️", "FORWARD COLLISION WARNING"),
    "unsafe_parking":                ("🅿️", "UNSAFE PARKING"),
    "speeding":                      ("🚨", "SPEEDING OVER POSTED"),
    "harsh_event":                   ("⚠️", "HARSH EVENT"),
    "inattentive_driving":           ("😵", "INATTENTIVE DRIVING"),
    "drowsy_driving":                ("😴", "POSSIBLE DROWSINESS"),
    "harsh_acceleration":            ("🚀", "HARSH ACCELERATION"),
    "harsh_turn":                    ("↩️", "HARSH TURN"),
    "hard_cornering":                ("↩️", "HARSH TURN"),
    "no_seat_belt":                  ("🚫", "NO SEAT BELT"),
    # ── optional: alerted on only where EXTRA_EVENT_TYPES names them ──
    "road_facing_cam_obstruction":   ("📷", "ROAD CAMERA OBSTRUCTED"),
    "driver_facing_cam_obstruction": ("📷", "DRIVER CAMERA OBSTRUCTED"),
    "obstructed_camera":             ("📷", "CAMERA OBSTRUCTED"),
    "seat_belt_violation":           ("🔒", "SEAT BELT VIOLATION"),
    "near_miss":                     ("⚠️", "NEAR MISS"),
    "drowsiness":                    ("😴", "DROWSINESS"),
    "tailgating":                    ("🚗", "TAILGATING"),
}

# The types that are one company's choice rather than everybody's. HF and MZ CARGO take
# none of them; JTR, a Motive fleet, takes all but tailgating. The first two are Motive's
# names for a blocked camera and the third is Samsara's; seat_belt_violation, near_miss
# and drowsiness are Motive's; tailgating is sent by both.
OPTIONAL_EVENT_TYPES = frozenset({
    "road_facing_cam_obstruction", "driver_facing_cam_obstruction", "obstructed_camera",
    "seat_belt_violation", "near_miss", "drowsiness", "tailgating",
})

CORE_EVENT_TYPES = frozenset(EVENT_TYPES) - OPTIONAL_EVENT_TYPES

# Where Samsara's name for an event differs from Motive's: type → (headline, label).
_SAMSARA_WORDING = {
    "hard_brake":          ("HARSH BRAKE", "Harsh Brake"),
    "stop_sign_violation": ("ROLLING STOP", "Rolling Stop"),
}


def alerted_types(extra=None) -> frozenset[str]:
    """The types a deployment acts on: the core, plus the optional ones `extra` names.

    A name in `extra` that is not an optional type is ignored rather than refused — a
    typo in a variable should cost that one type, not the boot — and so is a core type,
    which is alerted on anyway.
    """
    extra = config.EXTRA_EVENT_TYPES if extra is None else extra
    return CORE_EVENT_TYPES | (OPTIONAL_EVENT_TYPES & frozenset(extra))


def unknown_extra_types(extra=None) -> list[str]:
    """Names in EXTRA_EVENT_TYPES that match nothing — for one warning at startup."""
    extra = config.EXTRA_EVENT_TYPES if extra is None else extra
    return sorted(frozenset(extra) - frozenset(EVENT_TYPES))


def event_title(event_type: str, wording: str | None = None) -> tuple[str, str] | None:
    """(emoji, headline) for a type in the given provider's wording, or None for a type
    this build has never heard of. `wording` is "samsara" or "motive"; left out, it is
    the deployment's own (config.EVENT_WORDING)."""
    known = EVENT_TYPES.get(event_type)
    if known is None:
        return None
    emoji, title = known
    if (wording or config.EVENT_WORDING) == "samsara" and event_type in _SAMSARA_WORDING:
        title = _SAMSARA_WORDING[event_type][0]
    return emoji, title


def event_label(event_type: str, default: str, wording: str | None = None) -> str:
    """`default`, unless this type is one the providers name differently and the wording
    in force is Samsara's. For the places that have their own title-cased labels — the
    reports and the filter chips below — and need only the two that vary."""
    if (wording or config.EVENT_WORDING) == "samsara" and event_type in _SAMSARA_WORDING:
        return _SAMSARA_WORDING[event_type][1]
    return default


# (event_types, emoji, label) for every row there could be, in the order the panel shows
# them. A deployment's rows are these with the types it does not alert on taken out, and
# a row left with none taken out with them.
_ALL_ROWS = [
    (("speeding",),                     "🚨", "Speeding"),
    (("hard_brake",),                   "🛑", "Hard Brake"),
    (("harsh_acceleration",),           "🚀", "Harsh Acceleration"),
    (("harsh_turn", "hard_cornering"),  "↩️", "Harsh Turn"),
    (("forward_collision_warning",),    "⚠️", "Forward Collision"),
    (("tailgating",),                   "🚗", "Tailgating"),
    (("stop_sign_violation",),          "🛑", "Stop Sign Violation"),
    (("cell_phone",),                   "📵", "Cell Phone Usage"),
    (("seat_belt_violation",),          "🔒", "Seat Belt Violation"),
    (("no_seat_belt",),                 "🚫", "No Seat Belt"),
    (("inattentive_driving",),          "😵", "Inattentive Driving"),
    # Samsara calls it drowsy_driving, Motive calls it drowsiness. One row, both types:
    # a filter that listed only Samsara's spelling would silently drop every Motive
    # drowsiness alert for the group.
    (("drowsy_driving", "drowsiness"),  "😴", "Drowsiness"),
    (("road_facing_cam_obstruction", "driver_facing_cam_obstruction", "obstructed_camera"),
     "📷", "Camera Obstructed"),
    (("unsafe_parking",),               "🅿️", "Unsafe Parking"),
    (("near_miss",),                    "⚠️", "Near Miss"),
    (("crash",),                        "💥", "Crash"),
    # Samsara's generic harsh event: what an alert falls back to when its specific type
    # is one we don't map. Listed so a group that sets any filter at all can keep
    # receiving it — leaving it out would make "everything except speeding" quietly mean
    # "everything except speeding and any harsh event we couldn't name".
    (("harsh_event",),                  "⚠️", "Other Harsh Event"),
]


def filter_rows(alerted=None, wording: str | None = None) -> list[tuple[tuple[str, ...], str, str]]:
    """The filter's rows for a deployment that alerts on `alerted`, labelled in `wording`."""
    alerted = ALERTED_TYPES if alerted is None else alerted
    rows = []
    for types, emoji, label in _ALL_ROWS:
        kept = tuple(t for t in types if t in alerted)
        if kept:
            rows.append((kept, emoji, event_label(kept[0], label, wording)))
    return rows


ALERTED_TYPES = alerted_types()

# (event_types, emoji, label) — order is the order shown in the panel. A row's first
# event_type doubles as its token wherever the UI needs one string per row.
GROUP_FILTER_TYPES = filter_rows()

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
