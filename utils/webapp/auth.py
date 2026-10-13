"""Authentication for the admin Mini App.

A Telegram Mini App is handed a signed credential on launch — `initData`, a URL-encoded
query string whose `hash` field only someone holding the bot token could have produced.
Verifying it identifies the caller with no login, no password, and no session table.

The frontend sends that string back on every request in `X-Telegram-Init-Data`, and every
request re-verifies it. A cookie session was the obvious alternative and was rejected:
initData is *already* a self-contained signed credential, so a cookie would trade one
HMAC (microseconds) for server-side state, a CSRF surface, and SameSite behaviour inside
Telegram's in-app webview that we would have to reason about. Stateless is less to break.

Note this is NOT the same algorithm as `_verify_hmac` in utils/webhook_handler.py. That
one signs a raw request body with a shared provider secret. Telegram signs a sorted
key=value check-string with a key that is itself derived from the bot token, so the two
cannot share code even though both end in a constant-time compare.
"""

import hashlib
import hmac
import json
import logging
import time
from functools import wraps
from urllib.parse import parse_qsl

from aiohttp import web

from data import config
from utils.db_api.admins import is_admin, is_super_admin

logger = logging.getLogger(__name__)

# How old an initData string may be and still be accepted.
#
# Telegram issues it once when the panel opens and never refreshes it, so this is really
# "how long may a panel stay open before it has to be reopened". A short window would log
# a dispatcher out mid-task for no security gain against the realistic threat (a leaked
# string, not a live attacker); a much longer one leaves a stolen string useful for days.
# One day is the balance, and reopening the panel is a two-tap recovery.
_MAX_AUTH_AGE = 24 * 60 * 60


class AuthError(Exception):
    """initData was absent, malformed, forged, or stale. Always a 401 — never a 403,
    because we never established who the caller is."""


def _check_signature(init_data: str) -> dict:
    """Verify Telegram's initData signature and return its fields.

    Raises AuthError if the string is missing its hash, fails the HMAC, or is older than
    _MAX_AUTH_AGE. Returns the parsed key/value pairs on success (with `hash` removed).
    """
    if not init_data:
        raise AuthError("no initData supplied")

    # strict_parsing rejects a malformed string outright instead of silently yielding a
    # subset of its fields; keep_blank_values keeps a legitimately empty one (Telegram
    # does send those) from being dropped, which would change the check string and fail
    # every request from an affected client.
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True, keep_blank_values=True))
    except ValueError as e:
        raise AuthError(f"malformed initData: {e}")

    provided = pairs.pop("hash", "")
    if not provided:
        raise AuthError("initData carries no hash")

    # Every remaining field participates, sorted by key. That is what makes tampering with
    # any single value — not just adding or removing the hash — fail the check.
    #
    # `hash` is the ONLY field removed. Telegram clients from Bot API 7.10 also send a
    # `signature` field, and it is part of this check string. Dropping it too is a real
    # and easy mistake — Telegram's *other* validation flow, the Ed25519 one for
    # third parties verifying without the bot token, does exclude both — and the symptom
    # is that every modern client 401s while older ones keep working.
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret_key = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(expected, provided):
        raise AuthError("initData signature mismatch")

    # Checked only after the signature, so a forged auth_date can't be used to probe
    # anything: by this point the whole string is known to be Telegram's.
    try:
        auth_age = time.time() - int(pairs.get("auth_date", "0"))
    except (TypeError, ValueError):
        raise AuthError("initData has an unreadable auth_date")
    if auth_age > _MAX_AUTH_AGE:
        raise AuthError(f"initData is stale ({int(auth_age)}s old)")
    # A future auth_date means our clock and Telegram's disagree, and a large enough skew
    # would make _MAX_AUTH_AGE unenforceable. Five minutes absorbs ordinary drift.
    if auth_age < -300:
        raise AuthError("initData is dated in the future")

    return pairs


def telegram_id_from(init_data: str) -> int:
    """The verified Telegram user id behind an initData string.

    Raises AuthError for anything that fails verification or carries no user — the latter
    happens for launch contexts we don't serve (inline mode has no `user` field), and an
    anonymous caller is not someone we can authorize.
    """
    pairs = _check_signature(init_data)
    try:
        user = json.loads(pairs.get("user", ""))
        return int(user["id"])
    except (ValueError, KeyError, TypeError):
        raise AuthError("initData carries no usable user")


def _requires(check):
    """Build a route decorator that admits only callers passing `check(request, id)`.

    On success the verified id is stashed at request["telegram_id"] so handlers never
    re-parse the header, and so a mutation can log who performed it.
    """
    def decorator(handler):
        @wraps(handler)
        async def wrapper(request: web.Request) -> web.Response:
            try:
                telegram_id = telegram_id_from(request.headers.get("X-Telegram-Init-Data", ""))
            except AuthError as e:
                logger.warning(f"[webapp] rejected request from {request.remote}: {e}")
                return web.json_response({"error": "unauthorized"}, status=401)

            if not await check(request, telegram_id):
                # 403, not 401: we know exactly who this is, they just aren't allowed.
                logger.warning(f"[webapp] {telegram_id} denied {request.method} {request.path}")
                return web.json_response({"error": "forbidden"}, status=403)

            request["telegram_id"] = telegram_id
            return await handler(request)
        return wrapper
    return decorator


# The checks are wrapped in lambdas rather than passed by reference on purpose: a direct
# reference is bound once at import and would keep pointing at the original function even
# after the module attribute is replaced. Deferring the lookup to call time is what lets
# a test swap in a fake — and what makes the authorization rule come from one place at
# the moment it is asked, rather than from a snapshot taken when this module loaded.

# Any active admin — or a maintainer, who passes by config and needs no `admins` row.
require_admin = _requires(lambda _request, telegram_id: is_admin(telegram_id))
# Destructive mutations only. Same maintainer shortcut applies.
require_super = _requires(lambda _request, telegram_id: is_super_admin(telegram_id))
