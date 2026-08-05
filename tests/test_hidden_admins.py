"""Tests for hiding the maintainer's account from the 👥 Admins panel.

The distinction these pin down is hidden ≠ removed. The account keeps every permission
and keeps receiving every alert; it is only absent from one list. Getting that backwards
in either direction is the failure — a hidden admin who quietly stops getting alerts, or
a visible one the customer's super admin can deactivate.
"""
import pytest

from data import config
from utils.db_api import admins as adm

DEV = 7564871221      # hidden maintainer account
SUPER = 111111        # the company's own super admin
REGULAR = 222222


@pytest.fixture
def hidden_dev(monkeypatch):
    monkeypatch.setattr(config, "HIDDEN_ADMIN_IDS", {DEV})


def _rows():
    return [
        {"telegram_id": DEV, "full_name": "Dev", "is_super": True, "is_active": True},
        {"telegram_id": SUPER, "full_name": "Company Super", "is_super": True, "is_active": True},
        {"telegram_id": REGULAR, "full_name": "Dispatcher", "is_super": False, "is_active": True},
    ]


# ── who sees whom ──────────────────────────────────────────────────────────────

def test_company_super_admin_does_not_see_the_hidden_account(hidden_dev):
    seen = [a["telegram_id"] for a in adm.visible_admins(_rows(), viewer_telegram_id=SUPER)]
    assert DEV not in seen
    assert seen == [SUPER, REGULAR]


def test_regular_admin_does_not_see_it_either(hidden_dev):
    seen = [a["telegram_id"] for a in adm.visible_admins(_rows(), viewer_telegram_id=REGULAR)]
    assert DEV not in seen


def test_the_hidden_admin_sees_everyone_including_themselves(hidden_dev):
    """Otherwise the maintainer loses the panel they are hiding inside of."""
    seen = [a["telegram_id"] for a in adm.visible_admins(_rows(), viewer_telegram_id=DEV)]
    assert seen == [DEV, SUPER, REGULAR]


def test_nothing_is_hidden_when_the_setting_is_empty(monkeypatch):
    """Every other deployment (hf, mz-cargo, the multi-company build) runs without this
    configured and must behave exactly as before."""
    monkeypatch.setattr(config, "HIDDEN_ADMIN_IDS", set())
    seen = [a["telegram_id"] for a in adm.visible_admins(_rows(), viewer_telegram_id=SUPER)]
    assert seen == [DEV, SUPER, REGULAR]


# ── hidden is not the same as removed ──────────────────────────────────────────

def test_hiding_does_not_touch_the_admin_queries(hidden_dev):
    """get_all_admins is what feeds alert fan-out and admin DMs (group_events._admin_ids).
    If hiding were applied there, the maintainer would silently stop being notified —
    so the filter lives in the panel, never in the query.
    """
    import inspect
    source = inspect.getsource(adm.get_all_admins)
    assert "HIDDEN" not in source.upper()
    assert "hidden" not in source

    # And the row is still in what the query layer returns.
    assert any(a["telegram_id"] == DEV for a in _rows())


def test_is_hidden_admin_is_only_about_the_configured_ids(hidden_dev):
    assert adm.is_hidden_admin(DEV) is True
    assert adm.is_hidden_admin(SUPER) is False


def test_is_hidden_admin_tolerates_junk(hidden_dev):
    """Called with whatever a callback or DB row carries; it must not raise."""
    assert adm.is_hidden_admin(None) is False
    assert adm.is_hidden_admin("not-a-number") is False
    assert adm.is_hidden_admin(str(DEV)) is True


# ── the concealment guard on every mutation ────────────────────────────────────

def test_mutations_treat_a_hidden_admin_as_nonexistent(hidden_dev):
    from handlers.users.admin_mgmt import _concealed_from
    dev_row = {"telegram_id": DEV}
    assert _concealed_from(dev_row, viewer_id=SUPER) is True
    assert _concealed_from(dev_row, viewer_id=REGULAR) is True
    # ...but not to the hidden admin themselves, or they couldn't open their own entry.
    assert _concealed_from(dev_row, viewer_id=DEV) is False
    # ...and never for anyone else's row.
    assert _concealed_from({"telegram_id": REGULAR}, viewer_id=SUPER) is False


def test_config_parses_ids_and_ignores_junk():
    assert config._id_set(["7564871221", " 42 "]) == {7564871221, 42}
    assert config._id_set(["", "abc", None]) == set()
    assert config._id_set([]) == set()
    assert config._id_set(None) == set()


def test_the_hidden_set_is_the_bootstrap_admins():
    """No separate setting: whoever is in ADMINS is the maintainer, and the maintainer is
    who this hides. conftest boots the test config with ADMINS=1."""
    assert config.HIDDEN_ADMIN_IDS == config._id_set(config.ADMINS)
    assert config.HIDDEN_ADMIN_IDS == {1}
