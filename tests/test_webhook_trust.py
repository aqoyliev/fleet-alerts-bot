"""What the two public webhook URLs accept.

Both endpoints sit on the open internet under fixed paths, and whatever they accept is
put straight in front of the fleet's admins — a forged crash included. Two rules decide
that, and both are here:

  • a delivery that cannot be signature-checked is refused, unless the deployment has
    explicitly opted into unsigned deliveries, and
  • a Samsara delivery whose signed timestamp is old is refused as a replay, so a
    captured request cannot be sent again tomorrow.
"""

import time

import pytest

from data import config
import utils.webhook_handler as wh


# ── no secret configured ───────────────────────────────────────────────────────

def test_an_unsigned_delivery_is_refused_by_default(monkeypatch):
    monkeypatch.setattr(config, "ALLOW_UNSIGNED_WEBHOOKS", False)

    assert wh._unsigned_allowed("motive", "203.0.113.9") is False


def test_the_opt_out_lets_an_unsigned_delivery_through(monkeypatch):
    """For a provider that genuinely cannot sign, or the first hour of bring-up."""
    monkeypatch.setattr(config, "ALLOW_UNSIGNED_WEBHOOKS", True)

    assert wh._unsigned_allowed("motive", "203.0.113.9") is True


def test_the_refusal_names_what_to_set(monkeypatch, caplog):
    monkeypatch.setattr(config, "ALLOW_UNSIGNED_WEBHOOKS", False)
    with caplog.at_level("ERROR"):
        wh._unsigned_allowed("samsara", "203.0.113.9")

    logged = caplog.text
    assert "SAMSARA_WEBHOOK_SECRET" in logged
    assert "ALLOW_UNSIGNED_WEBHOOKS" in logged


# ── replay ─────────────────────────────────────────────────────────────────────

def test_a_fresh_timestamp_in_seconds_is_accepted():
    assert wh._timestamp_is_fresh(str(int(time.time()))) is True


def test_a_fresh_timestamp_in_milliseconds_is_accepted():
    """Samsara's header unit is not worth a production surprise — both are read."""
    assert wh._timestamp_is_fresh(str(int(time.time() * 1000))) is True


@pytest.mark.parametrize("age", [3600, 86400])
def test_an_old_timestamp_is_refused(age):
    assert wh._timestamp_is_fresh(str(int(time.time()) - age)) is False


def test_a_timestamp_from_the_future_is_refused():
    assert wh._timestamp_is_fresh(str(int(time.time()) + 3600)) is False


def test_ordinary_clock_drift_is_tolerated():
    assert wh._timestamp_is_fresh(str(int(time.time()) - 30)) is True


@pytest.mark.parametrize("raw", ["", "not-a-number", None])
def test_a_missing_or_unreadable_timestamp_is_refused(raw):
    assert wh._timestamp_is_fresh(raw) is False
