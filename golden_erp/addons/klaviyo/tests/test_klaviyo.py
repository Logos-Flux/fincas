"""Klaviyo adapter behaviour beyond the fixture cases (docs/crm/phase6-adapters.md 3.2, steps 3 and 3b).

Consent can never be granted by the provider; suppression goes both ways and
idempotently; an erased person is never re-pushed; events are idempotent;
tenants stay apart; the API key never reaches an output. Every request is
recorded by a ``FixtureTransport``; no socket is opened.
"""

from __future__ import annotations

import io
import json
import logging
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from core import outbox
from core.integrations import accounts as services
from core.integrations import ratelimit
from core.integrations.transport import use_transport
from core.models import Company, ExternalRef, IntegrationAccount, OutboxEvent, SavedFilter, UserProfile
from core.outbox.testing import drain
from core.testing.integration_fixtures import FixtureTransport, fixture_secret, unlimited_buckets
from parties.consent import address_hash, can_contact
from parties.models import ConsentRecord, Party, PartyEmail, Suppression

from .. import values
from ..adapter import KlaviyoAdapter

PLUGINS = ["golden_erp.addons.klaviyo.plugin.KlaviyoPlugin"]
API_KEY = fixture_secret("api_key")
LIST = "XFIX01"
SEG_LIST = "XFIX02"
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=dt_timezone.utc)


def ok(method, path, status=202, payload=None, repeatable=True):
    return {"request": {"method": method, "path": path}, "response": {"status": status},
            "payload": payload, "repeatable": repeatable}


def imported(profile_id):
    return ok("POST", values.PROFILE_IMPORT, 201, {"data": {"type": "profile", "id": profile_id}})


JOBS = [ok("POST", values.SUBSCRIBE), ok("POST", values.UNSUBSCRIBE), ok("POST", values.SUPPRESS),
        ok("POST", values.DELETION), ok("DELETE", values.LIST_PROFILES.format(list_id=SEG_LIST), 204),
        ok("POST", values.LIST_PROFILES.format(list_id=SEG_LIST), 204)]


def item(event_id, metric, profile_id, email, *, when=T0, props=None, version=1):
    return {"schema_version": version, "provider": "klaviyo", "payload": {
        "event": {"type": "event", "id": event_id,
                  "attributes": {"datetime": when.isoformat(), "event_properties": props or {}}},
        "metric": {"type": "metric", "id": "m", "attributes": {"name": metric}},
        "profile": {"type": "profile", "id": profile_id, "attributes": {"email": email}},
    }}


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class KlaviyoTestCase(TestCase):
    def setUp(self):
        ratelimit.reset()
        buckets = unlimited_buckets()  # pacing has its own tests (core); here every request is sent
        buckets.__enter__()
        self.addCleanup(buckets.__exit__, None, None, None)
        self.company = Company.objects.create(name="Tenant A")
        self.account = self.make_account(self.company)
        self.transport = FixtureTransport(responses=[imported("01FIXTUREPROFILEADA0000000")] + JOBS)

    def make_account(self, company, settings=None):
        account = IntegrationAccount.objects.create(
            company=company, provider="klaviyo", enabled=True, settings=settings or {"list_id": LIST},
            sync_state={"initial_sync_done": True})
        services.set_credential(account, "api_key", API_KEY)
        return account

    def person(self, email, *, given="Ada", family="Lovelace", company=None, marketable=True, consent=None):
        party = Party.objects.create(company=company or self.company, kind=Party.Kind.PERSON, given_name=given,
                                     family_name=family, origin=Party.Origin.ERP, marketable=marketable)
        PartyEmail.objects.create(party=party, email=email, purpose="work")
        party.refresh_from_db()
        if consent:
            self.consent(party, consent)
        return party

    def consent(self, party, state, *, when=None):
        return ConsentRecord.objects.create(party=party, company=party.company, channel="email",
                                            purpose="marketing", state=state, occurred_at=when or T0,
                                            source="portal")

    def sync(self, party, key="1"):
        outbox.enqueue("klaviyo.profile", outbox.EventSpec(
            idempotency_key=f"test:{party.pk}:{key}", payload={}, company_id=party.company_id,
            object_type="parties.party", object_id=party.pk, party_id=party.pk,
        ))
        with use_transport(self.transport):
            drain("klaviyo.profile")

    def adapter(self, account=None):
        return KlaviyoAdapter(account or self.account, self.transport)

    def ingest(self, envelope, account=None):
        with outbox.origin("klaviyo"):
            return self.adapter(account).ingest(envelope)

    def paths(self, start=0):
        return [(r["method"], r["path"]) for r in self.transport.recorded[start:]]

    def ref(self, party):
        return ExternalRef.objects.filter(source="klaviyo", object_type="profile", object_id=party.pk).first()


