"""Tests for utils/quickmanage.py against a fake QuickManage served by aiohttp's own test
server on localhost: the real HTTP path, no real API. Retry sleeps and the token clock
are patched, so nothing waits."""
import asyncio
import logging

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from data import config
from utils import quickmanage as qm

CID, SECRET = "client-id-test", "client-secret-test-XYZ"


def _env(data, message="ok", error_fields=None):
    return {"error-fields": error_fields, "message": message, "data": data}


class FakeQM:
    """The bits of QuickManage the client uses. `trucks` is {id: detail record};
    `drivers` {id: record}. `script` maps a path to a list of (status, headers) answers
    served first (before the real handler), e.g. {"/x/trucks/search": [(503, {})]}."""

    def __init__(self):
        self.trucks, self.drivers = {}, {}
        self.creds = {CID: SECRET}
        self.tokens: set[str] = set()
        self.expire_in = 3600
        self.auth_calls = 0
        self.requests: list[tuple[str, str, str | None]] = []   # method, path, Authorization
        self.bodies: list[dict] = []
        self.script: dict[str, list] = {}
        self.url = ""

    def count(self, path):
        return sum(1 for _, p, _ in self.requests if p == path)

    def add_truck(self, tid, unit, status="active", drivers=()):
        self.trucks[tid] = {"id": tid, "unit": unit, "unit_number": unit, "status": status,
                            "vin": "VIN", "drivers": list(drivers)}

    # handlers
    async def _pre(self, request):
        auth = request.headers.get("Authorization")
        self.requests.append((request.method, request.path, auth))
        queue = self.script.get(request.path)
        if queue:
            status, headers = queue.pop(0)
            return web.json_response(_env(None, "scripted"), status=status, headers=headers)
        return None

    async def auth(self, request):
        pre = await self._pre(request)
        if pre is not None:
            return pre
        self.auth_calls += 1
        body = await request.json()
        if self.creds.get(body.get("client_id")) != body.get("client_secret"):
            return web.json_response(_env(None, "invalid credentials"), status=401)
        token = f"tok-{self.auth_calls}"
        self.tokens.add(token)
        from datetime import datetime, timedelta, timezone
        expire = (datetime.now(timezone(timedelta(hours=-4))) + timedelta(seconds=self.expire_in))
        return web.json_response(_env({"access_token": token,
                                       "expire": expire.isoformat(timespec="seconds")}))

    async def _authed(self, request):
        pre = await self._pre(request)
        if pre is not None:
            return pre
        token = (request.headers.get("Authorization") or "").removeprefix("Bearer ")
        if token not in self.tokens:
            return web.json_response(_env(None, "unauthorized"), status=401)
        return None

    @staticmethod
    def _matches(item, filters):
        for f in filters:
            field = f["field"]
            value = str(item.get(field) or "")
            if f["operator"] == "match":
                if f["value"].lower() not in value.lower():
                    return False
            elif value != f["value"]:
                return False
        return True

    async def _search(self, request, records):
        denied = await self._authed(request)
        if denied is not None:
            return denied
        body = await request.json()
        self.bodies.append(body)
        hits = [r for r in records if self._matches(r, body["filters"])]
        size, page = body["page_size"], body["page"]
        narrow = [{k: v for k, v in r.items() if k != "drivers"} for r in hits]
        return web.json_response(_env({"count": len(hits),
                                       "items": narrow[page * size:(page + 1) * size]}))

    async def trucks_search(self, request):
        return await self._search(request, list(self.trucks.values()))

    async def drivers_search(self, request):
        return await self._search(request, list(self.drivers.values()))

    async def truck_get(self, request):
        denied = await self._authed(request)
        if denied is not None:
            return denied
        truck = self.trucks.get(request.match_info["id"])
        if truck is None:
            return web.json_response(_env(None, "not found"), status=404)
        return web.json_response(_env(truck))


def _driver(did, first, last, status="active", email=None, phone=None, middle=None):
    return {"id": did, "first_name": first, "middle_name": middle, "last_name": last,
            "status": status, "email": email, "phone": phone, "role": "driver",
            "assigned_truck": {"id": "t", "number": "4042"}}


