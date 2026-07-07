"""Tests for Motive webhook HMAC verification (utils/webhook_handler._verify_hmac).

Motive (née KeepTruckin) signs each webhook with HMAC-SHA1 hex over the raw body in
X-KT-Webhook-Signature. The Samsara path (SHA256) is covered elsewhere; these tests
guard the SHA1 path against silent regressions that would 403 every live alert.
"""
import hashlib

from utils.webhook_handler import _verify_hmac


# Motive's own documented example: payload + shared secret → expected signature.
# The 40-char hex confirms SHA1, not SHA256 — keep this as a regression anchor.
_MOTIVE_SECRET = "d9d36bea41c44ae49d1bfc4a48ba2abe"
_MOTIVE_BODY = b'["fault_code_closed"]'
_MOTIVE_SIG = "f04a8386a21a6cba0447024e83b3f0983352bb72"


def test_motive_documented_example_verifies_with_sha1():
    assert _verify_hmac(_MOTIVE_SECRET, _MOTIVE_BODY, _MOTIVE_SIG, hashlib.sha1) is True


def test_motive_sha256_rejects_motive_signature():
    assert _verify_hmac(_MOTIVE_SECRET, _MOTIVE_BODY, _MOTIVE_SIG, hashlib.sha256) is False


def test_motive_tampered_body_fails():
    assert _verify_hmac(_MOTIVE_SECRET, _MOTIVE_BODY + b"x", _MOTIVE_SIG, hashlib.sha1) is False


def test_motive_empty_signature_fails():
    assert _verify_hmac(_MOTIVE_SECRET, _MOTIVE_BODY, "", hashlib.sha1) is False
