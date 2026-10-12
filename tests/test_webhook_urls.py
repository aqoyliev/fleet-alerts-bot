"""Which URL reaches which webhook handler.

HF's Samsara dashboard was configured against the multi-company build, which put the
company in the path: it delivers to /webhook/samsara/hf. This build has no company in
its paths, and if it answered only /webhook/samsara the deploy would 404 every delivery
from a provider nobody had touched — the alerts would stop and nothing in the bot would
have failed.

So the old forms are still routed. These resolve each path through a real aiohttp router
and check which handler it lands on, which is the only thing that can go wrong here:
/webhook/{company} matches /webhook/samsara just as well as the Samsara route does, and
aiohttp takes whichever was registered first.
"""
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from data import config
import utils.webhook_handler as wh


@pytest.fixture
def app():
    app = web.Application()
    wh.add_webhook_routes(app)
    return app


async def _handler_for(app, method, path):
    match = await app.router.resolve(make_mocked_request(method, path))
    return match.handler


@pytest.mark.parametrize("path", [
    "/webhook/samsara",
    "/webhook/samsara/hf",        # where HF's Samsara dashboard has always delivered
    "/webhook/samsara/anything",  # the segment is ignored, not checked
])
async def test_samsara_paths_reach_the_samsara_handler(app, path):
    assert await _handler_for(app, "POST", path) is wh.samsara_webhook


@pytest.mark.parametrize("path", [
    "/webhook/motive",
    "/webhook/motive/hf",
    "/webhook/hf",     # the oldest form, from before Samsara was supported at all
    "/webhook",
])
async def test_motive_paths_reach_the_motive_handler(app, path):
    assert await _handler_for(app, "POST", path) is wh.motive_webhook


async def test_the_bare_company_route_does_not_swallow_the_provider_routes(app):
    """/webhook/{company} would match both of these. Registration order is what stops
    a Samsara delivery being parsed as a Motive one and dropped as an unknown type."""
    assert await _handler_for(app, "POST", "/webhook/samsara") is wh.samsara_webhook
    assert await _handler_for(app, "POST", "/webhook/motive") is wh.motive_webhook


async def test_a_delivery_to_the_old_samsara_url_is_processed(app, monkeypatch):
    """End to end over a real socket: the legacy URL, a signed-off delivery, and the
    event handed to _handle_event."""
    handled = []

    async def _handle(bot, event, api_key=None, **_k):
        handled.append(event)

    def _run_now(coro):
        # _spawn schedules a background task; closing over it here keeps the test from
        # leaving one pending and lets the assertion read the result directly.
        import asyncio
        return asyncio.ensure_future(coro)

    monkeypatch.setattr(config, "SAMSARA_WEBHOOK_SECRET", "")
    monkeypatch.setattr(config, "ALLOW_UNSIGNED_WEBHOOKS", True)
    monkeypatch.setattr(wh, "_handle_event", _handle)
    monkeypatch.setattr(wh, "_spawn", _run_now)
    app["bot"] = object()

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/webhook/samsara/hf", json={
            "eventType": "SpeedingEventStarted", "eventId": "legacy-url-1",
            "data": {"vehicle": {"id": "v1", "name": "Unit 1196"},
                     "severityLevel": "Heavy", "startTime": "2026-05-22T15:00:00Z"},
        })
        assert resp.status == 200

    assert len(handled) == 1
    assert handled[0]["type"] == "speeding"
    assert handled[0]["vehicle"]["number"] == "Unit 1196"


async def test_an_unsigned_delivery_to_the_old_url_is_still_refused(app, monkeypatch):
    """Keeping the old path is not a way around the signature. With no secret set and
    no opt-in, the legacy URL answers exactly as the new one does."""
    monkeypatch.setattr(config, "SAMSARA_WEBHOOK_SECRET", "")
    monkeypatch.setattr(config, "ALLOW_UNSIGNED_WEBHOOKS", False)
    app["bot"] = object()

    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/webhook/samsara/hf", json={"eventType": "x"})
        assert resp.status == 403


async def test_health_answers(app):
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200
        assert await resp.text() == "OK"
