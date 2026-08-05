"""Tests for the ⭐ Make super admin control.

Before this, the only route to super admin was /transfer, which hands the role over and
demotes whoever used it — so a company could not gain a second super admin without its
maintainer losing access. These pin down where the button appears, and that promoting
someone doesn't cost the promoter their own role.
"""
import pytest

from data import config
from keyboards.inline.admin_mgmt import admin_detail_keyboard

MAINTAINER = 7564871221
SUPER = 111111


def _labels(kb):
    return [b.text for row in kb.inline_keyboard for b in row]


def _admin(**over):
    row = {"id": 5, "telegram_id": 222222, "full_name": "Dispatcher", "username": None,
           "is_super": False, "is_active": True}
    row.update(over)
    return row


# ── where the button appears ───────────────────────────────────────────────────

def test_super_admin_can_promote_an_active_regular_admin():
    assert "⭐ Make super admin" in _labels(admin_detail_keyboard(_admin(), is_super=True))


def test_the_other_controls_are_still_there():
    """Promotion is an addition to the panel, not a replacement — the four operations a
    super admin already had must survive."""
    labels = _labels(admin_detail_keyboard(_admin(), is_super=True))
    assert "⛔ Deactivate" in labels and "🗑 Remove" in labels
    labels = _labels(admin_detail_keyboard(_admin(is_active=False), is_super=True))
    assert "✅ Activate" in labels and "🗑 Remove" in labels


def test_a_regular_admin_is_offered_nothing():
    labels = _labels(admin_detail_keyboard(_admin(), is_super=False))
    assert "⭐ Make super admin" not in labels
    assert labels == ["◀ Back to List"]


def test_a_deactivated_admin_cannot_be_promoted():
    """A deactivated super admin is a state the panel can't undo: none of these controls
    are drawn for a super, so there would be no way back."""
    labels = _labels(admin_detail_keyboard(_admin(is_active=False), is_super=True))
    assert "⭐ Make super admin" not in labels


def test_an_existing_super_is_not_offered_promotion():
    labels = _labels(admin_detail_keyboard(_admin(is_super=True), is_super=True))
    assert "⭐ Make super admin" not in labels


def test_the_callback_carries_the_admin_id():
    kb = admin_detail_keyboard(_admin(id=42), is_super=True)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "adm_promote:42" in data


# ── promotion does not demote the promoter ─────────────────────────────────────

async def test_promote_only_touches_the_target(monkeypatch):
    """The distinction from transfer_super_admin, which demotes the current holder. If
    promotion did that, the maintainer would lose access every time a company gained a
    super admin."""
    from utils.db_api import admins as adm

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


# ── the hidden maintainer stays unreachable ────────────────────────────────────

def test_the_hidden_account_is_still_concealed_from_promotion(monkeypatch):
    monkeypatch.setattr(config, "HIDDEN_ADMIN_IDS", {MAINTAINER})
    from handlers.users.admin_mgmt import _concealed_from
    assert _concealed_from({"telegram_id": MAINTAINER}, viewer_id=SUPER) is True