class ConsentGatingTests(KlaviyoTestCase):
    def test_a_person_without_marketing_consent_is_never_pushed_to_a_list(self):
        seg = _segment(self, "Ad")
        self.account.settings = {"list_id": LIST, "segment_lists": {f"saved_filter:{seg.pk}": SEG_LIST}}
        self.account.save()
        never = self.person("adam@example.net", given="Adam")                    # in the segment, no record
        gone = self.person("adele@example.org", given="Adele", consent="granted")
        self.consent(gone, "withdrawn", when=T0 + timedelta(hours=1))           # latest is a withdrawal
        cold = self.person("adrian@example.com", given="Adrian", marketable=False, consent="granted")
        for party in (never, gone, cold):
            self.sync(party)
        with use_transport(self.transport):
            KlaviyoAdapter(self.account, self.transport).reconcile(full=True)
            drain()
        self.assertEqual(self.transport.recorded, [], "no request at all for people golden may not market to")
        self.assertFalse(ExternalRef.objects.filter(source="klaviyo").exists())

    def test_granted_is_subscribed_with_golden_consent_time_then_steady(self):
        ada = self.person("ada@example.com", consent="granted")
        self.sync(ada)
        self.assertEqual(self.paths(), [("POST", values.PROFILE_IMPORT), ("POST", values.SUBSCRIBE)])
        body = self.transport.recorded[1]["json"]
        marketing = body["data"]["attributes"]["profiles"]["data"][0]["attributes"]["subscriptions"]["email"]
        self.assertEqual(marketing["marketing"]["consented_at"], "2026-03-01T12:00:00Z")
        self.assertEqual(body["data"]["relationships"]["list"]["data"]["id"], LIST)
        before = len(self.transport.recorded)
        self.sync(ada, key="2")
        self.assertEqual(self.paths(before), [], "an unchanged party sends nothing")

    def test_losing_consent_unsubscribes_once(self):
        ada = self.person("ada@example.com", consent="granted")
        self.sync(ada)
        self.consent(ada, "withdrawn", when=T0 + timedelta(days=1))
        before = len(self.transport.recorded)
        self.sync(ada, key="2")
        self.sync(ada, key="3")
        self.assertEqual(self.paths(before), [("POST", values.UNSUBSCRIBE)])

    def test_no_list_configured_dead_letters_instead_of_guessing(self):
        self.account.settings = {}
        self.account.save()
        ada = self.person("ada@example.com", consent="granted")
        self.sync(ada)
        row = OutboxEvent.objects.get(idempotency_key=f"test:{ada.pk}:1")
        self.assertEqual((row.state, row.last_error), ("dead", "no_list_id"))


