"""Every value that reaches a message from outside the code is HTML-escaped.

The bot sends with parse_mode=HTML, and Telegram rejects a message whose entities don't
parse. A rejection is not a cosmetic problem: the alert is not delivered at all. Driver
names, reverse-geocoded addresses, vehicle names and group titles are all written by
someone else, so each one is escaped at the point it is interpolated.

`&` is the realistic trigger — addresses like "Main St & 5th" are ordinary — and `<` is
the one that would otherwise let a crafted group title inject markup into an admin's DM.
"""

from utils import group_texts
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


# ── the group-facing texts ─────────────────────────────────────────────────────

def test_a_group_setup_message_escapes_the_unit_it_echoes():
    assert "&lt;script&gt;" in group_texts.joined_registered("<script>")


def test_an_unknown_unit_message_escapes_the_roster_suggestions():
    text = group_texts.joined_unknown_unit("A & B Trucking", "57<1", ["unit&1"])

    assert "A &amp; B Trucking" in text
    assert "57&lt;1" in text
    assert "unit&amp;1" in text


# ── the helper itself ──────────────────────────────────────────────────────────

def test_esc_covers_the_three_characters_telegram_cares_about():
    assert esc("<&>") == "&lt;&amp;&gt;"


def test_esc_renders_a_missing_value_as_nothing_rather_than_none():
    assert esc(None) == ""
