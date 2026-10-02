"""Listing a group the bot is already in, from its chat id.

Telegram gives a bot no way to ask which chats it is in, so a group it joined while an
older build was running is recorded nowhere and cannot appear in the panel that exists to
configure it. The endpoint under test takes the chat id from the DM the admins were sent
at the time and turns it back into a row.

The property worth protecting is the one that makes a hand-typed number safe to accept:
the bot's own membership is the only evidence the endpoint trusts. Every test below that
refuses an id therefore asserts the *absence of a write* rather than a status code — an
id that reached the table without the bot being in that chat would put a group in the
panel that nobody could ever remove by leaving it.
"""

import hashlib
import hmac
import json
import time
from types import SimpleNamespace
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
    """Duck-typed aiohttp request — the handler touches exactly these members."""

    def __init__(self, body=None, user_id=111):
        self.headers = {"X-Telegram-Init-Data": _init_data(user_id)}
        self.match_info = {}
        self.query = {}
        self.method = "POST"
        self.path = "/panel/api/groups"
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


class _FakeBot:
    """The three Telegram calls the handler makes, and nothing else.

    `chat=None` stands for the usual refusal: Telegram answers "chat not found" for an id
    the bot has no access to, which is also its answer for an id someone mistyped.
    """

    def __init__(self, chat=None, status="member"):
        self._chat = chat
        self._status = status
        self.asked = []

    @property
    def me(self):
        async def _me():
            return SimpleNamespace(id=999)
        return _me()

    async def get_chat(self, chat_id):
        self.asked.append(chat_id)
        if self._chat is None:
            raise RuntimeError("Bad Request: chat not found")
        return self._chat

    async def get_chat_member(self, chat_id, user_id):
        assert user_id == 999, "membership must be checked for the bot itself"
        return SimpleNamespace(status=self._status)


def _chat(chat_id=GROUP_ID, title="Ruhoallah Assadi #303", kind="supergroup"):
    return SimpleNamespace(id=chat_id, title=title, type=kind)


@pytest.fixture
def panel(monkeypatch):
    """Admit the caller, stub the table, and record every write."""
    writes = {"registered": []}

    monkeypatch.setattr(auth, "is_admin", _always(True))
    monkeypatch.setattr(auth, "is_super_admin", _always(True))
    monkeypatch.setattr(config, "MAIN_GROUP_ID", -100999)
    monkeypatch.setattr(config, "CRASH_GROUP_ID", -100888)

    async def _get_group(_tgid):
        return None

    async def _register(tgid, title):
        writes["registered"].append((tgid, title))

    monkeypatch.setattr(api, "get_group", _get_group)
    monkeypatch.setattr(api, "register_unassigned_group", _register)
    monkeypatch.setattr(api, "bot", _FakeBot(_chat()))
    return writes


# ── the happy path ──────────────────────────────────────────────────────────────

async def test_a_group_the_bot_is_in_is_listed_with_no_unit(panel):
    resp = await api.attach_group(_Request({"telegram_group_id": GROUP_ID}))

    assert resp.status == 200
    assert _body(resp) == {"ok": True, "already": False,
                           "telegram_group_id": GROUP_ID,
                           "title": "Ruhoallah Assadi #303"}
    # No unit, ever: the next screen has the roster picker on it, and a unit guessed from
    # the title here is something the admin would have to notice and undo there.
    assert panel["registered"] == [(GROUP_ID, "Ruhoallah Assadi #303")]


async def test_the_title_comes_from_telegram_not_from_the_browser(panel, monkeypatch):
    """The client sends an id and nothing else. A title it could send would be a string
    the panel then shows as the name of a chat it has never looked at."""
    monkeypatch.setattr(api, "bot", _FakeBot(_chat(title="UNIT 1234 / ALI")))

    await api.attach_group(_Request({"telegram_group_id": GROUP_ID,
                                     "title": "<b>anything</b>"}))

    assert panel["registered"] == [(GROUP_ID, "UNIT 1234 / ALI")]


async def test_an_id_arriving_as_a_string_is_accepted(panel):
    """The input is a text field, so the value is "-1001234567890", not a number. Group
    ids are past 2^53 often enough that the client must be free to send them as text."""
    resp = await api.attach_group(_Request({"telegram_group_id": str(GROUP_ID)}))

    assert resp.status == 200
    assert panel["registered"] == [(GROUP_ID, "Ruhoallah Assadi #303")]


