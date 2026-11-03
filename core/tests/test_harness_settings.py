"""The release-harness settings and URLconf (docs/release-testing.md).

- ``config.settings.harness`` refuses to import unless GOLDEN_HARNESS=1 (or
  DEBUG), ALLOWED_HOSTS is loopback only and DATABASE_URL is Postgres; each
  refusal is one line naming what is missing and pointing at the docs.
- Prod and dev settings resolve ``config.urls``, which mounts none of the
  harness routes.
- The harness routes do what the harness relies on: the raising view sits
  behind a redirect, the webhook receiver dedupes by delivery id, and a token
  minted by the stub identity provider logs a customer into the portal
  through the real ``SupabaseJWT`` provider (never trusted).
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import jwt
from django.conf import settings
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import Resolver404, resolve

from config import harness_views

ROOT = Path(settings.BASE_DIR)
HARNESS_PATHS = (
    "/harness/boom/", "/harness/boom/raise/", "/harness/webhooks/",
    "/harness/idp/.well-known/jwks.json", "/harness/idp/token/",
    "/harness/idp/auth/v1/admin/users/x",
)

IDP = {
    "HARNESS_IDP_URL": "http://testserver/harness/idp",
    "HARNESS_IDP_ISSUER": "http://testserver/harness/idp",
    "HARNESS_IDP_JWKS_URL": "http://testserver/harness/idp/.well-known/jwks.json",
    "HARNESS_IDP_SERVICE_KEY": "harness-stub-service-key",
    "HARNESS_IDP_PROVIDER": "harness-stub",
    "HARNESS_WEBHOOK_LOG": None,
}


def _import_harness(**env):
    """Import the module in a subprocess with exactly this environment; return (rc, out+err)."""
    base = {k: v for k, v in os.environ.items()
            if k not in ("DEBUG", "GOLDEN_HARNESS", "ALLOWED_HOSTS", "DATABASE_URL", "DJANGO_SETTINGS_MODULE")}
    # python-dotenv never overrides a variable that is set, even to "": a
    # developer's .env cannot leak a DATABASE_URL into the "missing" case.
    base.update({"DATABASE_URL": "", "ALLOWED_HOSTS": ""})
    base.update(env)
    code = (
        "import config.settings.harness as s;"
        "print('ROOT_URLCONF=' + s.ROOT_URLCONF);"
        "print('PORTAL=' + str(s.GOLDEN_PORTAL_COMPANY));"
        "print('TRUSTED=' + str([p.get('trusted') for p in s.GOLDEN_IDENTITY_PROVIDERS]));"
        "from core.integrations import transport as t;"
        "print('TRANSPORT=' + type(t.get_transport()).__name__)"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=base, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


class HarnessSettingsGuardTests(SimpleTestCase):
    def test_refuses_without_the_flag(self):
        rc, out = _import_harness(DATABASE_URL="postgres://u:p@127.0.0.1/db")
        self.assertNotEqual(rc, 0)
        self.assertIn("GOLDEN_HARNESS=1", out)
        self.assertIn("docs/release-testing.md", out)

    def test_refuses_a_non_loopback_host(self):
        rc, out = _import_harness(GOLDEN_HARNESS="1", ALLOWED_HOSTS="127.0.0.1,erp.example.com",
                                  DATABASE_URL="postgres://u:p@127.0.0.1/db")
        self.assertNotEqual(rc, 0)
        self.assertIn("erp.example.com", out)
        self.assertIn("loopback", out)

    def test_refuses_sqlite(self):
        rc, out = _import_harness(GOLDEN_HARNESS="1")
        self.assertNotEqual(rc, 0)
        self.assertIn("DATABASE_URL", out)
        self.assertIn("Postgres", out)

    def test_loads_with_the_flag_loopback_and_postgres(self):
        rc, out = _import_harness(GOLDEN_HARNESS="1", ALLOWED_HOSTS="localhost,127.0.0.1",
                                  DATABASE_URL="postgres://u:p@127.0.0.1/db")
        self.assertEqual(rc, 0, out)
        self.assertIn("ROOT_URLCONF=config.harness_urls", out)
        self.assertIn("PORTAL=0c0ffee0-4e1e-4a5e-9a11-0000000d0001", out)
        self.assertIn("TRUSTED=[False]", out)
        # The offline provider stub is installed for the whole server process (R10.07).
        self.assertIn("TRANSPORT=HarnessStubTransport", out)

    def test_debug_flag_also_admits(self):
        rc, out = _import_harness(DEBUG="1", DATABASE_URL="postgres://u:p@127.0.0.1/db")
        self.assertEqual(rc, 0, out)


class ProdMountsNoHarnessUrlsTests(SimpleTestCase):
    def test_prod_and_dev_root_urlconf_is_config_urls(self):
        for name in ("config.settings.base", "config.settings.prod"):
            with self.subTest(name):
                self.assertEqual(importlib.import_module(name).ROOT_URLCONF, "config.urls")

    def test_config_urls_and_golden_urls_resolve_no_harness_route(self):
        for urlconf in ("config.urls", "golden_erp.urls"):
            for path in HARNESS_PATHS:
                with self.subTest(urlconf=urlconf, path=path), self.assertRaises(Resolver404):
                    resolve(path, urlconf=urlconf)

    def test_shipped_code_never_names_the_harness_urlconf(self):
        hits = []
        for py in ROOT.glob("golden_erp/**/*.py"):
            if "harness_urls" in py.read_text(encoding="utf-8"):
                hits.append(str(py.relative_to(ROOT)))
        self.assertEqual(hits, [])


@override_settings(ROOT_URLCONF="config.harness_urls", **IDP)
class HarnessViewTests(TestCase):
    def test_boom_is_a_redirect_to_a_view_that_raises(self):
        response = self.client.get("/harness/boom/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/harness/boom/raise/")
        quiet = Client(raise_request_exception=False)
        with self.assertLogs("django.request", "ERROR") as logs:
            response = quiet.get("/harness/boom/raise/")
        self.assertEqual(response.status_code, 500)
        self.assertTrue(any("harness boom" in line for line in logs.output), logs.output)

    def test_webhook_receiver_dedupes_by_delivery_id(self):
        harness_views.receiver.clear()
        self.addCleanup(harness_views.receiver.clear)
        post = lambda did, body: self.client.post(  # noqa: E731
            "/harness/webhooks/", data=json.dumps(body), content_type="application/json",
            headers={"X-Golden-Delivery-Id": did, "X-Golden-Event": "order.created"})
        first = post("d-1", {"n": 1}).json()
        again = post("d-1", {"n": 1}).json()
        other = post("d-2", {"n": 2}).json()
        self.assertEqual((first["duplicate"], again["duplicate"], other["duplicate"]), (False, True, False))
        listing = self.client.get("/harness/webhooks/").json()
        self.assertEqual(listing["count"], 2)
        self.assertEqual(listing["repeats"], {"d-1": 1})
        self.assertEqual(listing["deliveries"][0]["event"], "order.created")
        self.assertEqual(self.client.delete("/harness/webhooks/").status_code, 204)
        self.assertEqual(self.client.get("/harness/webhooks/").json()["count"], 0)

    def test_webhook_receiver_appends_to_the_log_file(self):
        harness_views.receiver.clear()
        self.addCleanup(harness_views.receiver.clear)
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "webhooks.jsonl")
            with override_settings(HARNESS_WEBHOOK_LOG=path):
                self.client.post("/harness/webhooks/", data="{}", content_type="application/json",
                                 headers={"X-Golden-Delivery-Id": "d-9"})
            lines = Path(path).read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["delivery_id"], "d-9")

    def test_state_endpoint_reads_rows_and_the_gl_summary(self):
        from core.models import Company

        company = Company.objects.create(name="State Co")
        rows = self.client.get("/harness/state/core.Company/", {"name": "State Co"}).json()
        self.assertEqual(rows["count"], 1)
        self.assertEqual(rows["rows"][0]["id"], str(company.pk))
        self.assertEqual(self.client.get("/harness/state/nope.Nope/").status_code, 404)
        self.assertEqual(self.client.get("/harness/state/core.Company/", {"no_such_field": 1}).status_code, 400)
        gl = self.client.get("/harness/state/gl/", {"company": str(company.pk)}).json()
        self.assertTrue(gl["balanced"])
        self.assertEqual(gl["entries"], [])

    def test_stub_idp_jwks_signs_minted_tokens_and_admin_api_needs_the_key(self):
        minted = self.client.post("/harness/idp/token/", data=json.dumps({"email": "pat@example.com"}),
                                  content_type="application/json").json()
        jwks = self.client.get("/harness/idp/.well-known/jwks.json").json()
        key = jwt.PyJWK(jwks["keys"][0])
        claims = jwt.decode(minted["token"], key.key, algorithms=["RS256"], audience="authenticated",
                            issuer="http://testserver/harness/idp")
        self.assertEqual(claims["sub"], minted["subject"])
        self.assertEqual(claims["email"], "pat@example.com")
        url = f"/harness/idp/auth/v1/admin/users/{minted['subject']}"
        self.assertEqual(self.client.get(url).status_code, 401)
        good = {"apikey": "harness-stub-service-key", "Authorization": "Bearer harness-stub-service-key"}
        self.assertEqual(self.client.get(url, headers=good).json()["email"], "pat@example.com")
        self.assertEqual(self.client.get("/harness/idp/auth/v1/admin/users/nobody", headers=good).status_code, 404)

    def test_a_stub_token_logs_a_customer_into_the_portal(self):
        """The real provider, pointed at the stub: JWKS verify, Admin API confirm, self-register."""
        from core.identity import providers as registry
        from core.identity.models import UserIdentity
        from core.tests.helpers import portal_tenant
        from customers.models import Customer

        portal_tenant()
        provider = {
            "name": "harness-stub", "backend": "core.identity.providers.SupabaseJWT",
            "entry_points": ["portal"], "self_register": True, "roles_on_create": ["customer"],
            "link_by_verified_email": True, "trusted": False,
            "options": {"jwks_url_setting": "HARNESS_IDP_JWKS_URL", "issuer_setting": "HARNESS_IDP_ISSUER",
                        "audience": "authenticated", "url_setting": "HARNESS_IDP_URL",
                        "service_role_key_setting": "HARNESS_IDP_SERVICE_KEY"},
        }
        client = self.client

        # No live sockets in the suite: the provider's JWKS fetch and Admin API
        # call are routed to the test client (the harness server answers them
        # over loopback for real).
        def fetch_data(jwk_client):
            jwks = client.get("/harness/idp/.well-known/jwks.json").json()
            jwk_client.jwk_set_cache.put(jwks)
            return jwks

        def get(url, headers=None, **kwargs):
            response = client.get(url.replace("http://testserver", ""), headers=headers or {})
            return mock.Mock(status_code=response.status_code, json=mock.Mock(return_value=response.json()))

        registry._jwks_clients.clear()
        self.addCleanup(registry._jwks_clients.clear)
        with override_settings(GOLDEN_IDENTITY_PROVIDERS=[provider]), \
                mock.patch.object(jwt.PyJWKClient, "fetch_data", autospec=True, side_effect=fetch_data), \
                mock.patch("httpx.get", side_effect=get):
            minted = client.post("/harness/idp/token/", data=json.dumps({"email": "new.customer@example.com"}),
                                 content_type="application/json").json()
            response = client.get("/api/v1/portal/customer/orders/",
                                  headers={"Authorization": f"Bearer {minted['token']}"})
        self.assertEqual(response.status_code, 200, response.content[:300])
        customer = Customer.objects.get(email="new.customer@example.com")
        identity = UserIdentity.objects.get(user=customer.user)
        self.assertEqual((identity.provider, identity.subject), ("harness-stub", minted["subject"]))
        self.assertFalse(customer.user.is_staff)


class StubTransportTests(SimpleTestCase):
    """The fixture provider's API answers locally; nothing else is reached (R10.07)."""

    def test_fixture_host_answers_and_other_hosts_are_refused(self):
        from config.harness_transport import FIXTURE_HOST, HarnessStubTransport
        from core.integrations.transport import TransportError, get_transport, use_transport

        stub = HarnessStubTransport()
        auth = {"Authorization": "Bearer harness-token"}
        me = stub.request("GET", f"https://{FIXTURE_HOST}/v1/me", headers=auth)
        self.assertEqual(me.status, 200)
        self.assertIn("workspace", me.json())
        self.assertEqual(stub.request("GET", f"https://{FIXTURE_HOST}/v1/me", headers={}).status, 401)
        created = stub.request("POST", f"https://{FIXTURE_HOST}/v1/contacts", headers={**auth, "Idempotency-Key": "p1"},
                               json={"email": "a@example.com"})
        self.assertTrue(created.json()["id"].startswith("c_"))
        with self.assertRaises(TransportError):
            stub.request("GET", "https://api.github.com/", headers=auth)
        with use_transport(stub):
            self.assertIs(get_transport(), stub)

    def test_the_fixture_adapter_test_connection_passes_through_the_stub(self):
        from config.harness_transport import HarnessStubTransport
        from core.models import Company, IntegrationAccount
        from golden_erp.addons.fixture_crm.adapter import FixtureCrmAdapter

        stub = HarnessStubTransport()
        account = IntegrationAccount(company=Company(name="H"), provider=FixtureCrmAdapter.provider_id)
        adapter = FixtureCrmAdapter(account, stub)
        with mock.patch.object(adapter, "_auth", return_value={"Authorization": "Bearer x"}):
            result = adapter.test_connection()
        self.assertTrue(result["ok"])
        self.assertEqual(result["what"], "Connected.")