class SuppressionBothWaysTests(KlaviyoTestCase):
    def test_golden_suppression_reaches_klaviyo_exactly_once(self):
        ada = self.person("ada@example.com", consent="granted")
        self.sync(ada)
        self.reconcile()  # baseline watermark
        Suppression.objects.create(company=self.company, channel="email", address_hash=address_hash("ada@example.com"),
                                   reason="hard_bounce", source="erp")
        before = len(self.transport.recorded)
        self.reconcile()
        self.assertEqual(self.paths(before), [("POST", values.UNSUBSCRIBE), ("POST", values.SUPPRESS)])
        suppress = self.transport.recorded[-1]["json"]
        self.assertEqual(suppress["data"]["attributes"]["profiles"]["data"][0]["attributes"]["email"],
                         "ada@example.com")
        before = len(self.transport.recorded)
        self.reconcile()
        self.reconcile(full=True)
        self.sync(ada, key="again")
        self.assertEqual(self.paths(before), [], "the suppression is pushed once, ever")

    def test_opt_out_suppression_unsubscribes_but_is_not_pushed_as_a_suppression(self):
        ada = self.person("ada@example.com", consent="granted")
        self.sync(ada)
        Suppression.objects.create(company=self.company, channel="email", address_hash=address_hash("ada@example.com"),
                                   reason="opt_out", source="erp")
        before = len(self.transport.recorded)
        self.sync(ada, key="2")
        self.assertEqual(self.paths(before), [("POST", values.UNSUBSCRIBE)])

    def test_klaviyo_unsubscribe_is_a_withdrawal_and_a_suppression_never_a_grant(self):
        from customers.models import Customer

        ada = self.person("ada@example.com", consent="granted")
        # bulk_create: the account exists already; a save() would mirror a second person.
        [customer] = Customer.objects.bulk_create([Customer(company=self.company, email="ada@example.com",
                                                            accepts_marketing=True, party=ada)])
        self.sync(ada)
        grants = ConsentRecord.objects.filter(state="granted").count()
        event = item("01FIXTUREEVENTUNSUB000001", values.METRIC_UNSUBSCRIBED, "01FIXTUREPROFILEADA0000000",
                     "ada@example.com", when=T0 + timedelta(days=2))
        first = self.ingest(event)
        second = self.ingest(event)
        self.assertTrue(first[0].changed)
        self.assertFalse(second[0].changed)
        self.assertEqual(ConsentRecord.objects.filter(state="granted").count(), grants, "never a grant")
        withdrawals = ConsentRecord.objects.filter(party=ada, state="withdrawn")
        # Party-wide, and for the address (can_contact answers from an address row first).
        self.assertEqual(sorted(withdrawals.values_list("address_normalized", flat=True)), ["", "ada@example.com"])
        self.assertEqual(set(withdrawals.values_list("source", "source_detail")),
                         {("api", "klaviyo:01FIXTUREEVENTUNSUB000001")})
        self.assertEqual(list(Suppression.objects.filter(company=self.company).values_list("reason", flat=True)),
                         ["opt_out"])
        self.assertEqual(can_contact(ada, "email", "marketing", "ada@example.com").reason, "suppressed:opt_out")
        customer.refresh_from_db()
        self.assertFalse(customer.accepts_marketing, "the customer flag follows, so no stale re-grant")
        before = len(self.transport.recorded)
        self.sync(ada, key="after")
        self.assertEqual(self.paths(before), [], "no echo: Klaviyo already holds the unsubscribe")

    def test_klaviyo_subscribe_writes_no_consent(self):
        grace = self.person("grace@example.net", given="Grace", family="Hopper")
        result = self.ingest(item("01FIXTUREEVENTSUBSCR00001", values.METRIC_SUBSCRIBED, "01FIXTUREPROFILEGRACE00000",
                                  "grace@example.net"))
        self.assertEqual(result[0].reason, "provider_subscribe_ignored")
        self.assertFalse(ConsentRecord.objects.exists())
        self.assertEqual(can_contact(grace, "email", "marketing", "grace@example.net").reason, "no_consent")
        report = self.adapter().reconcile(full=True)
        self.assertEqual(report["provider_subscribes_ignored"], 1)
        with use_transport(self.transport):
            drain()
        self.assertEqual(self.transport.recorded, [])

    def test_a_subscribe_after_an_unsubscribe_does_not_lift_anything(self):
        ada = self.person("ada@example.com")
        self.consent(ada, "granted", when=T0 - timedelta(days=30))
        self.ingest(item("e1", values.METRIC_UNSUBSCRIBED, "p1", "ada@example.com"))
        self.ingest(item("e2", values.METRIC_SUBSCRIBED, "p1", "ada@example.com", when=T0 + timedelta(hours=1)))
        self.assertFalse(can_contact(ada, "email", "marketing", "ada@example.com").allowed)
        self.assertEqual(ConsentRecord.objects.filter(party=ada).latest("occurred_at").state, "withdrawn")
        self.assertTrue(Suppression.objects.filter(lifted_at__isnull=True, reason="opt_out").exists())

    def test_hard_bounce_supersedes_opt_out_and_soft_bounce_writes_nothing(self):
        self.person("ada@example.com")
        self.ingest(item("e1", values.METRIC_UNSUBSCRIBED, "p1", "ada@example.com"))
        self.ingest(item("e2", values.METRIC_BOUNCED, "p1", "ada@example.com", props={"Bounce Type": "soft"}))
        self.assertEqual(Suppression.objects.count(), 1)
        self.ingest(item("e3", values.METRIC_BOUNCED, "p1", "ada@example.com", props={"Bounce Type": "hard"}))
        active = Suppression.objects.get(lifted_at__isnull=True)
        lifted = Suppression.objects.get(lifted_at__isnull=False)
        self.assertEqual((active.reason, lifted.reason, lifted.lift_reason),
                         ("hard_bounce", "opt_out", "superseded:hard_bounce"))
        self.ingest(item("e4", values.METRIC_UNSUBSCRIBED, "p1", "ada@example.com"))
        self.assertEqual(Suppression.objects.get(lifted_at__isnull=True).reason, "hard_bounce",
                         "an opt-out never replaces a hard bounce")

    def reconcile(self, full=False):
        with use_transport(self.transport):
            KlaviyoAdapter(IntegrationAccount.objects.get(pk=self.account.pk), self.transport).reconcile(full=full)
            drain()


