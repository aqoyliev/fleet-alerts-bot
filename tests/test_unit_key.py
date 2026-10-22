"""Tests for utils/unit_key.py."""
import pytest

from utils.unit_key import unit_candidates


@pytest.mark.parametrize("value, expected", [
    ("4063   O/O", ["4063OO", "4063"]),
    ("4063 O/O", ["4063OO", "4063"]),
    ("1304 active", ["1304ACTIVE", "1304"]),
    ("212566  SUB", ["212566SUB", "212566"]),
    ("T-120", ["T120"]),
    ("F9121", ["F9121"]),
    ("O/O 4063", ["OO4063"]),
    ("", []),
    (None, []),
])
def test_unit_candidates(value, expected):
    assert unit_candidates(value) == expected
