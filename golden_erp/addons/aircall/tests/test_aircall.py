"""Aircall behaviour beyond the fixture snapshots (docs/crm/phase6-adapters.md 3.4, step 5).

What the snapshot of the contract test does not show: duration, agent,
outcome, summary; where the recording link lives (raw payload only) and
that it is never requested; the token never stored; tenant isolation;
credentials never in output; ``touch_activity``; re-delivery no-ops;
erased numbers; the poll cursor; erasure.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from communications.models import Communication, CommunicationRaw
from core.integrations import accounts as services
from core.models import Company, ExternalRef, OutboxEvent, UserProfile
from core.outbox.testing import drain
from core.plugins.dispatch import suppressed
from core.testing.integration_fixtures import FixtureTransport, fixture_secret
from crm import engagements
from crm.models import Engagement
from parties.consent import address_hash
from parties.models import Party, PartyPhone, Suppression

from .. import values
from ..adapter import AircallAdapter
from .helpers import API_KEY, PLUGINS

T0 = 1772461200
REC = "https://media.example.com/aircall/recordings/1001.mp3?expires=1772470000"
VM = "https://media.example.com/aircall/voicemails/1001.mp3?expires=1772470000"
TOKEN = fixture_secret("webhook_secret")


def call(cid=1001, **kw):
    base = {"id": cid, "direction": "inbound", "status": "done", "missed_call_reason": None, "started_at": T0,
            "answered_at": T0 + 12, "ended_at": T0 + 400, "duration": 400, "raw_digits": "+1 202-555-0143",
            "number": {"id": 5101, "digits": "+1 202-555-0100"},
            "user": {"id": 7201, "name": "Sam Seller", "email": "sam@example.org"},
            "recording": REC, "voicemail": None, "asset": None, "comments": [], "tags": []}
    base.update(kw)
    return base


def item(data, event="call.ended", version=1):
    return {"schema_version": version, "provider": "aircall",
            "payload": {"resource": "call", "event": event, "timestamp": T0 + 410, "data": data}}


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class AircallTestCase(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Tenant A")
        self.other = Company.objects.create(name="Tenant B")
        self.acct = self.make_account(self.company, TOKEN)
        self.ada = Party.objects.create(company=self.company, kind=Party.Kind.PERSON, given_name="Ada",
                                        family_name="Lovelace", origin=Party.Origin.ERP)
        PartyPhone.objects.create(party=self.ada, number_raw="+1 202-555-0143", purpose="work")
        user = get_user_model().objects.create_user("sam", email="sam@example.org", password="x", is_staff=True)
        self.sam = UserProfile.objects.create(user=user, company=self.company, display_name="Sam")

    def make_account(self, company, token):
        acct = services.create_account(company, "aircall", settings={}, enabled=True)
        services.set_credential(acct, "api_key", API_KEY)
        services.set_credential(acct, "webhook_secret", token)
        return acct

    def adapter(self, account=None, transport=None):
        return AircallAdapter(account or self.acct, transport or FixtureTransport())

    def ingest(self, data, account=None, transport=None, **kw):
        from django.db import transaction

        from core import outbox

        adapter = self.adapter(account, transport)
        with outbox.origin("aircall"), transaction.atomic():
            return adapter.ingest(item(data, **kw))

    def post(self, account, body: dict, token=TOKEN):
        raw = json.dumps({**body, "token": token}, sort_keys=True).encode()
        url = reverse("integration-webhook", args=["aircall", account.pk])
        return self.client.post(url, data=raw, content_type="application/json")


class MappingAsBuiltTests(AircallTestCase):
    def test_answered_call_fields_agent_and_raw(self):
        transport = FixtureTransport()
        [outcome] = self.ingest(call(tags=[{"name": "quote"}]), transport=transport)
        self.assertTrue(outcome.created)
        comm = Communication.objects.get(company=self.company)
        self.assertEqual((comm.kind, comm.direction, comm.status), ("call", "inbound", "completed"))
        self.assertEqual(comm.duration_seconds, 400)
        self.assertEqual(comm.ended_at, datetime.fromtimestamp(T0 + 400, tz=dt_timezone.utc))
        self.assertEqual(comm.performed_by, self.sam)
        self.assertEqual(comm.party, self.ada)
        self.assertEqual(comm.metadata["aircall"]["outcome"], "answered")
        self.assertEqual(comm.metadata["aircall"]["tags"], ["quote"])
        self.assertTrue(comm.metadata["aircall"]["has_recording"])
        self.assertFalse(comm.is_editable)
        # The recording link: verbatim in the raw payload, nowhere else, never requested.
        raw = CommunicationRaw._base_manager.get(communication=comm)
        self.assertEqual(raw.payload["recording"], REC)
        self.assertEqual(raw.provider, "aircall")
        row = Communication.objects.filter(pk=comm.pk).values().get()
        self.assertNotIn("media.example.com", json.dumps(row, default=str))
        ref = ExternalRef.objects.get(company=self.company, source="aircall")
        self.assertNotIn("media.example.com", ref.source_url + json.dumps(ref.metadata))
        self.assertEqual(ref.source_url, "https://dashboard.aircall.io/conversations/1001")
        self.assertEqual(transport.recorded, [])

    def test_voicemail_is_failed_and_never_fetched(self):
        transport = FixtureTransport()
        self.ingest(call(answered_at=None, voicemail=VM, asset=VM, recording=None,
                         missed_call_reason="out_of_opening_hours"), transport=transport)
        comm = Communication.objects.get(company=self.company)
        self.assertEqual(comm.status, "failed")
        self.assertEqual(comm.metadata["aircall"]["outcome"], "voicemail")
        self.assertEqual(comm.metadata["aircall"]["missed_call_reason"], "out_of_opening_hours")
        self.assertEqual(transport.recorded, [])

    def test_unknown_number_is_party_less_and_creates_no_party(self):
        parties = Party.objects.count()
        self.ingest(call(raw_digits="+1 202-555-0188", user=None, answered_at=None,
                         missed_call_reason="agents_did_not_answer"))
        comm = Communication.objects.get(company=self.company)
        self.assertIsNone(comm.party_id)
        self.assertEqual(comm.status, "failed")
        self.assertIsNone(comm.performed_by_id)
        self.assertEqual(Party.objects.count(), parties)

    def test_archived_or_unknown_agent_is_not_performed_by(self):
        self.sam.user.is_active = False
        self.sam.user.save(update_fields=["is_active"])
        self.ingest(call())
        comm = Communication.objects.get(company=self.company)
        self.assertIsNone(comm.performed_by_id)
        self.assertTrue(comm.metadata["aircall"]["agent_unmatched"])
        self.ingest(call(1002, user={"id": 1, "email": "nobody@example.org"}))
        ref = ExternalRef.objects.get(company=self.company, source="aircall", source_id="1002")
        unknown = Communication.objects.get(pk=ref.object_id)
        self.assertIsNone(unknown.performed_by_id)
        self.assertTrue(unknown.metadata["aircall"]["agent_unmatched"])

    def test_agent_of_another_tenant_is_not_matched(self):
        user = get_user_model().objects.create_user("bo", email="bo@example.org", password="x", is_staff=True)
        UserProfile.objects.create(user=user, company=self.other, display_name="Bo")
        self.ingest(call(user={"id": 2, "email": "bo@example.org"}))
        self.assertIsNone(Communication.objects.get(company=self.company).performed_by_id)

    def test_comment_update_rewrites_summary_on_the_same_row(self):
        self.ingest(call())
        self.ingest(call(comments=[{"id": 1, "content": "Wants 40 panels.", "posted_at": T0 + 500}]),
                    event="call.commented")
        comm = Communication.objects.get(company=self.company)
        self.assertEqual(comm.summary, "Wants 40 panels.")
        self.assertEqual(Communication.objects.filter(company=self.company).count(), 1)

    def test_in_progress_call_writes_nothing(self):
        [outcome] = self.ingest(call(status="answered", ended_at=None))
        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.reason, "in_progress")
        self.assertFalse(Communication.objects.exists())


class RedeliveryTests(AircallTestCase):
    def test_re_signed_media_links_are_not_a_change(self):
        self.ingest(call())
        comm = Communication.objects.get()
        before = (comm.updated_at, list(comm.participants.values_list("pk", flat=True)))
        resigned = REC.replace("expires=1772470000", "expires=1772479999")
        [again] = self.ingest(call(recording=resigned, asset=resigned), event=values.POLL_EVENT)
        self.assertFalse(again.changed)
        comm.refresh_from_db()
        self.assertEqual((comm.updated_at, list(comm.participants.values_list("pk", flat=True))), before)

    def test_a_late_older_delivery_does_not_roll_the_summary_back(self):
        two = [{"id": 1, "content": "first", "posted_at": T0 + 5}, {"id": 2, "content": "second", "posted_at": T0 + 9}]
        self.ingest(call(comments=two), event="call.commented")
        [late] = self.ingest(call(comments=two[:1]), event="call.commented")
        self.assertEqual(late.reason, "older")
        [ended] = self.ingest(call())
        self.assertFalse(ended.changed)
        self.assertEqual(Communication.objects.get().summary, "first\nsecond")

    def test_a_real_change_still_updates(self):
        self.ingest(call())
        [again] = self.ingest(call(tags=[{"name": "vip"}]))
        self.assertTrue(again.changed)
        self.assertEqual(Communication.objects.get().metadata["aircall"]["tags"], ["vip"])

    def test_commented_without_comment_ids_falls_back_to_the_body_hash(self):
        a = {"resource": "call", "event": "call.commented", "timestamp": T0 + 1,
             "data": call(comments=[{"content": "a"}])}
        b = {**a, "timestamp": T0 + 2, "data": call(comments=[{"content": "a"}, {"content": "b"}])}
        self.post(self.acct, a)
        self.post(self.acct, b)
        self.assertEqual(OutboxEvent.objects.filter(consumer="aircall.inbound").count(), 2)
        self.assertTrue(all(":sha256:" in k for k in OutboxEvent.objects.values_list("idempotency_key", flat=True)))

    def test_same_call_id_from_webhook_then_poll_is_a_no_op(self):
        self.ingest(call())
        [again] = self.ingest(call(), event=values.POLL_EVENT)
        self.assertFalse(again.changed)
        self.assertEqual(Communication.objects.count(), 1)
        self.assertEqual(CommunicationRaw._base_manager.count(), 1)

    def test_replayed_webhook_one_row_one_communication(self):
        body = {"resource": "call", "event": "call.ended", "timestamp": T0 + 410, "data": call()}
        self.assertEqual(self.post(self.acct, body).status_code, 200)
        second = self.post(self.acct, body)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(OutboxEvent.objects.filter(consumer="aircall.inbound").count(), 1)
        drain()
        self.assertEqual(Communication.objects.count(), 1)

    def test_bad_token_and_malformed_body_refused(self):
        body = {"resource": "call", "event": "call.ended", "timestamp": T0, "data": call()}
        self.assertEqual(self.post(self.acct, body, token="wrong").status_code, 401)
        url = reverse("integration-webhook", args=["aircall", self.acct.pk])
        self.assertEqual(self.client.post(url, data=b"not json", content_type="application/json").status_code, 401)
        no_token = json.dumps(body).encode()
        self.assertEqual(self.client.post(url, data=no_token, content_type="application/json").status_code, 401)
        self.assertFalse(OutboxEvent.objects.exists())


class ErasureTests(AircallTestCase):
    def erase_number(self, number="+12025550143"):
        for channel in ("phone", "sms"):
            Suppression.objects.create(company=self.company, channel=channel, address_hash=address_hash(number),
                                       reason=Suppression.Reason.ERASURE, source="erp", source_detail="erasure")

    def test_erased_number_writes_nothing(self):
        self.erase_number()
        [outcome] = self.ingest(call())
        self.assertEqual(outcome.reason, "erased_number")
        self.assertFalse(Communication.objects.exists())
        self.assertFalse(CommunicationRaw._base_manager.exists())
        self.assertFalse(ExternalRef.objects.filter(source="aircall").exists())

    def test_erasure_marked_on_an_earlier_suppression_counts(self):
        Suppression.objects.create(company=self.company, channel="phone", address_hash=address_hash("+12025550143"),
                                   reason=Suppression.Reason.OPT_OUT, source="erp", source_detail="erasure; opt-out")
        self.assertEqual(self.ingest(call())[0].reason, "erased_number")

    def test_plain_opt_out_does_not_drop_the_call(self):
        Suppression.objects.create(company=self.company, channel="phone", address_hash=address_hash("+12025550143"),
                                   reason=Suppression.Reason.OPT_OUT, source="erp")
        self.assertTrue(self.ingest(call())[0].created)

    def test_number_erased_after_ingest_blocks_the_comment_update(self):
        self.ingest(call())
        self.erase_number()
        [outcome] = self.ingest(call(comments=[{"id": 1, "content": "x", "posted_at": T0}]), event="call.commented")
        self.assertEqual(outcome.reason, "erased_number")
        self.assertEqual(Communication.objects.get().summary, "")

    def test_collector_names_nothing_and_erase_sends_nothing(self):
        from core import outbox

        transport = FixtureTransport()
        adapter = self.adapter(transport=transport)
        self.assertEqual(adapter.erasure_refs(object()), [])
        self.assertIsInstance(adapter.erase([]), outbox.Done)
        self.assertEqual(transport.recorded, [])


class TenantIsolationTests(AircallTestCase):
    def test_other_tenants_token_is_refused_and_writes_nothing(self):
        other = self.make_account(self.other, "fixture-webhook-secret-B")
        body = {"resource": "call", "event": "call.ended", "timestamp": T0, "data": call()}
        self.assertEqual(self.post(self.acct, body, token="fixture-webhook-secret-B").status_code, 401)
        self.assertEqual(self.post(other, body, token=TOKEN).status_code, 401)
        self.assertEqual(self.post(other, body, token="fixture-webhook-secret-B").status_code, 200)
        drain()
        self.assertFalse(Communication.objects.filter(company=self.company).exists())
        comm = Communication.objects.get(company=self.other)
        self.assertIsNone(comm.party_id)  # Ada is tenant A's
        self.assertIsNone(comm.performed_by_id)  # so is Sam

    def test_same_call_id_in_two_tenants_is_two_communications(self):
        other = self.make_account(self.other, "fixture-webhook-secret-B")
        self.ingest(call())
        self.ingest(call(), account=other)
        self.assertEqual(Communication.objects.filter(company=self.company).count(), 1)
        self.assertEqual(Communication.objects.filter(company=self.other).count(), 1)
        self.assertEqual(Communication.objects.get(company=self.company).party, self.ada)


class SecretsTests(AircallTestCase):
    def test_token_is_never_stored(self):
        body = {"resource": "call", "event": "call.ended", "timestamp": T0, "data": call()}
        with self.assertLogs("golden_erp.integrations", level="INFO") as logs:
            self.post(self.acct, body)
            self.post(self.acct, body, token="a-guess")
        row = OutboxEvent.objects.get(consumer="aircall.inbound")
        self.assertNotIn("token", row.payload["payload"])
        self.assertNotIn(TOKEN, json.dumps(row.payload))
        drain()
        raw = CommunicationRaw._base_manager.get()
        self.assertNotIn(TOKEN, json.dumps(raw.payload))
        text = "\n".join(logs.output)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("a-guess", text)

    def test_basic_auth_is_redacted_in_every_recorded_request(self):
        import base64

        page = {"schema_version": 1, "provider": "aircall", "request": {"method": "GET", "path": "/v1/calls"},
                "payload": {"calls": [], "meta": {"next_page_link": None}}}
        transport = FixtureTransport(responses=[page])
        self.adapter(transport=transport).poll({"from": T0})
        [request] = transport.recorded
        self.assertEqual(request["headers"]["authorization"], "Basic <redacted:api_key>")
        encoded = base64.b64encode(API_KEY.encode()).decode()
        self.assertNotIn(encoded, repr(transport.recorded))
        self.assertNotIn("fixture-api-token-1", repr(transport.recorded))

    def test_half_a_key_is_an_adapter_error_without_the_secret(self):
        from core.plugins.crm_adapter import AdapterError

        services.set_credential(self.acct, "api_key", "only-the-token-xyz")
        with self.assertRaises(AdapterError) as ctx:
            self.adapter().poll({"from": T0})
        self.assertNotIn("only-the-token-xyz", str(ctx.exception))


class ActivityTests(AircallTestCase):
    def setUp(self):
        super().setUp()
        self.eng = engagements.create(company=self.company, party=self.ada)
        Engagement._base_manager.filter(pk=self.eng.pk).update(last_activity_at=None)

    def last(self):
        return Engagement._base_manager.get(pk=self.eng.pk).last_activity_at

    def test_live_answered_call_touches_its_engagement(self):
        self.ingest(call())
        comm = Communication.objects.get()
        self.assertEqual(comm.engagement_id, self.eng.pk)
        self.assertEqual(self.last(), comm.occurred_at)

    def test_missed_call_does_not_touch(self):
        self.ingest(call(answered_at=None, missed_call_reason="agents_did_not_answer"))
        self.assertIsNone(self.last())

    def test_first_sync_suppressed_still_touches_once(self):
        with suppressed():
            self.ingest(call())
        self.assertEqual(self.last(), Communication.objects.get().occurred_at)

    def test_backfill_never_moves_the_clock_back(self):
        later = timezone.now()
        Engagement._base_manager.filter(pk=self.eng.pk).update(last_activity_at=later)
        with suppressed():
            self.ingest(call())
        self.assertEqual(self.last(), later)


class PollCursorTests(AircallTestCase):
    def page(self, calls, *, query, next_link=None):
        return {"schema_version": 1, "provider": "aircall", "request": {"method": "GET", "path": "/v1/calls",
                                                                         "query": query},
                "payload": {"calls": calls, "meta": {"next_page_link": next_link}}}

    def q(self, start, page=1):
        return {"from": str(start), "order": "asc", "page": str(page), "per_page": "50"}

    def test_cursor_moves_to_last_call_when_nothing_is_in_progress(self):
        t = FixtureTransport(responses=[self.page([call(1, started_at=T0), call(2, started_at=T0 + 60)],
                                                  query=self.q(T0 - 10))])
        page = self.adapter(transport=t).poll({"from": T0 - 10})
        self.assertEqual(page.cursor, {"from": T0 + 60})
        self.assertFalse(page.more)
        self.assertEqual([i["payload"]["event"] for i in page.items], [values.POLL_EVENT] * 2)

    def test_first_poll_starts_at_since(self):
        since = datetime.fromtimestamp(T0, tz=dt_timezone.utc).isoformat()
        t = FixtureTransport(responses=[self.page([], query=self.q(T0))])
        self.assertEqual(self.adapter(transport=t).poll({"since": since}).cursor, {"from": T0})

    def test_stale_in_progress_call_does_not_hold_forever(self):
        old = int((timezone.now() - timedelta(days=2)).timestamp())
        t = FixtureTransport(responses=[self.page([call(1, started_at=old, status="answered"),
                                                   call(2, started_at=old + 60)], query=self.q(old))])
        self.assertEqual(self.adapter(transport=t).poll({"from": old}).cursor, {"from": old + 60})

    def test_capped_listing_moves_on_from_the_last_call(self):
        last_page = values.MAX_LISTING // values.PER_PAGE
        t = FixtureTransport(responses=[self.page([call(1, started_at=T0 + 5, status="answered")],
                                                  query=self.q(T0, last_page), next_link="https://x")])
        page = self.adapter(transport=t).poll({"from": T0, "page": last_page})
        self.assertEqual(page.cursor, {"from": T0 + 5})
        self.assertFalse(page.more)

    def test_validate_settings_refuses_unknown_keys(self):
        self.assertTrue(self.adapter().validate_settings({"list_id": "x"}))
        self.assertEqual(self.adapter().validate_settings({"initial_since": 30}), [])