def _segment(test, q):
    user = get_user_model().objects.create_user(f"rep-{q}", email=f"rep-{q.lower()}@example.org", password="x",
                                                is_staff=True)
    UserProfile.objects.create(user=user, company=test.company, display_name="rep")
    return SavedFilter.objects.create(company=test.company, owner=user, list_id="parties.party", name=q,
                                      params={"q": q}, visibility="tenant")


class SegmentListTests(KlaviyoTestCase):
    """3b, against saved filters (Phase 5 step 11's GOLDEN_CRM_SEGMENTS is not built yet)."""

    def setUp(self):
        super().setUp()
        self.filter = _segment(self, "Ada")
        self.account.settings = {"list_id": LIST, "segment_lists": {f"saved_filter:{self.filter.pk}": SEG_LIST}}
        self.account.save()

    def lists_of(self, party):
        return self.ref(party).metadata.get("lists")

    def test_entering_and_leaving_a_segment_moves_list_membership_only_while_allowed(self):
        ada = self.person("ada@example.com", consent="granted")
        self.sync(ada)
        subscribed = [r["json"]["data"]["relationships"]["list"]["data"]["id"]
                      for r in self.transport.recorded if r["path"] == values.SUBSCRIBE]
        self.assertEqual(subscribed, [LIST], "consent rides on the main list only")
        self.assertIn(("POST", values.LIST_PROFILES.format(list_id=SEG_LIST)), self.paths(),
                      "a segment list is membership only")
        self.assertEqual(self.lists_of(ada), [SEG_LIST])
        # Leaves the segment (renamed): taken off the segment list, still on the main list.
        ada.given_name, ada.display_name = "Augusta", "Augusta Lovelace"
        ada.save()
        before = len(self.transport.recorded)
        self.sync(ada, key="left")
        self.assertEqual(self.paths(before)[-1],
                         ("DELETE", values.LIST_PROFILES.format(list_id=SEG_LIST)))
        self.assertNotIn(("POST", values.UNSUBSCRIBE), self.paths(before))
        self.assertEqual(self.lists_of(ada), [])
        # Back in the segment, but consent withdrawn: unsubscribed, never re-added.
        ada.given_name, ada.display_name = "Ada", "Ada Lovelace"
        ada.save()
        self.consent(ada, "withdrawn", when=T0 + timedelta(days=3))
        before = len(self.transport.recorded)
        self.sync(ada, key="back")
        self.assertEqual(self.paths(before), [("POST", values.UNSUBSCRIBE)])

    def test_reconcile_finds_segment_drift(self):
        ada = self.person("ada@example.com", consent="granted")
        self.sync(ada)
        self.reconcile_ok()
        ada.given_name, ada.display_name = "Augusta", "Augusta Lovelace"
        ada.save()
        OutboxEvent.objects.filter(consumer="klaviyo.profile", state="pending").delete()  # the save's own row
        before = len(self.transport.recorded)
        self.reconcile_ok()
        self.assertEqual(self.paths(before)[-1], ("DELETE", values.LIST_PROFILES.format(list_id=SEG_LIST)))

    def test_a_private_filter_is_not_a_segment(self):
        SavedFilter.objects.filter(pk=self.filter.pk).update(visibility="private")
        ada = self.person("ada@example.com", consent="granted")
        self.sync(ada)
        self.assertFalse(self.lists_of(ada))
        self.assertNotIn(("POST", values.LIST_PROFILES.format(list_id=SEG_LIST)), self.paths())

    def reconcile_ok(self):
        with use_transport(self.transport):
            KlaviyoAdapter(IntegrationAccount.objects.get(pk=self.account.pk), self.transport).reconcile()
            drain()