@pytest.fixture
async def fake(monkeypatch):
    f = FakeQM()
    def route(name):
        async def handler(request):      # looked up per request, so tests can override
            return await getattr(f, name)(request)
        return handler

    app = web.Application()
    app.router.add_post("/auth/token", route("auth"))
    app.router.add_post("/x/trucks/search", route("trucks_search"))
    app.router.add_post("/x/drivers/search", route("drivers_search"))
    app.router.add_get("/x/trucks/{id}", route("truck_get"))
    server = TestServer(app)
    await server.start_server()
    f.url = str(server.make_url("")).rstrip("/")

    qm._reset_caches()
    monkeypatch.setattr(config, "QUICKMANAGE_BASE_URL", f.url + "/")
    monkeypatch.setenv("QUICKMANAGE_CLIENT_ID_CO", CID)
    monkeypatch.setenv("QUICKMANAGE_CLIENT_SECRET_CO", SECRET)
    sleeps = []

    async def _no_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(qm, "_sleep", _no_sleep)
    f.sleeps = sleeps
    yield f
    await qm.close()
    await server.close()
    qm._reset_caches()


# ── configuration ────────────────────────────────────────────────────────────────

def test_config_helpers_use_the_slug_suffix(monkeypatch):
    monkeypatch.setenv("QUICKMANAGE_CLIENT_ID_MY_CO", " id1 ")
    monkeypatch.setenv("QUICKMANAGE_CLIENT_SECRET_MY_CO", "s1")
    assert config.quickmanage_client_id("my-co") == "id1"
    assert config.quickmanage_client_secret("My-Co") == "s1"
    assert config.quickmanage_client_id("other") == ""
    assert config.quickmanage_client_id(None) == ""
    assert config.quickmanage_client_secret("") == ""


def test_is_configured_needs_both(monkeypatch):
    monkeypatch.delenv("QUICKMANAGE_CLIENT_ID_ZED", raising=False)
    monkeypatch.delenv("QUICKMANAGE_CLIENT_SECRET_ZED", raising=False)
    assert not qm.is_configured("zed")
    monkeypatch.setenv("QUICKMANAGE_CLIENT_ID_ZED", "x")
    assert not qm.is_configured("zed")
    monkeypatch.setenv("QUICKMANAGE_CLIENT_SECRET_ZED", "y")
    assert qm.is_configured("zed")
    assert qm.is_configured("ZED")


async def test_not_configured_raises_without_any_request(fake, monkeypatch):
    monkeypatch.delenv("QUICKMANAGE_CLIENT_SECRET_CO")
    assert not qm.is_configured("co")
    with pytest.raises(qm.QuickManageUnavailable):
        await qm.truck_drivers("co", "4042")
    with pytest.raises(qm.QuickManageUnavailable):
        await qm.search_drivers("co", "John Smith")
    assert fake.requests == []


# ── auth ─────────────────────────────────────────────────────────────────────────

async def test_token_is_fetched_once_and_reused(fake):
    fake.add_truck("t1", "4042")
    fake.add_truck("t2", "4043")
    await qm.truck_drivers("co", "4042")
    await qm.truck_drivers("co", "4043")
    assert fake.auth_calls == 1
    bearer = {a for m, p, a in fake.requests if p != "/auth/token"}
    assert bearer == {"Bearer tok-1"}


async def test_auth_request_carries_credentials_in_json(fake):
    fake.add_truck("t1", "4042")
    await qm.truck_drivers("co", "4042")
    # the credentials went to /auth/token only; the other calls carry just the bearer
    assert fake.requests[0] == ("POST", "/auth/token", None)
    assert all(a == "Bearer tok-1" for m, p, a in fake.requests[1:])


async def test_concurrent_callers_share_one_login_and_one_fetch(fake):
    fake.add_truck("t1", "4042", drivers=[_driver("d1", "John", "Smith")])
    results = await asyncio.gather(*(qm.truck_drivers("co", "4042") for _ in range(5)))
    assert all(r.found for r in results)
    assert fake.auth_calls == 1
    assert fake.count("/x/trucks/search") == 1
    assert fake.count("/x/trucks/t1") == 1


