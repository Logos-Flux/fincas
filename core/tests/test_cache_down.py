"""The back office with the cache unreachable (F-086, F-087).

The 2026-10-18 staging drill stopped Redis: every page answered 500 from the
label registry's cache read in the nav context processor, and a login POST
500ed from the throttle's attempt counter. The plan's outage table (section
8) asks for pages that render and a login that says "try again in a
moment". ``RaisingCache`` stands in for a dead backend: every operation
raises the way redis-py does. ``core.cache_guard`` is the one place that
decides what an outage costs; these tests pin that contract.
"""

import re
from pathlib import Path
from unittest import mock

from django.core.cache.backends.base import BaseCache
from django.db import connection
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core import cache_guard, once
from core.auth_views import StorefrontTokenThrottle
from core.checks import trusted_proxies_check
from core.label_registry import stored_any, stored_tenant
from core.models import Company, LabelOverride
from core.oidc_views import _throttled
from core.tests.helpers import make_staff_user
from core.throttling import CACHE_DOWN_MESSAGE, LOGIN_ATTEMPTS_PER_MINUTE, CouponValidateThrottle


class CacheDown(Exception):
    pass


class RaisingCache(BaseCache):
    """A cache whose backend is gone: every call raises, and counts."""

    calls = 0

    def __init__(self, location, params):
        super().__init__(params)

    def _boom(self, *args, **kwargs):
        type(self).calls += 1
        raise CacheDown("Error -2 connecting to redis:6379. Name or service not known.")

    add = get = set = touch = delete = get_many = set_many = delete_many = clear = incr = decr = has_key = _boom


RAISING = {"default": {"BACKEND": "core.tests.test_cache_down.RaisingCache"}}
PROXY = "10.0.0.2"


def later(seconds):
    """``time.monotonic`` advanced by ``seconds`` for the guard."""
    return mock.patch.object(cache_guard.time, "monotonic", return_value=cache_guard.time.monotonic() + seconds)


class GuardState(TestCase):
    """Every test starts with the breaker closed, nothing remembered and the warning armed."""

    def setUp(self):
        super().setUp()
        cache_guard._reset()
        RaisingCache.calls = 0
        self.addCleanup(cache_guard._reset)


@override_settings(CACHES=RAISING)
class LabelsWithCacheDownTests(GuardState):
    def test_label_reads_fall_back_to_the_database(self):
        company = Company.objects.create(name="Cache Co")
        self.assertFalse(stored_any())
        self.assertEqual(stored_tenant(str(company.pk)), {})

    def test_tenant_rows_fall_back_to_the_database(self):
        company = Company.objects.create(name="Cache Co")
        # save() runs invalidate_tenant under the raising cache: no 500.
        LabelOverride.objects.create(company=company, key="crm.engagement", singular="Deal", plural="Deals", article="a")
        self.assertTrue(stored_any())
        self.assertEqual(stored_tenant(str(company.pk))["crm.engagement"].singular, "Deal")

    def test_an_outage_costs_the_database_once_per_minute_not_per_label(self):
        company = Company.objects.create(name="Cache Co")
        LabelOverride.objects.create(company=company, key="crm.engagement", singular="Deal", plural="Deals", article="a")
        cache_guard._reset()
        RaisingCache.calls = 0
        # First resolution: the flag and the rows, one query each, and ONE
        # backend attempt: the breaker opens on the first failure.
        with self.assertNumQueries(2):
            stored_tenant(str(company.pk))
        self.assertEqual(RaisingCache.calls, 1)
        # A page resolves dozens of labels; the rest come from the fallback
        # without touching the backend (a hanging Redis would cost a socket
        # timeout per attempt).
        with self.assertNumQueries(0):
            for _ in range(30):
                stored_tenant(str(company.pk))
        self.assertEqual(RaisingCache.calls, 1)
        # A minute on, the database is asked again (and the backend retried once).
        with later(61), self.assertNumQueries(2):
            stored_tenant(str(company.pk))
        self.assertEqual(RaisingCache.calls, 2)

    def test_every_back_office_page_still_renders_with_one_backend_attempt(self):
        company = Company.objects.create(name="Cache Co")
        LabelOverride.objects.create(company=company, key="crm.engagement", singular="Deal", plural="Deals", article="a")
        make_staff_user("ops", password="pw")
        self.client.login(username="ops", password="pw")
        for name in ("erp-dashboard", "erp-order-list", "erp-customer-list"):
            with self.subTest(name=name):
                cache_guard._reset()
                RaisingCache.calls = 0
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(RaisingCache.calls, 1, "one attempt, then the breaker")

    def test_the_outage_is_logged_once_per_message_then_at_debug(self):
        with self.assertLogs("core.cache_guard", level="DEBUG") as first:
            stored_any()
        self.assertEqual([r.levelname for r in first.records], ["WARNING"])
        # Breaker closed again, warning interval not over: the repeat is DEBUG.
        with later(cache_guard.BREAKER_S + 1), self.assertLogs("core.cache_guard", level="DEBUG") as second:
            stored_any()
        self.assertEqual([r.levelname for r in second.records], ["DEBUG"])
        # Past the interval the warning (with its traceback) is loud again.
        with later(cache_guard.WARN_INTERVAL_S + cache_guard.BREAKER_S + 2), \
                self.assertLogs("core.cache_guard", level="WARNING"):
            stored_any()


