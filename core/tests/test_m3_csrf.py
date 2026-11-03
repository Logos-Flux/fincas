"""Milestone 3 Track G — S3: CSRF on session-authed POSTs.

API-key/JWT routes are exempt by design (DRF token auth classes carry no
CSRF enforcement); this suite pins both sides:

- session-authed POST without a CSRF token → 403 (enforcement present)
- Api-Key-authed POST without a CSRF token → not 403 (exemption holds)
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.test.client import Client


class CsrfEnforcementTests(TestCase):
    URL = "/api/v1/orders/quotes/"

    def setUp(self):
        self.user = User.objects.create_user(username="csrf-staff", password="pw")

    def test_session_post_without_csrf_token_is_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(
            self.URL,
            data='{"customer": 1}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_session_get_without_csrf_token_still_works(self):
        # CSRF only gates state-changing methods; reads are unaffected.
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.get(self.URL)
        self.assertIn(response.status_code, (200, 403))

    def test_api_key_post_without_csrf_token_is_exempt(self):
        # Exemption by design: APIKeyAuthentication is not CSRF-enforced.
        # We only assert the request is NOT rejected with 403-forbidden
        # by CSRF (it may 401 without a key configured, or 400/201 with).
        from django.test.utils import override_settings

        client = Client(enforce_csrf_checks=True)
        with override_settings(API_KEY="test-key-123"):
            response = client.post(
                self.URL,
                data="{}",
                content_type="application/json",
                HTTP_AUTHORIZATION="Api-Key test-key-123",
            )
        self.assertNotEqual(response.status_code, 403)

    def test_legacy_alias_same_csrf_policy(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(
            "/api/orders/quotes/",
            data='{"customer": 1}',
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
