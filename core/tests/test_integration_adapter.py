"""The adapter base: transport, the 2.9 policy, rate limits with a fake clock,
the registry hook and the class rules (docs/crm/phase6-adapters.md 2.2-2.3, 2.9).
"""

from __future__ import annotations

from datetime import timedelta

import httpx
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from core.integrations import accounts as services
from core.integrations import ratelimit
from core.integrations.ratelimit import RateLimit
from core.integrations.transport import HttpxTransport, Response, TransportError
from core.plugins import plugin_registry
from core.plugins.base import BasePlugin
from core.plugins.crm_adapter import (
    BaseCrmAdapter,
    Capability,
    CredentialsRejected,
    MissingCredential,
    ProviderRejected,
    RateLimited,
    Throttled,
    Transient,
    check_adapter_class,
)
from core.testing.integration_fixtures import FixtureMissing, FixtureTransport, fixture_secret
from golden_erp.addons.fixture_crm.adapter import FixtureCrmAdapter, FixtureOAuthAdapter

from .test_integration_secrets import IntegrationTestCase


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def resp(status, payload=None, headers=None, path="/v1/me", method="GET"):
    return {"schema_version": 1, "provider": "fixture", "request": {"method": method, "path": path},
            "response": {"status": status, "headers": headers or {}}, "payload": payload}


