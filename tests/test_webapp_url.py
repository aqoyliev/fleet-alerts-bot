"""Working out where this deployment is reachable from a phone.

The panel needs exactly one thing the container cannot otherwise know: its own public
URL. Getting this wrong is quiet — the bot boots, alerts keep flowing, and the only
symptom is a keyboard button that opens nothing (or no button at all) — so the resolution
order is pinned here rather than left to be discovered in production.
"""

import pytest

from data.config import _resolve_webapp_url


def test_an_explicit_url_is_used_as_given():
    assert _resolve_webapp_url("https://fleet.example", "") == "https://fleet.example"


def test_a_trailing_slash_is_stripped():
    """The panel path is appended as "/panel/", so a trailing slash here would produce
    "//panel/" — which most servers tolerate and Telegram's URL validation does not."""
    assert _resolve_webapp_url("https://fleet.example/", "") == "https://fleet.example"


def test_railway_supplies_the_domain_when_nothing_was_configured():
    """The point of the whole function: a Railway deployment needs no manual step."""
    assert (_resolve_webapp_url("", "web-production-0e3ac.up.railway.app")
            == "https://web-production-0e3ac.up.railway.app")


def test_an_explicit_url_outranks_the_railway_domain():
    """A custom domain, or a proxy in front of the service, is a fact only the operator
    knows — so a stated value must never be second-guessed by the platform's."""
    assert (_resolve_webapp_url("https://panel.cpt.example", "web-production.up.railway.app")
            == "https://panel.cpt.example")


@pytest.mark.parametrize("domain", ["https://host.example", "http://host.example"])
def test_a_domain_that_already_carries_a_scheme_is_not_prefixed_again(domain):
    assert _resolve_webapp_url("", domain) == domain


def test_nothing_configured_stays_blank():
    """Blank is a supported state, not a failure: the panel is still served, it just
    isn't advertised on anyone's keyboard. Returning a placeholder here instead would put
    a dead button in front of every admin."""
    assert _resolve_webapp_url("", "") == ""
