"""Setting a group's unit from the panel tells the group — and nobody else.

The in-chat /setunit announces itself twice: a reply in the group and a DM to every
admin. The panel's version deliberately keeps only the first half. The admin who tapped
the truck is looking at the result of their own tap, so a DM about it is noise; the
drivers were told on the day the bot joined that nothing would arrive until somebody set
a unit, and without this they are never told that somebody has.

The other property under test is that the message cannot cost the edit. The unit is in
the table before the send is attempted, so a group that has restricted the bot must not
turn a stored change into an error the admin would retry.
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
    def __init__(self, body=None, match=None, user_id=111):
        self.headers = {"X-Telegram-Init-Data": _init_data(user_id)}
        self.match_info = match or {"tgid": str(GROUP_ID)}
        self.query = {}
        self.method = "POST"
        self.path = "/panel/api/groups/x/unit"
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


def _resolver(result):
    async def _resolve(unit):
        return result if isinstance(result, tuple) else (result, unit)
    return _resolve


class _Recorder:
    """Stands in for the bot. Records every chat written to, so a test can assert that
    an admin's DM was *not* one of them."""

    def __init__(self, fail=False):
        self.sent = []
        self._fail = fail

    async def send_message(self, chat_id, text, **_kw):
        if self._fail:
            raise RuntimeError("Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text))


@pytest.fixture
def panel(monkeypatch):
    """Admit the caller, stub the table, record the writes and the messages."""
    writes = {"unit": []}
    state = {"group": {"id": 1, "telegram_group_id": GROUP_ID, "title": "ALI / ABDI",
                       "vehicle_number": None, "enabled": True, "left_at": None}}

    monkeypatch.setattr(auth, "is_admin", _always(True))
    monkeypatch.setattr(auth, "is_super_admin", _always(True))
    monkeypatch.setattr(config, "MAIN_GROUP_ID", -100999)
    monkeypatch.setattr(config, "CRASH_GROUP_ID", -100888)
    monkeypatch.setattr(api, "resolve_unit", _resolver("ok"))

    async def _get_group(_tgid):
        return dict(state["group"])

    async def _set_unit(tgid, unit):
        writes["unit"].append((tgid, unit))

    monkeypatch.setattr(api, "get_group", _get_group)
    monkeypatch.setattr(api, "set_group_unit", _set_unit)

    recorder = _Recorder()
    monkeypatch.setattr(api, "bot", recorder)
    return {"writes": writes, "state": state, "bot": recorder}


# ── what the group is told ──────────────────────────────────────────────────────

async def test_a_group_given_its_first_unit_is_told_it_is_connected(panel):
    resp = await api.set_unit(_Request({"unit": "unit571"}))

    assert resp.status == 200
    chat_id, text = panel["bot"].sent[0]
    assert chat_id == GROUP_ID
    assert "Connected" in text and "unit571" in text
    # The whole point of the message: the chat learns alerts are coming.
    assert "alerts here as they happen" in text


async def test_only_the_group_hears_about_it(panel):
    """The /setunit path DMs every admin as well. This one must not — see the module
    docstring. Asserted as "exactly one chat", so an admin DM added later fails here."""
    await api.set_unit(_Request({"unit": "unit571"}))

    assert [chat for chat, _ in panel["bot"].sent] == [GROUP_ID]


async def test_repointing_a_group_names_both_trucks(panel):
    """"Connected — unit 9" in a chat that has been carrying unit 4's alerts all month
    reads as a bug. The message says what it is replacing."""
    panel["state"]["group"]["vehicle_number"] = "unit571"

    await api.set_unit(_Request({"unit": "unit604"}))

    _, text = panel["bot"].sent[0]
    assert "unit604" in text and "unit571" in text
    assert "instead" in text


async def test_a_muted_group_is_not_promised_alerts(panel):
    """set_group_unit leaves the mute alone, so "I'll post alerts here" would be false.
    /enable is advertised nowhere else except the reply to /disable."""
    panel["state"]["group"]["enabled"] = False

    await api.set_unit(_Request({"unit": "unit571"}))

    _, text = panel["bot"].sent[0]
    assert "muted" in text and "/enable" in text


async def test_setting_the_same_unit_again_says_nothing(panel):
    """A no-op edit — reselecting the truck already shown — must not post to the chat."""
    panel["state"]["group"]["vehicle_number"] = "unit571"

    resp = await api.set_unit(_Request({"unit": "unit571"}))

    assert resp.status == 200
    assert panel["bot"].sent == []


# ── what the message may not cost ───────────────────────────────────────────────

async def test_a_group_that_refuses_the_message_still_gets_its_unit(panel, monkeypatch):
    """The unit is stored before the send is attempted. A bot that has been restricted
    in the chat must not turn a successful edit into an error the admin would retry."""
    monkeypatch.setattr(api, "bot", _Recorder(fail=True))

    resp = await api.set_unit(_Request({"unit": "unit571"}))

    assert resp.status == 200
    assert panel["writes"]["unit"] == [(GROUP_ID, "unit571")]


@pytest.mark.parametrize("verdict,status", [("missing", 409), ("unavailable", 503)])
async def test_a_refused_unit_is_not_announced(panel, monkeypatch, verdict, status):
    """Nothing was written, so there is nothing to tell the group — and a chat told it
    now follows a truck it does not follow is worse than silence."""
    monkeypatch.setattr(api, "resolve_unit", _resolver(verdict))

    async def _suggest(*_a, **_k):
        return []
    monkeypatch.setattr(api, "suggest_units", _suggest)

    resp = await api.set_unit(_Request({"unit": "007"}))

    assert resp.status == status
    assert panel["writes"]["unit"] == []
    assert panel["bot"].sent == []
