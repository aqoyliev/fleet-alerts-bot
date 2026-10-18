"""The two screens the Mini App took over from the bot's chat keyboard.

Both are thin — the numbers come from utils/violation_report.py and the switches from
utils/db_api/admins.py — so what is worth testing is the wiring: that a notification
switch writes every provider spelling behind its label, that the report file is sent to
the person who asked and to nobody they can name, and that neither endpoint trusts a
value out of the query string.
"""

import json

import hashlib
import hmac
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import pytest

from utils.webapp import api, auth

BOT_TOKEN = "123:test"
ADMIN = 111


def _init_data(user_id: int = ADMIN) -> str:
    fields = {"user": json.dumps({"id": user_id}), "auth_date": str(int(time.time()))}
    check = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class _Request:
    def __init__(self, body=None, query=None, user_id=ADMIN):
        self.headers = {"X-Telegram-Init-Data": _init_data(user_id)}
        self.match_info = {}
        self.query = query or {}
        self.method = "POST" if body is not None else "GET"
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


def _always(value):
    async def _check(_telegram_id):
        return value
    return _check


def _payload(resp):
    return json.loads(resp.body)


@pytest.fixture
def panel(monkeypatch):
    monkeypatch.setattr(auth, "is_admin", _always(True))
    monkeypatch.setattr(auth, "is_super_admin", _always(False))
    monkeypatch.setattr(api, "is_super_admin", _always(False))
    return monkeypatch


# ── notification switches ───────────────────────────────────────────────────────

def test_a_switch_covers_every_spelling_behind_its_label():
    """Samsara says drowsy_driving, Motive says drowsiness, and get_subscribed_admins
    matches the stored type exactly. A switch that wrote only its own token would leave
    one provider's drowsiness alerts going nowhere — the bug that was found in the group
    filters, which must not be reintroduced one screen over."""
    assert set(api._subscription_group("drowsy_driving")) == {"drowsy_driving", "drowsiness"}
    assert len(api._subscription_group("road_facing_cam_obstruction")) == 3
    assert api._subscription_group("crash") == ("crash",)
    assert api._subscription_group("not_a_type") == ()


async def test_a_row_reads_as_on_when_any_of_its_types_is_subscribed(panel):
    panel.setattr(api, "get_admin_subscriptions", _always(["drowsiness"]))
    rows = _payload(await api.settings(_Request()))["notifications"]

    by_type = {r["type"]: r for r in rows}
    assert by_type["drowsy_driving"]["on"] is True
    assert by_type["speeding"]["on"] is False
    # Crash is first and carries its own line: it is the one switch that starts on.
    assert rows[0]["type"] == "crash" and rows[0]["note"]


async def test_turning_a_switch_on_writes_all_of_its_types(panel):
    written = []

    async def _set(telegram_id, event_type, on, **kw):
        written.append((telegram_id, event_type, on))

    panel.setattr(api, "set_subscription", _set)
    resp = await api.set_notification(_Request({"type": "drowsy_driving", "on": True}))

    assert resp.status == 200
    assert written == [(ADMIN, "drowsy_driving", True), (ADMIN, "drowsiness", True)]


async def test_a_switch_only_ever_writes_the_callers_own_row(panel):
    """The id in the body is ignored on purpose. Reading it would turn a personal
    preference screen into a way to silence somebody else's crash alerts."""
    written = []

    async def _set(telegram_id, event_type, on, **kw):
        written.append(telegram_id)

    panel.setattr(api, "set_subscription", _set)
    await api.set_notification(
        _Request({"type": "speeding", "on": False, "telegram_id": 999, "admin_id": 999}))

    assert written == [ADMIN]


async def test_an_unknown_event_type_is_refused(panel):
    written = []

    async def _set(*a, **k):
        written.append(a)

    panel.setattr(api, "set_subscription", _set)
    resp = await api.set_notification(_Request({"type": "'; DROP TABLE", "on": True}))

    assert resp.status == 400
    assert written == []


# ── the report ──────────────────────────────────────────────────────────────────

async def test_an_unknown_period_falls_back_rather_than_failing(panel):
    """A read, from a query string, on the screen that is useless while it errors. The
    periods are a closed set, so anything else is simply not one of them."""
    async def _totals(since, until):
        return {"total": 0, "speeding": 0, "crashes": 0, "units": 0}

    async def _rows(since, until):
        return []

    async def _top(since=None, until=None, limit=10):
        return []

    async def _groups():
        return []

    panel.setattr(api, "get_totals", _totals)
    panel.setattr(api, "get_type_counts", _rows)
    panel.setattr(api, "get_daily_counts", _rows)
    panel.setattr(api, "get_top_violators", _top)
    panel.setattr(api, "get_groups_overview", _groups)

    data = _payload(await api.stats(
        _Request(query={"period": "../etc"})))

    assert data["period"]["key"] == "today"


