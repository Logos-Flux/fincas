"""Coupon-validate throttle buckets: per shopper, not per credential.

A storefront calls /api/v1/discounts/validate/ as one service identity
(Api-Key or bearer), so keying the throttle on the user gave every shopper a
single shared 60/min budget. Service identities and anonymous callers are
keyed by client IP (core.client_ip, honouring TRUSTED_PROXIES); a signed-in
person by user.
"""

from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ImproperlyConfigured
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from core.authentication import issue_api_jwt
from core.client_ip import client_ip
from core.tests.helpers import make_staff_user
from core.throttling import CouponValidateThrottle

API_KEY = "coupon-throttle-test-key"
URL = "/api/v1/discounts/validate/"
PROXY = "10.0.0.5"


class ClientIpTests(SimpleTestCase):
    def _request(self, remote, xff=None):
        meta = {"REMOTE_ADDR": remote}
        if xff is not None:
            meta["HTTP_X_FORWARDED_FOR"] = xff
        return RequestFactory().get("/", **meta)

    @override_settings(TRUSTED_PROXIES=[])
    def test_no_trusted_proxy_ignores_forwarded_for(self):
        self.assertEqual(client_ip(self._request("203.0.113.9", "198.51.100.1")), "203.0.113.9")

    @override_settings(TRUSTED_PROXIES=[PROXY])
    def test_untrusted_peer_cannot_spoof_forwarded_for(self):
        self.assertEqual(client_ip(self._request("203.0.113.9", "198.51.100.1")), "203.0.113.9")

    @override_settings(TRUSTED_PROXIES=[PROXY])
    def test_trusted_proxy_yields_the_forwarded_client(self):
        self.assertEqual(client_ip(self._request(PROXY, "198.51.100.1")), "198.51.100.1")

    @override_settings(TRUSTED_PROXIES=["10.0.0.0/8"])
    def test_a_spoofed_prefix_is_never_reached(self):
        # The client sent "1.2.3.4"; the trusted proxy appended the real peer.
        request = self._request("10.0.0.5", "1.2.3.4, 198.51.100.1, 10.0.0.9")
        self.assertEqual(client_ip(request), "198.51.100.1")

    @override_settings(TRUSTED_PROXIES=[PROXY])
    def test_malformed_hop_falls_back_to_the_proxy(self):
        self.assertEqual(client_ip(self._request(PROXY, "not-an-ip")), PROXY)

    @override_settings(TRUSTED_PROXIES=[PROXY])
    def test_trusted_proxy_without_header_is_itself(self):
        self.assertEqual(client_ip(self._request(PROXY)), PROXY)

    @override_settings(TRUSTED_PROXIES=["nonsense"])
    def test_invalid_trusted_proxy_entry_fails_loudly(self):
        with self.assertRaises(ImproperlyConfigured):
            client_ip(self._request(PROXY, "198.51.100.1"))


@override_settings(API_KEY=API_KEY, TRUSTED_PROXIES=[PROXY])
@mock.patch.object(CouponValidateThrottle, "rate", "2/minute")
class CouponThrottleBucketTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        # api-service exists already (core.0009; post_migrate stand-in in tests).

    def _post(self, client, shopper_ip, **extra):
        return client.post(
            URL,
            {"code": "NOPE", "subtotal": "10.00"},
            format="json",
            REMOTE_ADDR=PROXY,
            HTTP_X_FORWARDED_FOR=shopper_ip,
            **extra,
        )

    def _assert_shoppers_have_their_own_budget(self, client, **auth):
        for _ in range(2):
            self.assertEqual(self._post(client, "198.51.100.1", **auth).status_code, 200)
        self.assertEqual(self._post(client, "198.51.100.1", **auth).status_code, 429)
        # Same credential, another shopper: a fresh bucket.
        self.assertEqual(self._post(client, "198.51.100.2", **auth).status_code, 200)

    def test_api_key_callers_are_bucketed_per_shopper_ip(self):
        self._assert_shoppers_have_their_own_budget(
            APIClient(), HTTP_AUTHORIZATION=f"Api-Key {API_KEY}"
        )

    def test_bearer_callers_are_bucketed_per_shopper_ip(self):
        token = issue_api_jwt()
        self._assert_shoppers_have_their_own_budget(
            APIClient(), HTTP_AUTHORIZATION=f"Bearer {token}"
        )

    def test_a_signed_in_person_is_bucketed_per_user(self):
        # A signed-in human must be staff: PrincipalPermission (identity step 1)
        # rejects any other human on staff API routes, and shoppers are not
        # auth.User rows yet (that is identity step 3).
        person = make_staff_user("shopper", password="pw")
        client = APIClient()
        client.force_authenticate(user=person)
        for _ in range(2):
            self.assertEqual(self._post(client, "198.51.100.1").status_code, 200)
        # Moving IP does not reset a person's budget.
        self.assertEqual(self._post(client, "198.51.100.2").status_code, 429)
