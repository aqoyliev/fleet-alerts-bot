"""The route table, driven through real aiohttp routing.

The guard tests call handlers directly, which proves the rules but says nothing about
whether a handler is actually reachable at the path it is supposed to be, or whether one
got registered without its decorator. That second failure is the dangerous one — it does
not break anything visible, it just serves the fleet's data to whoever asks — so the main
test here walks the router and asserts that *every* API route refuses an anonymous
caller, and will therefore cover routes nobody has written yet.
"""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from utils.webapp import api, auth
from utils.webapp.routes import setup_routes

BOT_TOKEN = "123:test"


def _init_data(user_id: int = 111) -> str:
    fields = {"user": json.dumps({"id": user_id}), "auth_date": str(int(time.time()))}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def _always(value):
    async def _check(_telegram_id):
        return value
    return _check


@pytest.fixture
async def client():
    """A real server with the panel mounted, exercised over a real socket.

    Built by hand rather than through the aiohttp_client fixture, which would need
    `-p aiohttp.pytest_plugin` in pytest.ini — not worth editing shared config for one
    file.
    """
    app = web.Application()
    setup_routes(app)
    async with TestClient(TestServer(app)) as c:
        yield c


def _api_routes(app) -> list[tuple[str, str]]:
    """(method, path) for every /panel/api route currently registered."""
    found = []
    for route in app.router.routes():
        path = getattr(route.resource, "canonical", "")
        if path.startswith("/panel/api"):
            found.append((route.method, path))
    return found


# ── the structural guarantee ────────────────────────────────────────────────────

async def test_every_api_route_refuses_an_anonymous_caller(client):
    """Walks the router rather than listing paths, so a route added later is covered by
    this test the day it is written. A 401 here is the only acceptable answer — anything
    else means the endpoint served data without checking who was asking."""
    routes = _api_routes(client.app)
    assert routes, "no API routes registered — the table is not wired up"

    for method, path in routes:
        # Path params are irrelevant: authentication must be refused before the handler
        # ever looks at them.
        url = path.replace("{tgid}", "-100123").replace("{admin_id}", "1")
        resp = await client.request(method, url)
        assert resp.status == 401, f"{method} {path} answered {resp.status} unauthenticated"


async def test_a_forged_credential_is_refused_everywhere(client):
    for method, path in _api_routes(client.app):
        url = path.replace("{tgid}", "-100123").replace("{admin_id}", "1")
        resp = await client.request(method, url, headers={
            "X-Telegram-Init-Data": "user=%7B%22id%22%3A1%7D&auth_date=1&hash=deadbeef",
        })
        assert resp.status == 401, f"{method} {path} accepted a forged hash"


async def test_a_verified_non_admin_is_refused_everywhere(client, monkeypatch):
    monkeypatch.setattr(auth, "is_admin", _always(False))
    monkeypatch.setattr(auth, "is_super_admin", _always(False))

    for method, path in _api_routes(client.app):
        url = path.replace("{tgid}", "-100123").replace("{admin_id}", "1")
        resp = await client.request(method, url,
                                    headers={"X-Telegram-Init-Data": _init_data()})
        assert resp.status == 403, f"{method} {path} let a non-admin through"


async def test_super_only_routes_refuse_a_plain_admin(client, monkeypatch):
    monkeypatch.setattr(auth, "is_admin", _always(True))
    monkeypatch.setattr(auth, "is_super_admin", _always(False))

    for path in ("/panel/api/groups/-100123/remove", "/panel/api/admins",
                 "/panel/api/admins/1/update", "/panel/api/admins/1/remove"):
        resp = await client.post(path, json={"confirm": True},
                                 headers={"X-Telegram-Init-Data": _init_data()})
        assert resp.status == 403, f"{path} let a plain admin mutate"


# ── the shell ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/panel", "/panel/", "/panel/app.css", "/panel/app.js"])
async def test_static_files_are_served_without_a_credential(client, path):
    """The shell itself is public — it has to load before it can read initData, and it
    contains nothing but markup. Everything it then asks for is authenticated."""
    resp = await client.get(path)
    assert resp.status == 200
    assert await resp.text()


async def test_the_shell_is_not_cached(client):
    """Telegram's WebView caches hard enough that a stale app.js after a deploy is
    genuinely painful to diagnose from a phone."""
    resp = await client.get("/panel/")
    assert "no-store" in resp.headers.get("Cache-Control", "")


async def test_the_panel_does_not_shadow_the_webhook_paths(client):
    """The panel shares an application with the provider webhooks. Nothing it registers
    may sit on their paths."""
    for _method, path in _api_routes(client.app):
        assert not path.startswith("/webhook")
        assert path != "/health"


# ── a real authorized round trip ────────────────────────────────────────────────

async def test_bootstrap_returns_the_shape_the_frontend_reads(client, monkeypatch):
    monkeypatch.setattr(auth, "is_admin", _always(True))
    # api imported is_super_admin into its own namespace to answer "can this caller see
    # the destructive controls", separately from the decorator's authorization check.
    monkeypatch.setattr(api, "is_super_admin", _always(True))

    resp = await client.get("/panel/api/bootstrap",
                            headers={"X-Telegram-Init-Data": _init_data(user_id=42)})
    assert resp.status == 200
    data = await resp.json()

    assert data["me"] == {"telegram_id": 42, "is_super": True}
    assert data["company"]["name"]
    assert data["event_types"] and {"type", "emoji", "label"} <= set(data["event_types"][0])


async def test_bootstrap_never_hands_the_samsara_key_to_the_browser(client, monkeypatch):
    """Only whether a key exists, never the key. Stated as a test because "just for
    debugging" is exactly how a credential ends up in a JSON payload."""
    from data import config

    monkeypatch.setattr(auth, "is_admin", _always(True))
    monkeypatch.setattr(api, "is_super_admin", _always(False))
    monkeypatch.setattr(config, "SAMSARA_API_KEY", "samsara_api_SECRETVALUE")

    resp = await client.get("/panel/api/bootstrap",
                            headers={"X-Telegram-Init-Data": _init_data()})
    payload = await resp.text()

    assert "SECRETVALUE" not in payload
    assert (await resp.json())["samsara_enabled"] is True


async def test_groups_serializes_datetimes(client, monkeypatch):
    """asyncpg returns datetime objects, which json refuses. The _json_default hook is
    easy to leave out of a new endpoint and only fails once real rows exist."""
    from datetime import datetime, timezone

    monkeypatch.setattr(auth, "is_admin", _always(True))

    async def _overview():
        return [{"id": 1, "telegram_group_id": -100, "title": "Unit 571",
                 "vehicle_number": "unit571", "enabled": True,
                 "created_at": datetime.now(timezone.utc), "event_types": []}]

    async def _counts(_since):
        return {"unit571": 4}

    monkeypatch.setattr(api, "get_groups_overview", _overview)
    monkeypatch.setattr(api, "get_counts_by_vehicle", _counts)

    resp = await client.get("/panel/api/groups",
                            headers={"X-Telegram-Init-Data": _init_data()})
    assert resp.status == 200
    row = (await resp.json())[0]
    assert row["filter_mode"] == "all"      # no rows in group_event_types = every type
    assert row["alerts_7d"] == 4
    assert isinstance(row["created_at"], str)
