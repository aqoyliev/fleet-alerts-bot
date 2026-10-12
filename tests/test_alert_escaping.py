"""Every value that reaches a message from outside the code is HTML-escaped.

The bot sends with parse_mode=HTML, and Telegram rejects a message whose entities don't
parse. A rejection is not a cosmetic problem: the alert is not delivered at all. Driver
names, reverse-geocoded addresses and vehicle names are all written by someone else, so
each one is escaped at the point it is interpolated.

`&` is the realistic trigger — addresses like "Main St & 5th" are ordinary — and `<` is
the one that would otherwise let a crafted vehicle name inject markup into the group.
"""

from utils.daily_report import _format_daily_report
from utils.tg_text import esc
import utils.webhook_handler as wh


def _event(**over):
    event = {
        "id": 1,
        "type": "hard_brake",
        "vehicle": {"number": "1234"},
        "driver": {"name": "Ann"},
        "location": "Main St",
        "start_time": "2026-09-19T14:00:00Z",
    }
    event.update(over)
    return event


# ── the alert card ─────────────────────────────────────────────────────────────

def test_an_ampersand_in_the_address_survives_as_an_entity():
    card = wh._format_event(_event(location="Main St & 5th Ave"))

    assert "Main St &amp; 5th Ave" in card
    assert "Main St & 5th" not in card


def test_a_tag_in_the_driver_name_is_shown_not_interpreted():
    card = wh._format_event(_event(driver={"name": "<b>Ann</b>"}))

    assert "&lt;b&gt;Ann&lt;/b&gt;" in card
    # The card's own markup is untouched — only the interpolated value is escaped.
    assert "<b>Driver:</b>" in card


def test_the_vehicle_name_is_escaped_inside_its_code_tag():
    card = wh._format_event(_event(vehicle={"number": "unit<1"}))

    assert "<code>unit&lt;1</code>" in card


def test_the_speeding_card_escapes_its_enriched_location():
    card = wh._format_event(
        _event(type="speeding"),
        samsara={"location": "I-95 & Exit 3", "max_speed_mph": 81.0},
    )

    assert "I-95 &amp; Exit 3" in card


def test_the_crash_video_caption_escapes_the_unit():
    assert "<code>a&amp;b</code>" in wh._format_crash_video_caption(
        _event(type="crash", vehicle={"number": "a&b"}))


# ── the reports ────────────────────────────────────────────────────────────────

def test_the_daily_report_escapes_the_unit_and_the_company():
    """The unit is whatever Samsara's roster calls the truck, and it is read back out of
    the database long after the alert that carried it was escaped."""
    text = _format_daily_report(
        "A & B Freight", [{"event_type": "speeding", "vehicle_number": "57<1", "total": 2}],
        "May 07, 2026")

    assert "A &amp; B Freight" in text
    assert "Unit 57&lt;1" in text


# ── the helper itself ──────────────────────────────────────────────────────────

def test_esc_covers_the_three_characters_telegram_cares_about():
    assert esc("<&>") == "&lt;&amp;&gt;"


def test_esc_renders_a_missing_value_as_nothing_rather_than_none():
    assert esc(None) == ""