async def test_401_triggers_one_new_login_and_a_retry(fake):
    fake.add_truck("t1", "4042", drivers=[_driver("d1", "John", "Smith")])
    fake.add_truck("t2", "4043")
    await qm.truck_drivers("co", "4042")
    fake.tokens.clear()                      # the server forgets the token (expired early)
    result = await qm.truck_drivers("co", "4043")
    assert result.found and result.truck.id == "t2"
    assert fake.auth_calls == 2
    assert fake.requests[-1][2] == "Bearer tok-2"


async def test_401_after_a_new_login_raises_auth_error(fake):
    fake.add_truck("t1", "4042")

    async def mint_unusable_tokens(request):
        fake.auth_calls += 1
        return web.json_response(_env({"access_token": f"dead-{fake.auth_calls}",
                                       "expire": "2099-01-01T00:00:00+00:00"}))
    fake.auth = mint_unusable_tokens           # tokens it hands out are never accepted
    with pytest.raises(qm.QuickManageAuthError):
        await qm.truck_drivers("co", "4042")
    assert fake.auth_calls == 2                # the login, and exactly one re-login


async def test_token_is_renewed_before_it_expires(fake, monkeypatch):
    fake.add_truck("t1", "4042")
    fake.add_truck("t2", "4043")
    clock = {"now": 1000.0}
    monkeypatch.setattr(qm, "_monotonic", lambda: clock["now"])
    await qm.truck_drivers("co", "4042")
    assert fake.auth_calls == 1
    clock["now"] += 3600 - 400               # still more than 5 min before the expiry
    await qm.truck_drivers("co", "4043")
    assert fake.auth_calls == 1
    clock["now"] += 200                      # now inside the 5 minute margin
    qm._truck_cache.clear()
    await qm.truck_drivers("co", "4043")
    assert fake.auth_calls == 2
    assert fake.requests[-1][2] == "Bearer tok-2"


def test_lifetime_keeps_a_margin_but_not_on_short_tokens(monkeypatch):
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(qm, "_utcnow", lambda: now)
    iso = lambda s: (now + timedelta(seconds=s)).astimezone(timezone(timedelta(hours=-4))).isoformat()
    assert qm._lifetime(iso(3600)) == pytest.approx(3300)
    assert qm._lifetime(iso(200)) == pytest.approx(100)       # half, not negative
    assert qm._lifetime(iso(-5)) == 0.0                        # already expired
    assert qm._lifetime("garbage") == qm.TOKEN_DEFAULT_LIFETIME
    assert qm._lifetime(None) == qm.TOKEN_DEFAULT_LIFETIME


async def test_refused_credentials_raise_auth_error_and_are_remembered(fake, caplog):
    caplog.set_level(logging.DEBUG)
    fake.creds = {CID: "other-secret"}
    with pytest.raises(qm.QuickManageAuthError) as exc:
        await qm.truck_drivers("co", "4042")
    with pytest.raises(qm.QuickManageUnavailable):
        await qm.truck_drivers("co", "4043")
    assert fake.count("/auth/token") == 1      # the second call did not try again
    # neither the secret nor a token ever reaches the messages or the logs
    assert SECRET not in str(exc.value)
    assert SECRET not in caplog.text


async def test_tokens_are_per_company(fake, monkeypatch):
    fake.creds["id-b"] = "secret-b"
    monkeypatch.setenv("QUICKMANAGE_CLIENT_ID_BETA", "id-b")
    monkeypatch.setenv("QUICKMANAGE_CLIENT_SECRET_BETA", "secret-b")
    fake.add_truck("t1", "4042")
    await qm.truck_drivers("co", "4042")
    await qm.truck_drivers("beta", "4042")
    assert fake.auth_calls == 2
    assert {a for _, p, a in fake.requests if p == "/x/trucks/search"} == {"Bearer tok-1", "Bearer tok-2"}


async def test_auth_response_without_token_is_an_error(fake):
    async def no_token(request):
        return web.json_response(_env({"expire": "2099-01-01T00:00:00+00:00"}))
    fake.auth = no_token
    with pytest.raises(qm.QuickManageError):
        await qm.truck_drivers("co", "4042")


# ── truck -> drivers ─────────────────────────────────────────────────────────────

