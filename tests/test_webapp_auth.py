"""The admin panel's front door.

Every other test in this suite guards a behaviour. These guard the one thing that decides
whether a stranger can read the fleet's whole alert history and deactivate its admins, so
they are deliberately paranoid: it is not enough that a good initData passes and a
scribbled one fails, because a verifier that computed the wrong thing consistently would
do both.
"""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from utils.webapp import auth

# Matches the dummy token tests/conftest.py puts in the environment before data.config
# is imported. The signing below has to agree with it or nothing verifies.
BOT_TOKEN = "123:test"


def _sign(fields: dict) -> str:
    """Build a real initData string the way a Telegram client would.

    Written out longhand rather than by calling into utils.webapp.auth, so this is a
    second implementation of the algorithm. A helper that shared code with the verifier
    would agree with it even when both are wrong, which is the failure test_known_vector
    below exists to catch.
    """
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    signed = dict(fields)
    signed["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(signed)


def _init_data(user_id: int = 111, auth_date: float | None = None,
               first_name: str = "Dispatcher", **extra) -> str:
    return _sign({
        "user": json.dumps({"id": user_id, "first_name": first_name}),
        "auth_date": str(int(auth_date if auth_date is not None else time.time())),
        "query_id": "AAAAAAAA",
        **extra,
    })


# ── the algorithm itself ────────────────────────────────────────────────────────

def test_known_vector_pins_the_key_derivation():
    """The secret key is HMAC(key="WebAppData", msg=bot_token) — in that order.

    Swapping the two arguments still produces 32 plausible bytes, still verifies against
    a test helper that made the same swap, and rejects every real Telegram client. Only a
    constant computed outside both implementations can catch that, which is the same
    reason tests/test_motive_hmac.py anchors itself on Motive's published example.
    """
    derived = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).hexdigest()
    assert derived == "b0d44b10eee2e2e64487f0bbc29e44cf963bca3c1517c90a249d29fe53f2a22d"
    # And the verifier accepts data signed with exactly that key.
    assert auth.telegram_id_from(_init_data(user_id=42)) == 42


def test_valid_init_data_yields_the_user_id():
    assert auth.telegram_id_from(_init_data(user_id=99887766)) == 99887766


def test_tampered_field_is_rejected():
    """Changing a value while keeping the original hash must fail — this is what proves
    the check string covers every field, not merely that a hash was present."""
    good = _init_data(user_id=111)
    forged = good.replace("111", "222")
    assert forged != good
    with pytest.raises(auth.AuthError):
        auth.telegram_id_from(forged)


def test_tampered_hash_is_rejected():
    good = _init_data()
    bad = good[:-1] + ("0" if good[-1] != "0" else "1")
    with pytest.raises(auth.AuthError):
        auth.telegram_id_from(bad)


def test_data_signed_with_another_token_is_rejected():
    fields = {"user": json.dumps({"id": 5}), "auth_date": str(int(time.time()))}
    check_string = "\n".join(f"{k}={fields[k]}" for k in sorted(fields))
    wrong_secret = hmac.new(b"WebAppData", b"999:other", hashlib.sha256).digest()
    fields["hash"] = hmac.new(wrong_secret, check_string.encode(), hashlib.sha256).hexdigest()
    with pytest.raises(auth.AuthError):
        auth.telegram_id_from(urlencode(fields))


def test_signature_field_still_verifies():
    """Telegram clients from Bot API 7.10 send an extra `signature` field, and it belongs
    in the data-check-string. Excluding it — as Telegram's separate Ed25519 flow does —
    would 401 every modern client while older ones kept working, which is a miserable bug
    to find in the field. This pins the field in."""
    assert auth.telegram_id_from(_init_data(signature="abc123def")) == 111


def test_non_ascii_name_verifies():
    """Guards the UTF-8 encoding of the check string; a Cyrillic first name is ordinary
    for this fleet's dispatchers."""
    assert auth.telegram_id_from(_init_data(first_name="Иван")) == 111


