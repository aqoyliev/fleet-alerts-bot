"""The panel's one group card: reading its mute state and toggling it.

There is exactly one group in this build, so unlike single-company there is no path
param to validate — the endpoint always acts on the singleton alert_group row.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from utils.webapp import api, auth

BOT_TOKEN = "123:test"


def _init_data(user_id: int = 111) -> str:
    fields = {"user": json.dumps({"id": user_id}), "auth_date": str(int(time.time()))}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class _Request:
    def __init__(self, body=None):
        self.headers = {"X-Telegram-Init-Data": _init_data()}
        self.match_info = {}
        self.query = {}
        self.method = "POST"
        self.path = "/panel/api/group/enabled"
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


def _always(value):
    async def _check(_telegram_id):
        return value
    return _check


@pytest.fixture
def panel(monkeypatch):
    monkeypatch.setattr(auth, "is_admin", _always(True))
    monkeypatch.setattr(auth, "is_super_admin", _always(True))


async def test_group_reports_the_singleton_status(panel, monkeypatch):
    async def _status():
        return {"telegram_group_id": -100999, "enabled": False}

    monkeypatch.setattr(api, "get_group_status", _status)
    resp = await api.group(_Request())

    assert resp.status == 200
    assert _body(resp) == {"telegram_group_id": -100999, "enabled": False}


async def test_group_status_is_null_before_the_seed_runs(panel, monkeypatch):
    async def _status():
        return None

    monkeypatch.setattr(api, "get_group_status", _status)
    resp = await api.group(_Request())

    assert _body(resp) is None


async def test_toggling_enabled_writes_by_the_seeded_chat_id(panel, monkeypatch):
    writes = []

    async def _status():
        return {"telegram_group_id": -100999, "enabled": True}

    async def _set(tgid, enabled):
        writes.append((tgid, enabled))

    monkeypatch.setattr(api, "get_group_status", _status)
    monkeypatch.setattr(api, "set_group_enabled", _set)

    resp = await api.set_group_enabled_route(_Request({"enabled": False}))

    assert resp.status == 200
    assert writes == [(-100999, False)]


async def test_toggling_before_the_seed_runs_is_a_404_not_a_crash(panel, monkeypatch):
    async def _status():
        return None

    monkeypatch.setattr(api, "get_group_status", _status)
    resp = await api.set_group_enabled_route(_Request({"enabled": True}))

    assert resp.status == 404