async def test_the_calendar_periods_the_report_file_supports_are_marked(panel):
    """The screen offers five windows and the downloadable file supports two of them.
    Which is which is the server's to say, not something the client should hard-code a
    second time."""
    async def _totals(since, until):
        return {"total": 0, "speeding": 0, "crashes": 0, "units": 0}

    async def _rows(since, until):
        return []

    async def _top(since=None, until=None, limit=10):
        return []

    async def _groups():
        return []

    panel.setattr(api, "get_totals", _totals)
    panel.setattr(api, "get_type_counts", _rows)
    panel.setattr(api, "get_daily_counts", _rows)
    panel.setattr(api, "get_top_violators", _top)
    panel.setattr(api, "get_groups_overview", _groups)

    for period, downloadable in (("today", True), ("yesterday", False), ("7d", False),
                                 ("30d", False), ("last_week", True),
                                 ("last_month", True)):
        data = _payload(await api.stats(_Request(query={"period": period})))
        assert data["period"]["key"] == period
        assert data["period"]["downloadable"] is downloadable, period
        assert data["period"]["range"]


def test_yesterday_is_the_whole_of_the_day_that_just_ended():
    """Midnight to midnight, not "the last 24 hours" -- the second is what a person
    asking about yesterday morning means, and it is also what the date label says."""
    since, until, label = api._period_range("yesterday")

    assert label == "Yesterday"
    assert (since.hour, since.minute, since.second) == (0, 0, 0)
    assert (until.hour, until.minute, until.second) == (0, 0, 0)
    assert until - since == timedelta(days=1)
    assert until.date() == datetime.now(tz=api.ET).date()


def test_a_calendar_window_names_the_last_day_inside_it_not_the_next_one():
    """The windows end at the midnight that begins the next one, so printing that
    midnight said the week of Oct 5 ran to Oct 12."""
    since = datetime(2026, 10, 5, tzinfo=api.ET)
    week = api._range_text("last_week", since, datetime(2026, 10, 12, tzinfo=api.ET))
    day = api._range_text("yesterday", datetime(2026, 10, 16, tzinfo=api.ET),
                          datetime(2026, 10, 17, tzinfo=api.ET))
    rolling = api._range_text("7d", since, datetime(2026, 10, 11, 14, 30, tzinfo=api.ET))

    assert week == "Oct 05 — Oct 11, 2026"
    assert day == "Oct 16, 2026"
    assert rolling == "Oct 05 — Oct 11, 2026"


async def test_the_report_file_goes_to_the_caller_and_nobody_else(panel):
    sent = []

    class _Bot:
        async def send_document(self, chat_id, document, caption=None):
            sent.append((chat_id, document.filename, caption))

    async def _build(period, company, **kw):
        return b"report body"

    panel.setattr(api, "bot", _Bot())
    panel.setattr(api, "build_report_file", _build)
    resp = await api.send_report(
        _Request({"period": "today", "telegram_id": 999}))

    assert resp.status == 200
    assert len(sent) == 1
    chat_id, filename, _ = sent[0]
    assert chat_id == ADMIN
    assert filename.endswith("_today_report.pdf")


async def test_a_bad_period_is_refused_before_anything_is_built(panel):
    built = []

    async def _build(*a, **k):
        built.append(a)
        return b""

    panel.setattr(api, "build_report_file", _build)
    assert (await api.send_report(_Request({"period": "forever"}))).status == 400
    assert built == []


async def test_a_chat_that_cannot_be_reached_is_said_out_loud(panel):
    """Almost always the same cause: the admin has never pressed /start, so the bot has
    no chat to send to. A silent 200 here is a file the person waits for forever."""
    class _Bot:
        async def send_document(self, *a, **k):
            raise RuntimeError("chat not found")

    async def _build(*a, **k):
        return b"report body"

    panel.setattr(api, "bot", _Bot())
    panel.setattr(api, "build_report_file", _build)
    resp = await api.send_report(_Request({"period": "last_week"}))

    assert resp.status == 502
    assert "/start" in _payload(resp)["message"]


# ── the feed's filter chips ─────────────────────────────────────────────────────

async def test_a_feed_chip_matches_every_spelling_behind_its_label(panel):
    """The chips are the catalog's rows, and a row can be several provider spellings.
    Matching the row's first type exactly meant the Camera Obstructed chip could only
    ever find Motive's road-facing events — on a Samsara-only fleet, which stores
    `obstructed_camera`, it returned an empty feed every time."""
    asked = []

    async def _recent(limit=50, before_ts=None, before_id=None, event_types=None,
                      vehicle_number=None):
        asked.append(event_types)
        return []

    panel.setattr(api, "get_recent_events", _recent)

    await api.alerts(_Request(query={"type": "road_facing_cam_obstruction"}))
    assert set(asked[-1]) == {"road_facing_cam_obstruction",
                              "driver_facing_cam_obstruction", "obstructed_camera"}

    await api.alerts(_Request(query={"type": "drowsy_driving"}))
    assert set(asked[-1]) == {"drowsy_driving", "drowsiness"}


async def test_a_type_outside_the_catalog_still_filters_on_itself(panel):
    """Crash is deliberately absent from GROUP_FILTER_TYPES — it is never a per-group
    filter — but it is the one alert anybody opens this feed to find."""
    asked = []

    async def _recent(limit=50, before_ts=None, before_id=None, event_types=None,
                      vehicle_number=None):
        asked.append(event_types)
        return []

    panel.setattr(api, "get_recent_events", _recent)

    await api.alerts(_Request(query={"type": "crash"}))
    assert asked[-1] == ["crash"]

    await api.alerts(_Request(query={}))
    assert asked[-1] is None