class EventsInTests(KlaviyoTestCase):
    def test_received_and_opened_are_one_communication_and_idempotent(self):
        from communications.models import Communication

        ada = self.person("ada@example.com")
        props = {"$message": "01FIXTURECAMPAIGNMSG00001", "Subject": "Spring panels"}
        received = item("r1", values.METRIC_RECEIVED, "p1", "ada@example.com", props=props)
        opened = item("o1", values.METRIC_OPENED, "p1", "ada@example.com", when=T0 + timedelta(minutes=5), props=props)
        clicked = item("c1", values.METRIC_CLICKED, "p1", "ada@example.com", when=T0 + timedelta(minutes=9),
                       props=props)
        for _ in range(2):
            for envelope in (received, opened, opened, clicked):
                self.ingest(envelope)
        comm = Communication.objects.get()
        self.assertEqual((comm.kind, comm.direction, comm.party_id, comm.engagement_id, comm.body_text),
                         ("marketing_email", "outbound", ada.pk, None, ""))
        self.assertEqual(comm.metadata["klaviyo.campaign_id"], "01FIXTURECAMPAIGNMSG00001")
        self.assertEqual((comm.metadata["klaviyo.opens"], comm.metadata["klaviyo.clicks"]), (1, 1))
        self.assertEqual(comm.metadata["klaviyo.first_open_at"], "2026-03-01T12:05:00Z")
        self.assertEqual(self.ingest(opened)[0].changed, False)

    def test_marketing_sends_do_not_count_as_activity(self):
        from communications.kinds import get_kind

        kind = get_kind("marketing_email")
        self.assertEqual((kind.counts_as_activity, kind.directions, kind.raw_schema_version),
                         (False, ("outbound",), 1))

    def test_unknown_and_erased_profiles_write_nothing(self):
        from core.provenance import erased_source_id

        ada = self.person("ada@example.com")
        ExternalRef.objects.create(company=self.company, source="klaviyo", object_type="profile",
                                   source_id=erased_source_id("p-gone"), metadata={"erased": True},
                                   content_type=ContentType.objects.get_for_model(Party), object_id=ada.pk)
        for envelope in (item("e1", values.METRIC_UNSUBSCRIBED, "p-unknown", "stranger@example.org"),
                         item("e2", values.METRIC_UNSUBSCRIBED, "p-gone", "ada@example.com")):
            outcome = self.ingest(envelope)[0]
            self.assertTrue(outcome.skip)
        self.assertFalse(ConsentRecord.objects.exists())
        self.assertFalse(Suppression.objects.exists())
        self.assertFalse(Party.objects.filter(primary_email_normalized="stranger@example.org").exists())

    def test_newer_envelope_is_refused(self):
        from core.plugins.crm_adapter import AdapterError

        self.person("ada@example.com")
        with self.assertRaises(AdapterError):
            self.ingest(item("e1", values.METRIC_UNSUBSCRIBED, "p1", "ada@example.com", version=2))
        self.assertFalse(ConsentRecord.objects.exists())


class ErasureTests(KlaviyoTestCase):
    def test_an_erased_party_is_deleted_at_klaviyo_and_never_pushed_again(self):
        from parties.erasure import _Footprint, erase_party

        ada = self.person("ada@example.com", consent="granted")
        self.sync(ada)
        ref = self.ref(ada)
        fp = _Footprint(parties={ada.pk}, refs={ref.pk})
        refs = self.adapter().erasure_refs(fp)
        self.assertEqual([(r.object_type, r.source_id) for r in refs], [("profile", "01FIXTUREPROFILEADA0000000")])
        result = erase_party(ada, reason="Requested by the person")
        self.assertEqual(self.adapter().erasure_refs(fp), [], "after erasure the ids are hashes, not ids")
        # erase_party enqueued the row with the ids read before hashing (step 8).
        enqueued = OutboxEvent.objects.get(consumer="klaviyo.erase")
        self.assertEqual(enqueued.idempotency_key, f"erase:{result.erasure_id}")
        self.assertEqual(enqueued.payload["refs"],
                         [{"object_type": r.object_type, "source_id": r.source_id} for r in refs])
        before = len(self.transport.recorded)
        with use_transport(self.transport):
            drain()
        self.assertIn(("POST", values.DELETION), self.paths(before))
        row = OutboxEvent.objects.get(consumer="klaviyo.erase")
        self.assertEqual(row.payload, {"erased": True, "count": 1})
        before = len(self.transport.recorded)
        self.sync(ada, key="after-erasure")
        with use_transport(self.transport):
            KlaviyoAdapter(IntegrationAccount.objects.get(pk=self.account.pk), self.transport).reconcile(full=True)
            drain()
        self.assertEqual(self.paths(before), [], "an erased person is never re-pushed")
        sync_row = OutboxEvent.objects.get(idempotency_key=f"test:{ada.pk}:after-erasure")
        self.assertEqual(sync_row.state, "cancelled")
        # Inbound for the erased profile or address is dropped.
        for envelope in (item("e1", values.METRIC_UNSUBSCRIBED, "01FIXTUREPROFILEADA0000000", ""),
                         item("e2", values.METRIC_RECEIVED, "p-new", "ada@example.com")):
            self.assertTrue(self.ingest(envelope)[0].skip)


