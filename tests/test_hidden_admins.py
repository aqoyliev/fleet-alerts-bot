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


def test_is_maintainer_is_only_about_the_configured_ids(hidden_dev):
    assert adm.is_maintainer(DEV) is True
    assert adm.is_maintainer(SUPER) is False


def test_is_maintainer_tolerates_junk(hidden_dev):
    """Called with whatever a callback or DB row carries; it must not raise."""
    assert adm.is_maintainer(None) is False
    assert adm.is_maintainer("not-a-number") is False
    assert adm.is_maintainer(str(DEV)) is True


# ── a maintainer cannot be locked out ──────────────────────────────────────────

async def test_a_maintainer_is_super_even_when_the_row_says_otherwise(hidden_dev, monkeypatch):
    """The bug this fixes, seen live on CPT: transfer_super_admin promotes the target and
    demotes whoever used it, so handing the role to the customer stripped the maintainer's
    own management access — and a restart didn't restore it.
    """
    async def _demoted_row(sql, *args):
        return {"is_super": False, "is_active": True}

    monkeypatch.setattr(adm.db, "fetchrow", _demoted_row)
    assert await adm.is_super_admin(DEV) is True
    # ...and the override is only ever about the configured ids.
    assert await adm.is_super_admin(SUPER) is False


async def test_a_maintainer_is_an_admin_even_with_no_row_at_all(hidden_dev, monkeypatch):
    async def _no_row(sql, *args):
        return None

    monkeypatch.setattr(adm.db, "fetchrow", _no_row)
    assert await adm.is_admin(DEV) is True
    assert await adm.is_admin(SUPER) is False


async def test_startup_puts_the_maintainer_row_back(monkeypatch):
    """Seeding used to be ON CONFLICT DO NOTHING, which is why the demotion survived a
    restart. It now re-asserts what config says."""
    executed = []

    async def _execute(sql, *args):
        executed.append((" ".join(sql.split()), args))

    async def _ensure_user(*args, **kwargs):
        pass

    monkeypatch.setattr(adm.db, "execute", _execute)
    monkeypatch.setattr("utils.db_api.users.ensure_user", _ensure_user)

    await adm.seed_super_admins([DEV])

    assert len(executed) == 1
    sql, args = executed[0]
    assert "DO UPDATE SET is_super = TRUE, is_active = TRUE" in sql
    assert args == (DEV,)


# ── the concealment guard on every mutation ────────────────────────────────────

def test_mutations_treat_a_hidden_admin_as_nonexistent(hidden_dev):
    dev_row = {"telegram_id": DEV}
    assert adm._concealed_from(dev_row, viewer_id=SUPER) is True
    assert adm._concealed_from(dev_row, viewer_id=REGULAR) is True
    # ...but not to the hidden admin themselves, or they couldn't open their own entry.
    assert adm._concealed_from(dev_row, viewer_id=DEV) is False
    # ...and never for anyone else's row.
    assert adm._concealed_from({"telegram_id": REGULAR}, viewer_id=SUPER) is False


def test_the_hidden_account_is_still_concealed_from_promotion(hidden_dev):
    assert adm._concealed_from({"telegram_id": DEV}, viewer_id=SUPER) is True


async def test_promote_only_touches_the_target(monkeypatch):
    """The distinction from transfer_super_admin, which demotes the current holder. If
    promotion did that, the maintainer would lose access every time a company gained a
    super admin."""
    executed = []

    async def _fake_execute(sql, *args):
        executed.append((" ".join(sql.split()), args))

    monkeypatch.setattr(adm.db, "execute", _fake_execute)
    await adm.promote_to_super(42)

    assert len(executed) == 1
    sql, args = executed[0]
    assert sql == "UPDATE admins SET is_super = TRUE WHERE id = $1"
    assert args == (42,)
    assert "FALSE" not in sql.upper()


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
