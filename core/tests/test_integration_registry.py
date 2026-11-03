"""ERP_INTEGRATIONS registry, connection-test registration, runner and page."""

from __future__ import annotations

import copy
import json

from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core import integration_checks
from core import integration_registry as reg
from core.models import IntegrationCheck
from golden_erp.integrations import ERP_INTEGRATIONS

# Built at runtime so no credential-shaped literal sits in the tree (secret scanners).
SECRET = "-".join(["not", "a", "real", "credential", "value"])
# Shapes the redactor must catch even when not a configured value; low-entropy
# on purpose so secret scanners do not flag them.
B64_TOKEN = "aaaa+bbbb/cccc" + "=="
BASIC_CRED = "dXNlcjp" + "wYXNz" + "="
PASSWORD_VALUE = "hunter" + "2hunter2"

MANAGED = {
    "key": "demo_service",
    "service": "Demo service",
    "category": "internal",
    "consumers": [{"app": "erp", "purpose": "Testing", "source": "core/tests"}],
    "credentials": [{"name": "DEMO_SERVICE_API_KEY", "app": "erp", "role": "secret"}],
    "owner": "engineering",
    "owner_basis": "test fixture",
    "status": "managed",
    "connection_tests": ["demo_ping"],
}


class _RegisteredTestMixin:
    """Registers ``demo_ping`` for the test and removes it afterwards."""

    behaviour = "ok"

    def setUp(self):
        super().setUp()

        def demo_ping():
            if self.behaviour == "crash":
                raise RuntimeError(SECRET)
            if self.behaviour == "leak":
                return integration_checks.result(
                    False, f"provider echoed {SECRET} and Bearer abcdef123456 and "
                           f"Bearer {B64_TOKEN} and Authorization: Basic {BASIC_CRED} and "
                           f"password={PASSWORD_VALUE} apikey={PASSWORD_VALUE}x secret: {PASSWORD_VALUE}y",
                    error_class="integration_auth_failed", http_status=401,
                    observed={"host": "api.example.com", "raw": {"nested": SECRET},
                              "url": "https://x/?key=" + SECRET, "methods": ["get", "x" * 500]},
                )
            return integration_checks.result(True, "Demo answered.", http_status=200, latency_ms=12,
                                              observed={"host": "api.example.com"})

        reg.register_connection_test("demo_ping", demo_ping, label="Demo ping")
        self.addCleanup(reg.unregister_connection_test, "demo_ping")