class TenancyTests(KlaviyoTestCase):
    def test_tenants_stay_apart(self):
        other = Company.objects.create(name="Tenant B")
        other_account = self.make_account(other)
        bea = self.person("bea@example.com", given="Bea", company=other, consent="granted")
        # Tenant A's account knows no bea: her events reaching A write nothing anywhere.
        self.ingest(item("e1", values.METRIC_UNSUBSCRIBED, "p-bea", "bea@example.com"))
        self.assertFalse(ConsentRecord.objects.filter(state="withdrawn").exists())
        self.assertFalse(Suppression.objects.exists())
        # A's reconcile never enqueues B's people; B's own account syncs them.
        self.adapter().reconcile(full=True)
        self.assertFalse(OutboxEvent.objects.filter(party_id=bea.pk, company=self.company).exists())
        KlaviyoAdapter(other_account, self.transport).reconcile(full=True)
        row = OutboxEvent.objects.get(party_id=bea.pk, consumer="klaviyo.profile")
        self.assertEqual(row.company_id, other.pk)
        with use_transport(self.transport):
            drain()
        self.assertEqual(ExternalRef.objects.get(source="klaviyo").company_id, other.pk)
        # A sync row naming B's party under A's company finds nothing in A.
        outbox.enqueue("klaviyo.profile", outbox.EventSpec(
            idempotency_key="cross", payload={}, company_id=self.company.pk, object_id=bea.pk))
        before = len(self.transport.recorded)
        with use_transport(self.transport):
            drain()
        self.assertEqual(self.paths(before), [])
        self.assertEqual(OutboxEvent.objects.get(idempotency_key="cross").state, "cancelled")


class SignalTests(KlaviyoTestCase):
    def rows(self):
        return OutboxEvent.objects.filter(consumer="klaviyo.profile", state="pending")

    def test_party_saves_enqueue_one_coalesced_sync(self):
        ada = self.person("ada@example.com")
        ada.given_name = "Augusta"
        ada.save()
        ada.save(update_fields=["given_name"])
        self.assertEqual(self.rows().count(), 1)
        self.assertEqual(self.rows().get().party_id, ada.pk)

    def test_no_row_for_irrelevant_updates_organisations_inbound_writes_or_disabled_accounts(self):
        ada = self.person("ada@example.com")
        self.rows().delete()
        ada.save(update_fields=["updated_at"])
        Party.objects.create(company=self.company, kind=Party.Kind.ORGANISATION, legal_name="Acme",
                             origin=Party.Origin.ERP)
        with outbox.origin("klaviyo"):
            ada.given_name = "Augusta"
            ada.save()
        IntegrationAccount.objects.filter(pk=self.account.pk).update(enabled=False)
        ada.given_name = "Ada"
        ada.save()
        self.assertEqual(list(self.rows().values_list("payload", flat=True)), [])


class SecretHygieneTests(KlaviyoTestCase):
    def test_the_api_key_never_reaches_requests_rows_logs_or_errors(self):
        ada = self.person("ada@example.com", consent="granted")
        self.transport = FixtureTransport(responses=[ok("POST", values.PROFILE_IMPORT, 400,
                                                        {"errors": [{"detail": "bad"}]})])
        with self.assertLogs("golden_erp.integrations", level=logging.WARNING) as logs:
            self.sync(ada)
        recorded = json.dumps(self.transport.recorded)
        self.assertIn("Klaviyo-API-Key <redacted:api_key>", recorded)
        row = OutboxEvent.objects.get(idempotency_key=f"test:{ada.pk}:1")
        self.assertEqual(row.state, "dead")
        account = IntegrationAccount.objects.get(pk=self.account.pk)
        for text in (recorded, json.dumps(row.payload), row.last_error, "\n".join(logs.output),
                     json.dumps(account.settings), json.dumps(account.sync_state), account.last_error or ""):
            self.assertNotIn(API_KEY, text)

    def test_settings_validation(self):
        adapter = self.adapter()
        self.assertEqual(adapter.validate_settings({"list_id": LIST, "log_sends": False}), [])
        errors = adapter.validate_settings({"list_id": "", "log_sends": "yes",
                                            "segment_lists": {"segment:vip": "", "saved_filter:nope": "L"}})
        self.assertEqual(len(errors), 5, errors)