class TokenBucketTests(SimpleTestCase):
    def setUp(self):
        ratelimit.reset()
        self.addCleanup(ratelimit.reset)

    def test_bucket_spends_then_reports_the_wait_and_refills_with_the_clock(self):
        clock = FakeClock()
        limit = RateLimit(requests=2, per_seconds=10)
        self.assertEqual(ratelimit.take("a", limit, clock=clock), 0.0)
        self.assertEqual(ratelimit.take("a", limit, clock=clock), 0.0)
        self.assertAlmostEqual(ratelimit.take("a", limit, clock=clock), 5.0)
        clock.advance(4.9)
        self.assertGreater(ratelimit.take("a", limit, clock=clock), 0.0)
        clock.advance(0.2)
        self.assertEqual(ratelimit.take("a", limit, clock=clock), 0.0)
        self.assertEqual(ratelimit.take("b", limit, clock=clock), 0.0, "buckets are per key")

    def test_bucket_never_holds_more_than_its_size(self):
        clock = FakeClock()
        limit = RateLimit(requests=1, per_seconds=1)
        ratelimit.take("a", limit, clock=clock)
        clock.advance(3600)
        self.assertEqual(ratelimit.take("a", limit, clock=clock), 0.0)
        self.assertGreater(ratelimit.take("a", limit, clock=clock), 0.0)

    def test_retry_after_parses_seconds_and_dates_and_is_capped(self):
        now = timezone.now()
        self.assertEqual(ratelimit.retry_after_seconds("120"), 120.0)
        self.assertEqual(ratelimit.retry_after_seconds("999999"), ratelimit.MAX_PAUSE.total_seconds())
        date = (now + timedelta(seconds=90)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        self.assertAlmostEqual(ratelimit.retry_after_seconds(date, now=now), 90, delta=1.5)
        self.assertIsNone(ratelimit.retry_after_seconds("soon"))
        self.assertEqual(ratelimit.retry_after_seconds("-5"), 0.0)

    def test_account_settings_may_raise_the_limit(self):
        default = RateLimit(1, 1)
        self.assertEqual(ratelimit.effective_limit(default, {"rate_limit": {"requests": 50, "per_seconds": 10}}),
                         RateLimit(50, 10))
        self.assertEqual(ratelimit.effective_limit(default, {"rate_limit": {"requests": 0}}), default)
        self.assertEqual(ratelimit.effective_limit(default, {}), default)


class HttpxTransportTests(SimpleTestCase):
    """The production transport, exercised only through httpx.MockTransport (no socket)."""

    def test_request_is_sent_and_the_answer_normalised(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(201, json={"id": "c1"}, headers={"X-Thing": "1"})

        transport = HttpxTransport(httpx.Client(transport=httpx.MockTransport(handler)))
        answer = transport.request("POST", "https://api.fixture.example.com/v1/contacts",
                                   headers={"Authorization": "Bearer t"}, params={"a": "1"}, json={"x": 1})
        self.assertEqual((answer.status, answer.json(), answer.header("x-thing")), (201, {"id": "c1"}, "1"))
        self.assertEqual(seen, {"url": "https://api.fixture.example.com/v1/contacts?a=1", "auth": "Bearer t"})

    def test_a_transport_failure_carries_the_class_only(self):
        url = "https://x.example.com/?" + "token=" + "fx-" + "k" * 16  # fake, built at runtime

        def handler(request):
            raise httpx.ConnectError(f"failed for {url}")

        transport = HttpxTransport(httpx.Client(transport=httpx.MockTransport(handler)))
        with self.assertRaises(TransportError) as ctx:
            transport.request("GET", url, headers={})
        self.assertEqual(str(ctx.exception), "ConnectError")


class HttpPolicyTests(IntegrationTestCase):
    """BaseCrmAdapter.http applies 2.9: 429 pauses, 401 pauses, 5xx retries, 4xx dead."""

    def setUp(self):
        ratelimit.reset()
        self.addCleanup(ratelimit.reset)
        self.acct = self.account(enabled=True, settings={"rate_limit": {"requests": 100, "per_seconds": 1}})
        services.set_credential(self.acct, "api_key", fixture_secret("api_key"))

    def adapter(self, *responses):
        transport = FixtureTransport(responses=list(responses))
        return FixtureCrmAdapter(self.acct, transport), transport

    def test_429_pauses_the_account_for_retry_after_and_raises_throttled(self):
        adapter, _ = self.adapter(resp(429, {"error": "slow down"}, {"Retry-After": "120"}))
        before = timezone.now()
        with self.assertRaises(Throttled) as ctx:
            adapter.http("GET", "/v1/me", headers=adapter._auth())
        self.assertEqual(ctx.exception.after, 120.0)
        self.acct.refresh_from_db()
        self.assertGreaterEqual(self.acct.paused_until, before + timedelta(seconds=119))
        self.assertEqual(self.acct.last_error, "rate_limited")
        self.assertGreater(ratelimit.paused_for(self.acct), 100)

    def test_429_on_a_grant_pauses_that_grant_only(self):
        rep = self.make_profile()
        grant = services.set_credential(self.acct, "oauth_refresh", "fixture-rt", owner=rep)
        adapter, _ = self.adapter(resp(429, None, {"Retry-After": "30"}))
        with self.assertRaises(Throttled):
            adapter.http("GET", "/v1/me", grant=grant)
        grant.refresh_from_db()
        self.acct.refresh_from_db()
        self.assertIsNotNone(grant.paused_until)
        self.assertIsNone(self.acct.paused_until)

    def make_profile(self):
        from crm.tests.helpers import profile

        return profile(self.company, "rep")

    def test_401_pauses_and_a_new_credential_lifts_the_pause(self):
        adapter, _ = self.adapter(resp(401, {"error": "bad key fixture-api-key-1"}))
        with self.assertRaises(CredentialsRejected) as ctx:
            adapter.http("GET", "/v1/me", headers=adapter._auth())
        self.assertNotIn(fixture_secret("api_key"), str(ctx.exception))
        self.acct.refresh_from_db()
        self.assertEqual(self.acct.last_error, "credentials_rejected")
        self.assertTrue(self.acct.is_paused())
        services.set_credential(self.acct, "api_key", fixture_secret("api_key").replace("-1", "-2"))
        self.acct.refresh_from_db()
        self.assertFalse(self.acct.is_paused())

    def test_5xx_and_transport_errors_are_transient(self):
        adapter, _ = self.adapter(resp(503, None))
        with self.assertRaises(Transient):
            adapter.http("GET", "/v1/me")

        class Broken:
            def request(self, *a, **k):
                raise TransportError("ReadTimeout")

        with self.assertRaises(Transient) as ctx:
            FixtureCrmAdapter(self.acct, Broken()).http("GET", "/v1/me")
        self.assertIn("transport_ReadTimeout", ctx.exception.code)

    def test_other_4xx_is_rejected_with_the_status_only(self):
        adapter, _ = self.adapter(resp(422, {"error": "email fixture-api-key-1 invalid"}))
        with self.assertRaises(ProviderRejected) as ctx:
            adapter.http("GET", "/v1/me", headers=adapter._auth())
        self.assertEqual(ctx.exception.status, 422)
        self.assertNotIn("fixture-api-key-1", str(ctx.exception))

    def test_expected_statuses_are_returned(self):
        adapter, _ = self.adapter(resp(404, None))
        self.assertEqual(adapter.http("GET", "/v1/me", expect=(404,)).status, 404)

    def test_an_empty_bucket_raises_rate_limited_without_a_request(self):
        acct = self.acct
        acct.settings = {}
        adapter, transport = self.adapter(resp(200, {}), resp(200, {}))
        clock = FakeClock()
        from unittest import mock

        with mock.patch.object(ratelimit, "_monotonic", clock):
            adapter.rate_limit  # 5/s for the fixture adapter
            for _ in range(5):
                transport.responses.append(resp(200, {}))
                adapter.http("GET", "/v1/me")
            with self.assertRaises(RateLimited) as ctx:
                adapter.http("GET", "/v1/me")
            self.assertAlmostEqual(ctx.exception.after, 0.2, places=3)
            self.assertEqual(len(transport.recorded), 5, "a spent bucket sends nothing")
            clock.advance(0.2)
            adapter.http("GET", "/v1/me")
        self.assertEqual(len(transport.recorded), 6)

    def test_fixture_transport_names_a_missing_response_and_redacts_secrets(self):
        adapter, transport = self.adapter()
        with self.assertRaises(FixtureMissing) as ctx:
            adapter.http("GET", "/v1/contacts", headers=adapter._auth(), params={"golden_id": "x"})
        self.assertIn("no recorded response for GET /v1/contacts?golden_id=x", str(ctx.exception))
        self.assertEqual(transport.recorded[0]["headers"], {"authorization": "Bearer <redacted:api_key>"})

    def test_missing_credential_is_an_adapter_error(self):
        adapter, _ = self.adapter()
        with self.assertRaises(MissingCredential):
            adapter.secret("webhook_secret")


class RegistryTests(SimpleTestCase):
    def test_installed_adapters_come_from_plugins(self):
        with override_settings(GOLDEN_PLUGINS=["golden_erp.addons.fixture_crm.plugin.FixtureCrmPlugin"]):
            self.assertEqual(set(plugin_registry.get_crm_adapters()), {"fixture", "fixture_oauth"})
        with override_settings(GOLDEN_PLUGINS=[]):
            self.assertEqual(plugin_registry.get_crm_adapters(), {})

    def test_duplicate_ids_and_wrong_classes_fail_closed(self):
        class Twin(BasePlugin):
            name = "twin"

            def get_crm_adapters(self):
                return {"fixture": FixtureCrmAdapter}

        class Wrong(BasePlugin):
            name = "wrong"

            def get_crm_adapters(self):
                return {"fixture": object}

        class Misfiled(BasePlugin):
            name = "misfiled"

            def get_crm_adapters(self):
                return {"other": FixtureCrmAdapter}

        for plugins, needle in (
            (["golden_erp.addons.fixture_crm.plugin.FixtureCrmPlugin", Twin], "Duplicate CRM adapter"),
            ([Wrong], "BaseCrmAdapter subclass"),
            ([Misfiled], "its own provider_id"),
        ):
            with override_settings(GOLDEN_PLUGINS=plugins):
                with self.assertRaisesMessage(ImproperlyConfigured, needle):
                    plugin_registry.get_crm_adapters()


class ClassRuleTests(SimpleTestCase):
    def test_the_fixture_adapters_pass(self):
        check_adapter_class(FixtureCrmAdapter)
        check_adapter_class(FixtureOAuthAdapter)

    def test_a_declared_capability_needs_its_methods(self):
        class Lazy(BaseCrmAdapter):
            provider_id = "lazy"
            capabilities = frozenset({Capability.WEBHOOK})
            credential_kinds = frozenset({"webhook_secret"})

        with self.assertRaisesMessage(AssertionError, "does not override verify_webhook"):
            check_adapter_class(Lazy)

    def test_a_push_needs_a_dedupe_key(self):
        class Pusher(BaseCrmAdapter):
            provider_id = "pusher"
            capabilities = frozenset({Capability.PUSH_TASKS})

            @classmethod
            def outbox_consumers(cls):
                return []

        with self.assertRaisesMessage(AssertionError, "push_dedupe"):
            check_adapter_class(Pusher)

    def test_provider_id_shape(self):
        class Long(BaseCrmAdapter):
            provider_id = "x" * 33

        with self.assertRaises(AssertionError):
            check_adapter_class(Long)

    def test_an_adapter_without_provider_id_fails_at_definition(self):
        with self.assertRaises(TypeError):
            class Nameless(BaseCrmAdapter):
                pass

    def test_settings_validation(self):
        adapter = FixtureCrmAdapter(None, None)
        self.assertEqual(adapter.validate_settings({"mailbox": "m", "initial_since": 30}), [])
        errors = adapter.validate_settings({"mailbox": 1, "bogus": True, "rate_limit": {"requests": -1}})
        self.assertEqual(len(errors), 3)
        self.assertTrue(adapter.validate_settings([]))


class ResponseTests(SimpleTestCase):
    def test_response_helpers(self):
        r = Response(status=204)
        self.assertTrue(r.ok)
        self.assertIsNone(r.json())


class RefusingTransportTests(SimpleTestCase):
    def test_a_test_that_forgot_use_transport_cannot_reach_httpx(self):
        from core.integrations.transport import RefusingTransport, get_transport
        from core.testing.no_network import LiveSocketError, expect_violations

        transport = get_transport()
        self.assertIsInstance(transport, RefusingTransport)
        with expect_violations(), self.assertRaises(LiveSocketError):
            transport.request("GET", "https://api.fixture.example.com/v1/me?x=1", headers={})