class RegistryValidationTests(SimpleTestCase):
    def _with(self, mutate, base=None):
        entries = copy.deepcopy(base if base is not None else ERP_INTEGRATIONS)
        mutate(entries)
        return override_settings(ERP_INTEGRATIONS=entries)

    def _assert_rejected(self, mutate, needle, base=None):
        with self.assertRaises(ImproperlyConfigured) as cm:
            with self._with(mutate, base):
                pass
        self.assertIn(needle, str(cm.exception))
        return str(cm.exception)

    def test_shipped_inventory_is_valid(self):
        self.assertEqual(len(reg.INTEGRATIONS), len(ERP_INTEGRATIONS))
        reg.check_connection_tests()

    def test_not_a_list(self):
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_INTEGRATIONS={"key": "x"}):
                pass

    def test_unknown_status(self):
        self._assert_rejected(lambda e: e[0].update(status="live"), "['status']")

    def test_unknown_category(self):
        self._assert_rejected(lambda e: e[0].update(category="misc"), "['category']")

    def test_unknown_owner(self):
        self._assert_rejected(lambda e: e[0].update(owner="someone"), "['owner']")

    def test_named_owner_needs_a_basis(self):
        self._assert_rejected(lambda e: e[0].pop("owner_basis"), "owner_basis")

    def test_bad_key(self):
        self._assert_rejected(lambda e: e[0].update(key="Not Snake"), "snake_case")

    def test_credential_value_is_rejected_without_echoing_it(self):
        msg = self._assert_rejected(
            lambda e: e[0]["credentials"].append({"name": SECRET, "app": "erp", "role": "secret"}),
            "NAME only",
        )
        self.assertNotIn(SECRET, msg)

    def test_unknown_consumer_app(self):
        self._assert_rejected(lambda e: e[0]["consumers"][0].update(app="worker"), "consumer")

    def test_duplicate_key(self):
        self._assert_rejected(lambda e: e.append(copy.deepcopy(e[-1])), "duplicate key")

    def test_bootstrap_flag_and_status_go_together(self):
        self._assert_rejected(lambda e: e[-1].update(bootstrap=True), "bootstrap")

    def test_managed_row_needs_a_test(self):
        self._assert_rejected(lambda e: e.append(dict(MANAGED, connection_tests=[])), "names no connection test")

    def test_tests_only_on_managed_rows(self):
        self._assert_rejected(lambda e: e.append(dict(MANAGED, status="inventoried")), "only 'managed' rows test")

    def test_test_claimed_by_two_rows(self):
        def mutate(e):
            e.append(dict(MANAGED))
            e.append(dict(MANAGED, key="demo_two"))
        self._assert_rejected(mutate, "claimed by two rows")

    def test_unknown_field(self):
        self._assert_rejected(lambda e: e[0].update(api_key="x"), "unknown field")

    def test_vocabulary_is_config(self):
        with override_settings(ERP_INTEGRATION_APPS=["erp", "worker"]):
            with self._with(lambda e: e[0]["consumers"].append(
                    {"app": "worker", "purpose": "p", "source": "s"})):
                self.assertIn("worker", reg.rows()[0]["apps"])

    def test_local_app_must_be_listed(self):
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_INTEGRATION_LOCAL_APP="worker"):
                pass

    def test_owners_must_include_unassigned(self):
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_INTEGRATION_OWNERS=["engineering"]):
                pass

    def test_valid_override_reloads_in_place(self):
        with self._with(lambda e: e.pop()):
            self.assertEqual(len(reg.INTEGRATIONS), len(ERP_INTEGRATIONS) - 1)
        self.assertEqual(len(reg.INTEGRATIONS), len(ERP_INTEGRATIONS))

    def test_summary_partitions_the_inventory(self):
        s = reg.summary()
        self.assertEqual(sum(s["by_status"].values()), s["total"])
        self.assertEqual(sum(s["by_owner"].values()), s["total"])
        self.assertEqual(len(s["unassigned"]), s["by_owner"]["unassigned"])

    def test_bootstrap_rows_have_no_tests(self):
        for row in reg.rows():
            if row["bootstrap"]:
                self.assertEqual(row["status"], "bootstrap")
                self.assertEqual(row["connection_tests"], [])

    def test_presence_is_a_bool_never_a_value(self):
        with self.settings(EASYPOST_API_KEY="present-value-000"):
            self.assertIs(reg.credential_presence("EASYPOST_API_KEY", "erp"), True)
        with self.settings(EASYPOST_API_KEY=""):
            self.assertIs(reg.credential_presence("EASYPOST_API_KEY", "erp"), False)
        with override_settings(ERP_INTEGRATION_APPS=["erp", "worker"]):
            self.assertIsNone(reg.credential_presence("EASYPOST_API_KEY", "worker"))


class ConnectionTestRegistrationTests(SimpleTestCase):
    def tearDown(self):
        reg.unregister_connection_test("demo_ping")

    def test_register_as_decorator_and_list(self):
        @reg.register_connection_test("demo_ping", label="Demo")
        def ping():
            return integration_checks.result(True, "ok")

        self.assertIs(reg.connection_tests()["demo_ping"].func, ping)
        self.assertEqual(reg.connection_tests()["demo_ping"].label, "Demo")

    def test_reregistering_the_same_function_is_a_noop(self):
        def ping():
            return integration_checks.result(True, "ok")

        reg.register_connection_test("demo_ping", ping)
        reg.register_connection_test("demo_ping", ping)

    def test_name_clash_is_rejected(self):
        reg.register_connection_test("demo_ping", lambda: None)
        with self.assertRaises(ImproperlyConfigured):
            reg.register_connection_test("demo_ping", lambda: None)

    def test_bad_name_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            reg.register_connection_test("Demo Ping", lambda: None)

    def test_unregistered_reference_fails_the_check(self):
        with override_settings(ERP_INTEGRATIONS=[*ERP_INTEGRATIONS, MANAGED]):
            with self.assertRaises(ImproperlyConfigured) as cm:
                reg.check_connection_tests()
            self.assertIn("demo_ping", str(cm.exception))
            errors = reg._connection_tests_check()
            self.assertEqual([e.id for e in errors], ["core.E030"])
            self.assertEqual(reg.available_targets(), [])

    def test_registered_reference_passes(self):
        reg.register_connection_test("demo_ping", lambda: None)
        with override_settings(ERP_INTEGRATIONS=[*ERP_INTEGRATIONS, MANAGED]):
            reg.check_connection_tests()
            self.assertEqual(reg._connection_tests_check(), [])
            self.assertEqual(reg.available_targets(), ["demo_ping"])