class GuardRecoveryTests(GuardState):
    def test_the_breaker_closes_on_the_next_success_and_remembered_values_expire(self):
        with override_settings(CACHES=RAISING):
            cache_guard.cache_set("k", "remembered", 60)
            self.assertEqual(cache_guard.cache_get("k"), "remembered")
            self.assertTrue(cache_guard.is_down())
        # The cache is back (locmem) but the breaker holds for BREAKER_S.
        self.assertEqual(cache_guard.cache_get("k"), "remembered")
        with later(cache_guard.BREAKER_S + 1):
            self.assertIsNone(cache_guard.cache_get("k"))
            self.assertFalse(cache_guard.is_down())

    def test_recovery_is_probed_by_one_caller_at_a_time(self):
        with override_settings(CACHES=RAISING):
            cache_guard.cache_get("k")
        self.assertTrue(cache_guard.is_down())
        with later(cache_guard.BREAKER_S + 1):
            self.assertFalse(cache_guard.is_down(), "the first caller probes")
            self.assertTrue(cache_guard.is_down(), "the others wait for the probe")
            cache_guard.ok()
            self.assertFalse(cache_guard.is_down())
            self.assertFalse(cache_guard.is_down())

    def test_the_fallback_is_bounded(self):
        with override_settings(CACHES=RAISING):
            for i in range(cache_guard.FALLBACK_MAX + 50):
                cache_guard.cache_set(f"k{i}", i, 60)
        self.assertLessEqual(len(cache_guard._fallback), cache_guard.FALLBACK_MAX)
        self.assertIsNone(cache_guard._remembered("k0", None), "the oldest entries were evicted")

    def test_a_rewritten_key_is_young_again(self):
        with override_settings(CACHES=RAISING):
            cache_guard.cache_set("first", 1, 60)
            for i in range(cache_guard.FALLBACK_MAX - 1):
                cache_guard.cache_set(f"k{i}", i, 60)
            cache_guard.cache_set("first", 2, 60)  # rewritten: moves to the end
            cache_guard.cache_set("one-more", 0, 60)  # evicts k0, not "first"
        self.assertEqual(cache_guard._remembered("first", None), 2)
        self.assertIsNone(cache_guard._remembered("k0", None))


@override_settings(CACHES=RAISING)
class LoginWithCacheDownTests(GuardState):
    def test_login_form_renders(self):
        self.assertEqual(self.client.get(reverse("erp-login")).status_code, 200)

    def test_login_post_says_try_again_and_keeps_the_input(self):
        make_staff_user("ops", password="pw")
        response = self.client.post(reverse("erp-login"), {"username": "ops", "password": "pw"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Retry-After"], "60")
        self.assertIn("no-store", response["Cache-Control"])
        body = response.content.decode()
        self.assertIn(CACHE_DOWN_MESSAGE, body)
        self.assertIn('value="ops"', body)
        self.assertNotIn("Invalid credentials", body)
        # Nobody is signed in: the attempt was refused, not let through uncounted.
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_the_refusal_never_authenticates_and_skips_the_demo_hint_query(self):
        # The 503 page carries an unbound form, so rendering it cannot run
        # authenticate(); and it skips the demo-hint superuser query and the
        # provider lookup (the context processors' own reads still run).
        make_staff_user("ops", password="pw")
        with mock.patch("django.contrib.auth.forms.authenticate") as authenticate, \
                CaptureQueriesContext(connection) as queries:
            response = self.client.post(reverse("erp-login"), {"username": "ops", "password": "wrong"})
        self.assertEqual(response.status_code, 503)
        authenticate.assert_not_called()
        self.assertFalse([q["sql"] for q in queries if "auth_user" in q["sql"]], "no user lookup on the refusal")

    @override_settings(GOLDEN_CACHE_DOWN_FAIL_OPEN=True)
    def test_the_escape_hatch_lets_an_operator_in_uncounted(self):
        make_staff_user("ops", password="pw")
        response = self.client.post(reverse("erp-login"), {"username": "ops", "password": "pw"})
        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)


@override_settings(CACHES=RAISING)
class ApiThrottlesWithCacheDownTests(GuardState):
    def test_token_obtain_refuses_with_429_and_the_whole_window(self):
        response = self.client.post(reverse("api-token-obtain"), {"api_key": "x"}, HTTP_X_API_KEY="x")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "60")

    def test_every_cache_counted_throttle_refuses_for_the_window(self):
        request = RequestFactory().post("/", REMOTE_ADDR="203.0.113.9")
        request.user = mock.Mock(is_authenticated=False)
        for throttle in (StorefrontTokenThrottle(), CouponValidateThrottle()):
            with self.subTest(throttle=type(throttle).__name__):
                self.assertFalse(throttle.allow_request(request, None))
                self.assertEqual(throttle.wait(), throttle.duration)

    def test_the_breaker_spares_the_backend(self):
        request = RequestFactory().post("/", REMOTE_ADDR="203.0.113.9")
        for _ in range(5):
            CouponValidateThrottle().allow_request(request, None)
        self.assertEqual(RaisingCache.calls, 1)

    @override_settings(GOLDEN_CACHE_DOWN_FAIL_OPEN=True)
    def test_the_escape_hatch_lets_requests_through(self):
        request = RequestFactory().post("/", REMOTE_ADDR="203.0.113.9")
        self.assertTrue(CouponValidateThrottle().allow_request(request, None))

    def test_a_key_derivation_bug_is_not_read_as_an_outage(self):
        request = RequestFactory().post("/", REMOTE_ADDR="203.0.113.9")
        throttle = CouponValidateThrottle()
        with mock.patch.object(CouponValidateThrottle, "get_cache_key", side_effect=KeyError("bug")):
            with self.assertRaises(KeyError):
                throttle.allow_request(request, None)
        self.assertFalse(cache_guard.is_down())


