"""Admin management through the panel refuses the same things the old in-chat flow did.

Self-removal, touching a super admin, and mutating a concealed maintainer are all
refused. Those rules exist because losing them costs someone their access with no way
back that isn't a hand-written SQL statement — the panel is now the only surface that
mutates admins, so it carries every one of them itself.
"""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from data import config
from utils.webapp import api, auth

BOT_TOKEN = "123:test"
ACTOR = 111            # the super admin doing the clicking
MAINTAINER = 999       # in config.ADMINS, hidden from everyone else


def _init_data(user_id: int) -> str:
    fields = {"user": json.dumps({"id": user_id}), "auth_date": str(int(time.time()))}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class _Request:
    def __init__(self, body=None, match=None, user_id=ACTOR):
        self.headers = {"X-Telegram-Init-Data": _init_data(user_id)}
        self.match_info = match or {}
        self.query = {}
        self.method = "POST"
        self.path = "/panel/api/admins"
        self.remote = "1.2.3.4"
        self._body = body if body is not None else {}
        self._store = {}

    async def json(self):
        return self._body

    def __setitem__(self, key, value):
        self._store[key] = value

    def __getitem__(self, key):
        return self._store[key]


def _always(value):
    async def _check(_telegram_id):
        return value
    return _check


# id → row, as get_admin_by_id would return it
ADMINS = {
    1: {"id": 1, "telegram_id": ACTOR, "full_name": "Actor", "is_super": True, "is_active": True},
    2: {"id": 2, "telegram_id": 222, "full_name": "Plain", "is_super": False, "is_active": True},
    3: {"id": 3, "telegram_id": 333, "full_name": "Other super", "is_super": True, "is_active": True},
    4: {"id": 4, "telegram_id": 444, "full_name": "Dormant", "is_super": False, "is_active": False},
    5: {"id": 5, "telegram_id": MAINTAINER, "full_name": "Hidden", "is_super": True, "is_active": True},
}


@pytest.fixture
def panel(monkeypatch):
    writes = {"active": [], "promoted": [], "deleted": [], "added": []}

    monkeypatch.setattr(auth, "is_admin", _always(True))
    monkeypatch.setattr(auth, "is_super_admin", _always(True))
    # The maintainer set is derived from ADMINS at import; override it directly so the
    # concealment rule is exercised without rebuilding config.
    monkeypatch.setattr(config, "HIDDEN_ADMIN_IDS", {MAINTAINER})

    async def _by_id(admin_id):
        return ADMINS.get(admin_id)

    async def _set_active(admin_id, active):
        writes["active"].append((admin_id, active))

    async def _promote(admin_id):
        writes["promoted"].append(admin_id)

    async def _delete(admin_id):
        writes["deleted"].append(admin_id)

    async def _add(telegram_id, added_by=None, is_super=False):
        writes["added"].append(telegram_id)
        return 9

    async def _ensure_user(*a, **k):
        return None

    monkeypatch.setattr(api, "get_admin_by_id", _by_id)
    monkeypatch.setattr(api, "set_admin_active", _set_active)
    monkeypatch.setattr(api, "promote_to_super", _promote)
    monkeypatch.setattr(api, "delete_admin", _delete)
    monkeypatch.setattr(api, "add_admin", _add)
    monkeypatch.setattr(api, "ensure_user", _ensure_user)
    return writes


# ── removal ─────────────────────────────────────────────────────────────────────

async def test_you_cannot_remove_yourself(panel):
    resp = await api.delete_admin_route(_Request({"confirm": True}, {"admin_id": "1"}))
    assert resp.status == 403
    assert panel["deleted"] == []


async def test_a_super_admin_cannot_be_removed(panel):
    resp = await api.delete_admin_route(_Request({"confirm": True}, {"admin_id": "3"}))
    assert resp.status == 403
    assert panel["deleted"] == []


async def test_removal_requires_confirmation(panel):
    resp = await api.delete_admin_route(_Request({}, {"admin_id": "2"}))
    assert resp.status == 400
    assert panel["deleted"] == []


async def test_a_plain_admin_can_be_removed(panel):
    resp = await api.delete_admin_route(_Request({"confirm": True}, {"admin_id": "2"}))
    assert resp.status == 200
    assert panel["deleted"] == [2]


# ── activation ──────────────────────────────────────────────────────────────────

