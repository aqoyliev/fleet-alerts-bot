"""Tests for utils/driver_lookup.py and utils/vitality.py, with a faked HTTP session.
No network: every request goes to _FakeSession, which routes by path."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest

import utils.driver_lookup as dl
from utils import vitality as vt

_PROVIDER = "prov_TEST_KEY"
_COMPANY = "comp_TEST_KEY"
NOW = datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env(data, total_pages=1, page=1):
    return {"description": "success", "data": data, "page": page, "size": 200,
            "total_elements": len(data) if isinstance(data, list) else 0,
            "total_pages": total_pages}


class _FakeResp:
    def __init__(self, status=200, body=None, delay=0.0):
        self.status = status
        self._body = body
        self._delay = delay

    async def __aenter__(self):
        if self._delay:
            await asyncio.sleep(self._delay)
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return str(self._body or "")


class _FakeSession:
    """routes: {path: response | exception | [responses...] | callable(params)}.
    A list is consumed in order, repeating the last. Records every call."""
    def __init__(self, routes):
        self.routes = routes
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        path = url.split("api.test", 1)[1]
        self.calls.append({"path": path, **kwargs})
        r = self.routes.get(path)
        if r is None:
            return _FakeResp(404, "not found")
        if isinstance(r, list):
            n = sum(1 for c in self.calls if c["path"] == path) - 1
            r = r[min(n, len(r) - 1)]
        if callable(r) and not isinstance(r, _FakeResp):
            r = r(kwargs.get("params") or {})
        if isinstance(r, BaseException):
            raise r
        return r

    def count(self, path):
        return sum(1 for c in self.calls if c["path"] == path)


VEH = "/v2/latest-vehicle-status"
STATUS = "/v2/latest-driver-status"
DRIVERS = "/v2/drivers"


def _vehicle(number, driver_id="d-1", ts=None, status="IN_MOTION"):
    row = {"number": number, "vin": "VIN", "status": status,
           "timestamp": _iso(ts or NOW), "lat": "1", "lon": "2", "speed": 50}
    if driver_id is not None:
        row["driver_id"] = driver_id
    return row


def _routes(vehicles=None, status="DS_D", drivers=None):
    vehicles = vehicles if vehicles is not None else [_vehicle("T-120")]
    drivers = drivers if drivers is not None else [
        {"driver_id": "d-1", "first_name": " JOHN ", "last_name": "SMITH  ", "username": "jsmith"}]
    return {
        VEH: _FakeResp(200, _env(vehicles)),
        STATUS: lambda p: _FakeResp(200, _env([{"driver_id": p.get("driver_id"), "username": "jsmith",
                                                "current_status": status}])),
        DRIVERS: _FakeResp(200, _env(drivers)),
    }


@pytest.fixture
def env(monkeypatch):
    vt._reset_caches()
    dl._warned_at.clear()
    monkeypatch.setattr(vt.config, "VITALITY_PROVIDER_KEY", _PROVIDER, raising=False)
    monkeypatch.setattr(vt.config, "VITALITY_BASE_URL", "https://api.test/", raising=False)

    async def _no_sleep(s):
        pass

    monkeypatch.setattr(vt, "_sleep", _no_sleep)
    keys = {"co": _COMPANY}
    key_calls = []

    async def _key(slug):
        key_calls.append(slug)
        return keys.get(slug)

    monkeypatch.setattr(dl, "get_vitality_company_key", _key)
    state = {"keys": keys, "key_calls": key_calls}

    def install(routes):
        session = _FakeSession(routes)
        monkeypatch.setattr(vt, "_get_http_session", lambda: session)
        state["session"] = session
        return session

    state["install"] = install
    yield state
    vt._reset_caches()
    dl._warned_at.clear()


MOTIVE_EVENT = {"driver": {"id": 4242, "first_name": "John", "last_name": "Smith"}}


# ── the happy path ───────────────────────────────────────────────────────────────

async def test_vitality_driver_found(env):
    s = env["install"](_routes())
    info = await dl.resolve_driver("co", MOTIVE_EVENT, "T-120", NOW - timedelta(minutes=2))
    assert info == dl.DriverInfo(name="JOHN SMITH", ref="vitality:d-1", email=None,
                                 source="vitality", on_duty=True)
    call = s.calls[0]
    assert call["headers"]["X-API-Provider-Key"] == _PROVIDER
    assert call["headers"]["X-API-Company-Key"] == _COMPANY
    assert call["headers"]["accept"] == "application/json"
    assert call["params"] == {"limit": 200, "page": 1}
    assert s.count(STATUS) == 1 and s.calls[[c["path"] for c in s.calls].index(STATUS)][
        "params"] == {"driver_id": "d-1"}


@pytest.mark.parametrize("fleet_number,event_vehicle", [
    ("T-120", "T120"), ("T120", "T-120"), ("t 120", "T-120"),
    ("0942", "942"), ("942", "0942"), ("00942", "942"),
])
async def test_unit_matching(env, fleet_number, event_vehicle):
    env["install"](_routes([_vehicle("999", driver_id="other"), _vehicle(fleet_number)]))
    info = await dl.resolve_driver("co", {}, event_vehicle, NOW)
    assert info.ref == "vitality:d-1"


async def test_unknown_unit_falls_back(env):
    env["install"](_routes([_vehicle("555")]))
    info = await dl.resolve_driver("co", MOTIVE_EVENT, "T-120", NOW)
    assert info == dl.DriverInfo(name="John Smith", ref="motive:4242", source="event")


def test_lookup_helper():
    units = vt._index_units([_vehicle("0942"), {"number": ""}, {"no": "number"}])
    assert vt.lookup(units, "942").number == "0942"
    assert vt.lookup(units, "0942").number == "0942"
    assert vt.lookup(units, "") is None
    assert vt.lookup(units, None) is None


def test_lookup_suffix_falls_back_to_unit():
    units = vt._index_units([_vehicle("4063"), _vehicle("0942")])
    assert vt.lookup(units, "4063 O/O").number == "4063"
    assert vt.lookup(units, "4063   O/O").number == "4063"
    assert vt.lookup(units, "942 ACTIVE").number == "0942"
    assert vt.lookup(units, "9999 O/O") is None


def test_lookup_exact_match_beats_suffix_fallback():
    units = vt._index_units([_vehicle("4063", driver_id="plain"), _vehicle("4063OO", driver_id="exact")])
    assert vt.lookup(units, "4063 O/O").driver_id == "exact"


# ── when the snapshot cannot tell ────────────────────────────────────────────────

async def test_stale_timestamp_falls_back(env):
    env["install"](_routes([_vehicle("T-120", ts=NOW - timedelta(minutes=45))]))
    info = await dl.resolve_driver("co", MOTIVE_EVENT, "T-120", NOW)
    assert info.source == "event" and info.ref == "motive:4242"


async def test_timestamp_compared_with_occurred_at(env):
    # Reported 40 min ago, event 35 min ago: 5 min apart -> accepted.
    env["install"](_routes([_vehicle("T-120", ts=NOW - timedelta(minutes=40))]))
    info = await dl.resolve_driver("co", {}, "T-120", NOW - timedelta(minutes=35))
    assert info.source == "vitality"


async def test_naive_occurred_at_read_as_utc(env):
    env["install"](_routes([_vehicle("T-120", ts=NOW - timedelta(minutes=1))]))
    naive = (NOW - timedelta(minutes=2)).replace(tzinfo=None)
    info = await dl.resolve_driver("co", {}, "T-120", naive)
    assert info.source == "vitality"


async def test_no_occurred_at_compares_with_now(env):
    env["install"](_routes([_vehicle("T-120", ts=NOW - timedelta(hours=2))]))
    assert (await dl.resolve_driver("co", {}, "T-120", None)).source == "unknown"


async def test_no_driver_id_falls_back(env):
    env["install"](_routes([_vehicle("T-120", driver_id=None)]))
    info = await dl.resolve_driver("co", MOTIVE_EVENT, "T-120", NOW)
    assert info.source == "event" and info.ref == "motive:4242"


# ── Vitality unavailable ─────────────────────────────────────────────────────────

async def test_data_null_falls_back_and_warns_once_per_hour(env, caplog, monkeypatch):
    s = env["install"]({VEH: _FakeResp(200, {"description": "success", "data": None,
                                             "total_pages": 0})})
    caplog.set_level(logging.WARNING, logger="utils.driver_lookup")
    for _ in range(3):
        info = await dl.resolve_driver("co", MOTIVE_EVENT, "T-120", NOW)
        assert info.source == "event" and info.ref == "motive:4242"
    warnings = [r for r in caplog.records if "unavailable" in r.getMessage()]
    assert len(warnings) == 1
    assert "co" in warnings[0].getMessage()
    assert s.count(VEH) == 1  # the outage itself is cached too
    # After the hour (and the negative cache), it warns again.
    real = vt.time.monotonic
    monkeypatch.setattr(vt.time, "monotonic", lambda: real() + 4000)
    monkeypatch.setattr(dl.time, "monotonic", lambda: real() + 4000)
    await dl.resolve_driver("co", MOTIVE_EVENT, "T-120", NOW)
    assert len([r for r in caplog.records if "unavailable" in r.getMessage()]) == 2
    assert s.count(VEH) == 2


async def test_empty_fleet_is_unavailable(env, caplog):
    env["install"]({VEH: _FakeResp(200, _env([]))})
    caplog.set_level(logging.WARNING, logger="utils.driver_lookup")
    assert (await dl.resolve_driver("co", {}, "T-120", NOW)).source == "unknown"
    assert any("unavailable" in r.getMessage() for r in caplog.records)


async def test_403_falls_back_without_retry(env, caplog):
    s = env["install"]({VEH: _FakeResp(403, "Forbidden")})
    caplog.set_level(logging.WARNING, logger="utils.driver_lookup")
    info = await dl.resolve_driver("co", MOTIVE_EVENT, "T-120", NOW)
    assert info.source == "event"
    assert s.count(VEH) == 1
    assert any("HTTP 403" in r.getMessage() for r in caplog.records)


async def test_5xx_retried_once_then_falls_back(env):
    s = env["install"]({VEH: _FakeResp(502, "<html>bad gateway</html>")})
    info = await dl.resolve_driver("co", MOTIVE_EVENT, "T-120", NOW)
    assert info.source == "event"
    assert s.count(VEH) == 2


async def test_5xx_then_success(env):
    routes = _routes()
    routes[VEH] = [_FakeResp(500, "oops"), routes[VEH]]
    s = env["install"](routes)
    assert (await dl.resolve_driver("co", {}, "T-120", NOW)).source == "vitality"
    assert s.count(VEH) == 2


async def test_network_error_retried_once(env):
    s = env["install"]({VEH: aiohttp.ClientConnectionError("down")})
    assert (await dl.resolve_driver("co", {}, "T-120", NOW)).source == "unknown"
    assert s.count(VEH) == 2


async def test_timeout_falls_back(env, monkeypatch, caplog):
    monkeypatch.setattr(dl, "VITALITY_TIMEOUT", 0.05)
    routes = _routes()
    routes[VEH] = _FakeResp(200, routes[VEH]._body, delay=1.0)
    env["install"](routes)
    caplog.set_level(logging.WARNING, logger="utils.driver_lookup")
    info = await dl.resolve_driver("co", MOTIVE_EVENT, "T-120", NOW)
    assert info == dl.DriverInfo(name="John Smith", ref="motive:4242", source="event")
    assert any("timed out" in r.getMessage() for r in caplog.records)


async def test_db_error_never_raises(env, monkeypatch):
    async def boom(slug):
        raise RuntimeError("db down")
    monkeypatch.setattr(dl, "get_vitality_company_key", boom)
    s = env["install"](_routes())
    info = await dl.resolve_driver("co", MOTIVE_EVENT, "T-120", NOW)
    assert info.ref == "motive:4242"
    assert s.calls == []


# ── no keys -> no HTTP ───────────────────────────────────────────────────────────

async def test_no_provider_key_makes_no_request(env, monkeypatch):
    monkeypatch.setattr(vt.config, "VITALITY_PROVIDER_KEY", "", raising=False)
    s = env["install"](_routes())
    info = await dl.resolve_driver("co", MOTIVE_EVENT, "T-120", NOW)
    assert info.source == "event"
    assert s.calls == [] and env["key_calls"] == []


async def test_no_company_key_makes_no_request(env):
    s = env["install"](_routes())
    info = await dl.resolve_driver("other-co", MOTIVE_EVENT, "T-120", NOW)
    assert info.source == "event"
    assert s.calls == []


# ── duty status ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code,expected", [
    ("DS_D", True), ("DS_ON", True), ("DR_IND_YM", True),
    ("DS_OFF", False), ("DS_SB", False), ("DR_IND_PC", False),
    (None, None), ("", None), ("SOMETHING_NEW", None), ("ds_d", True),
])
def test_duty_from_status(code, expected):
    assert dl.duty_from_status(code) is expected


@pytest.mark.parametrize("code,expected", [("DS_SB", False), ("DR_IND_YM", True), (None, None)])
async def test_on_duty_from_status_endpoint(env, code, expected):
    env["install"](_routes(status=code))
    info = await dl.resolve_driver("co", {}, "T-120", NOW)
    assert info.ref == "vitality:d-1" and info.on_duty is expected


async def test_status_failure_keeps_driver(env):
    routes = _routes()
    routes[STATUS] = _FakeResp(500, "oops")
    env["install"](routes)
    info = await dl.resolve_driver("co", {}, "T-120", NOW)
    assert info.ref == "vitality:d-1" and info.on_duty is None and info.name == "JOHN SMITH"


# ── names ────────────────────────────────────────────────────────────────────────

async def test_name_mismatch_logged_and_vitality_kept(env, caplog):
    env["install"](_routes())
    caplog.set_level(logging.INFO, logger="utils.driver_lookup")
    info = await dl.resolve_driver("co", {"driver": {"id": 7, "name": "Bob Jones"}}, "T-120", NOW)
    assert info.name == "JOHN SMITH" and info.ref == "vitality:d-1"
    msgs = [r.getMessage() for r in caplog.records if "mismatch" in r.getMessage()]
    assert len(msgs) == 1 and "JOHN SMITH" in msgs[0] and "Bob Jones" in msgs[0]


async def test_same_name_different_case_and_spaces_not_logged(env, caplog):
    env["install"](_routes())
    caplog.set_level(logging.INFO, logger="utils.driver_lookup")
    await dl.resolve_driver("co", {"driver": {"name": "john   smith"}}, "T-120", NOW)
    assert not any("mismatch" in r.getMessage() for r in caplog.records)


async def test_roster_miss_fetches_single_driver(env):
    routes = _routes(drivers=[{"driver_id": "someone-else", "first_name": "A", "last_name": "B"}])
    routes["/v2/driver/d-1"] = _FakeResp(200, {"description": "success", "data": {
        "driver_id": "d-1", "first_name": "Jane ", "last_name": " Doe", "active": True}})
    s = env["install"](routes)
    assert (await dl.resolve_driver("co", {}, "T-120", NOW)).name == "Jane Doe"
    vt._snapshots.clear()  # force a new snapshot; the name must still be cached
    vt._statuses.clear()
    assert (await dl.resolve_driver("co", {}, "T-120", NOW)).name == "Jane Doe"
    assert s.count("/v2/driver/d-1") == 1 and s.count(DRIVERS) == 1


async def test_name_falls_back_to_username(env):
    routes = _routes(drivers=[])
    routes["/v2/driver/d-1"] = _FakeResp(404, "not found")
    env["install"](routes)
    info = await dl.resolve_driver("co", {}, "T-120", NOW)
    assert info.name == "jsmith" and info.ref == "vitality:d-1"


async def test_roster_paginates(env):
    def drivers(params):
        page = params["page"]
        assert params["limit"] == 1000
        rows = [{"driver_id": f"p{page}", "first_name": f"P{page}", "last_name": "X"}]
        if page == 2:
            rows.append({"driver_id": "d-1", "first_name": "Two", "last_name": "Pages"})
        return _FakeResp(200, _env(rows, total_pages=2, page=page))
    routes = _routes()
    routes[DRIVERS] = drivers
    s = env["install"](routes)
    assert (await dl.resolve_driver("co", {}, "T-120", NOW)).name == "Two Pages"
    assert s.count(DRIVERS) == 2


# ── caching ──────────────────────────────────────────────────────────────────────

async def test_snapshot_cached_across_calls(env):
    s = env["install"](_routes([_vehicle("T-120"), _vehicle("T-121", driver_id="d-2")]))
    await dl.resolve_driver("co", {}, "T-120", NOW)
    await dl.resolve_driver("co", {}, "T-121", NOW)
    assert s.count(VEH) == 1
    assert s.count(DRIVERS) == 1


async def test_concurrent_burst_shares_one_snapshot(env):
    routes = _routes()
    routes[VEH] = _FakeResp(200, routes[VEH]._body, delay=0.05)
    s = env["install"](routes)
    results = await asyncio.gather(*[dl.resolve_driver("co", {}, "T-120", NOW) for _ in range(5)])
    assert all(r.ref == "vitality:d-1" for r in results)
    assert s.count(VEH) == 1


async def test_snapshot_refetched_after_ttl(env, monkeypatch):
    s = env["install"](_routes())
    await dl.resolve_driver("co", {}, "T-120", NOW)
    real = vt.time.monotonic
    monkeypatch.setattr(vt.time, "monotonic", lambda: real() + vt.SNAPSHOT_TTL + 1)
    await dl.resolve_driver("co", {}, "T-120", NOW)
    assert s.count(VEH) == 2
    assert s.count(DRIVERS) == 1  # roster lives an hour


async def test_snapshot_paginates(env):
    def vehicles(params):
        page = params["page"]
        rows = [_vehicle(f"P{page}", driver_id=f"x{page}")]
        if page == 3:
            rows.append(_vehicle("T-120"))
        return _FakeResp(200, _env(rows, total_pages=3, page=page))
    routes = _routes()
    routes[VEH] = vehicles
    s = env["install"](routes)
    assert (await dl.resolve_driver("co", {}, "T-120", NOW)).ref == "vitality:d-1"
    assert s.count(VEH) == 3


# ── event fallback ───────────────────────────────────────────────────────────────

def test_event_fallback_motive_id():
    assert dl._from_event(MOTIVE_EVENT) == dl.DriverInfo(
        name="John Smith", ref="motive:4242", source="event")
    assert dl._from_event({"driver": {"id": 9}}) == dl.DriverInfo(ref="motive:9", source="event")
    assert dl._from_event({"current_driver": {"id": "77", "username": "u1"}}).ref == "motive:77"


def test_event_fallback_samsara_has_no_ref():
    info = dl._from_event({"_source": "samsara", "driver": {"id": 5, "name": "Sam"}})
    assert info == dl.DriverInfo(name="Sam", source="event")
    assert dl._from_event({"_source": "samsara", "driver_id": 5}) == dl.DriverInfo()


def test_event_fallback_motive_top_level_driver_id():
    # Real Motive payloads carry driver_id at the top level; used when driver.id is absent.
    assert dl._from_event({"driver_id": 555}) == dl.DriverInfo(ref="motive:555", source="event")
    info = dl._from_event({"driver_id": "556", "driver": {"first_name": "Ann", "last_name": "Lee"}})
    assert info == dl.DriverInfo(name="Ann Lee", ref="motive:556", source="event")
    # driver.id, when present, still wins.
    assert dl._from_event({"driver_id": 1, "driver": {"id": 2}}).ref == "motive:2"
    assert dl._from_event({"driver_id": None}) == dl.DriverInfo()


async def test_close_is_safe():
    await vt.close()
    await vt.close()
