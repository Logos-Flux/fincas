"""The Google Ads adapter beyond the fixture cases: the live sequence (win,
replay, reopen, re-win), outcome codes, the dry run, consent, erasure,
tenancy, the ``attribution`` scope and secrets never in output."""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from core import outbox
from core.integrations import accounts
from core.integrations.transport import use_transport
from core.models import ExternalRef, IntegrationAccount, OutboxEvent
from core.outbox.testing import drain
from core.testing.integration_fixtures import FixtureTransport, fixture_secret, load_json
from crm import engagements, touchpoints
from crm.tests.helpers import CrmTestCase, person
from parties.models import PartyEmail

from .. import values
from ..adapter import GoogleAdsAdapter

PLUGINS = ["golden_erp.addons.google_ads.plugin.GoogleAdsPlugin"]
NOW = datetime(2026, 3, 2, 15, 0, tzinfo=dt_timezone.utc)
FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
SETTINGS = {"customer_id": "123-456-7890", "conversion_actions": {"sales": "555000111"}}
TOKEN = {"schema_version": 1, "provider": "google_ads", "request": {"method": "POST", "path": "/token"},
         "payload": {"access_token": "fixture-access-1", "expires_in": 3600}, "repeatable": True}
UPLOAD = "/v25/customers/1234567890:uploadClickConversions"
ADJUST = "/v25/customers/1234567890:uploadConversionAdjustments"


def ok(path, n=1):
    return {"schema_version": 1, "provider": "google_ads", "request": {"method": "POST", "path": path},
            "payload": {"results": [{} for _ in range(n)]}}


