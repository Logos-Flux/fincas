"""The inbound webhook endpoint (docs/crm/phase6-adapters.md 2.8; step 1 "done when").

valid 200 and one row; tampered 401; stale 401; replay 200 with no second
row; unknown, disabled or mismatched account 404; a body signed for tenant A
posted to tenant B's account is 401; the inbound row is drained into
``communications.ingest.record`` in the right tenant only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import timedelta

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from communications.models import Communication
from core.integrations import accounts as services
from core.models import OutboxEvent
from core.outbox.testing import drain
from core.testing.integration_fixtures import fixture_secret
from parties.models import Party, PartyEmail

from .test_integration_secrets import PLUGINS, IntegrationTestCase


def message(msg_id="fx_msg_9001", frm="ada@example.com"):
    return {"id": msg_id, "type": "message.received", "occurred_at": "2026-03-02T14:30:00Z", "from": frm,
            "to": ["sales@example.org"], "subject": "Hello", "body": "Hi", "thread": "fx_thr_9"}


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class WebhookEndpointTests(IntegrationTestCase):
    def setUp(self):
        self.acct = self.account(enabled=True)
        services.set_credential(self.acct, "webhook_secret", fixture_secret("webhook_secret"))
        self.other_acct = self.account(company=self.other, enabled=True)
        services.set_credential(self.other_acct, "webhook_secret", "fixture-webhook-secret-B")
        ada = Party.objects.create(company=self.company, kind=Party.Kind.PERSON, given_name="Ada",
                                   family_name="Lovelace", origin=Party.Origin.ERP)
        PartyEmail.objects.create(party=ada, email="ada@example.com", purpose="work")
        twin = Party.objects.create(company=self.other, kind=Party.Kind.PERSON, given_name="Ada",
                                    family_name="Twin", origin=Party.Origin.ERP)
        PartyEmail.objects.create(party=twin, email="ada@example.com", purpose="work")

    def post(self, account, payload=None, *, secret=None, ts=None, delivery="fx_dlv_1", body=None,
             provider=None, tamper=False):
        payload = payload or message()
        raw = body if body is not None else json.dumps(payload, sort_keys=True).encode()
        stamp = str(int((ts or timezone.now()).timestamp()))
        key = (secret or fixture_secret("webhook_secret")).encode()
        sig = hmac.new(key, stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
        if tamper:
            raw = raw.replace(b"Hello", b"Pay up")
        url = reverse("integration-webhook", args=[provider or account.provider, account.pk])
        return self.client.post(url, data=raw, content_type="application/json",
                                HTTP_X_FIXTURE_SIGNATURE=sig, HTTP_X_FIXTURE_TIMESTAMP=stamp,
                                HTTP_X_FIXTURE_DELIVERY=delivery)

    def rows(self):
        return OutboxEvent.objects.filter(consumer="fixture.inbound")

    def test_valid_is_200_and_one_row_in_the_accounts_company(self):
        response = self.post(self.acct)
        self.assertEqual(response.status_code, 200)
        row = self.rows().get()
        self.assertEqual((row.company_id, row.idempotency_key), (self.company.pk, f"{self.acct.pk}:fx_dlv_1"))
        self.assertEqual(row.payload["provider"], "fixture")
        self.assertEqual(row.payload["payload"]["id"], "fx_msg_9001")
        self.assertNotIn(fixture_secret("webhook_secret"), response.content.decode() + json.dumps(row.payload))

    def test_replay_is_200_with_no_second_row(self):
        self.assertEqual(self.post(self.acct).status_code, 200)
        replay = self.post(self.acct)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json()["duplicate"])
        self.assertEqual(self.rows().count(), 1)

    def test_without_a_delivery_id_the_body_hash_dedupes(self):
        self.post(self.acct, delivery="")
        self.post(self.acct, delivery="")
        row = self.rows().get()
        self.assertTrue(row.idempotency_key.startswith(f"{self.acct.pk}:sha256:"))

    def test_tampered_is_401_and_writes_nothing(self):
        with self.assertLogs("golden_erp.integrations.webhook", "WARNING") as logs:
            response = self.post(self.acct, tamper=True)
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.rows().exists())
        self.assertIn("bad_signature", logs.output[0])
        self.assertNotIn("Pay up", "\n".join(logs.output), "the body is never logged")

    def test_stale_and_future_timestamps_are_401(self):
        for ts in (timezone.now() - timedelta(seconds=301), timezone.now() + timedelta(seconds=60)):
            self.assertEqual(self.post(self.acct, ts=ts).status_code, 401)
        self.assertFalse(self.rows().exists())

    def test_missing_signature_is_401(self):
        url = reverse("integration-webhook", args=["fixture", self.acct.pk])
        response = self.client.post(url, data=b"{}", content_type="application/json")
        self.assertEqual(response.status_code, 401)

    def test_unknown_disabled_and_mismatched_accounts_are_the_same_404(self):
        unknown = self.client.post(reverse("integration-webhook", args=["fixture", uuid.uuid4()]),
                                   data=b"{}", content_type="application/json")
        wrong_provider = self.post(self.acct, provider="fixture_oauth")
        self.acct.enabled = False
        self.acct.save()
        disabled = self.post(self.acct)
        for response in (unknown, wrong_provider, disabled):
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json(), {"detail": "Not found."})
        self.assertFalse(self.rows().exists())

    def test_a_body_signed_for_tenant_a_posted_to_tenant_b_is_401(self):
        response = self.post(self.other_acct, secret=fixture_secret("webhook_secret"))
        self.assertEqual(response.status_code, 401)
        self.assertFalse(OutboxEvent.objects.filter(company=self.other).exists())

    def test_the_same_delivery_id_in_two_tenants_is_two_rows(self):
        self.assertEqual(self.post(self.acct).status_code, 200)
        response = self.post(self.other_acct, secret="fixture-webhook-secret-B")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["duplicate"])
        self.assertEqual(set(self.rows().values_list("company_id", flat=True)), {self.company.pk, self.other.pk})

    def test_get_is_405_and_csrf_is_not_required(self):
        url = reverse("integration-webhook", args=["fixture", self.acct.pk])
        self.assertEqual(self.client.get(url).status_code, 405)
        from django.test import Client

        strict = Client(enforce_csrf_checks=True)
        stamp = str(int(timezone.now().timestamp()))
        raw = json.dumps(message()).encode()
        sig = hmac.new(fixture_secret("webhook_secret").encode(), stamp.encode() + b"." + raw,
                       hashlib.sha256).hexdigest()
        response = strict.post(url, data=raw, content_type="application/json", HTTP_X_FIXTURE_SIGNATURE=sig,
                               HTTP_X_FIXTURE_TIMESTAMP=stamp)
        self.assertEqual(response.status_code, 200)

    def test_oversized_body_is_413(self):
        big = json.dumps({"x": "a" * (1024 * 1024)}).encode()
        self.assertEqual(self.post(self.acct, body=big).status_code, 413)

    def test_drained_row_lands_in_ingest_record_for_its_tenant_only(self):
        self.post(self.acct)
        drain()
        comm = Communication.objects.get(provider="fixture")
        self.assertEqual(comm.company, self.company)
        self.assertEqual(comm.party.family_name, "Lovelace")
        self.assertFalse(Communication.objects.filter(company=self.other).exists())
        self.assertEqual(self.rows().get().state, "done")
        # A replay after processing is acknowledged and changes nothing.
        self.assertEqual(self.post(self.acct).status_code, 200)
        drain()
        self.assertEqual(Communication.objects.filter(provider="fixture").count(), 1)