async def test_only_the_exact_normalized_unit_matches(fake):
    fake.add_truck("a", "14042")
    fake.add_truck("b", "40420")
    fake.add_truck("c", "4042", drivers=[_driver("d1", "John", "Smith")])
    fake.add_truck("d", "4042-B")
    r = await qm.truck_drivers("co", "4042")
    assert r.found and r.truck.id == "c" and r.reason is None
    assert [d.id for d in r.drivers] == ["d1"]
    # the search itself was the partial "match" on unit_number
    assert fake.bodies[0]["filters"] == [
        {"field": "unit_number", "operator": "match", "value": "4042"}]
    assert fake.bodies[0]["page"] == 0 and fake.bodies[0]["query"] == ""


async def test_exact_match_ignores_case_and_punctuation(fake):
    fake.add_truck("a", "T120")
    r = await qm.truck_drivers("co", "t-120")     # the plain search misses "T120"; the key finds it
    assert r.found and r.truck.id == "a"
    fake.add_truck("b", "f 9121")
    r = await qm.truck_drivers("co", "F9121")
    assert r.found and r.truck.id == "b"


async def test_motive_style_suffix_falls_back_to_the_bare_unit(fake):
    fake.add_truck("a", "4063")
    fake.add_truck("b", "40631")
    r = await qm.truck_drivers("co", "4063   O/O")
    assert r.found and r.truck.id == "a"


async def test_full_unit_wins_over_the_bare_token(fake):
    fake.add_truck("a", "4063")
    fake.add_truck("b", "4063 O/O")
    r = await qm.truck_drivers("co", "4063 O/O")
    assert r.found and r.truck.id == "b"


async def test_unknown_unit_is_no_truck(fake):
    fake.add_truck("a", "14042")
    r = await qm.truck_drivers("co", "4042")
    assert not r.found and r.reason == qm.REASON_NO_TRUCK and r.drivers == ()
    r = await qm.truck_drivers("co", "  ")
    assert r.reason == qm.REASON_NO_TRUCK


async def test_several_exact_matches_are_ambiguous(fake):
    fake.add_truck("a", "4042", drivers=[_driver("d1", "John", "Smith")])
    fake.add_truck("b", "4042")
    r = await qm.truck_drivers("co", "4042")
    assert not r.found and r.reason == qm.REASON_AMBIGUOUS
    assert {t.id for t in r.matches} == {"a", "b"}
    assert r.drivers == ()
    assert fake.count("/x/trucks/a") == 0 and fake.count("/x/trucks/b") == 0


async def test_a_sold_truck_does_not_make_a_live_one_ambiguous(fake):
    fake.add_truck("old", "4042", status="sold", drivers=[_driver("d0", "Old", "Driver")])
    fake.add_truck("new", "4042", status="active", drivers=[_driver("d1", "John", "Smith")])
    r = await qm.truck_drivers("co", "4042")
    assert r.found and r.truck.id == "new"
    fake.add_truck("w1", "7000", status="sold")
    fake.add_truck("w2", "7000", status="total_loss")
    assert (await qm.truck_drivers("co", "7000")).reason == qm.REASON_AMBIGUOUS


async def test_terminated_drivers_are_dropped(fake):
    # The real truck record lists drivers as {id, first_name, last_name} only; status and
    # email come from GET /x/drivers/{id}.
    fake.add_truck("a", "4042", drivers=[
        _driver("d1", "John", "Smith", "active", email="john@example.com", phone="+1555"),
        _driver("d2", "Ann", "Invited", "invited", email="ann@example.com"),
        _driver("d3", "Tom", "Gone", "terminated", email="tom@example.com"),
        _driver("d4", " Mary  ", " Jones ", "ACTIVE", middle="Q"),
    ])
    r = await qm.truck_drivers("co", "4042")
    assert [d.id for d in r.drivers] == ["d1", "d2", "d4"]
    d1, _, d4 = r.drivers
    assert (d1.name, d1.email, d1.phone, d1.status) == ("John Smith", "john@example.com", "+1555", "active")
    assert d4.name == "Mary Jones" and d4.email is None and d4.phone is None
    assert d1.truck_unit == "4042"


async def test_truck_without_drivers(fake):
    fake.add_truck("a", "4042")
    r = await qm.truck_drivers("co", "4042")
    assert r.found and r.drivers == ()


