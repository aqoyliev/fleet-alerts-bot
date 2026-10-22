import base64
import logging

import aiohttp
import pytest

from utils import email_sender as es

_KEY = "re_TEST_SECRET_KEY_123"


class _FakeResp:
    def __init__(self, status, body="", headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def text(self):
        return self._body


class _FakeSession:
    """Returns scripted responses (or raises scripted exceptions) in order; repeats the
    last once exhausted. Records every post call."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        idx = min(len(self.calls), len(self._responses) - 1)
        self.calls.append({"url": url, **kwargs})
        r = self._responses[idx]
        if isinstance(r, BaseException):
            raise r
        return r


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(es.config, "RESEND_API_KEY", _KEY, raising=False)
    monkeypatch.setattr(es.config, "EMAIL_FROM", "Alerts <alerts@example.com>", raising=False)
    sleeps = []

    async def _no_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(es, "_sleep", _no_sleep)
    state = {"sleeps": sleeps}

    def _install(responses):
        session = _FakeSession(responses)
        monkeypatch.setattr(es, "_get_http_session", lambda: session)
        state["session"] = session
        return session

    state["install"] = _install
    return state


async def test_success(configured):
    session = configured["install"]([_FakeResp(200, '{"id":"x"}')])
    ok = await es.send_email("driver@example.com", "Subj", "<p>hi</p>", "hi")
    assert ok is True
    assert len(session.calls) == 1
    assert session.calls[0]["url"] == "https://api.resend.com/emails"


async def test_not_configured_makes_no_call(monkeypatch):
    monkeypatch.setattr(es.config, "RESEND_API_KEY", "", raising=False)
    monkeypatch.setattr(es.config, "EMAIL_FROM", "alerts@example.com", raising=False)
    session = _FakeSession([_FakeResp(200)])
    monkeypatch.setattr(es, "_get_http_session", lambda: session)
    assert es.is_configured() is False
    assert await es.send_email("driver@example.com", "s", "h", "t") is False
    assert session.calls == []


def test_is_configured_requires_both(monkeypatch):
    monkeypatch.setattr(es.config, "RESEND_API_KEY", "k", raising=False)
    monkeypatch.setattr(es.config, "EMAIL_FROM", "", raising=False)
    assert es.is_configured() is False
    monkeypatch.setattr(es.config, "EMAIL_FROM", "a@b.com", raising=False)
    assert es.is_configured() is True


@pytest.mark.parametrize("to", ["", "not-an-email", "a@b", "a b@c.com", "a@b.com,c@d.com", None])
async def test_invalid_recipient(configured, to):
    session = configured["install"]([_FakeResp(200)])
    assert await es.send_email(to, "s", "h", "t") is False
    assert session.calls == []


async def test_429_then_200_retries_and_honours_retry_after(configured):
    session = configured["install"]([
        _FakeResp(429, "rate limited", headers={"Retry-After": "7"}),
        _FakeResp(200),
    ])
    assert await es.send_email("driver@example.com", "s", "h", "t") is True
    assert len(session.calls) == 2
    assert configured["sleeps"] == [7.0]


async def test_500_three_times_gives_up(configured):
    session = configured["install"]([_FakeResp(500, "boom")])
    assert await es.send_email("driver@example.com", "s", "h", "t") is False
    assert len(session.calls) == 3
    assert len(configured["sleeps"]) == 2


async def test_400_does_not_retry(configured):
    session = configured["install"]([_FakeResp(400, "bad"), _FakeResp(200)])
    assert await es.send_email("driver@example.com", "s", "h", "t") is False
    assert len(session.calls) == 1
    assert configured["sleeps"] == []


async def test_network_error_returns_false_without_raising(configured):
    session = configured["install"]([aiohttp.ClientConnectionError("down")])
    assert await es.send_email("driver@example.com", "s", "h", "t") is False
    assert len(session.calls) == 3


async def test_network_error_then_success(configured):
    session = configured["install"]([aiohttp.ClientConnectionError("down"), _FakeResp(202)])
    assert await es.send_email("driver@example.com", "s", "h", "t") is True
    assert len(session.calls) == 2


async def test_unexpected_exception_is_swallowed(configured):
    configured["install"]([RuntimeError("weird")])
    assert await es.send_email("driver@example.com", "s", "h", "t") is False


async def test_payload_shape(configured):
    session = configured["install"]([_FakeResp(200)])
    atts = [es.EmailAttachment("cell-phone-1.mp4", b"\x00\x01video", "video/mp4"),
            es.EmailAttachment("empty.mp4", b"", "video/mp4")]
    assert await es.send_email(" driver@example.com ", "Phone use", "<b>x</b>", "x", atts)
    payload = session.calls[0]["json"]
    assert payload["from"] == "Alerts <alerts@example.com>"
    assert payload["to"] == ["driver@example.com"]
    assert payload["subject"] == "Phone use"
    assert payload["html"] == "<b>x</b>"
    assert payload["text"] == "x"
    assert len(payload["attachments"]) == 1
    a = payload["attachments"][0]
    assert a["filename"] == "cell-phone-1.mp4"
    assert base64.b64decode(a["content"]) == b"\x00\x01video"


async def test_no_attachments_key_when_none(configured):
    session = configured["install"]([_FakeResp(200)])
    assert await es.send_email("driver@example.com", "s", "h", "t")
    assert "attachments" not in session.calls[0]["json"]


async def test_api_key_only_in_auth_header_and_not_logged(configured, caplog):
    caplog.set_level(logging.DEBUG)
    session = configured["install"]([_FakeResp(500, "err"), _FakeResp(400, "bad")])
    await es.send_email("driver@example.com", "s", "secret body html", "secret body text")
    call = session.calls[0]
    assert call["headers"]["Authorization"] == f"Bearer {_KEY}"
    assert _KEY not in str(call["json"])
    assert _KEY not in caplog.text
    assert "secret body" not in caplog.text
    assert caplog.records  # something was logged


# --- pick_attachments -------------------------------------------------------------

def test_pick_videos_naming_and_order():
    out = es.pick_attachments([b"a" * 10, b"", b"b" * 5], is_video=True)
    assert [a.filename for a in out] == ["cell-phone-1.mp4", "cell-phone-2.mp4"]
    assert [a.content for a in out] == [b"a" * 10, b"b" * 5]
    assert all(a.content_type == "video/mp4" for a in out)


def test_pick_size_cap_skips_not_truncates():
    media = [b"1" * 6, b"2" * 6, b"3" * 4, b"4" * 1]
    out = es.pick_attachments(media, is_video=True, max_total_bytes=10)
    assert [a.content for a in out] == [b"1" * 6, b"3" * 4]
    assert sum(len(a.content) for a in out) <= 10


def test_pick_item_larger_than_cap_dropped():
    assert es.pick_attachments([b"x" * 11], is_video=True, max_total_bytes=10) == []


def test_pick_image_sniffing():
    jpeg = b"\xff\xd8\xff\xe0" + b"j" * 10
    png = b"\x89PNG\r\n\x1a\n" + b"p" * 10
    unknown = b"????"
    out = es.pick_attachments([jpeg, png, unknown], is_video=False)
    assert [(a.filename, a.content_type) for a in out] == [
        ("cell-phone-1.jpg", "image/jpeg"),
        ("cell-phone-2.png", "image/png"),
        ("cell-phone-3.jpg", "image/jpeg"),
    ]


def test_pick_empty_inputs():
    assert es.pick_attachments([], is_video=False) == []
    assert es.pick_attachments(None, is_video=True) == []


# --- html_from_text ---------------------------------------------------------------

def test_html_escapes_script():
    out = es.html_from_text("<script>alert('x')</script> & co")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&amp; co" in out


def test_html_newlines_and_links():
    out = es.html_from_text("Hi driver,\r\nSee https://example.com/v?a=1&b=2.\nThanks")
    assert out.count("<br>") == 2
    assert '<a href="https://example.com/v?a=1&amp;b=2">https://example.com/v?a=1&amp;b=2</a>.' in out
    assert out.endswith("Thanks")


def test_html_link_cannot_break_out_of_attribute():
    out = es.html_from_text('http://evil.com/"onmouseover="x <b>')
    assert 'href="http://evil.com/"' in out
    assert "onmouseover=\"x" not in out
    assert "<b>" not in out


def test_html_paren_url_and_empty():
    out = es.html_from_text("(see http://a.com/x_(y))")
    assert '<a href="http://a.com/x_(y)">' in out
    assert out.endswith("</a>)")
    assert es.html_from_text("") == ""


async def test_idempotency_key_header_sent(monkeypatch):
    sent = {}

    class _Resp:
        status = 200
        headers = {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def text(self):
            return ""

    class _Session:
        closed = False

        def post(self, url, json=None, headers=None, timeout=None):
            sent["headers"] = headers
            return _Resp()

    monkeypatch.setattr(es.config, "RESEND_API_KEY", "re_test", raising=False)
    monkeypatch.setattr(es.config, "EMAIL_FROM", "alerts@example.com", raising=False)
    monkeypatch.setattr(es, "_get_http_session", lambda: _Session())
    assert await es.send_email("driver@example.com", "s", "<p>h</p>", "h",
                               idempotency_key="cell-phone-123")
    assert sent["headers"]["Idempotency-Key"] == "cell-phone-123"
