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
    """A read, from a query string, on a screen that is useless while it errors. The
    three periods are a closed set, so anything else is simply not one of them."""
    asked = []

    async def _top(period, scope, limit=10):
        asked.append((period, scope))
        return []

    panel.setattr(api, "top_units", _top)
    data = _payload(await api.report(_Request(query={"period": "../etc", "type": "x"})))

    assert asked == [("last_week", "")]
    assert data["period"]["key"] == "last_week"


async def test_the_report_file_goes_to_the_caller_and_nobody_else(panel):
    sent = []

    class _Bot:
        async def send_document(self, chat_id, document, caption=None):
            sent.append((chat_id, document.filename, caption))

    async def _build(period, scope, company, **kw):
        return b"report body"

    panel.setattr(api, "bot", _Bot())
    panel.setattr(api, "build_report_file", _build)
    resp = await api.send_report(
        _Request({"period": "today", "type": "speeding", "telegram_id": 999}))

    assert resp.status == 200
    assert len(sent) == 1
    chat_id, filename, _ = sent[0]
    assert chat_id == ADMIN
    assert filename.endswith("_today_report.txt")


async def test_a_bad_period_is_refused_before_anything_is_built(panel):
    built = []

    async def _build(*a, **k):
        built.append(a)
        return b""

    panel.setattr(api, "build_report_file", _build)
    for body in ({"period": "forever"}, {"period": "today", "type": "everything"}):
        assert (await api.send_report(_Request(body))).status == 400
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