async def test_search_is_paged_until_the_exact_unit_turns_up(fake):
    for i in range(60):
        fake.add_truck(f"x{i}", f"9{i:02d}4042")       # all contain "4042"
    fake.add_truck("exact", "4042")
    r = await qm.truck_drivers("co", "4042")
    assert r.found and r.truck.id == "exact"
    assert [b["page"] for b in fake.bodies] == [0, 1]


async def test_result_is_cached_per_company_and_unit(fake):
    fake.add_truck("a", "4042", drivers=[_driver("d1", "John", "Smith")])
    first = await qm.truck_drivers("co", "4042")
    n = len(fake.requests)
    again = await qm.truck_drivers("co", "4042")
    assert again is first and len(fake.requests) == n
    # same unit spelled differently shares the key; another unit does not
    assert (await qm.truck_drivers("co", "4042 ")) is first
    assert len(fake.requests) == n
    await qm.truck_drivers("co", "5000")
    assert len(fake.requests) > n


async def test_cache_expires_after_ten_minutes(fake, monkeypatch):
    fake.add_truck("a", "4042", drivers=[_driver("d1", "John", "Smith")])
    clock = {"now": 1000.0}
    monkeypatch.setattr(qm, "_monotonic", lambda: clock["now"])
    await qm.truck_drivers("co", "4042")
    clock["now"] += 599
    await qm.truck_drivers("co", "4042")
    assert fake.count("/x/trucks/a") == 1
    clock["now"] += 2
    fake.trucks["a"]["drivers"] = []
    r = await qm.truck_drivers("co", "4042")
    assert fake.count("/x/trucks/a") == 2 and r.drivers == ()


async def test_cache_is_per_company(fake, monkeypatch):
    fake.creds["id-b"] = "secret-b"
    monkeypatch.setenv("QUICKMANAGE_CLIENT_ID_BETA", "id-b")
    monkeypatch.setenv("QUICKMANAGE_CLIENT_SECRET_BETA", "secret-b")
    fake.add_truck("a", "4042")
    await qm.truck_drivers("co", "4042")
    await qm.truck_drivers("beta", "4042")
    assert fake.count("/x/trucks/search") == 2


# ── failures and retries ─────────────────────────────────────────────────────────

async def test_5xx_is_retried_then_succeeds(fake):
    fake.add_truck("a", "4042")
    fake.script["/x/trucks/search"] = [(503, {}), (500, {})]
    r = await qm.truck_drivers("co", "4042")
    assert r.found
    assert fake.count("/x/trucks/search") == 3
    assert len(fake.sleeps) == 2


async def test_429_honours_retry_after_within_a_cap(fake):
    fake.add_truck("a", "4042")
    fake.script["/x/trucks/search"] = [(429, {"Retry-After": "3"})]
    assert (await qm.truck_drivers("co", "4042")).found
    assert fake.sleeps == [3.0]
    fake.script["/x/trucks/search"] = [(429, {"Retry-After": "600"})]
    fake.sleeps.clear()
    await qm.truck_drivers("co", "4043")    # no such truck, but it is searched
    assert fake.sleeps == [qm._MAX_RETRY_AFTER]


async def test_persistent_5xx_is_unavailable_and_not_cached(fake):
    fake.add_truck("a", "4042")
    fake.script["/x/trucks/search"] = [(502, {})] * 3
    with pytest.raises(qm.QuickManageUnavailable):
        await qm.truck_drivers("co", "4042")
    assert fake.count("/x/trucks/search") == 3
    r = await qm.truck_drivers("co", "4042")       # the failure was not cached
    assert r.found


async def test_login_outage_is_unavailable_and_briefly_remembered(fake, monkeypatch):
    fake.script["/auth/token"] = [(500, {})] * 3
    clock = {"now": 1000.0}
    monkeypatch.setattr(qm, "_monotonic", lambda: clock["now"])
    with pytest.raises(qm.QuickManageUnavailable):
        await qm.truck_drivers("co", "4042")
    assert fake.count("/auth/token") == 3
    with pytest.raises(qm.QuickManageUnavailable):         # within 30 s: not tried again
        await qm.truck_drivers("co", "4042")
    assert fake.count("/auth/token") == 3
    clock["now"] += qm.AUTH_OUTAGE_TTL + 1
    fake.add_truck("a", "4042")
    assert (await qm.truck_drivers("co", "4042")).found


