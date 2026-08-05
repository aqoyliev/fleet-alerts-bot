"""Tests for what the bot says in a driver group when it joins, and for /help.

These are wording tests, which is unusual — but the wording is the feature here. A group
that isn't registered looks identical to one that is (the bot is sitting in the chat
either way), so the only thing separating "set up" from "silently receiving nothing" is
whether the join message said so and named the fix.
"""
from utils import group_texts as gt

COMPANY = "CPT TRANSPORT INC"


# ── the group registered fine ──────────────────────────────────────────────────

def test_registered_names_the_unit_and_the_off_switch():
    text = gt.joined_registered("unit571")
    assert "unit571" in text
    assert "/disable" in text
    # The way back from a mute has to appear wherever the mute is offered.
    assert "/enable" in text


def test_registered_offers_a_correction_path():
    """Auto-detection picks the number out of the group title, so it can pick the wrong
    one; the confirmation is the moment a driver would notice."""
    assert "/setunit" in gt.joined_registered("unit571")


def test_unit_is_not_named_twice_when_samsara_already_says_unit():
    """CPT's roster is literally 'unit571', so a blind 'unit {name}' gives 'unit unit571'
    in the message that is meant to show the setup worked."""
    assert "unit unit571" not in gt.joined_registered("unit571")
    assert "unit unit571" not in gt.help_text(COMPANY, unit="unit571")


def test_a_bare_number_still_gets_the_word_unit():
    """Fleets that name trucks '571' need the label, or the message reads as a stray
    number."""
    assert "unit 571" in gt.joined_registered("571")
    assert "unit 571" in gt.help_text(COMPANY, unit="571")


def test_main_group_is_told_it_gets_everything():
    text = gt.joined_main_group(COMPANY)
    assert "every" in text.lower()
    assert COMPANY in text
    # It is not tied to one truck, so it must not be told to set a unit.
    assert "/setunit" not in text


# ── the group did not register ─────────────────────────────────────────────────

def test_missing_unit_says_nothing_will_arrive_and_how_to_fix_it():
    text = gt.joined_needs_unit()
    assert "/setunit" in text
    assert "nothing will be sent" in text.lower()


def test_unknown_unit_names_the_unit_and_lists_suggestions():
    text = gt.joined_unknown_unit(COMPANY, "5711", ["unit571", "unit786"])
    assert "5711" in text
    assert "unit571" in text and "unit786" in text
    assert "/setunit" in text


def test_unknown_unit_without_suggestions_has_no_dangling_label():
    """suggest_units returns [] when Samsara can't be reached, and an empty 'Closest
    trucks in Samsara:' line is worse than no line."""
    text = gt.joined_unknown_unit(COMPANY, "5711", [])
    assert "Closest" not in text
    assert "/setunit" in text


def test_unknown_unit_still_says_nothing_will_arrive():
    assert "nothing will be sent" in gt.joined_unknown_unit(COMPANY, "5711", []).lower()


# ── /help in a group ───────────────────────────────────────────────────────────

def test_help_in_a_configured_group_names_that_group_s_unit():
    text = gt.help_text(COMPANY, unit="unit571")
    assert "unit571" in text
    assert "/disable" in text and "/enable" in text


def test_help_in_an_unregistered_group_leads_with_the_problem():
    """The likeliest reason a driver types /help is that nothing has ever arrived."""
    text = gt.help_text(COMPANY, unit=None)
    assert "no alerts are being sent" in text.lower()
    assert "/setunit" in text


def test_help_hides_admin_commands_from_drivers():
    text = gt.help_text(COMPANY, unit="unit571", is_admin=False)
    for admin_only in ("/removegroup", "/events", "/event_list"):
        assert admin_only not in text


def test_help_does_not_offer_drivers_company_wide_reports():
    """/report and /top summarize every unit's violations. They work in a group, but a
    driver group is deliberately not told about them (utils/set_bot_commands)."""
    text = gt.help_text(COMPANY, unit="unit571", is_admin=False)
    assert "/report" not in text
    assert "/top" not in text


def test_help_shows_admins_the_full_set():
    text = gt.help_text(COMPANY, unit="unit571", is_admin=True)
    for cmd in ("/events", "/event_list", "/report", "/top", "/removegroup"):
        assert cmd in text


def test_help_in_the_main_group_does_not_offer_setunit():
    text = gt.help_text(COMPANY, unit=None, is_main=True)
    assert "/setunit" not in text
    # ...and must not claim the group is broken just because it has no unit of its own.
    assert "no alerts are being sent" not in text.lower()
