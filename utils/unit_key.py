"""Canonical key for matching truck unit numbers across systems.

Unit numbers are free-form text ("1304", "F9121", "T-120", legacy "<1304 >").
Motive/Samsara vehicle names, the pti-checker groups table and driver_groups all
spell them differently, so every comparison goes through this key.
"""
import re

_NON_ALNUM = re.compile(r"[^0-9A-Z]")


def normalize_unit(value: str | None) -> str:
    return _NON_ALNUM.sub("", (value or "").upper())


def unit_candidates(value: str | None) -> list[str]:
    """Keys to try, in order, when looking a truck up in another system.

    Motive/Samsara vehicle names sometimes carry a suffix after the unit number
    ("4063   O/O" owner-operator, "212566  SUB", "1304 ACTIVE"), while the other
    systems store just the unit ("4063"). The full key always comes first, so an exact
    match wins; the first whitespace-separated token is a fallback, and only when it
    contains a digit and differs from the full key ("O/O 4063" has no fallback).
    """
    full = normalize_unit(value)
    candidates = [full]
    tokens = (value or "").split()
    if len(tokens) > 1 and any(ch.isdigit() for ch in tokens[0]):
        candidates.append(normalize_unit(tokens[0]))
    return list(dict.fromkeys(c for c in candidates if c))