async def test_network_errors_and_timeouts_are_unavailable_after_retries(fake, monkeypatch):
    class _Down:
        closed = False
        calls = 0

        def __init__(self, exc):
            self.exc = exc

        def request(self, *a, **kw):
            type(self).calls += 1
            raise self.exc

    for exc in (aiohttp.ClientConnectionError("down"), asyncio.TimeoutError()):
        _Down.calls = 0
        fake.sleeps.clear()
        qm._reset_caches()
        monkeypatch.setattr(qm, "_get_http_session", lambda exc=exc: _Down(exc))
        with pytest.raises(qm.QuickManageUnavailable):
            await qm.truck_drivers("co", "4042")
        assert _Down.calls == 3 and len(fake.sleeps) == 2


async def test_other_4xx_is_a_plain_error(fake):
    fake.add_truck("a", "4042")
    fake.script["/x/trucks/search"] = [(400, {})]
    with pytest.raises(qm.QuickManageError) as exc:
        await qm.truck_drivers("co", "4042")
    assert not isinstance(exc.value, qm.QuickManageUnavailable)
    assert exc.value.status == 400
    assert fake.count("/x/trucks/search") == 1             # not retried


async def test_truck_vanishing_between_search_and_get_is_an_error(fake):
    fake.add_truck("gone", "4042")
    fake.script["/x/trucks/gone"] = [(404, {})]
    with pytest.raises(qm.QuickManageError) as exc:
        await qm.truck_drivers("co", "4042")
    assert exc.value.status == 404


async def test_malformed_body_is_an_error(fake):
    async def junk(request):
        return web.Response(text="<html>oops</html>", content_type="text/html")
    fake.trucks_search = junk
    fake.tokens.add("pre")
    with pytest.raises(qm.QuickManageError) as exc:
        await qm.truck_drivers("co", "4042")
    assert not isinstance(exc.value, qm.QuickManageUnavailable)


# ── driver search ────────────────────────────────────────────────────────────────

async def test_search_drivers_by_name_in_either_order(fake):
    fake.drivers["d1"] = _driver("d1", "John", "Smith", email="j@example.com", middle="Q")
    fake.drivers["d2"] = _driver("d2", "Johnny", "Smithers", "terminated")   # partial hits
    fake.drivers["d3"] = _driver("d3", "Ann", "Smith")
    found = await qm.search_drivers("co", "John Smith")
    assert [d.id for d in found] == ["d1"]       # d2 contains the text but is not the name
    assert found[0].email == "j@example.com"
    first_filters = fake.bodies[0]["filters"]
    assert {f["field"] for f in first_filters} == {"first_name", "last_name"}
    assert all(f["operator"] == "match" for f in first_filters)
    found = await qm.search_drivers("co", "SMITH, John")
    assert [d.id for d in found] == ["d1"]
    assert await qm.search_drivers("co", "Nobody Here") == []
    assert await qm.search_drivers("co", " 12 ") == []


async def test_search_drivers_single_name_tries_first_and_last(fake):
    fake.drivers["d1"] = _driver("d1", "Smith", "Adams")
    fake.drivers["d2"] = _driver("d2", "Zed", "Smith")
    found = await qm.search_drivers("co", "Smith")
    assert {d.id for d in found} == {"d1", "d2"}


async def test_terminated_drivers_are_found_by_search_with_their_status(fake):
    fake.drivers["d1"] = _driver("d1", "John", "Smith", "terminated")
    (d,) = await qm.search_drivers("co", "John Smith")
    assert d.status == "terminated"


async def test_close_closes_the_session_and_it_can_be_reopened(fake):
    fake.add_truck("a", "4042")
    await qm.truck_drivers("co", "4042")
    session = qm._http_session
    assert session is not None and not session.closed
    await qm.close()
    assert session.closed and qm._http_session is None
    qm._truck_cache.clear()
    assert (await qm.truck_drivers("co", "4042")).found
    assert qm._http_session is not session