def connect(company, settings=SETTINGS):
    account = IntegrationAccount.objects.create(company=company, provider=values.PROVIDER_ID,
                                                settings=settings, enabled=True)
    for kind in sorted(values.CREDENTIAL_KINDS):
        accounts.set_credential(account, kind, fixture_secret(kind))
    return account


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class _Base(CrmTestCase):
    def setUp(self):
        patcher = mock.patch("django.utils.timezone.now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.account = connect(self.company)

    def deal(self, *, party=None, click="Cj0KFIXTUREgclid01", kind="gclid", days=10, value=Decimal("1200.00"),
             currency="USD", company=None):
        company = company or self.company
        party = party or self.ada
        touch = None
        if click:
            touch, _ = touchpoints.record(company, party=party, source="api", occurred_at=NOW - timedelta(days=days),
                                          click_id_kind=kind, click_id=click)
        return engagements.create(company=company, party=party, first_touch=touch, last_touch=touch,
                                  expected_value=value, currency=currency)

    def run_outbox(self, responses):
        from core.integrations import ratelimit
        from core.testing.integration_fixtures import unlimited_buckets

        ratelimit.reset()
        transport = FixtureTransport(responses=responses)
        with use_transport(transport), unlimited_buckets():
            drain(values.CONSUMER)
        return transport

    def rows(self, **kw):
        return OutboxEvent.objects.filter(consumer=values.CONSUMER, **kw)


class LiveSequenceTests(_Base):
    def test_win_replay_reopen_rewin(self):
        eng = self.deal()
        engagements.win(eng)
        self.assertEqual(self.rows().count(), 1)
        sent = self.run_outbox([TOKEN, ok(UPLOAD)])
        uploads = [r for r in sent.recorded if r["path"] == UPLOAD]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(len(uploads[0]["json"]["conversions"]), 1)
        first_body = json.dumps(uploads[0]["json"], sort_keys=True)
        ref = ExternalRef.objects.get(source=values.PROVIDER_ID)
        self.assertEqual((ref.source_id, ref.object_id), (eng.number, eng.pk))

        # A replay (requeued after success, or a lease lost after the upload) sends nothing.
        outbox.requeue(consumer=values.CONSUMER)
        self.assertEqual(self.run_outbox([]).recorded, [])
        self.assertEqual(self.rows().get().state, "done")

        # The same fact again is no new row; the body is byte-identical when rebuilt.
        from .. import conversions

        plan = conversions.plan_upload(self.rows().get(), SETTINGS)
        self.assertEqual(plan.skip, "uploaded")
        ExternalRef.objects.all().delete()
        rebuilt = conversions.plan_upload(self.rows().get(), SETTINGS)
        self.assertEqual(json.dumps({"conversions": [rebuilt.row], "partialFailure": True}, sort_keys=True),
                         first_body)
        ref.save()  # restore (pk kept)

        eng.refresh_from_db()
        engagements.reopen(eng, source="system")
        retract = self.rows(event=values.RETRACT).get()
        self.assertIsNone(retract.party_id)  # a retraction names nobody: it survives an erasure
        sent = self.run_outbox([TOKEN, ok(ADJUST)])
        self.assertEqual([r["json"]["conversionAdjustments"][0]["adjustmentType"] for r in sent.recorded
                          if r["path"] == ADJUST], ["RETRACTION"])
        eng.refresh_from_db()
        engagements.win(eng)
        self.assertEqual(self.run_outbox([]).recorded, [])
        # The clock is pinned, so created_at ties: compare the set, not "the latest".
        self.assertEqual(sorted(self.rows(event=values.UPLOAD).values_list("state", "last_error")),
                         [("cancelled", values.NEEDS_REVIEW), ("done", "")])

    def test_a_reopen_before_any_upload_enqueues_no_retraction(self):
        eng = self.deal()
        engagements.win(eng)
        eng.refresh_from_db()
        engagements.reopen(eng, source="system")
        self.assertFalse(self.rows(event=values.RETRACT).exists())
        self.assertEqual(self.run_outbox([]).recorded, [])
        self.assertEqual(self.rows().get().last_error, values.NOT_WON)

    def test_skip_codes(self):
        for kwargs, code in (({"click": ""}, values.NO_CLICK), ({"kind": "fbclid"}, values.NO_CLICK),
                             ({"days": 120}, values.CLICK_TOO_OLD),  # the last touch, however old
                             ({"days": 90, "currency": "EUR"}, None)):
            with self.subTest(kwargs=kwargs):
                OutboxEvent.objects.all().delete()
                engagements.win(self.deal(party=person(self.company, "P", str(code)), **kwargs))
                self.run_outbox([TOKEN, ok(UPLOAD)])
                row = self.rows().get()
                self.assertEqual(row.last_error if code else row.state, code or "done")

    def test_partial_failure_keeps_codes_never_messages(self):
        for n in range(3):
            engagements.win(self.deal(party=person(self.company, f"P{n}", "X"), click=f"Cj0KFIXTUREgclid0{n}"))
        response = load_json(FIXTURES / "batch-partial-failure" / "responses" / "002.json")
        with self.assertLogs("golden_erp", logging.DEBUG) as logs:
            logging.getLogger("golden_erp").debug("start")
            self.run_outbox([TOKEN, response])
        states = sorted(self.rows().values_list("state", "last_error"))
        self.assertEqual(states, [("dead", "CLICK_NOT_FOUND"), ("done", ""), ("pending", "CONCURRENT_MODIFICATION")])
        stored = json.dumps(list(self.rows().values("payload", "last_error")), default=str)
        self.assertNotIn("example.com", stored)
        self.assertNotIn("example.com", "\n".join(logs.output))


class LostResponseTests(_Base):
    def fail_with(self, code):
        return {"schema_version": 1, "provider": "google_ads", "request": {"method": "POST", "path": UPLOAD},
                "payload": {"results": [{}], "partialFailureError": {"details": [{"errors": [
                    {"errorCode": {"conversionUploadError": code},
                     "location": {"fieldPathElements": [{"fieldName": "conversions", "index": 0}]}}]}]}}}

    def test_already_exists_after_a_lost_response_is_done_and_recorded(self):
        eng = self.deal()
        engagements.win(eng)
        self.run_outbox([TOKEN, self.fail_with("CLICK_CONVERSION_ALREADY_EXISTS")])
        self.assertEqual(self.rows().get().state, "done")
        self.assertTrue(ExternalRef.objects.filter(source=values.PROVIDER_ID, source_id=eng.number).exists())
        eng.refresh_from_db()
        engagements.reopen(eng, source="system")
        self.assertTrue(self.rows(event=values.RETRACT).exists())  # the reopen can still retract

    def test_a_reopen_during_the_upload_still_retracts(self):
        from .. import conversions

        eng = self.deal()
        engagements.win(eng)
        real = conversions.record_upload

        def reopen_first(event, planned):
            fresh = type(eng).objects.get(pk=eng.pk)
            engagements.reopen(fresh, source="system")  # committed while the request was in flight
            return real(event, planned)

        with mock.patch.object(conversions, "record_upload", side_effect=reopen_first):
            sent = self.run_outbox([TOKEN, ok(UPLOAD), ok(ADJUST)])
        self.assertEqual([r["path"] for r in sent.recorded], ["/token", UPLOAD, "/token", ADJUST])
        self.assertEqual(self.rows(event=values.RETRACT).get().state, "done")

    def test_two_rows_for_one_order_send_it_once(self):
        eng = self.deal()
        engagements.win(eng)
        eng.refresh_from_db()
        engagements.reopen(eng, source="system")  # before any upload: no retraction
        eng.refresh_from_db()
        engagements.win(eng)
        sent = self.run_outbox([TOKEN, ok(UPLOAD)])
        bodies = [r["json"]["conversions"] for r in sent.recorded if r["path"] == UPLOAD]
        self.assertEqual([len(b) for b in bodies], [1])
        self.assertEqual(sorted(self.rows().values_list("state", flat=True)), ["done", "done"])


class SecretsAndOutputTests(_Base):
    def test_credentials_never_reach_rows_logs_requests_or_errors(self):
        engagements.win(self.deal())
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("golden_erp").debug("start")
            sent = self.run_outbox([TOKEN, ok(UPLOAD)])
        secrets = [fixture_secret(k) for k in values.CREDENTIAL_KINDS] + ["fixture-client-secret-1",
                                                                            "fixture-access-1"]
        haystack = "\n".join([
            json.dumps(sent.recorded, default=str), "\n".join(logs.output),
            json.dumps(list(OutboxEvent.objects.values()), default=str),
            json.dumps(list(ExternalRef.objects.values("metadata", "source_id")), default=str),
        ])
        for secret in secrets:
            self.assertNotIn(secret, haystack)

    def test_the_dry_run_prints_the_fixture_upload_body(self):
        engagements.win(self.deal())
        out = io.StringIO()
        call_command("ads_conversions", "--account", str(self.account.pk), "--dry-run", stdout=out)
        printed = json.loads(out.getvalue())
        expected = load_json(FIXTURES / "won-with-gclid" / "requests.expected.json")[1]["json"]
        self.assertEqual(printed["upload"], [expected])
        self.assertEqual((printed["skipped"], printed[values.VALUE_UNKNOWN]), ([], []))
        self.assertNotIn("fixture-", out.getvalue())

    def test_dry_run_flags_value_unknown_and_lists_skips(self):
        engagements.win(self.deal(value=None))
        engagements.win(self.deal(party=person(self.company, "No", "Click"), click=""))
        out = io.StringIO()
        call_command("ads_conversions", "--account", str(self.account.pk), "--dry-run", stdout=out)
        printed = json.loads(out.getvalue())
        self.assertNotIn("conversionValue", printed["upload"][0]["conversions"][0])
        self.assertEqual(printed[values.VALUE_UNKNOWN], ["EN-00001"])
        self.assertEqual([s["reason"] for s in printed["skipped"]], [values.NO_CLICK])


class ConsentTests(_Base):
    def test_default_unspecified_and_per_account_override(self):
        engagements.win(self.deal())
        sent = self.run_outbox([TOKEN, ok(UPLOAD)])
        self.assertEqual(sent.recorded[1]["json"]["conversions"][0]["consent"], {"adUserData": "UNSPECIFIED"})
        self.account.settings = {**SETTINGS, "ad_user_data_consent": "GRANTED"}
        self.account.save()
        engagements.win(self.deal(party=person(self.company, "Grace", "Hopper"), click="Cj0KFIXTUREgclid02"))
        sent = self.run_outbox([TOKEN, ok(UPLOAD)])
        self.assertEqual(sent.recorded[1]["json"]["conversions"][0]["consent"], {"adUserData": "GRANTED"})

    def test_settings_validation(self):
        adapter = GoogleAdsAdapter(self.account, FixtureTransport())
        self.assertEqual(adapter.validate_settings(SETTINGS), [])
        errors = adapter.validate_settings({**SETTINGS, "ad_user_data_consent": "maybe", "bogus": 1})
        self.assertTrue(any(e.startswith("ad_user_data_consent") for e in errors))
        self.assertTrue(any("bogus" in e for e in errors))


class ErasureAndTenancyTests(_Base):
    def test_an_erased_party_is_never_uploaded_and_pending_rows_are_purged(self):
        from parties.erasure import erase_party

        eng = self.deal()
        engagements.win(eng)
        erase_party(self.ada, reason="test")
        self.assertFalse(self.rows().exists())  # erase_party deleted the pending upload
        self.assertIsNone(touchpoints.google_click(type(eng).objects.get(pk=eng.pk)))  # click id scrubbed
        # A row claimed before the erasure finished still sends nothing.
        outbox.enqueue(values.CONSUMER, outbox.EventSpec(
            idempotency_key="conv:late", event=values.UPLOAD, payload={}, company_id=self.company.pk,
            object_type="crm.engagement", object_id=eng.pk, party_id=self.ada.pk))
        self.assertEqual(self.run_outbox([]).recorded, [])
        self.assertEqual(self.rows().get().last_error, values.ERASED)
        # Step 8's hook.
        outbox.enqueue(values.CONSUMER, outbox.EventSpec(
            idempotency_key="conv:later", event=values.UPLOAD, payload={}, company_id=self.company.pk,
            object_id=eng.pk, party_id=self.ada.pk))
        self.assertEqual(GoogleAdsAdapter.purge_parties(self.company.pk, [self.ada.pk]), 1)
        self.assertEqual(GoogleAdsAdapter(self.account, FixtureTransport()).erasure_refs(None), [])

    def test_a_tenant_without_an_account_writes_nothing_and_batches_never_mix_tenants(self):
        other_party = person(self.other_company, "Bob", "Other")
        engagements.win(self.deal(company=self.other_company, party=other_party))
        self.assertFalse(self.rows().exists())
        connect(self.other_company)
        engagements.win(self.deal(company=self.other_company, party=person(self.other_company, "Cy", "B"),
                                  click="Cj0KFIXTUREgclid09"))
        engagements.win(self.deal())
        sent = self.run_outbox([TOKEN, ok(UPLOAD), TOKEN, ok(UPLOAD)])
        uploads = [r["json"]["conversions"] for r in sent.recorded if r["path"] == UPLOAD]
        self.assertEqual(sorted(len(u) for u in uploads), [1, 1])
        self.assertEqual(ExternalRef.objects.filter(source=values.PROVIDER_ID).count(), 2)
        for ref in ExternalRef.objects.filter(source=values.PROVIDER_ID):
            self.assertEqual(ref.company_id, type(ref.target).objects.get(pk=ref.object_id).company_id)

    def test_a_disabled_account_writes_no_row(self):
        self.account.enabled = False
        self.account.save()
        engagements.win(self.deal())
        self.assertFalse(self.rows().exists())


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class AttributionScopeTests(TestCase):
    def test_an_attribution_key_posts_touchpoints_and_nothing_else(self):
        from django.contrib.auth.models import Permission

        from core.models import Company
        from core.service_accounts import ensure_service_account, issue_api_key

        company = Company.objects.create(name="Scoped")
        ada = person(company)
        PartyEmail.objects.create(party=ada, email="ada@example.com")
        svc = ensure_service_account("gtm", company=company)
        for codename in ("add_touchpoint", "view_touchpoint", "view_engagement"):
            svc.user_permissions.add(Permission.objects.get(content_type__app_label="crm", codename=codename))
        _key, raw = issue_api_key(svc, name="gtm", scope="attribution")
        client = APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw}")
        response = client.post("/api/v1/crm/touchpoints/", {
            "email": "ada@example.com", "landing_url": "https://shop.example.com/?gclid=Cj0KFIXTUREgclid01",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["click_id_kind"], "gclid")
        # A write-only key learns nothing about who the email belongs to.
        writer = ensure_service_account("gtm-writer", company=company)
        writer.user_permissions.add(Permission.objects.get(content_type__app_label="crm",
                                                           codename="add_touchpoint"))
        _key, raw = issue_api_key(writer, name="gtm-writer", scope="attribution")
        blind = APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw}")
        for email in ("ada@example.com", "nobody@example.com"):
            response = blind.post("/api/v1/crm/touchpoints/", {"email": email}, format="json")
            self.assertEqual((response.status_code, response.json()), (202, {"status": "accepted"}))
        self.assertIn(client.get("/api/v1/crm/engagements/").status_code, (401, 403))
