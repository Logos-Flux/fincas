"""Pub/Sub push token verification (golden_erp/addons/gmail/pubsub.py). A key generated per run; no network."""

from __future__ import annotations

import base64
import json
from datetime import datetime
from datetime import timezone as dt_timezone

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.test import SimpleTestCase

from .. import pubsub

NOW = datetime(2026, 3, 2, 15, 0, tzinfo=dt_timezone.utc)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _int(n: int) -> str:
    return _b64(n.to_bytes((n.bit_length() + 7) // 8, "big"))


def jwks(key=KEY, kid="k1"):
    nums = key.public_key().public_numbers()
    return {"keys": [{"kty": "RSA", "kid": kid, "alg": "RS256", "n": _int(nums.n), "e": _int(nums.e)}]}


def token(claims=None, *, key=KEY, kid="k1", alg="RS256"):
    stamp = int(NOW.timestamp())
    body = {"iss": "https://accounts.google.com", "aud": "audience-1", "iat": stamp - 5, "exp": stamp + 3600,
            "email": "push@example.com", "email_verified": True, **(claims or {})}
    head = _b64(json.dumps({"alg": alg, "kid": kid}).encode())
    payload = _b64(json.dumps(body).encode())
    sig = key.sign(f"{head}.{payload}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{payload}.{_b64(sig)}"


def push_body(address="rep@example.org", history="1010"):
    data = base64.b64encode(json.dumps({"emailAddress": address, "historyId": history}).encode()).decode()
    return {"message": {"data": data, "messageId": "m-1"}, "subscription": "projects/p/subscriptions/s"}


class VerifyTests(SimpleTestCase):
    def verify(self, tok, keys=None, **kw):
        kw.setdefault("audience", "audience-1")
        return pubsub.verify(tok, keys or jwks(), now=NOW, **kw)

    def refused(self, reason, tok, keys=None, **kw):
        with self.assertRaises(pubsub.TokenRefused) as ctx:
            self.verify(tok, keys, **kw)
        self.assertEqual(ctx.exception.reason, reason)
        self.assertNotIn(tok, str(ctx.exception))

    def test_a_valid_token(self):
        claims = self.verify(token(), service_account="push@example.com")
        self.assertEqual(claims["aud"], "audience-1")

    def test_each_failed_check(self):
        self.refused("bad_signature", token(key=OTHER_KEY))
        tampered = token().split(".")
        tampered[1] = _b64(json.dumps({"aud": "audience-1"}).encode())
        self.refused("bad_signature", ".".join(tampered))
        self.refused("unknown_key", token(kid="k9"))
        self.refused("unsupported_token", token(alg="HS256"))
        self.refused("malformed_token", "not-a-token")
        self.refused("wrong_issuer", token({"iss": "https://evil.example.com"}))
        self.refused("wrong_audience", token({"aud": "audience-2"}))
        self.refused("wrong_audience", token(), audience="")
        stamp = int(NOW.timestamp())
        self.refused("expired", token({"exp": stamp - 120}))
        self.refused("not_yet_valid", token({"iat": stamp + 600}))
        self.refused("wrong_service_account", token({"email": "x@example.com"}), service_account="push@example.com")
        self.refused("wrong_service_account", token({"email_verified": False}), service_account="push@example.com")

    def test_bearer_and_notification_parsing(self):
        self.assertEqual(pubsub.bearer({"authorization": "Bearer abc"}), "abc")
        self.assertEqual(pubsub.bearer({"authorization": "Basic abc"}), "")
        self.assertEqual(pubsub.notification(push_body()), ("m-1", "rep@example.org", "1010"))
        self.assertIsNone(pubsub.notification({"message": {"data": "!!"}}))
        self.assertIsNone(pubsub.notification(push_body(address="nobody")))
        self.assertIsNone(pubsub.notification(push_body(history="12a")))