async def test_a_group_the_bot_was_removed_from_is_listed_again(panel, monkeypatch):
    """left_at is set when the bot is kicked. Being back in the chat is the whole claim
    this endpoint verifies, so a row marked left is exactly what it should revive — and
    register_unassigned_group leaves the unit alone, so it comes back configured."""
    async def _left(_tgid):
        return {"id": 7, "telegram_group_id": GROUP_ID, "title": "old name",
                "vehicle_number": "unit571", "enabled": False, "left_at": "2026-10-01"}
    monkeypatch.setattr(api, "get_group", _left)

    resp = await api.attach_group(_Request({"telegram_group_id": GROUP_ID}))

    assert resp.status == 200
    assert _body(resp)["already"] is False
    assert panel["registered"] == [(GROUP_ID, "Ruhoallah Assadi #303")]


# ── refusals ────────────────────────────────────────────────────────────────────

async def test_a_chat_the_bot_cannot_open_is_refused(panel, monkeypatch):
    monkeypatch.setattr(api, "bot", _FakeBot(None))

    resp = await api.attach_group(_Request({"telegram_group_id": GROUP_ID}))

    assert resp.status == 404
    assert _body(resp)["error"] == "chat_unreachable"
    assert panel["registered"] == []


async def test_a_chat_the_bot_has_been_kicked_from_is_refused(panel, monkeypatch):
    """getChat can still answer for a chat the bot was thrown out of. Membership is the
    evidence, so it is checked rather than inferred from getChat having worked."""
    monkeypatch.setattr(api, "bot", _FakeBot(_chat(), status="kicked"))

    resp = await api.attach_group(_Request({"telegram_group_id": GROUP_ID}))

    assert resp.status == 404
    assert _body(resp)["error"] == "not_a_member"
    assert panel["registered"] == []


async def test_a_private_chat_is_refused(panel, monkeypatch):
    """A person's id instead of a group's. Registering it would address a truck's alerts
    to one human being's DMs."""
    monkeypatch.setattr(api, "bot", _FakeBot(_chat(chat_id=111, title=None, kind="private")))

    resp = await api.attach_group(_Request({"telegram_group_id": 111}))

    assert resp.status == 400
    assert _body(resp)["error"] == "not_a_group"
    assert panel["registered"] == []


@pytest.mark.parametrize("value", ["", "   ", "abc", "-100abc", None, "-100.5"])
async def test_a_value_that_is_not_an_id_is_refused(panel, value):
    resp = await api.attach_group(_Request({"telegram_group_id": value}))

    assert resp.status == 400
    assert _body(resp)["error"] == "bad_request"
    assert panel["registered"] == []


@pytest.mark.parametrize("tgid", [-100999, -100888])
async def test_the_configured_chats_are_refused(panel, tgid):
    """The main group is recreated from .env on every boot and the crash group is
    deliberately not in this table at all — neither is the panel's to add."""
    resp = await api.attach_group(_Request({"telegram_group_id": tgid}))

    assert resp.status == 403
    assert _body(resp)["error"] == "reserved_group"
    assert panel["registered"] == []


async def test_a_group_already_listed_is_not_written_again(panel, monkeypatch):
    """Re-registering clears a mute. An admin who mistypes an id onto a working group
    must not silently turn its alerts back on."""
    async def _listed(_tgid):
        return {"id": 7, "telegram_group_id": GROUP_ID, "title": "G8PZ-7X5-FF2 / ALI",
                "vehicle_number": "G8PZ-7X5-FF2", "enabled": False, "left_at": None}
    monkeypatch.setattr(api, "get_group", _listed)

    resp = await api.attach_group(_Request({"telegram_group_id": GROUP_ID}))

    assert resp.status == 200
    assert _body(resp) == {"ok": True, "already": True,
                           "telegram_group_id": GROUP_ID,
                           "title": "G8PZ-7X5-FF2 / ALI"}
    assert panel["registered"] == []


async def test_a_non_admin_cannot_attach_a_group(panel, monkeypatch):
    monkeypatch.setattr(auth, "is_admin", _always(False))

    resp = await api.attach_group(_Request({"telegram_group_id": GROUP_ID}))

    assert resp.status == 403
    assert panel["registered"] == []