# ── freshness ───────────────────────────────────────────────────────────────────

def test_stale_init_data_is_rejected_despite_a_valid_signature():
    old = time.time() - (25 * 60 * 60)
    with pytest.raises(auth.AuthError):
        auth.telegram_id_from(_init_data(auth_date=old))


def test_init_data_just_inside_the_window_is_accepted():
    recent = time.time() - (23 * 60 * 60)
    assert auth.telegram_id_from(_init_data(auth_date=recent)) == 111


def test_future_dated_init_data_is_rejected():
    with pytest.raises(auth.AuthError):
        auth.telegram_id_from(_init_data(auth_date=time.time() + 3600))


def test_small_clock_skew_is_tolerated():
    assert auth.telegram_id_from(_init_data(auth_date=time.time() + 60)) == 111


# ── malformed input ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["", "not-a-query-string", "user=x&auth_date=1"])
def test_malformed_init_data_raises_autherror_not_something_else(raw):
    """Every rejection path must land on AuthError. Anything else becomes a 500, which
    tells a prober more than a 401 does and pages someone at 3am."""
    with pytest.raises(auth.AuthError):
        auth.telegram_id_from(raw)


def test_correctly_signed_data_without_a_user_is_rejected():
    """Signed by Telegram, but from a launch context that carries no user (inline mode).
    An anonymous caller is not someone we can authorize, so it is not a caller we admit."""
    with pytest.raises(auth.AuthError):
        auth.telegram_id_from(_sign({"auth_date": str(int(time.time())), "query_id": "AAA"}))


# ── the route decorators ────────────────────────────────────────────────────────

class _Request:
    """Duck-typed aiohttp request: the decorators only ever read these three things."""

    def __init__(self, init_data: str | None, method: str = "GET", path: str = "/api/x"):
        self.headers = {"X-Telegram-Init-Data": init_data} if init_data is not None else {}
        self.method = method
        self.path = path
        self.remote = "1.2.3.4"
        self._store: dict = {}

    def __setitem__(self, key, value):
        self._store[key] = value

    def __getitem__(self, key):
        return self._store[key]


async def _handler(request):
    from aiohttp import web
    return web.json_response({"seen": request["telegram_id"]})


async def test_missing_header_is_401(monkeypatch):
    monkeypatch.setattr(auth, "is_admin", _always(True))
    resp = await auth.require_admin(_handler)(_Request(None))
    assert resp.status == 401


async def test_valid_signature_from_a_non_admin_is_403(monkeypatch):
    monkeypatch.setattr(auth, "is_admin", _always(False))
    resp = await auth.require_admin(_handler)(_Request(_init_data()))
    assert resp.status == 403


async def test_valid_signature_from_an_admin_reaches_the_handler(monkeypatch):
    monkeypatch.setattr(auth, "is_admin", _always(True))
    resp = await auth.require_admin(_handler)(_Request(_init_data(user_id=777)))
    assert resp.status == 200
    assert json.loads(resp.body)["seen"] == 777


async def test_plain_admin_is_refused_by_require_super(monkeypatch):
    monkeypatch.setattr(auth, "is_super_admin", _always(False))
    resp = await auth.require_super(_handler)(_Request(_init_data()))
    assert resp.status == 403


async def test_forged_init_data_never_reaches_the_authorization_check(monkeypatch):
    """A 401 must be decided before is_admin runs. If a forged string could still trigger
    the DB lookup, the panel would answer measurably faster for real admin ids than for
    invented ones."""
    called = []

    async def _spy(telegram_id):
        called.append(telegram_id)
        return True

    monkeypatch.setattr(auth, "is_admin", _spy)
    resp = await auth.require_admin(_handler)(_Request("user=x&hash=deadbeef"))
    assert resp.status == 401
    assert called == []


def _always(value: bool):
    async def _check(_telegram_id):
        return value
    return _check
