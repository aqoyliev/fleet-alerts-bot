"""Tests for utils/samsara/safety.py: the GET /fleet/safety-events lookup and the
retrying wait, through a faked aiohttp session. No network, no real sleeping."""
from datetime import datetime, timedelta, timezone

import pytest

from utils.samsara import safety

AT = datetime(2026, 10, 23, 14, 0, 0, tzinfo=timezone.utc)
VEH = "281474977"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(id_, offset_s=0, labels=("mobileUsage",), state="needsReview", vehicle=VEH):
    item = {
        "id": id_,
        "time": _iso(AT + timedelta(seconds=offset_s)),
        "vehicle": {"id": vehicle, "name": "T-120"},
        "coachingState": state,
        "behaviorLabels": [{"label": l, "name": l, "source": "automated"} for l in labels],
    }
    return item


def _page(items, cursor=None):
    return {"data": items, "pagination": {"endCursor": cursor or "", "hasNextPage": bool(cursor)}}


class _Resp:
    def __init__(self, status, payload):
        self.status, self._payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    async def text(self):
        return "boom"


class _Session:
    """Answers each GET with the next scripted response (the last repeats) and
    records the request."""
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def get(self, url, params=None, headers=None):
        self.requests.append({"url": url, "params": dict(params or {}), "headers": headers})
        idx = min(len(self.requests) - 1, len(self.responses) - 1)
        resp = self.responses[idx]
        if isinstance(resp, Exception):
            raise resp
        return resp


@pytest.fixture
def http(monkeypatch):
    state = {}
    sleeps = []

    async def no_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(safety, "_sleep", no_sleep)

    def install(*responses):
        session = _Session(responses)
        monkeypatch.setattr(safety, "_session", lambda: session)
        state["session"] = session
        return session

    state["install"], state["sleeps"] = install, sleeps
    return state


# ── find_safety_event ────────────────────────────────────────────────────────────

async def test_request_shape(http):
    s = http["install"](_Resp(200, _page([_item("a")])))
    assert (await safety.find_safety_event("KEY", VEH, AT))["id"] == "a"
    req = s.requests[0]
    assert req["url"] == "https://api.samsara.com/fleet/safety-events"
    assert req["params"] == {"startTime": "2026-10-23T13:58:00Z", "endTime": "2026-10-23T14:02:00Z",
                             "vehicleIds": VEH}
    assert req["headers"]["Authorization"] == "Bearer KEY"


async def test_requires_the_label_and_logs_what_it_saw(http, caplog):
    http["install"](_Resp(200, _page([_item("a", labels=("genericDistraction",)),
                                      _item("b", 5, labels=("noSeatbelt", "someNewLabel"))])))
    with caplog.at_level("INFO", logger=safety.logger.name):
        assert await safety.find_safety_event("KEY", VEH, AT) is None
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "genericDistraction" in msg and "someNewLabel" in msg


async def test_picks_the_labelled_item_closest_in_time(http):
    http["install"](_Resp(200, _page([
        _item("far", -90),
        _item("closest-unlabelled", 1, labels=("genericDistraction",)),
        _item("near", 20, state="dismissed"),
        _item("after", 45),
    ])))
    found = await safety.find_safety_event("KEY", VEH, AT)
    assert found["id"] == "near" and found["coachingState"] == "dismissed"


async def test_item_with_several_labels_matches(http):
    http["install"](_Resp(200, _page([_item("a", labels=("genericDistraction", "mobileUsage"))])))
    assert (await safety.find_safety_event("KEY", VEH, AT))["id"] == "a"


async def test_other_vehicles_are_ignored(http):
    http["install"](_Resp(200, _page([_item("other", 0, vehicle="999"), _item("mine", 60)])))
    assert (await safety.find_safety_event("KEY", VEH, AT))["id"] == "mine"


async def test_follows_pagination(http):
    s = http["install"](_Resp(200, _page([_item("p1", 100)], cursor="c1")),
                        _Resp(200, _page([_item("p2", 3)], cursor="c2")),
                        _Resp(200, _page([_item("p3", -50)])))
    assert (await safety.find_safety_event("KEY", VEH, AT))["id"] == "p2"
    assert len(s.requests) == 3
    assert "after" not in s.requests[0]["params"]
    assert s.requests[1]["params"]["after"] == "c1" and s.requests[2]["params"]["after"] == "c2"