@override_settings(CACHES=RAISING)
class OidcStartWithCacheDownTests(GuardState):
    def test_the_start_throttle_tells_its_caller_the_cache_is_gone(self):
        request = RequestFactory().get("/", REMOTE_ADDR="203.0.113.9")
        with self.assertRaises(cache_guard.CacheUnavailable):
            _throttled(request)

    @override_settings(GOLDEN_CACHE_DOWN_FAIL_OPEN=True)
    def test_the_escape_hatch_lets_the_start_through(self):
        request = RequestFactory().get("/", REMOTE_ADDR="203.0.113.9")
        self.assertFalse(_throttled(request))


@override_settings(CACHES=RAISING)
class OnceGuardWithCacheDownTests(GuardState):
    def test_a_claim_is_remembered_in_this_process(self):
        company = Company.objects.create(name="Cache Co")
        request = RequestFactory().post("/", {once.ONCE_FIELD: once.mint()})
        request.user = make_staff_user("ops", password="pw")
        self.assertTrue(once.claim(request, "record_payment", company))
        self.assertFalse(once.claim(request, "record_payment", company), "the repeat is refused")
        once.release(request, "record_payment", company)
        self.assertTrue(once.claim(request, "record_payment", company), "released: the retry may run")

    def test_a_claim_made_while_down_holds_after_recovery(self):
        company = Company.objects.create(name="Cache Co")
        request = RequestFactory().post("/", {once.ONCE_FIELD: once.mint()})
        request.user = make_staff_user("ops", password="pw")
        self.assertTrue(once.claim(request, "record_payment", company))
        # The backend is back (locmem) and never saw the claim: still refused.
        with override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}), \
                later(cache_guard.BREAKER_S + 1):
            self.assertFalse(once.claim(request, "record_payment", company))

    def test_a_double_click_wins_the_local_claim_once(self):
        import threading

        company = Company.objects.create(name="Cache Co")
        token = once.mint()
        request = RequestFactory().post("/", {once.ONCE_FIELD: token})
        request.user = make_staff_user("ops", password="pw")
        results = []
        start = threading.Barrier(8)

        def attempt():
            start.wait()
            results.append(once.claim(request, "record_payment", company, token))

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(True), 1, results)

    def test_a_bad_object_is_a_bug_not_an_outage(self):
        request = RequestFactory().post("/", {once.ONCE_FIELD: once.mint()})
        with self.assertRaises(AttributeError):
            once.claim(request, "send_order", object())


class LoginCounterTests(GuardState):
    def test_a_key_recreated_by_incr_gets_its_window_back(self):
        # Django's Redis incr is EXISTS then INCR: expiry in between recreates
        # the key with no TTL. A count of 1 after a failed add means exactly
        # that, and the window is restored.
        make_staff_user("ops", password="pw")
        with mock.patch("core.throttling.cache") as fake:
            fake.add.return_value = False
            fake.incr.return_value = 1
            self.client.post(reverse("erp-login"), {"username": "ops", "password": "wrong"})
        fake.touch.assert_called_once()


