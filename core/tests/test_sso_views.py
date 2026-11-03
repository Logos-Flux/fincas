"""Tests for Staff SSO handoff to the storefront."""

import base64
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qs, urlparse

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

SECRET = "unit-test-staff-session-secret-0123456789"
CALLBACK = "https://storefront.example.test/staff/callback"


def _b64url_decode(s: str) -> bytes:
    pad = (4 - len(s) % 4) % 4
    return base64.urlsafe_b64decode(s + "=" * pad)


def _verify(token: str, secret: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 2:
        return None
    payload_b64, sig_b64 = parts
    expected = (
        base64.urlsafe_b64encode(
            hmac.new(
                secret.encode("utf-8"),
                payload_b64.encode("ascii"),
                hashlib.sha256,
            ).digest()
        )
        .rstrip(b"=")
        .decode("ascii")
    )
    if not hmac.compare_digest(sig_b64, expected):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    if payload.get("staff") is not True:
        return None
    if not isinstance(payload.get("exp"), int):
        return None
    if payload["exp"] * 1000 <= int(time.time() * 1000):
        return None
    return payload


@override_settings(STAFF_SESSION_SECRET=SECRET, STAFF_SSO_CALLBACK_URL=CALLBACK)
class StaffSsoConfiguredTest(TestCase):
    def setUp(self):
        self.url = reverse("erp-sso-storefront")
        self.staff = User.objects.create_user(
            username="staffer", password="pw", is_staff=True
        )
        self.superuser = User.objects.create_user(
            username="root", password="pw", is_superuser=True, is_staff=False
        )
        self.civilian = User.objects.create_user(
            username="customer", password="pw"
        )

    def test_unauthenticated_redirects_to_login(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/erp/login/", resp["Location"])
        self.assertNotIn("token=", resp["Location"])

    def test_non_staff_forbidden(self):
        self.client.force_login(self.civilian)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 403)

    def test_post_not_allowed(self):
        self.client.force_login(self.staff)
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, 405)

    def _assert_valid_handoff(self, username):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        target = urlparse(resp["Location"])
        self.assertEqual(
            f"{target.scheme}://{target.netloc}{target.path}", CALLBACK
        )
        token = parse_qs(target.query)["token"][0]
        payload = _verify(token, SECRET)
        self.assertIsNotNone(payload, "minted token must verify against the shared secret")
        self.assertIs(payload["staff"], True)
        self.assertEqual(payload["sub"], username)
        self.assertLessEqual(payload["exp"] - int(time.time()), 95)
        self.assertGreater(payload["exp"] - int(time.time()), 0)
        self.assertIsNone(_verify(token, SECRET + "wrong"))

    def test_staff_gets_valid_handoff(self):
        self.client.force_login(self.staff)
        self._assert_valid_handoff("staffer")

    def test_superuser_gets_valid_handoff(self):
        self.client.force_login(self.superuser)
        self._assert_valid_handoff("root")


@override_settings(STAFF_SESSION_SECRET="", STAFF_SSO_CALLBACK_URL=CALLBACK)
class StaffSsoUnconfiguredTest(TestCase):
    def test_fails_closed_when_secret_unset(self):
        url = reverse("erp-sso-storefront")
        staff = User.objects.create_user(username="s", password="pw", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)
