"""The panel's Group screen: the one alerts group, its mute and its event filter.

There is exactly one group on this branch, so unlike single-company there is no group id
in any path and nothing to validate about one — every endpoint here acts on the
singleton row. What is left to get wrong is the state an admin reads back: whether an
empty filter is shown as "everything" or as "nothing", and what the screen is told when
no group has been configured at all.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from utils.webapp import api, auth

BOT_TOKEN = "123:test"
GROUP = -1003786362177


def _init_data(user_id: int = 111) -> str:
    fields = {"user": json.dumps({"id": user_id}), "auth_date": str(int(time.time()))}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class _Request:
    def __init__(self, body=None, query=None):
        self.headers = {"X-Telegram-Init-Data": _init_data()}
        self.match_info = {}
        self.query = query or {}
        self.method = "POST"
        self.path = "/panel/api/group"
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
    """An admin at the panel, with the group layer scripted. state["status"] is the row
    (None = no group configured); writes are recorded."""
    state = {"status": {"telegram_group_id": GROUP, "enabled": True, "event_types": []},
             "enabled_writes": [], "filter_writes": [], "total": 37}

    async def _status():
        return state["status"]

    async def _set_enabled(enabled):
        state["enabled_writes"].append(enabled)

    async def _get_filter():
        return list(state["status"]["event_types"])

    async def _set_filter(event_types):
        state["filter_writes"].append(set(event_types))
        state["status"]["event_types"] = sorted(event_types)

    async def _totals(since, until):
        return {"total": state["total"], "speeding": 0, "crashes": 0, "units": 0}

    monkeypatch.setattr(auth, "is_admin", _always(True))
    monkeypatch.setattr(auth, "is_super_admin", _always(False))
    monkeypatch.setattr(api, "get_group_status", _status)
    monkeypatch.setattr(api, "set_group_enabled", _set_enabled)
    monkeypatch.setattr(api, "get_group_event_types", _get_filter)
    monkeypatch.setattr(api, "set_group_event_types", _set_filter)
    monkeypatch.setattr(api, "get_totals", _totals)
    return state


# ── reading the group ──────────────────────────────────────────────────────────

async def test_the_group_screen_gets_the_one_row(panel):
    resp = await api.group(_Request())

    assert resp.status == 200
    assert _body(resp) == {
        "configured": True, "telegram_group_id": GROUP, "enabled": True,
        "event_types": [], "filter_mode": "all", "alerts_7d": 37,
    }


async def test_an_empty_filter_is_named_all_not_left_to_look_like_nothing(panel):
    """Zero rows means "every type". Handed to the UI as a bare empty list it would be
    drawn as a group that receives no alerts at all."""
    assert _body(await api.group(_Request()))["filter_mode"] == "all"

    panel["status"]["event_types"] = ["crash", "speeding"]
    data = _body(await api.group(_Request()))
    assert data["filter_mode"] == "custom"
    assert data["event_types"] == ["crash", "speeding"]


async def test_a_muted_group_reads_as_muted(panel):
    panel["status"]["enabled"] = False

    assert _body(await api.group(_Request()))["enabled"] is False


async def test_no_group_is_an_answer_not_an_error(panel):
    """The screen has something to say in this state — no group is set, alerts are going
    to DMs only — and a 404 would have it apologise for a failed request instead."""
    panel["status"] = None
    resp = await api.group(_Request())

    assert resp.status == 200
    assert _body(resp) == {"configured": False}


async def test_the_dashboard_is_told_when_the_group_is_silent(panel, monkeypatch):
    """Rides along with the stats: the numbers on that screen are alerts that were
    counted and — in these two states — posted to nobody."""
    async def _rows(*_a, **_k):
        return []

    monkeypatch.setattr(api, "get_type_counts", _rows)
    monkeypatch.setattr(api, "get_top_violators", _rows)
    monkeypatch.setattr(api, "get_daily_counts", _rows)

    assert _body(await api.stats(_Request()))["group"] == {"configured": True, "muted": False}

    panel["status"]["enabled"] = False
    assert _body(await api.stats(_Request()))["group"] == {"configured": True, "muted": True}

    panel["status"] = None
    assert _body(await api.stats(_Request()))["group"] == {"configured": False, "muted": False}


# ── muting ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("enabled", [False, True])
async def test_the_switch_writes_the_one_row(panel, enabled):
    resp = await api.set_enabled(_Request({"enabled": enabled}))

    assert resp.status == 200
    assert _body(resp) == {"ok": True, "enabled": enabled}
    assert panel["enabled_writes"] == [enabled]


async def test_muting_with_no_group_is_a_404_not_a_silent_success(panel):
    """An UPDATE that matches no row succeeds. Answering ok here would show a switch
    flipping on a group that does not exist."""
    panel["status"] = None
    resp = await api.set_enabled(_Request({"enabled": False}))

    assert resp.status == 404
    assert _body(resp)["error"] == "no_group"
    assert panel["enabled_writes"] == []


# ── the event filter ───────────────────────────────────────────────────────────

async def test_turning_one_type_off_from_all_lists_every_other_type(panel):
    from data.event_catalog import GROUP_FILTER_TYPE_SET

    resp = await api.toggle_event(_Request({"action": "toggle", "event_type": "speeding"}))
    data = _body(resp)

    assert resp.status == 200
    assert data["filter_mode"] == "custom"
    assert set(data["event_types"]) == GROUP_FILTER_TYPE_SET - {"speeding"}
    assert panel["filter_writes"] == [GROUP_FILTER_TYPE_SET - {"speeding"}]


async def test_turning_the_last_missing_type_back_on_collapses_to_all(panel):
    """The rule lives in next_event_filter, on the server, and the response carries the
    result — so the client never has to reimplement it and drift."""
    from data.event_catalog import GROUP_FILTER_TYPE_SET

    panel["status"]["event_types"] = sorted(GROUP_FILTER_TYPE_SET - {"speeding"})
    data = _body(await api.toggle_event(_Request({"action": "toggle",
                                                  "event_type": "speeding"})))

    assert data == {"ok": True, "event_types": [], "filter_mode": "all"}
    assert panel["filter_writes"] == [set()]


async def test_crash_can_be_taken_off_the_group(panel):
    data = _body(await api.toggle_event(_Request({"action": "toggle",
                                                  "event_type": "crash"})))

    assert "crash" not in data["event_types"]


async def test_the_harsh_turn_chip_moves_both_of_its_types(panel):
    panel["status"]["event_types"] = ["speeding"]
    data = _body(await api.toggle_event(_Request({"action": "toggle",
                                                  "event_type": "harsh_turn"})))

    assert data["event_types"] == ["hard_cornering", "harsh_turn", "speeding"]


async def test_reset_clears_the_filter(panel):
    panel["status"]["event_types"] = ["speeding"]
    data = _body(await api.toggle_event(_Request({"action": "all"})))

    assert data == {"ok": True, "event_types": [], "filter_mode": "all"}
    assert panel["filter_writes"] == [set()]


@pytest.mark.parametrize("event_type", ["tailgating", "hard_cornering", "", None, "'; DROP"])
async def test_only_a_row_token_from_the_catalog_is_accepted(panel, event_type):
    """Not a type HF doesn't alert on, not the second half of a row on its own, and not
    whatever a browser cares to send."""
    resp = await api.toggle_event(_Request({"action": "toggle", "event_type": event_type}))

    assert resp.status == 400
    assert _body(resp)["error"] == "bad_event_type"
    assert panel["filter_writes"] == []


async def test_the_filter_cannot_be_edited_with_no_group(panel):
    panel["status"] = None
    resp = await api.toggle_event(_Request({"action": "all"}))

    assert resp.status == 404
    assert panel["filter_writes"] == []


# ── who may ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("handler", [api.group, api.set_enabled, api.toggle_event])
async def test_a_non_admin_is_refused(panel, monkeypatch, handler):
    monkeypatch.setattr(auth, "is_admin", _always(False))

    resp = await handler(_Request({"enabled": False, "action": "all"}))

    assert resp.status == 403
    assert panel["enabled_writes"] == [] and panel["filter_writes"] == []


async def test_any_admin_may_mute_not_only_a_super_admin(panel):
    """Muting is operational, not administrative: whoever is on shift has to be able to
    stop a flood. Managing who the admins are is what needs a super admin."""
    resp = await api.set_enabled(_Request({"enabled": False}))

    assert resp.status == 200
