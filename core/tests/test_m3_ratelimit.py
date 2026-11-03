"""Milestone 3 Track G — S8: rate limiting + lockout (characterization).

Pins per-endpoint throttles (F-046/047, core/throttling.py): small bursts
pass untouched while sustained guessing trips a 429. Rates are threaded
so the sub-limit probes (15/15/25) never throttle and the burst probes
(30/60/100) always do.
"""

from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from core.tests.helpers import make_staff_user


class LoginBurstTests(TestCase):
    URL = "/erp/login/"

    def test_password_guessing_is_never_throttled_today(self):
        user = make_staff_user(username="burst-user", password="right")
        codes = set()
        for _ in range(15):
            response = self.client.post(
                self.URL, {"username": "burst-user", "password": "wrong"}
            )
            codes.add(response.status_code)
        # No 429/403-lockout exists: every attempt gets a normal re-render.
        self.assertNotIn(429, codes)
        print(f"\n[S8-characterize] 15 bad logins → {sorted(codes)} (no throttle)")
        self.assertTrue(
            user.is_active
        )  # account never locked

    def test_login_burst_eventually_throttled(self):
        for _ in range(30):
            response = self.client.post(
                self.URL, {"username": "nobody", "password": "wrong"}
            )
        self.assertEqual(response.status_code, 429)


class TokenBurstTests(TestCase):
    URL = "/api/v1/core/auth/token/"

    def test_token_guessing_is_never_throttled_today(self):
        from django.test.utils import override_settings

        # The burst sibling runs first alphabetically and shares the
        # throttle cache — reset so this sub-limit probe starts clean.
        cache.clear()
        codes = set()
        with override_settings(API_KEY="burst-key"):
            for _ in range(15):
                response = APIClient().post(
                    self.URL,
                    {},
                    format="json",
                    HTTP_AUTHORIZATION="Api-Key wrong-key",
                )
                codes.add(response.status_code)
        self.assertEqual(codes, {401})
        print(f"\n[S8-characterize] 15 bad token fetches → {sorted(codes)}")

    def test_token_burst_eventually_throttled(self):
        from django.test.utils import override_settings

        with override_settings(API_KEY="burst-key"):
            for _ in range(60):
                response = APIClient().post(
                    self.URL,
                    {},
                    format="json",
                    HTTP_AUTHORIZATION="Api-Key wrong-key",
                )
        self.assertEqual(response.status_code, 429)


class CouponBurstTests(TestCase):
    URL = "/api/v1/discounts/validate/"

    def setUp(self):
        self.user = make_staff_user(username="coupon-burst", password="pw")
        self.api = APIClient()
        self.api.force_authenticate(user=self.user)

    def test_coupon_guessing_is_never_throttled_today(self):
        # The burst sibling runs first alphabetically and shares the
        # throttle cache — reset so this sub-limit probe starts clean.
        cache.clear()
        codes = set()
        for i in range(25):
            response = self.api.post(
                self.URL,
                {"code": f"GUESS-{i}", "subtotal": "100.00"},
                format="json",
            )
            codes.add(response.status_code)
        self.assertNotIn(429, codes)
        print(f"\n[S8-characterize] 25 coupon guesses → {sorted(codes)}")

    def test_coupon_burst_eventually_throttled(self):
        for i in range(100):
            response = self.api.post(
                self.URL,
                {"code": f"GUESS-{i}", "subtotal": "100.00"},
                format="json",
            )
        self.assertEqual(response.status_code, 429)