async def test_driver_may_be_absent_and_time_may_be_bad(http):
    item = _item("a")
    bad = {**_item("bad", 1), "time": "nonsense"}
    http["install"](_Resp(200, _page([bad, item])))
    assert (await safety.find_safety_event("KEY", VEH, AT))["id"] == "a"


@pytest.mark.parametrize("response", [
    _Resp(500, {}), _Resp(401, {}), _Resp(200, ValueError("not json")), OSError("conn reset"),
])
async def test_errors_return_none_and_never_raise(http, caplog, response):
    http["install"](response)
    with caplog.at_level("WARNING", logger=safety.logger.name):
        assert await safety.find_safety_event("KEY", VEH, AT) is None
    assert caplog.records    # logged as an error, not as "not found"
    assert all("No 'mobileUsage' safety event" not in r.getMessage() for r in caplog.records)


async def test_missing_key_or_vehicle_returns_none(http):
    s = http["install"](_Resp(200, _page([_item("a")])))
    assert await safety.find_safety_event("", VEH, AT) is None
    assert await safety.find_safety_event("KEY", "", AT) is None
    assert s.requests == []


async def test_naive_time_is_read_as_utc(http):
    s = http["install"](_Resp(200, _page([_item("a")])))
    await safety.find_safety_event("KEY", VEH, AT.replace(tzinfo=None))
    assert s.requests[0]["params"]["startTime"] == "2026-10-23T13:58:00Z"


async def test_key_is_never_logged(http, caplog):
    http["install"](_Resp(403, {}))
    with caplog.at_level("DEBUG"):
        await safety.find_safety_event("SECRET-KEY-123", VEH, AT)
    assert all("SECRET-KEY-123" not in r.getMessage() for r in caplog.records)


# ── wait_for_safety_event ────────────────────────────────────────────────────────

async def test_wait_returns_as_soon_as_found(http):
    s = http["install"](_Resp(200, _page([])), _Resp(200, _page([])),
                        _Resp(200, _page([_item("a", state="needsCoaching")])))
    result = await safety.wait_for_safety_event("KEY", VEH, AT)
    assert result.status == "found" and result.coaching_state == "needsCoaching"
    assert result.attempts == 3 and len(s.requests) == 3
    assert http["sleeps"] == [30, 30, 60]          # +30s, +60s, +120s


async def test_wait_not_found_after_every_attempt(http):
    s = http["install"](_Resp(200, _page([_item("x", labels=("noSeatbelt",))])))
    result = await safety.wait_for_safety_event("KEY", VEH, AT)
    assert result.status == "not_found" and result.event is None
    assert result.attempts == len(safety.POLL_MARKS) == len(s.requests)
    assert sum(http["sleeps"]) == safety.POLL_MARKS[-1] == 480


async def test_wait_keeps_trying_through_errors(http):
    s = http["install"](_Resp(500, {}), OSError("down"), _Resp(200, _page([_item("a")])))
    result = await safety.wait_for_safety_event("KEY", VEH, AT)
    assert result.status == "found" and len(s.requests) == 3


async def test_wait_ending_on_an_error_reports_error(http):
    http["install"](_Resp(503, {}))
    result = await safety.wait_for_safety_event("KEY", VEH, AT, marks=(1, 2))
    assert result.status == "error" and result.attempts == 2


async def test_wait_without_key_does_not_call(http):
    s = http["install"](_Resp(200, _page([_item("a")])))
    assert (await safety.wait_for_safety_event("", VEH, AT)).status == "error"
    assert s.requests == [] and http["sleeps"] == []


def test_allowed_states():
    for state in ("needsReview", "needsCoaching", "needsRecognition"):
        assert safety.is_allowed(state)
    for state in ("coached", "dismissed", "autoDismissed", "reviewed", "archived",
                  "manualReview", "recognized", "invalid", None, "", "needsreview",
                  safety.STATE_PENDING, safety.STATE_NOT_FOUND, safety.STATE_LOOKUP_ERROR):
        assert not safety.is_allowed(state)
