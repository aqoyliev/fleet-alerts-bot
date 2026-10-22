"""Send evidence emails (cell-phone-use clips/photos) to drivers via Resend.

Resend API: POST https://api.resend.com/emails with `Authorization: Bearer <key>` and a
JSON body {from, to: [...], subject, html, text, attachments: [{filename, content}]},
where attachment `content` is base64. The whole request (attachments after base64)
must stay under ~40MB, so callers should cap raw media with pick_attachments.

Everything here is best-effort: send_email never raises, it logs and returns False.
"""
import asyncio
import base64
import html as _html
import logging
import re
from dataclasses import dataclass

import aiohttp

from data import config

logger = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"

_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 2.0          # seconds; attempt n waits base * 2**(n-1)
_MAX_RETRY_AFTER = 60.0      # never sleep longer than this on a Retry-After header
_RESEND_LIMIT_BYTES = 40 * 1024 * 1024
# Uploads can be tens of MB, so allow a generous total.
_timeout = aiohttp.ClientTimeout(total=180)

_EMAIL_RE = re.compile(r"^[^@\s<>,;\"']+@[^@\s<>,;\"']+\.[^@\s<>,;\"'.]+$")
_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_URL_TRAILING = ".,;:!?)]}'\""

_JPEG_MAGIC = b"\xff\xd8\xff"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@dataclass
class EmailAttachment:
    filename: str
    content: bytes
    content_type: str   # e.g. "video/mp4", "image/jpeg"


# Own lazily-created session: webhook_handler is expected to import this module, so
# borrowing its session would be a circular import. Created inside the running loop.
_http_session: aiohttp.ClientSession | None = None


def _get_http_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = aiohttp.ClientSession()
    return _http_session


async def close() -> None:
    """Close the module's HTTP session (call on shutdown)."""
    global _http_session
    if _http_session is not None and not _http_session.closed:
        await _http_session.close()
    _http_session = None


async def _sleep(seconds: float) -> None:
    # Indirection so tests can skip real waits.
    await asyncio.sleep(seconds)


def is_configured() -> bool:
    """True only when both the Resend API key and the sender address are set."""
    return bool(getattr(config, "RESEND_API_KEY", "")) and bool(getattr(config, "EMAIL_FROM", ""))


def _valid_email(addr: str) -> bool:
    return isinstance(addr, str) and bool(_EMAIL_RE.match(addr.strip()))


def _mask(addr: str) -> str:
    """Recipient for logs: keep the domain, hide most of the local part."""
    local, _, domain = addr.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def _retry_after(headers) -> float | None:
    try:
        value = headers.get("Retry-After") if headers is not None else None
        if value is None:
            return None
        return max(0.0, min(float(value), _MAX_RETRY_AFTER))
    except (TypeError, ValueError):
        return None