class CommandTests(KlaviyoTestCase):
    def test_integrations_sync_reconcile(self):
        self.person("ada@example.com", consent="granted")
        out = io.StringIO()
        call_command("integrations_sync", "klaviyo", "--reconcile", stdout=out)
        self.assertIn("1 account(s)", out.getvalue())
        self.assertIn("1 enqueued", out.getvalue())
        self.assertEqual(OutboxEvent.objects.filter(consumer="klaviyo.profile").count(), 1)
        self.assertTrue(IntegrationAccount.objects.get(pk=self.account.pk).sync_state.get("reconcile_at"))


class ReviewRegressionTests(KlaviyoTestCase):
    """Paths the first review found uncovered."""

    def test_pushes_wait_for_the_first_poll(self):
        IntegrationAccount.objects.filter(pk=self.account.pk).update(sync_state={})
        ada = self.person("ada@example.com", consent="granted")
        self.sync(ada)
        row = OutboxEvent.objects.get(idempotency_key=f"test:{ada.pk}:1")
        self.assertEqual((row.state, row.last_error), ("cancelled", "waiting_for_first_poll"))
        self.assertEqual(self.transport.recorded, [])
        report = KlaviyoAdapter(IntegrationAccount.objects.get(pk=self.account.pk), self.transport).reconcile()
        self.assertEqual(report, {"enqueued": 0, "waiting_for_first_poll": 1})

    def test_an_old_address_profile_is_unsubscribed_when_the_email_changes(self):
        ada = self.person("ada@example.com", consent="granted")
        self.sync(ada)
        old = self.ref(ada)
        PartyEmail.objects.filter(party=ada).delete()
        PartyEmail.objects.create(party=ada, email="ada@example.net", purpose="work")
        Party.objects.filter(pk=ada.pk).update(primary_email_normalized="ada@example.net")
        self.transport.responses.insert(0, imported("01FIXTUREPROFILEADANEW0000"))
        before = len(self.transport.recorded)
        self.sync(ada, key="moved")
        calls = [c for c in self.transport.recorded[before:] if c["path"] == values.UNSUBSCRIBE]
        self.assertEqual(len(calls), 1)
        unsub = calls[0]["json"]["data"]["attributes"]["profiles"]["data"][0]
        self.assertEqual(unsub, {"type": "profile", "id": old.source_id, "attributes": {}},
                         "the old profile is unsubscribed by id; its address is not golden's to send")
        old.refresh_from_db()
        self.assertEqual(old.metadata["state"], "unsubscribed")

    def test_erase_retry_resumes_after_a_partial_failure(self):
        self.transport = FixtureTransport(responses=[
            ok("POST", values.DELETION, 202, repeatable=False),
            {"request": {"method": "POST", "path": values.DELETION}, "response": {"status": 503}},
            ok("POST", values.DELETION, 404, repeatable=False),
        ])
        outbox.enqueue("klaviyo.erase", outbox.EventSpec(
            idempotency_key="erase:partial", company_id=self.company.pk,
            payload={"refs": [{"object_type": "profile", "source_id": "p1"},
                              {"object_type": "profile", "source_id": "p2"}]},
        ), ignore_suppression=True)
        with use_transport(self.transport):
            drain("klaviyo.erase")
        row = OutboxEvent.objects.get(idempotency_key="erase:partial")
        self.assertEqual(row.state, "pending")
        self.assertEqual(row.payload, {"refs": [{"object_type": "profile", "source_id": "p2"}], "count_total": 2})
        OutboxEvent.objects.filter(pk=row.pk).update(next_attempt_at=timezone.now())
        with use_transport(self.transport):
            drain("klaviyo.erase")
        row.refresh_from_db()
        sent = [r["json"]["data"]["attributes"]["profile"]["data"]["id"] for r in self.transport.recorded]
        self.assertEqual(sent, ["p1", "p2", "p2"], "p1 is never re-sent; p2's 404 (already gone) is done")
        self.assertEqual((row.state, row.payload), ("done", {"erased": True, "count": 2}))

    def test_a_segment_cannot_be_the_main_list(self):
        errors = self.adapter().validate_settings(
            {"list_id": LIST, "segment_lists": {"saved_filter:5e9f1c2a-7b3d-4c8e-9a10-0f1e2d3c4b5a": LIST}})
        self.assertEqual(len(errors), 1)
        self.assertIn("main list_id", errors[0])