async def test_you_cannot_deactivate_yourself(panel, monkeypatch):
    """A guard the chat flow never needed but a panel does: is_admin requires is_active,
    so a super admin who switches their own row off is locked out of both surfaces."""
    monkeypatch.setitem(ADMINS, 1, {**ADMINS[1], "is_super": False})
    resp = await api.update_admin(_Request({"is_active": False}, {"admin_id": "1"}))
    assert resp.status == 403
    assert panel["active"] == []


async def test_a_super_admin_cannot_be_deactivated(panel):
    resp = await api.update_admin(_Request({"is_active": False}, {"admin_id": "3"}))
    assert resp.status == 403
    assert panel["active"] == []


async def test_a_plain_admin_can_be_deactivated(panel):
    resp = await api.update_admin(_Request({"is_active": False}, {"admin_id": "2"}))
    assert resp.status == 200
    assert panel["active"] == [(2, False)]


# ── promotion ───────────────────────────────────────────────────────────────────

async def test_promoting_a_plain_admin_works(panel):
    resp = await api.update_admin(_Request({"is_super": True}, {"admin_id": "2"}))
    assert resp.status == 200
    assert panel["promoted"] == [2]


async def test_an_inactive_admin_cannot_be_promoted(panel):
    resp = await api.update_admin(_Request({"is_super": True}, {"admin_id": "4"}))
    assert resp.status == 400
    assert panel["promoted"] == []


async def test_an_existing_super_is_not_promoted_again(panel):
    resp = await api.update_admin(_Request({"is_super": True}, {"admin_id": "3"}))
    assert resp.status == 400
    assert panel["promoted"] == []


# ── the concealed maintainer ────────────────────────────────────────────────────

async def test_a_maintainer_looks_exactly_like_a_deleted_admin(panel):
    """Distinguishing "hidden" from "gone" would turn the panel into an oracle for which
    id is the bootstrap account — precisely what the concealment exists to prevent. The
    two responses must be byte-identical, so they are compared rather than just checked
    for a matching status."""
    hidden = await api.delete_admin_route(_Request({"confirm": True}, {"admin_id": "5"}))
    absent = await api.delete_admin_route(_Request({"confirm": True}, {"admin_id": "77"}))

    assert hidden.status == absent.status == 404
    assert hidden.body == absent.body
    assert panel["deleted"] == []


async def test_a_maintainer_cannot_be_mutated(panel):
    for body in ({"is_active": False}, {"is_super": True}):
        resp = await api.update_admin(_Request(body, {"admin_id": "5"}))
        assert resp.status == 404
    assert panel["active"] == [] and panel["promoted"] == []


async def test_adding_a_maintainer_reports_success_without_doing_anything(panel):
    """Mirrors _finish_add: a distinguishable "already an admin" here would leak the same
    secret the not-found shape above protects."""
    resp = await api.create_admin(_Request({"telegram_id": MAINTAINER}))
    assert resp.status == 200
    assert panel["added"] == []


async def test_a_maintainer_can_still_see_their_own_kind(panel):
    """The concealment is one-way. A maintainer looking at the panel sees everyone,
    themselves included — otherwise they could not manage their own deployment.

    The proof is in which rule answers: the same request that a company admin gets a bare
    "Admin not found" for reaches the real refusals when a maintainer makes it.
    """
    concealed = await api.delete_admin_route(
        _Request({"confirm": True}, {"admin_id": "5"}))
    visible = await api.delete_admin_route(
        _Request({"confirm": True}, {"admin_id": "5"}, user_id=MAINTAINER))

    assert concealed.status == 404
    assert json.loads(concealed.body)["error"] == "not_found"
    # Row 5 *is* the maintainer, so what stops them is the self-removal rule — a real
    # answer about a row they can see, not a denial that it exists.
    assert visible.status == 403
    assert json.loads(visible.body)["error"] == "self_action"


# ── adding ──────────────────────────────────────────────────────────────────────

async def test_a_new_admin_is_added(panel):
    resp = await api.create_admin(_Request({"telegram_id": 555}))
    assert resp.status == 200
    assert panel["added"] == [555]


@pytest.mark.parametrize("value", ["", "abc", None])
async def test_a_non_numeric_id_is_rejected(panel, value):
    resp = await api.create_admin(_Request({"telegram_id": value}))
    assert resp.status == 400
    assert panel["added"] == []


async def test_a_plain_admin_cannot_add_anyone(panel, monkeypatch):
    monkeypatch.setattr(auth, "is_super_admin", _always(False))
    resp = await api.create_admin(_Request({"telegram_id": 555}))
    assert resp.status == 403
    assert panel["added"] == []
