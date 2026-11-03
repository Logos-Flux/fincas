"""A credential never leaves its ciphertext (docs/crm/phase6-adapters.md 2.7, step 1 "done when").

One test drives every surface that could carry a secret — the model's
``str``/``repr``, the admin change list and change page, the Integrations
page, the webhook and connection-test responses, ``AdapterError`` text from
failing calls, outbox rows, ``IntegrationCheck`` rows, ``AuditLog`` rows and
every log record emitted meanwhile — and greps all of it for the plaintext
and the ciphertext of every credential involved.
"""

from __future__ import annotations

import json
import logging
import secrets

from django.contrib.auth.models import Permission, User
from django.test import override_settings
from django.urls import reverse

from core.integrations import accounts as services
from core.integrations.transport import use_transport
from core.models import AuditLog, IntegrationCheck, IntegrationCredential, OutboxEvent, UserProfile
from core.outbox.testing import drain
from core.plugins.payroll import AdapterError
from core.testing.integration_fixtures import FixtureTransport, unlimited_buckets
from core.tests.helpers import make_staff_user
from golden_erp.addons.fixture_crm.adapter import FixtureCrmAdapter
from parties.models import Party, PartyEmail

from .test_integration_secrets import PLUGINS, IntegrationTestCase

# Built at runtime so a secret scanner never sees a key-shaped literal (fake values).
API_KEY = "fixture-LEAKCANARY-api-" + secrets.token_hex(4)
HOOK = "fixture-LEAKCANARY-hook-" + secrets.token_hex(4)


def answer(status, payload, method="GET", path="/v1/me", headers=None):
    return {"schema_version": 1, "provider": "fixture", "request": {"method": method, "path": path},
            "response": {"status": status, "headers": headers or {}}, "payload": payload}


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class NoLeakTests(IntegrationTestCase):
    def test_no_surface_carries_a_secret(self):
        texts: list[str] = []
        acct = self.account(enabled=True)
        services.set_credential(acct, "api_key", API_KEY)
        services.set_credential(acct, "webhook_secret", HOOK)
        creds = list(IntegrationCredential.objects.filter(account=acct))
        needles = [API_KEY, HOOK] + [c.ciphertext for c in creds]

        root = logging.getLogger()
        with self.assertLogs(root, "DEBUG") as logs, unlimited_buckets():
            # model text
            texts += [str(c) for c in creds] + [repr(c) for c in creds] + [str(acct), repr(acct)]

            # admin (superuser) and the Integrations page (staff with the change permission)
            admin_user = User.objects.create_superuser("root", "root@example.com", "pw")
            self.client.force_login(admin_user)
            texts.append(self.client.get(reverse("admin:core_integrationcredential_changelist")).content.decode())
            texts.append(self.client.get(
                reverse("admin:core_integrationcredential_change", args=[creds[0].pk])).content.decode())
            texts.append(self.client.get(
                reverse("admin:core_integrationaccount_change", args=[acct.pk])).content.decode())
            staff = make_staff_user("ann", password="pw")
            UserProfile.objects.create(user=staff, company=self.company, display_name="ann")
            staff.user_permissions.add(Permission.objects.get(codename="change_integrationaccount"))
            self.client.force_login(staff)
            texts.append(self.client.get(reverse("erp-integrations")).content.decode())

            # a connection test whose provider echoes the key, then one that is refused
            echo = answer(200, {"workspace": f"key={API_KEY}"})
            refused = answer(401, {"error": f"bad key {API_KEY}"})
            for response in (echo, refused):
                with use_transport(FixtureTransport(responses=[response])):
                    texts.append(self.client.post(reverse("erp-integration-account-test", args=[acct.pk]),
                                                  HTTP_ACCEPT="application/json").content.decode())
            texts.append(self.client.get(reverse("erp-integrations")).content.decode())
            acct.paused_until = None
            acct.save()

            # adapter errors from failing calls
            for response in (answer(401, {"e": API_KEY}), answer(422, {"e": API_KEY}),
                             answer(500, {"e": API_KEY}), answer(429, {"e": API_KEY}, headers={"Retry-After": "1"})):
                adapter = FixtureCrmAdapter(acct, FixtureTransport(responses=[response]))
                try:
                    adapter.http("GET", "/v1/me", headers=adapter._auth())
                except AdapterError as exc:
                    texts.append(str(exc))
                acct.paused_until = None
                acct.save()

            # webhooks: a bad signature and a good one
            url = reverse("integration-webhook", args=["fixture", acct.pk])
            texts.append(self.client.post(url, data=b"{}", content_type="application/json",
                                          HTTP_X_FIXTURE_SIGNATURE=HOOK, HTTP_X_FIXTURE_TIMESTAMP="1").content.decode())

            # a push that fails at the provider, through the outbox
            ada = Party.objects.create(company=self.company, kind=Party.Kind.PERSON, given_name="Ada",
                                       family_name="Lovelace", origin=Party.Origin.ERP)
            PartyEmail.objects.create(party=ada, email="ada@example.com", purpose="work")
            from core import outbox

            outbox.enqueue("fixture.contact", outbox.EventSpec(
                idempotency_key="leak:1", payload={"object_id": str(ada.pk)}, company_id=self.company.pk))
            push = FixtureTransport(responses=[
                answer(422, {"error": f"duplicate {API_KEY}"}, method="POST", path="/v1/contacts")])
            with use_transport(push):
                drain()
            sent = push.recorded
        texts += [json.dumps({"payload": o.payload, "error": o.last_error}, default=str)
                  for o in OutboxEvent.objects.all()]
        texts += [json.dumps({"what": c.what, "observed": c.observed, "class": c.error_class})
                  for c in IntegrationCheck.objects.all()]
        texts += [json.dumps(a.changes, default=str) for a in AuditLog.objects.all()]
        texts += logs.output
        self.assertTrue(OutboxEvent.objects.filter(consumer="fixture.contact", state="dead").exists())
        self.assertEqual(IntegrationCheck.objects.count(), 2)

        haystack = "\n".join(texts)
        for needle in needles:
            self.assertNotIn(needle, haystack)
        # Not vacuous: the key did go to the provider, as the redacted header shows.
        self.assertIn("Bearer <redacted:api_key>", json.dumps(sent))