def profile_answer(profile_id, consent="UNSUBSCRIBED", timestamp="2025-06-01T09:00:00Z", suppression=()):
    return {"request": {"method": "GET", "path": values.PROFILE.format(profile_id=profile_id)},
            "response": {"status": 200}, "repeatable": True,
            "payload": {"data": {"type": "profile", "id": profile_id, "attributes": {"subscriptions": {
                "email": {"marketing": {"consent": consent, "timestamp": timestamp,
                                        "suppression": list(suppression)}}}}}}}


class AdoptionTests(KlaviyoTestCase):
    """First connect onto profiles Klaviyo already had (second review)."""

    PID = "01FIXTUREPROFILEADA0000000"

    def adopt(self, *answers):
        self.transport = FixtureTransport(responses=[ok("POST", values.PROFILE_IMPORT, 200,
                                                        {"data": {"type": "profile", "id": self.PID}})]
                                          + list(answers) + JOBS)

    def test_a_failed_opt_out_read_is_retried_never_skipped(self):
        ada = self.person("ada@example.com")
        self.consent(ada, "granted", when=T0 - timedelta(days=400))
        self.adopt({"request": {"method": "GET", "path": values.PROFILE.format(profile_id=self.PID)},
                    "response": {"status": 503}})
        self.sync(ada)
        self.sync(ada, key="2")  # a second row for the party, while the first waits to retry
        self.assertNotIn(("POST", values.SUBSCRIBE), self.paths(), "nothing is subscribed before the read")
        self.assertEqual(self.ref(ada).metadata.get("optout_check"), "pending")
        self.transport.responses.append(profile_answer(self.PID))
        self.sync(ada, key="3")
        self.assertNotIn(("POST", values.SUBSCRIBE), self.paths())
        self.assertFalse(can_contact(ada, "email", "marketing", "ada@example.com").allowed)
        self.assertNotIn("optout_check", self.ref(ada).metadata)

    def test_a_refusal_older_than_golden_grant_is_overtaken(self):
        ada = self.person("ada@example.com", consent="granted")  # granted at T0 (2026-03-01)
        self.adopt(profile_answer(self.PID, timestamp="2025-06-01T09:00:00Z"))
        self.sync(ada)
        self.assertIn(("POST", values.SUBSCRIBE), self.paths(), "golden's newer opt-in wins")
        self.assertFalse(ConsentRecord.objects.filter(state="withdrawn").exists())
        self.assertFalse(Suppression.objects.exists())

    def test_an_address_fault_is_recorded_even_when_overtaken(self):
        ada = self.person("ada@example.com", consent="granted")
        self.adopt(profile_answer(self.PID, consent="SUBSCRIBED", suppression=[
            {"reason": "HARD_BOUNCE", "timestamp": "2025-06-01T09:00:00Z"}]))
        self.sync(ada)
        self.assertNotIn(("POST", values.SUBSCRIBE), self.paths())
        self.assertEqual(Suppression.objects.get().reason, "hard_bounce")
        self.assertFalse(ConsentRecord.objects.filter(state="withdrawn").exists())

    def test_an_old_address_refusal_never_blocks_the_primary(self):
        ada = self.person("ada@example.com", consent="granted")
        ExternalRef.objects.create(company=self.company, source="klaviyo", object_type="profile",
                                   source_id="p-old", content_type=ContentType.objects.get_for_model(Party),
                                   object_id=ada.pk, metadata={"address": "0" * 64, "state": "subscribed"})
        self.transport = FixtureTransport(responses=[imported(self.PID),
                                                     ok("POST", values.UNSUBSCRIBE, 400, repeatable=False)] + JOBS)
        self.sync(ada)
        row = OutboxEvent.objects.get(idempotency_key=f"test:{ada.pk}:1")
        self.assertEqual(row.state, "done")
        self.assertIn(("POST", values.SUBSCRIBE), self.paths())
        old = ExternalRef.objects.get(source_id="p-old")
        self.assertEqual(old.metadata["unsub_failed"], 400)