async def send_email(to: str, subject: str, html: str, text: str,
                     attachments: list[EmailAttachment] | None = None,
                     idempotency_key: str | None = None) -> bool:
    """Send one email through Resend. True on 2xx, False otherwise; never raises.

    Pass `idempotency_key` (e.g. derived from the event id) so a retry after a 5xx that
    Resend actually accepted doesn't deliver the same evidence email twice."""
    try:
        if not is_configured():
            logger.warning("[email] Resend not configured; skipping send")
            return False
        if not _valid_email(to):
            logger.warning("[email] Invalid recipient address; skipping send")
            return False
        to = to.strip()

        payload = {
            "from": config.EMAIL_FROM,
            "to": [to],
            "subject": subject,
            "html": html,
            "text": text,
        }
        encoded = [
            {
                "filename": a.filename,
                "content": base64.b64encode(a.content).decode("ascii"),
                "content_type": a.content_type,
            }
            for a in (attachments or [])
            if a.content
        ]
        if encoded:
            payload["attachments"] = encoded
            total = sum(len(a["content"]) for a in encoded) + len(html or "") + len(text or "")
            if total > _RESEND_LIMIT_BYTES:
                logger.warning(f"[email] Payload ~{total} bytes exceeds Resend's ~40MB limit; "
                               f"the send will likely be rejected")

        headers = {
            "Authorization": f"Bearer {config.RESEND_API_KEY}",
            "Content-Type": "application/json",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key[:256]
        recipient = _mask(to)

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            delay = _BACKOFF_BASE * (2 ** (attempt - 1))
            try:
                session = _get_http_session()
                async with session.post(RESEND_URL, json=payload, headers=headers,
                                        timeout=_timeout) as resp:
                    status = resp.status
                    if 200 <= status < 300:
                        logger.info(f"[email] Sent to {recipient} "
                                    f"({len(encoded)} attachment(s), attempt {attempt})")
                        return True
                    try:
                        body = (await resp.text())[:300]
                    except Exception:
                        body = ""
                    if status == 429 or status >= 500:
                        ra = _retry_after(getattr(resp, "headers", None))
                        if ra is not None:
                            delay = ra
                        logger.warning(f"[email] Resend {status} for {recipient} "
                                       f"(attempt {attempt}/{_MAX_ATTEMPTS}): {body}")
                    else:
                        logger.error(f"[email] Resend rejected email to {recipient} "
                                     f"with {status}: {body}")
                        return False
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.warning(f"[email] Network error sending to {recipient} "
                               f"(attempt {attempt}/{_MAX_ATTEMPTS}): {type(e).__name__}: {e}")
            if attempt < _MAX_ATTEMPTS:
                await _sleep(delay)

        logger.error(f"[email] Giving up on email to {recipient} after {_MAX_ATTEMPTS} attempts")
        return False
    except Exception as e:
        logger.error(f"[email] Unexpected error sending email: {type(e).__name__}: {e}")
        return False


def _sniff_image(blob: bytes) -> tuple[str, str]:
    """(extension, content_type) for an image blob; defaults to JPEG."""
    if blob.startswith(_PNG_MAGIC):
        return "png", "image/png"
    return "jpg", "image/jpeg"


def pick_attachments(media: list[bytes], is_video: bool,
                     max_total_bytes: int = 20 * 1024 * 1024) -> list[EmailAttachment]:
    """Turn one event's downloaded media into attachments, keeping items in order while
    the running total stays within max_total_bytes. An item that doesn't fit is skipped
    (never truncated); a later, smaller one may still fit. Empty blobs are dropped."""
    picked: list[EmailAttachment] = []
    total = 0
    for blob in media or []:
        if not blob:
            continue
        if total + len(blob) > max_total_bytes:
            logger.info(f"[email] Skipping {len(blob)}-byte attachment: over the "
                        f"{max_total_bytes}-byte cap")
            continue
        n = len(picked) + 1
        if is_video:
            ext, ctype = "mp4", "video/mp4"
        else:
            ext, ctype = _sniff_image(blob)
        picked.append(EmailAttachment(filename=f"cell-phone-{n}.{ext}", content=blob,
                                      content_type=ctype))
        total += len(blob)
    return picked


def html_from_text(text: str) -> str:
    """Plain text -> safe HTML: escape everything, link bare http(s) URLs, and turn
    newlines into <br>."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    parts: list[str] = []
    pos = 0
    for m in _URL_RE.finditer(text):
        url = m.group(0)
        stripped = url.rstrip(_URL_TRAILING)
        # Keep a closing paren that belongs to the URL, e.g. ".../Foo_(bar)".
        while stripped.count("(") > stripped.count(")") and len(stripped) < len(url) \
                and url[len(stripped)] == ")":
            stripped += ")"
        end = m.start() + len(stripped)
        parts.append(_html.escape(text[pos:m.start()]))
        if stripped in ("http://", "https://"):
            parts.append(_html.escape(stripped))
        else:
            href = _html.escape(stripped, quote=True)
            parts.append(f'<a href="{href}">{href}</a>')
        pos = end
    parts.append(_html.escape(text[pos:]))
    return "".join(parts).replace("\n", "<br>\n")