class LoginBreakerTests(GuardState):
    def test_a_429_does_not_orphan_the_recovery_probe(self):
        # Breaker opened by an unrelated failure, then expired: the login POST
        # that probes the backend gets a healthy answer (over the limit) and
        # must close the breaker before refusing, or every gate in the process
        # stays on the fallback for PROBE_TIMEOUT_S, renewable by that client.
        make_staff_user("ops", password="pw")
        from django.core.cache import cache as real_cache

        real_cache.set("login-throttle:127.0.0.1:ops", LOGIN_ATTEMPTS_PER_MINUTE + 5, 60)
        with override_settings(CACHES=RAISING):
            cache_guard.cache_get("unrelated")
        self.assertTrue(cache_guard.is_down())
        with later(cache_guard.BREAKER_S + 1):
            response = self.client.post(reverse("erp-login"), {"username": "ops", "password": "wrong"})
            self.assertEqual(response.status_code, 429)
            self.assertFalse(cache_guard.is_down())

    @override_settings(CACHES=RAISING, GOLDEN_CACHE_DOWN_FAIL_OPEN=True)
    def test_fail_open_never_touches_an_open_breaker(self):
        make_staff_user("ops", password="pw")
        cache_guard.cache_get("unrelated")  # opens the breaker
        RaisingCache.calls = 0
        response = self.client.post(reverse("erp-login"), {"username": "ops", "password": "pw"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(RaisingCache.calls, 0)


class LoginThrottleKeyTests(GuardState):
    """F-087: behind the deployment's proxy the bucket is the real client's."""

    def _post(self, forwarded_for):
        return self.client.post(
            reverse("erp-login"), {"username": "ops", "password": "wrong"},
            REMOTE_ADDR=PROXY, HTTP_X_FORWARDED_FOR=forwarded_for,
        )

    @override_settings(TRUSTED_PROXIES=[PROXY])
    def test_one_clients_bad_logins_do_not_lock_out_another(self):
        make_staff_user("ops", password="pw")
        for _ in range(LOGIN_ATTEMPTS_PER_MINUTE):
            self.assertEqual(self._post("198.51.100.1").status_code, 200)
        self.assertEqual(self._post("198.51.100.1").status_code, 429)
        self.assertEqual(self._post("198.51.100.2").status_code, 200)


class TrustedProxiesCheckTests(SimpleTestCase):
    @override_settings(TRUSTED_PROXIES=["not-an-address"])
    def test_a_malformed_entry_is_a_startup_error_not_a_500_per_login(self):
        self.assertEqual([c.id for c in trusted_proxies_check(None)], ["core.E080"])

    @override_settings(TRUSTED_PROXIES=[], SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"))
    def test_a_proxied_deployment_without_trusted_proxies_is_warned(self):
        self.assertEqual([c.id for c in trusted_proxies_check(None)], ["core.W114"])

    @override_settings(TRUSTED_PROXIES=[PROXY], SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"))
    def test_a_configured_deployment_is_clean(self):
        self.assertEqual(trusted_proxies_check(None), [])


class NoUnguardedCacheCallsTests(SimpleTestCase):
    """Every request-path cache call goes through ``core.cache_guard`` or a
    module that handles the backend's errors itself (F-086). A new direct
    ``cache.<op>(`` elsewhere reintroduces the 500 this suite exists for."""

    GUARDED = {
        "core/cache_guard.py",   # the guard
        "core/throttling.py",    # counters: refuse on failure (CacheTolerantThrottle, the login view)
        "core/oidc_views.py",    # counter: refuses on failure (_throttled)
        "core/testing/no_network.py",  # reads the CACHES setting dict, not the cache
        "erp_assist/core/_cache.py",   # its own in-memory dict named cache, not Django's
    }
    CALL = re.compile(
        r"\b(?:cache|caches\[[^\]]+\])\.(a?get|a?set|a?add|incr|decr|delete|get_many|set_many|delete_many|touch|clear|has_key)\("
    )

    def test_direct_cache_calls_are_only_where_they_are_handled(self):
        root = Path(__file__).resolve().parents[2]
        offenders = set()
        for path in root.rglob("*.py"):
            rel = path.relative_to(root).as_posix()
            if any(part in ("tests", "migrations", "node_modules", ".venv", "storefront") for part in path.parts) \
                    or rel.startswith(("scripts/", "config/")) or "/test_" in rel or rel.endswith("_test.py"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if self.CALL.search(text) and rel not in self.GUARDED:
                offenders.add(rel)
        self.assertEqual(offenders, set(), "route these through core.cache_guard or handle the backend error")