@override_settings(ERP_INTEGRATIONS=[*ERP_INTEGRATIONS, MANAGED], DEMO_SERVICE_API_KEY=SECRET)
class RunCheckTests(_RegisteredTestMixin, TestCase):
    def test_success_is_persisted(self):
        user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        out = integration_checks.run_check("demo_ping", user=user)
        self.assertTrue(out["ok"])
        row = IntegrationCheck.objects.get(pk=out["check_id"])
        self.assertEqual((row.target, row.ok, row.http_status, row.latency_ms), ("demo_ping", True, 200, 12))
        self.assertEqual(row.tested_by, user)
        self.assertEqual(integration_checks.last_results()["demo_ping"], row)

    def test_failure_is_redacted_before_storage(self):
        self.behaviour = "leak"
        out = integration_checks.run_check("demo_ping")
        row = IntegrationCheck.objects.get(pk=out["check_id"])
        stored = json.dumps({"what": row.what, "observed": row.observed, "error_class": row.error_class})
        self.assertNotIn(SECRET, stored)
        self.assertNotIn("abcdef123456", stored)
        for leaked in ("aaaa+bbbb", "cccc==", BASIC_CRED, PASSWORD_VALUE):
            self.assertNotIn(leaked, stored)
        self.assertIn("Bearer [redacted]", row.what)
        self.assertIn("Basic [redacted]", row.what)
        self.assertIn("password=[redacted]", row.what)
        self.assertFalse(row.ok)
        self.assertEqual(row.observed["host"], "api.example.com")
        self.assertIsNone(row.observed["raw"])
        self.assertIsNone(row.observed["url"])
        self.assertEqual(row.observed["methods"], ["get"])

    def test_crash_is_recorded_without_the_exception_text(self):
        self.behaviour = "crash"
        out = integration_checks.run_check("demo_ping")
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_class"], "integration_unexpected_response")
        self.assertIn("RuntimeError", out["what"])
        self.assertNotIn(SECRET, out["what"])

    def test_unknown_target_raises(self):
        with self.assertRaises(KeyError):
            integration_checks.run_check("nope")
        self.assertFalse(IntegrationCheck.objects.exists())


@override_settings(ERP_INTEGRATIONS=[*ERP_INTEGRATIONS, MANAGED], DEMO_SERVICE_API_KEY=SECRET)
class IntegrationsPageTests(_RegisteredTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user(username="staff", password="pw", is_staff=True)
        self.plain = User.objects.create_user(username="plain", password="pw")

    def test_page_renders_inventory_and_presence_never_values(self):
        self.client.force_login(self.staff)
        html = self.client.get(reverse("erp-integrations")).content.decode()
        self.assertIn("Demo service", html)
        self.assertIn("DEMO_SERVICE_API_KEY", html)
        self.assertIn("Demo ping", html)
        self.assertIn("Not tested yet", html)
        self.assertNotIn(SECRET, html)

    def test_page_is_staff_only(self):
        self.assertEqual(self.client.get(reverse("erp-integrations")).status_code, 302)
        self.client.force_login(self.plain)
        self.assertEqual(self.client.get(reverse("erp-integrations")).status_code, 403)

    def test_run_test_json(self):
        self.client.force_login(self.staff)
        resp = self.client.post(reverse("erp-integration-test", args=["demo_ping"]),
                                HTTP_ACCEPT="application/json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        self.assertEqual(IntegrationCheck.objects.count(), 1)

    def test_run_test_form_redirects_to_anchor_and_page_shows_result(self):
        self.client.force_login(self.staff)
        resp = self.client.post(reverse("erp-integration-test", args=["demo_ping"]))
        self.assertRedirects(resp, reverse("erp-integrations") + "#check-demo-ping",
                             fetch_redirect_response=False)
        html = self.client.get(reverse("erp-integrations")).content.decode()
        self.assertIn("Passed", html)
        self.assertIn("Demo answered.", html)

    def test_unknown_target_is_404_and_writes_nothing(self):
        self.client.force_login(self.staff)
        resp = self.client.post(reverse("erp-integration-test", args=["nope"]))
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(IntegrationCheck.objects.exists())

    def test_get_is_not_allowed(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("erp-integration-test", args=["demo_ping"])).status_code, 405)

    @override_settings(DEMO_USERNAMES=["demo"])
    def test_public_demo_account_cannot_run_a_test(self):
        # The demo login is public: a test pings a provider with this
        # server's real keys, so DemoGuardMiddleware refuses it.
        demo = User.objects.create_user(username="demo", password="pw", is_staff=True)
        self.client.force_login(demo)
        self.assertEqual(self.client.get(reverse("erp-integrations")).status_code, 200)
        resp = self.client.post(reverse("erp-integration-test", args=["demo_ping"]),
                                HTTP_ACCEPT="application/json")
        self.assertIn(resp.status_code, (302, 403))
        self.assertFalse(IntegrationCheck.objects.exists())

    def test_non_staff_cannot_run_a_test(self):
        resp = self.client.post(reverse("erp-integration-test", args=["demo_ping"]))
        self.assertEqual(resp.status_code, 302)
        self.client.force_login(self.plain)
        resp = self.client.post(reverse("erp-integration-test", args=["demo_ping"]))
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(IntegrationCheck.objects.exists())
