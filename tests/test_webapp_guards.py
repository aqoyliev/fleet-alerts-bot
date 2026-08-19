"""The panel must not be a softer path to the same tables than the bot is.

Every rule checked here is already enforced somewhere in handlers/ — by /setunit, by
/removegroup, by the 👥 Admins panel. Wherever the HTTP surface enforces less than the
chat surface, that gap is the whole bug, so these tests are written from the bot's rules
rather than from the panel's code.
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
GROUP_ID = -1001234567890


def _init_data(user_id: int = 111) -> str:
    fields = {"user": json.dumps({"id": user_id}), "auth_date": str(int(time.time()))}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class _Request:
    """Duck-typed aiohttp request — the handlers touch exactly these members."""

    def __init__(self, body=None, match=None, query=None, user_id=111):
        self.headers = {"X-Telegram-Init-Data": _init_data(user_id)}
        self.match_info = match or {}
        self.query = query or {}
        self.method = "POST"
        self.path = "/panel/api/test"
        self.remote = "1.2.3.4"
        self._body = body if body is not None else {}
        self._store = {}

    async def json(self):
        return self._body

    def __setitem__(self, key, value):
        self._store[key] = value

    def __getitem__(self, key):
        return self._store[key]


def _body(response):
    return json.loads(response.body)


@pytest.fixture
def panel(monkeypatch):
    """Admit the caller, stub out the database, and record every write.

    Writes are recorded rather than mocked away individually so a test can assert that
    *nothing* was written — which for the unit-validation rules is the actual property
    under test, not the status code that accompanies it.
    """
    writes = {"unit": [], "register": [], "enabled": [], "removed": [], "event_types": []}

    monkeypatch.setattr(auth, "is_admin", _always(True))
    monkeypatch.setattr(auth, "is_super_admin", _always(True))
    monkeypatch.setattr(config, "MAIN_GROUP_ID", -100999)
    monkeypatch.setattr(config, "CRASH_GROUP_ID", -100888)

    async def _get_group(tgid):
        return {"id": 1, "telegram_group_id": tgid, "title": "Unit 571",
                "vehicle_number": "unit571", "enabled": False}

    async def _set_unit(tgid, unit):
        writes["unit"].append((tgid, unit))

    async def _register(*a, **k):
        writes["register"].append((a, k))

    async def _set_enabled(tgid, enabled):
        writes["enabled"].append((tgid, enabled))

    async def _remove(tgid):
        writes["removed"].append(tgid)

    async def _set_types(tgid, types):
        writes["event_types"].append((tgid, set(types)))

    async def _get_types(_tgid):
        return []

    monkeypatch.setattr(api, "get_group", _get_group)
    monkeypatch.setattr(api, "set_group_unit", _set_unit)
    monkeypatch.setattr(api, "register_group", _register, raising=False)
    monkeypatch.setattr(api, "set_group_enabled", _set_enabled)
    monkeypatch.setattr(api, "remove_group", _remove)
    monkeypatch.setattr(api, "set_group_event_types", _set_types)
    monkeypatch.setattr(api, "get_group_event_types", _get_types)
    return writes


def _always(value: bool):
    async def _check(_telegram_id):
        return value
    return _check


def _resolver(result):
    async def _resolve(unit):
        return result if isinstance(result, tuple) else (result, unit)
    return _resolve


# ── unit changes ────────────────────────────────────────────────────────────────

async def test_an_unknown_unit_is_refused_and_nothing_is_written(panel, monkeypatch):
    """The status code matters less than the write that must not happen: a stored guess
    produces a group that looks configured and silently receives nothing."""
    monkeypatch.setattr(api, "resolve_unit", _resolver("missing"))
    monkeypatch.setattr(api, "suggest_units", _suggest(["unit2007"]))

    resp = await api.set_unit(_Request({"unit": "007"}, {"tgid": str(GROUP_ID)}))

    assert resp.status == 409
    assert panel["unit"] == []
    assert _body(resp)["suggestions"] == ["unit2007"]


async def test_a_samsara_outage_is_refused_and_nothing_is_written(panel, monkeypatch):
    monkeypatch.setattr(api, "resolve_unit", _resolver("unavailable"))

    resp = await api.set_unit(_Request({"unit": "571"}, {"tgid": str(GROUP_ID)}))

    assert resp.status == 503
    assert panel["unit"] == []


async def test_the_roster_spelling_is_what_gets_stored(panel, monkeypatch):
    monkeypatch.setattr(api, "resolve_unit", _resolver(("ok", "unit571")))

    resp = await api.set_unit(_Request({"unit": "571"}, {"tgid": str(GROUP_ID)}))

    assert resp.status == 200
    assert panel["unit"] == [(GROUP_ID, "unit571")]
    assert "unit571" in _body(resp)["note"]


async def test_changing_a_unit_uses_set_group_unit_not_register_group(panel, monkeypatch):
    """register_group clears the mute as a side effect of its ON CONFLICT clause. The
    fixture's group is muted; changing its unit must not quietly turn its alerts back on,
    so the endpoint has to use the narrow writer."""
    monkeypatch.setattr(api, "resolve_unit", _resolver(("ok", "unit2007")))

    await api.set_unit(_Request({"unit": "2007"}, {"tgid": str(GROUP_ID)}))

    assert panel["unit"] == [(GROUP_ID, "unit2007")]
    assert panel["register"] == [], "register_group would have un-muted the group"
    assert panel["enabled"] == [], "the mute state must not be touched at all"


@pytest.mark.parametrize("unit", ["", "   ", "no-digits-here", "x" * 51])
async def test_malformed_units_never_reach_samsara(panel, monkeypatch, unit):
    async def _explode(_unit):
        raise AssertionError("resolve_unit must not be called for invalid input")

    monkeypatch.setattr(api, "resolve_unit", _explode)
    resp = await api.set_unit(_Request({"unit": unit}, {"tgid": str(GROUP_ID)}))

    assert resp.status == 400
    assert panel["unit"] == []


# ── the chats configured in .env, not in the database ───────────────────────────

@pytest.mark.parametrize("field", ["MAIN_GROUP_ID", "CRASH_GROUP_ID"])
async def test_reserved_chats_cannot_be_edited(panel, field):
    """The main group is recreated by ensure_main_group on every boot and the crash group
    is deliberately absent from alert_groups entirely. Neither is the panel's to change,
    and pretending otherwise would be a lie with a deploy-shaped expiry."""
    tgid = getattr(config, field)

    for handler, body in ((api.set_enabled, {"enabled": True}),
                          (api.delete_group, {"confirm": True}),
                          (api.toggle_event, {"action": "all"})):
        resp = await handler(_Request(body, {"tgid": str(tgid)}))
        assert resp.status == 403

    assert panel["enabled"] == [] and panel["removed"] == []


async def test_a_non_numeric_group_id_is_a_400_not_a_crash(panel):
    resp = await api.set_enabled(_Request({"enabled": True}, {"tgid": "not-a-number"}))
    assert resp.status == 400


# ── removal ─────────────────────────────────────────────────────────────────────

async def test_removing_a_group_requires_confirmation(panel):
    resp = await api.delete_group(_Request({}, {"tgid": str(GROUP_ID)}))
    assert resp.status == 400
    assert panel["removed"] == []


async def test_a_confirmed_removal_goes_through(panel):
    resp = await api.delete_group(_Request({"confirm": True}, {"tgid": str(GROUP_ID)}))
    assert resp.status == 200
    assert panel["removed"] == [GROUP_ID]


async def test_a_plain_admin_cannot_remove_a_group(panel, monkeypatch):
    monkeypatch.setattr(auth, "is_super_admin", _always(False))
    resp = await api.delete_group(_Request({"confirm": True}, {"tgid": str(GROUP_ID)}))
    assert resp.status == 403
    assert panel["removed"] == []


# ── event filter ────────────────────────────────────────────────────────────────

async def test_toggling_off_one_type_from_all_materializes_the_rest(panel):
    """The empty allowlist means "every type". Toggling one off from that state has to
    write every OTHER type — the rule lives in next_event_filter and this proves the
    endpoint runs it rather than writing the single toggled value."""
    resp = await api.toggle_event(
        _Request({"action": "toggle", "event_type": "speeding"}, {"tgid": str(GROUP_ID)}))

    assert resp.status == 200
    tgid, written = panel["event_types"][0]
    assert tgid == GROUP_ID
    assert "speeding" not in written
    assert len(written) > 1
    assert _body(resp)["filter_mode"] == "custom"


async def test_resetting_to_all_writes_the_empty_allowlist(panel):
    resp = await api.toggle_event(_Request({"action": "all"}, {"tgid": str(GROUP_ID)}))

    assert panel["event_types"] == [(GROUP_ID, set())]
    assert _body(resp)["filter_mode"] == "all"


async def test_an_invented_event_type_is_rejected(panel):
    resp = await api.toggle_event(
        _Request({"action": "toggle", "event_type": "drop table"}, {"tgid": str(GROUP_ID)}))

    assert resp.status == 400
    assert panel["event_types"] == []


def _suggest(names):
    async def _s(_key, _unit):
        return names
    return _s
