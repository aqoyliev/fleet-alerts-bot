"""Parse a driver group's unit/truck number from its Telegram title + description.

Driver groups are named and described by dispatchers in a handful of conventions, e.g.

    Title: "UNIT: 708432 ZANFARA SALIFOU (CD)"     Desc: "UNIT: 708432 ..."
    Title: "1274 DHIDINBE, MADAR / YUSUF ..."       Desc: "Truck# 1274"
    Title: "TRUCK# 212575 LATODDRICK BARBER (LEASE)" Desc: "Truck 212575"

extract_vehicle_number() pulls the unit number out of those so alerts for that unit can
be routed to its group. It deliberately anchors to the UNIT/TRUCK keywords first (never a
bare "#") so it doesn't grab a trailer ("TRL # 5324016"), phone number, or strap count.
"""
import re

# "UNIT: 708432", "TRUCK# 212575", "Truck 1274" — keyword, optional : or #, then digits.
_KEYWORD_RE = re.compile(r"(?:UNIT|TRUCK)\s*[:#]*\s*(\d{3,7})", re.IGNORECASE)

# Fallback: a title that simply starts with the unit number, e.g. "1274 DHIDINBE ...".
# Applied to the title only — descriptions often start with a driver name or contain a
# trailer number, which this would otherwise mistake for the unit.
_LEADING_RE = re.compile(r"^\s*#?\s*(\d{3,7})\b")


def extract_vehicle_number(title: str | None, description: str | None) -> str | None:
    """Return the unit/truck number as a digit string, or None if none can be found."""
    title = title or ""
    description = description or ""

    for text in (title, description):
        m = _KEYWORD_RE.search(text)
        if m:
            return m.group(1)

    m = _LEADING_RE.match(title)
    if m:
        return m.group(1)

    return None
