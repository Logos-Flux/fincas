"""Erasure (docs/crm/phase2-party.md 7.4, implementation step 12).

Done when: after erasure no PII remains in Party, contact points, Customer,
Contact, Lead or AuditLog ``changes`` (asserted by a scan over every text
column of every model); orders survive; a re-import of an erased
ExternalRef is skipped; the suppression blocks marketing.
"""

from __future__ import annotations

import json
import secrets
from decimal import Decimal
from io import StringIO
from unittest import mock, skipUnless

from django.apps import apps
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission, User
from django.contrib.contenttypes.models import ContentType
from django.core.management import CommandError, call_command
from django.db import connection, models
from django.db.models import Q
from django.db.models.functions import Cast
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework.test import APIClient

from communications.models import Communication, CommunicationParticipant, CommunicationRaw, EmailLog
from core.identity.models import UserIdentity
from core.models import AuditLog, Company, ExternalRef, UserProfile
from core.provenance import is_erased_source
from core.tests.helpers import LOGIN_LOCK_ORDER, login_lock_order, make_staff_user
from crm.backfill import backfill_activity_communications
from crm.models import ActivityLog, Lead
from customers.models import Customer, CustomerAddress, CustomerContact
from fulfillment.models import PickTicket
from orders.models import Order, Quote, SalesInvoice
from parties.consent import can_contact
from parties.erasure import ERASED_PARTY_NAME, ErasureError, erase_party, tombstone_email
from parties.merge import MergeError, merge, unmerge
from parties.models import (
    ConsentRecord,
    Party,
    PartyEmail,
    PartyMergeLog,
    PartyPhone,
    PartyRelationship,
    Suppression,
)
from parties.reconcile import DRIFT, check
from payments.models import Payment
from unify.apply import apply_chunk, start_apply_run
from unify.models import ImportBatch, StagedRow
from webhooks.models import WebhookDeliveryLog, WebhookSubscription

BASE = "/api/v1/parties/"
API_KEY = "erasure-api-" + secrets.token_hex(8)

# The seeded person. Every string here is unique to them, so a hit anywhere
# after erasure is a leak.
GIVEN, FAMILY = "Zebulon", "Quaxworth"
EMAIL = "Zeb.Quax@Example.test"
EMAIL_NORM = "zeb.quax@example.test"
EXTRA_EMAIL = "zq.personal@example.test"
PHONE = "+1 202-555-0187"
PHONE_E164 = "+12025550187"
STREET = "77 Larkspur Hollow"
OTHER_STREET = "9 Zebulon Mews"
EBAY = "zebquax77"
TAX_CERT = "TX-ZQ-4411"
SUBJECT = "sub-zebquax"
IP = "203.0.113.97"
BC_ID = "BC-90210"
CLICK_ID = "gclid-zq-5512"
PII = (GIVEN, FAMILY, EMAIL_NORM, EXTRA_EMAIL, "202-555-0187", PHONE_E164, EBAY, TAX_CERT, SUBJECT,
       OTHER_STREET, IP, STREET, CLICK_ID)


def pii_hits(needles, allowed=frozenset()):
    """``[(model label, pk, needle)]`` for every row of every model whose
    char, text, email, URL or JSON columns contain a needle (case-insensitive).
    ``allowed`` is ``{(label, str(pk))}`` rows that may hold one (kept for
    accounting, and listed by the erasure result)."""
    hits = []
    for model in apps.get_models():
        if model._meta.proxy or not model._meta.managed:
            continue
        text = [f for f in model._meta.concrete_fields
                if isinstance(f, models.CharField | models.TextField | models.GenericIPAddressField)
                and not f.choices]
        json_fields = [f for f in model._meta.concrete_fields if isinstance(f, models.JSONField)]
        if not text and not json_fields:
            continue
        qs = model._base_manager.annotate(
            **{f"_pii_{f.name}": Cast(f.name, models.TextField()) for f in json_fields})
        for needle in needles:
            cond = Q()
            for f in text:
                cond |= Q(**{f"{f.attname}__icontains": needle})
            for f in json_fields:
                cond |= Q(**{f"_pii_{f.name}__icontains": needle})
            for pk in qs.filter(cond).values_list("pk", flat=True):
                if (model._meta.label, str(pk)) not in allowed:
                    hits.append((model._meta.label, str(pk), needle))
    return hits


def person(company, given="Ada", family="Lovelace", email=None, **kw):
    party = Party.objects.create(company=company, kind="person", given_name=given, family_name=family,
                                 display_name=f"{given} {family}", origin="erp", **kw)
    if email:
        PartyEmail.objects.create(party=party, email=email, purpose="personal", is_primary=True)
    return party


class _Seeded(TestCase):
    """One person, copied everywhere golden copies a person."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other_company = Company.objects.create(name="Tenant B")

    def setUp(self):
        self.user = User.objects.create_user("zebq", EMAIL_NORM, "pw", first_name=GIVEN, last_name=FAMILY)
        UserIdentity.objects.create(user=self.user, provider="google", subject=SUBJECT, email=EMAIL_NORM)
        self.customer = Customer.objects.create(
            first_name=GIVEN, last_name=FAMILY, email=EMAIL, phone=PHONE, company=self.company,
            accepts_marketing=True, notes=f"{GIVEN} prefers mornings", bc_id=BC_ID,
            erpnext_name=f"{GIVEN} {FAMILY}", ebay_username=EBAY, is_tax_exempt=True,
            tax_exempt_reason="resale", tax_exempt_certificate_number=TAX_CERT, user=self.user,
        )
        self.customer.refresh_from_db()
        self.party = Party.objects.get(pk=self.customer.account_holder_id)
        PartyEmail.objects.create(party=self.party, email=EXTRA_EMAIL, purpose="personal")
        self.used_address = CustomerAddress.objects.create(
            customer=self.customer, first_name=GIVEN, last_name=FAMILY, phone=PHONE,
            address_line_1=STREET, city="Springfield", state_province="IL", postal_code="62701",
            delivery_instructions=f"ask for {GIVEN}",
        )
        self.spare_address = CustomerAddress.objects.create(
            customer=self.customer, first_name=GIVEN, address_line_1=OTHER_STREET, city="Springfield",
            state_province="IL", postal_code="62702", bc_address_id="BCA-7",
        )
        self.contact = CustomerContact.objects.create(
            customer=self.customer, first_name=GIVEN, last_name=FAMILY, email=EMAIL, phone=PHONE,
            notes=f"met {GIVEN}",
        )
        self.lead = Lead.objects.create(
            company=self.company, first_name=GIVEN, last_name=FAMILY, email=EMAIL, phone=PHONE,
            message=f"{GIVEN} wants panels", city="Springfield", ip_address=IP,
        )
        self._seed_crm_sidecars()
        self._seed_crm_text()
        # A pre-step-6 activity (ActivityLog is frozen) and its step-6 copy.
        self.activity = ActivityLog(
            company=self.company, customer=self.customer, activity_type="call", subject=f"Call {GIVEN}",
            description=f"{FAMILY} asked about delivery", activity_date="2026-10-01T10:00:00Z",
        )
        ActivityLog.objects.bulk_create([self.activity])
        backfill_activity_communications()
        self.order = Order.objects.create(
            customer=self.customer, company=self.company, billing_address=self.used_address,
            shipping_address=self.used_address, grand_total=Decimal("120.00"), ip_address=IP,
            customer_message=f"Leave it with {GIVEN}", internal_notes=f"{FAMILY} called twice",
        )
        Payment.objects.create(
            payment_number="PAY-ERASE-1", order=self.order, payment_method="credit_card",
            payment_provider="stripe", amount=Decimal("120.00"),
            gateway_response={"billing_details": {"email": EMAIL_NORM, "name": f"{GIVEN} {FAMILY}"}},
        )
        PickTicket.objects.create(ticket_number="PT-ERASE-1", order=self.order, ship_to_address=STREET,
                                  ship_to_label=f"{GIVEN} {FAMILY}", ship_to_attention=GIVEN)
        subscription = WebhookSubscription.objects.create(
            target_url="https://hooks.example.test/in", company=self.company, subscribed_events=["*"])
        WebhookDeliveryLog.objects.create(
            subscription=subscription, event_type="customer.registered",
            payload={"customer_id": str(self.customer.pk), "email": EMAIL_NORM, "name": GIVEN})
        batch = ImportBatch.objects.create(company=self.company, source_kind="csv", source_name="crm.csv")
        StagedRow.objects.create(batch=batch, row_number=1, raw={"name": f"{GIVEN} {FAMILY}",
                                                                 "email": EMAIL_NORM}, content_hash="seed")
        self.invoice = SalesInvoice.objects.create(
            invoice_number="INV-ERASE-1", order=self.order, company=self.company,
            grand_total=Decimal("120.00"), balance_due=Decimal("0.00"),
        )
        self.guest_order = Order.objects.create(
            company=self.company, guest_email=EMAIL, grand_total=Decimal("15.00"),
        )
        self.quote = Quote.objects.create(
            customer=self.customer, company=self.company, sent_to_email=EMAIL,
            accepted_by_name=f"{GIVEN} {FAMILY}",
        )
        EmailLog.objects.create(
            document_type="sales_order", document_id=self.order.pk, document_number=self.order.order_number,
            recipient_email=EMAIL_NORM, subject="Your order", template_name="order", status="sent",
            triggered_by="auto",
        )
        AuditLog.objects.create(action="update", model_name="customers.Customer", object_id=self.customer.pk,
                                changes={"email": [None, EMAIL_NORM], "first_name": [None, GIVEN]})
        AuditLog.objects.create(action="update", model_name="orders.Order", object_id=self.order.pk,
                                changes={"note": f"emailed {EMAIL_NORM}"})
        LogEntry.objects.create(
            user=make_staff_user("admin-ui", password="pw"), content_type=ContentType.objects.get_for_model(Customer),
            object_id=str(self.customer.pk), object_repr=f"{GIVEN} {FAMILY}", action_flag=2,
            change_message=f"Changed email to {EMAIL_NORM}")
        self.unrelated_audit = AuditLog.objects.create(
            action="update", model_name="orders.Order", object_id=self.order.pk, changes={"status": "paid"})
        self.staff = make_staff_user("eraser", password="pw")
        UserProfile.objects.update_or_create(user=self.staff, defaults={"company": self.company})
        self.staff.user_permissions.add(Permission.objects.get(codename="erase_party"))
        self.staff = User.objects.get(pk=self.staff.pk)

    def _seed_crm_sidecars(self):
        """Phase 4 3.6: CRM ``metadata`` sidecars that copy the person, as an
        import would leave them. Until these existed the scan below passed
        only because no CRM row held the person at all."""
        from crm import engagements
        from crm.models import EngagementContact, Pipeline, Task, Touchpoint
        from parties.models import PartyRole

        pipeline = Pipeline.objects.get(company=self.company, key="sales")
        self.own_engagement = engagements.create(
            company=self.company, party=self.party, pipeline=pipeline, title="Rooftop array",
            metadata={"hubspot.email": EMAIL_NORM, "hubspot.firstname": GIVEN, "custom.package": "gold",
                      "backfill": True})
        self.own_task = Task.objects.create(
            company=self.company, party=self.party, title="Follow up",
            metadata={"legacy.note": f"call {FAMILY} on {PHONE}", "backfill": True, "nickname": "Zeb Q",
                      "dob": "1984-02-03"})
        self.own_touch = Touchpoint.objects.create(
            company=self.company, party=self.party, occurred_at="2026-10-01T09:00:00Z", channel="email",
            metadata={"form_email": EXTRA_EMAIL})
        self.own_role = PartyRole.objects.create(
            party=self.party, company=self.company, role="supplier", valid_from="2026-01-01",
            metadata={"hubspot.owner_note": f"{GIVEN} {FAMILY}", "lead": str(self.party.pk)})
        self.org = Party.objects.create(company=self.company, kind="organisation", display_name="Acme Roofs",
                                        origin="erp")
        self.org_engagement = engagements.create(
            company=self.company, party=self.org, pipeline=pipeline, title="Acme warehouse",
            metadata={"hubspot.contact_email": EMAIL_NORM, "custom.package": "silver",
                      "hubspot.dealname": "Acme warehouse"})
        EngagementContact.objects.create(engagement=self.org_engagement, company=self.company,
                                         party=self.party, role="decision_maker")
        self.org_task = Task.objects.create(
            company=self.company, engagement=self.org_engagement, title="Site visit",
            metadata={"hubspot.attendee": f"{GIVEN} {FAMILY}", "hubspot.slot": "am"})
        self.stray_task = Task.objects.create(  # neither theirs nor their engagement's
            company=self.company, title="Call back",
            metadata={"hubspot.callback": PHONE_E164, "hubspot.slot": "pm"})
        self.list_task = Task.objects.create(  # a non-object sidecar quoting them
            company=self.company, title="Odd import", metadata=[EMAIL_NORM, "x"])

    def _seed_crm_text(self):
        """Phase 3 free text that names or reaches the person: a titled deal
        with loss notes, tasks about them, their lead message logged as a
        note (``crm.intake``), an email with its raw provider payload and a
        touchpoint. Rows on the organisation's deal that do not mention them
        must survive untouched."""
        from communications.ingest import record
        from crm import engagements
        from crm.models import Pipeline, Task, Touchpoint

        pipeline = Pipeline.objects.get(company=self.company, key="sales")
        self.deal = engagements.create(company=self.company, party=self.party, pipeline=pipeline,
                                       title=f"{FAMILY} barn roof")
        engagements.lose(self.deal, "price", notes=f"{GIVEN} went with a cheaper installer",
                         note=f"{GIVEN} said no")
        self.deal_task = Task.objects.create(
            company=self.company, engagement=self.deal, title=f"Call {GIVEN} back",
            description=f"Send the brochure to {EXTRA_EMAIL}")
        self.about_task = Task.objects.create(
            company=self.company, party=self.party, title=f"Birthday card for {FAMILY}",
            description=f"{GIVEN} likes tulips")
        self.org_named_task = Task.objects.create(
            company=self.company, engagement=self.org_engagement, title=f"Brief {GIVEN} on the install",
            description="Bring the drawings")
        # The lead's message, logged as an intake note by crm.intake on Lead create.
        self.lead_note = Communication._base_manager.get(
            company=self.company, provider="lead_form", metadata__lead=str(self.lead.pk))
        self.assertIn(GIVEN, self.lead_note.body_text)
        self.email = record(
            self.company, "email", direction="inbound", occurred_at="2026-10-02T08:00:00Z",
            provider="gmail", external_id="gm-zq-1", known_max_version=1,
            envelope={"schema_version": 1, "provider": "gmail", "payload": {
                "from": f"{GIVEN} {FAMILY} <{EMAIL}>", "snippet": "about the barn roof"}},
            participants=[{"role": "from", "address": EMAIL, "display_name": f"{GIVEN} {FAMILY}"},
                          {"role": "to", "address": "sales@tenant-a.test", "display_name": "Sales"}],
            subject=f"Re: {FAMILY} barn", body_text=f"Thanks, {GIVEN}", thread_key="gm-thread-1",
        ).communication
        self.org_meeting = record(  # on the organisation's deal; names the person in passing
            self.company, "note", direction="internal", occurred_at="2026-10-03T08:00:00Z",
            subject="Site walk", body_text=f"{GIVEN} {FAMILY} joined from the roof",
            summary="Warehouse roof is sound", engagement=self.org_engagement,
            provider="meetrec", external_id="mr-acme-1", known_max_version=1,  # its payload never names them
            envelope={"schema_version": 1, "provider": "meetrec", "payload": {"room": "roof", "minutes": 40}},
        ).communication
        self.deal_touch = Touchpoint.objects.create(
            company=self.company, party=self.party, engagement=self.deal, occurred_at="2026-09-30T09:00:00Z",
            channel="paid_search", utm_source="google", utm_medium="cpc", utm_campaign="barns",
            utm_content=f"{GIVEN}-retarget", landing_url=f"https://shop.test/?email={EMAIL_NORM}",
            click_id_kind="gclid", click_id=CLICK_ID, source="lead_form")
        self.anonymous_touch = Touchpoint.objects.create(  # no party yet; the URL carries them encoded
            company=self.company, occurred_at="2026-09-29T09:00:00Z", channel="email", utm_campaign="barns",
            landing_url="https://shop.test/?e=" + EMAIL_NORM.replace("@", "%40"), source="lead_form")

    def erase(self, party=None, **kw):
        with self.captureOnCommitCallbacks(execute=True):
            return erase_party(party or self.party, self.staff, kw.pop("reason", "GDPR request #1"), **kw)


class SeedSanityTests(_Seeded):
    def test_the_scan_sees_the_seeded_person(self):
        found = {(label, needle) for label, _pk, needle in pii_hits(PII)}
        for label in ("parties.Party", "parties.PartyEmail", "parties.PartyPhone", "customers.Customer",
                      "customers.CustomerContact", "crm.Lead", "core.AuditLog", "core.ExternalRef",
                      "auth.User", "core.UserIdentity", "orders.Order", "orders.Quote", "crm.ActivityLog",
                      "payments.Payment", "fulfillment.PickTicket", "webhooks.WebhookDeliveryLog",
                      "unify.StagedRow", "admin.LogEntry", "crm.Engagement", "crm.Task",
                      "crm.Touchpoint", "parties.PartyRole", "crm.EngagementStageChange",
                      # CRM Phase 3 step 6: the activity copy and the lead's intake note.
                      "communications.Communication", "communications.CommunicationParticipant",
                      "communications.CommunicationRaw"):
            self.assertTrue(any(lbl == label for lbl, _ in found), label)
        self.assertIn(("parties.PartyAddress", STREET), found)


class CrmSidecarErasureTests(_Seeded):
    def meta(self, obj):
        return type(obj)._base_manager.get(pk=obj.pk).metadata

    def test_own_rows_lose_dotted_and_personal_keys_but_keep_system_keys(self):
        result = self.erase()
        self.assertEqual(self.meta(self.own_engagement), {"backfill": True})
        task_meta = self.meta(self.own_task)
        self.assertIn("erased_at", task_meta, "erasure marks the tasks it scrubbed (Phase 6 step 8)")
        task_meta.pop("erased_at")
        self.assertEqual(task_meta, {"backfill": True})  # client keys go too
        self.assertEqual(self.meta(self.own_touch), {})
        self.assertEqual(self.meta(self.own_role), {"lead": str(self.party.pk)})
        self.assertGreaterEqual(result.counts["crm_sidecars"], 6)

    def test_contact_rows_lose_only_keys_that_name_or_reach_the_person(self):
        self.erase()
        self.assertEqual(self.meta(self.org_engagement),
                         {"custom.package": "silver", "hubspot.dealname": "Acme warehouse"})
        self.assertEqual(self.meta(self.org_task), {"hubspot.slot": "am"})
        self.assertEqual(self.meta(self.stray_task), {"hubspot.slot": "pm"})
        self.assertEqual(self.meta(self.list_task), {})

    def test_names_match_whole_words_only(self):
        from parties.erasure import _mentions, _sidecar_terms

        terms = _sidecar_terms({"raw": set(), "names": {"Ada"}})
        self.assertTrue(_mentions("note", "met Ada today", terms))
        self.assertFalse(_mentions("lead", "3fada9c0-0000-4000-8000-000000000000", terms))
        self.assertFalse(_mentions("city", "Canada", terms))


class CrmTextErasureTests(_Seeded):
    """Phase 3 free text: own rows lose it whole, related rows only what names them."""

    def get(self, obj):
        return type(obj)._base_manager.get(pk=obj.pk)

    def test_the_persons_deal_and_tasks(self):
        from crm.models import EngagementStageChange

        result = self.erase()
        deal = self.get(self.deal)
        self.assertEqual((deal.title, deal.loss_notes), (ERASED_PARTY_NAME, ""))
        self.assertEqual(deal.loss_reason, "price")  # the reason key is not personal
        self.assertFalse(EngagementStageChange.objects.filter(engagement=self.deal).exclude(note="").exists())
        for task in (self.deal_task, self.about_task):
            task = self.get(task)
            self.assertEqual((task.title, task.description), ("Erased", ""))
        self.assertGreaterEqual(result.counts["tasks"], 3)

    def test_communications_participants_and_raw_payloads(self):
        result = self.erase()
        note = self.get(self.lead_note)
        self.assertEqual((note.subject, note.body_text), ("Erased", ""))
        # Only system keys stay: the lead link and its (non-personal) number.
        self.assertEqual(note.metadata.get("lead"), str(self.lead.pk))
        self.assertNotIn(self.lead.message, json.dumps(note.metadata))
        email = self.get(self.email)
        self.assertEqual((email.subject, email.body_text, email.thread_key), ("Erased", "", "gm-thread-1"))
        self.assertEqual(CommunicationRaw.objects.get(communication=email).payload, {"erased": True})
        sender = CommunicationParticipant.objects.get(communication=email, role="from")
        self.assertEqual((sender.address_raw, sender.address_normalized, sender.display_name), ("", "", ""))
        self.assertEqual(sender.party_id, self.party.pk)  # the timeline still reaches the tombstone
        sales = CommunicationParticipant.objects.get(communication=email, role="to")
        self.assertEqual(sales.address_normalized, "sales@tenant-a.test")
        self.assertEqual(result.counts["communication_payloads"], 2)  # theirs, and the org meeting's

    def test_related_rows_keep_what_does_not_name_the_person(self):
        self.erase()
        meeting = self.get(self.org_meeting)
        self.assertEqual((meeting.subject, meeting.body_text, meeting.summary),
                         ("Site walk", "", "Warehouse roof is sound"))
        # A re-fetch would rewrite the scrubbed body from the raw payload.
        self.assertEqual(CommunicationRaw.objects.get(communication=self.org_meeting).payload, {"erased": True})
        named = self.get(self.org_named_task)
        self.assertEqual((named.title, named.description), ("Erased", "Bring the drawings"))
        self.assertEqual(self.get(self.org_task).title, "Site visit")
        self.assertEqual(self.get(self.org_engagement).title, "Acme warehouse")

    def test_touchpoint_keeps_channel_and_campaign_only(self):
        self.erase()
        touch = self.get(self.deal_touch)
        self.assertEqual((touch.channel, touch.utm_source, touch.utm_medium, touch.utm_campaign),
                         ("paid_search", "google", "cpc", "barns"))
        self.assertEqual((touch.utm_content, touch.landing_url, touch.click_id, touch.click_id_kind),
                         ("", "", "", ""))
        anonymous = self.get(self.anonymous_touch)
        self.assertEqual((anonymous.landing_url, anonymous.utm_campaign), ("", "barns"))

    def test_a_refetch_does_not_write_the_person_back(self):
        from communications.ingest import record

        self.erase()
        for snippet in ("about the barn roof", "a new reply"):
            result = record(
                self.company, "email", direction="inbound", occurred_at="2026-10-02T08:00:00Z",
                provider="gmail", external_id="gm-zq-1", known_max_version=1,
                envelope={"schema_version": 1, "provider": "gmail", "payload": {
                    "from": f"{GIVEN} {FAMILY} <{EMAIL}>", "snippet": snippet}},
                participants=[{"role": "from", "address": EMAIL, "display_name": f"{GIVEN} {FAMILY}"}],
                subject=f"Re: {FAMILY} barn", body_text=f"Thanks, {GIVEN}",
            )
            self.assertFalse(result.changed)
            self.assertEqual(result.communication.pk, self.email.pk)
        self.assertEqual(self.get(self.email).subject, "Erased")
        self.assertEqual(CommunicationRaw.objects.get(communication=self.email).payload, {"erased": True})
        self.assertEqual(Communication.objects.filter(company=self.company, provider="gmail").count(), 1)


class NoPIIRemainsTests(_Seeded):
    def test_no_pii_remains_outside_the_kept_accounting_address(self):
        result = self.erase()
        kept = {("customers.CustomerAddress", pk) for item in result.kept
                if item["model"] == "customers.CustomerAddress" for pk in item["ids"]}
        self.assertEqual(kept, {("customers.CustomerAddress", str(self.used_address.pk))})
        self.assertIn("fulfillment.PickTicket", {item["model"] for item in result.kept})
        kept |= {("fulfillment.PickTicket", str(pk)) for pk in PickTicket.objects.values_list("pk", flat=True)}
        self.maxDiff = None
        self.assertEqual(pii_hits(PII, allowed=kept), [])
        # The kept address holds only its postal lines.
        address = CustomerAddress.objects.get(pk=self.used_address.pk)
        self.assertEqual((address.first_name, address.last_name, address.phone, address.delivery_instructions),
                         ("", "", "", ""))
        self.assertEqual({hit[2] for hit in pii_hits(PII)}, {STREET})

    def test_the_tombstone(self):
        self.erase()
        party = Party._base_manager.get(pk=self.party.pk)
        self.assertEqual(party.display_name, ERASED_PARTY_NAME)
        self.assertIsNotNone(party.erased_at)
        self.assertFalse(party.is_active)
        self.assertIsNone(party.user_id)
        self.assertEqual((party.given_name, party.family_name, party.metadata, party.tags), ("", "", {}, []))
        self.assertFalse(PartyEmail.objects.filter(party=party).exists())
        self.assertFalse(PartyPhone.objects.filter(party=party).exists())
        self.assertFalse(ConsentRecord.objects.filter(party=party).exists())
        self.assertFalse(PartyRelationship.objects.filter(Q(from_party=party) | Q(to_party=party)).exists())
        customer = Customer.objects.get(pk=self.customer.pk)
        self.assertEqual((customer.first_name, customer.last_name), ("Erased", ""))
        self.assertEqual(customer.email, tombstone_email(customer.pk))
        self.assertFalse(customer.is_active)
        self.assertIsNone(customer.user_id)
        self.assertTrue(customer.is_tax_exempt)  # the fact stays for past invoices; the certificate goes
        self.assertEqual(customer.tax_exempt_certificate_number, "")
        user = User.objects.get(pk=self.user.pk)
        self.assertFalse(user.is_active)
        self.assertFalse(user.has_usable_password())
        self.assertFalse(UserIdentity.objects.filter(user=user).exists())
        self.assertEqual(Lead.objects.get(pk=self.lead.pk).email, tombstone_email(self.lead.pk))
        self.assertIsNone(Lead.objects.get(pk=self.lead.pk).ip_address)

    def test_audit_entries_scrubbed_kept_and_one_written(self):
        self.erase(reason="Ticket 42")
        about_customer = AuditLog.objects.filter(object_id=self.customer.pk, action="update")
        self.assertEqual([e.changes for e in about_customer], [{"erased": True}])
        self.assertEqual(AuditLog.objects.get(pk=self.unrelated_audit.pk).changes, {"status": "paid"})
        entry = AuditLog.objects.get(action="erase", object_id=self.party.pk)
        self.assertEqual(entry.changes["reason"], "Ticket 42")
        self.assertEqual(entry.user.user_id, self.staff.pk)

    def test_reconcile_is_clean_and_a_later_save_does_not_resurrect(self):
        self.erase()
        customer = Customer.objects.get(pk=self.customer.pk)
        customer.pricing_tier = "wholesale"
        customer.save()
        CustomerContact.objects.get(pk=self.contact.pk).save()
        Lead.objects.get(pk=self.lead.pk).save()
        CustomerAddress.objects.get(pk=self.used_address.pk).save()
        drift = [i for i in check(self.company) if i.level == DRIFT]
        self.assertEqual(drift, [])
        self.assertFalse(PartyEmail.objects.filter(company=self.company, email__startswith="erased").exists())
        self.assertEqual(Party.objects.filter(company=self.company, erased_at__isnull=True,
                                              display_name__in=["Erased", ERASED_PARTY_NAME]).count(), 0)


class OrdersSurviveTests(_Seeded):
    def test_documents_survive_on_the_tombstone(self):
        result = self.erase()
        order = Order.objects.get(pk=self.order.pk)
        self.assertEqual(order.customer_id, self.customer.pk)
        self.assertEqual(order.billing_address_id, self.used_address.pk)
        self.assertEqual(order.grand_total, Decimal("120.00"))
        self.assertTrue(SalesInvoice.objects.filter(pk=self.invoice.pk, order=order).exists())
        quote = Quote.objects.get(pk=self.quote.pk)
        self.assertEqual((quote.customer_id, quote.sent_to_email, quote.accepted_by_name),
                         (self.customer.pk, "", ""))
        self.assertEqual(Order.objects.get(pk=self.guest_order.pk).guest_email, tombstone_email(self.party.pk))
        self.assertFalse(CustomerAddress.objects.filter(pk=self.spare_address.pk).exists())
        self.assertTrue(CustomerContact.objects.filter(pk=self.contact.pk).exists())
        orders = next(item for item in result.kept if item["model"] == "orders")
        self.assertEqual((orders["orders"], orders["quotes"], orders["invoices"]), (2, 1, 1))


@override_settings(GOLDEN_PARTY_SYNC_STRICT=True)
class ReimportSkippedTests(_Seeded):
    def test_refs_are_kept_flagged_and_keyed_by_hash(self):
        # A provider message id is not the person's: it stays as it is, so a
        # re-fetch finds the scrubbed communication instead of a new one.
        message_ct = ContentType.objects.get_for_model(Communication)
        refs = list(ExternalRef.objects.filter(company=self.company).exclude(
            content_type=message_ct).values_list("pk", flat=True))
        self.assertTrue(refs)
        self.erase()
        for ref in ExternalRef.objects.filter(pk__in=refs):
            self.assertTrue(ref.metadata.get("erased"), ref)
            self.assertTrue(ref.source_id.startswith("erased:"), ref)
        self.assertTrue(ExternalRef.objects.filter(content_type=message_ct, source_id="gm-zq-1").exists())
        for source, object_type, source_id in (("bigcommerce", "customer", BC_ID),
                                               ("erpnext", "customer", f"{GIVEN} {FAMILY}"),
                                               ("ebay", "buyer", EBAY),
                                               ("bigcommerce", "address", "BCA-7")):
            self.assertTrue(is_erased_source(self.company.pk, source, object_type, source_id), source_id)
        self.assertFalse(is_erased_source(self.other_company.pk, "bigcommerce", "customer", BC_ID))
        self.assertEqual(check_dangling(), [])

    def test_unify_import_of_an_erased_ref_is_skipped(self):
        self.erase()
        batch = ImportBatch.objects.create(company=self.company, source_kind="csv", source_name="crm.csv")
        erased = StagedRow.objects.create(
            batch=batch, row_number=1, raw={"title": "x"}, content_hash="h1",
            mapping_state=StagedRow.MappingState.APPROVED,
            mapped_values={"title": "Panel A", "external_ref": {
                "source": "bigcommerce", "object_type": "customer", "source_id": BC_ID}},
        )
        fresh = StagedRow.objects.create(
            batch=batch, row_number=2, raw={"title": "y"}, content_hash="h2",
            mapping_state=StagedRow.MappingState.APPROVED,
            mapped_values={"title": "Panel B", "external_ref": {
                "source": "bigcommerce", "object_type": "customer", "source_id": "BC-11111"}},
        )
        run = start_apply_run(batch, actor="tester")
        result = apply_chunk(str(run.pk), [])
        states = {r["row"]: r["state"] for r in result["results"]}
        self.assertEqual(states, {1: "skipped-erased", 2: "applied"})
        erased.refresh_from_db()
        fresh.refresh_from_db()
        self.assertEqual(erased.mapping_state, StagedRow.MappingState.NA)
        self.assertEqual(erased.applied_ref, "")
        self.assertEqual(fresh.mapping_state, StagedRow.MappingState.APPLIED)

    def test_bigcommerce_reimport_is_skipped(self):
        self.erase()
        before = (Customer.objects.count(), Order.objects.count())
        bc = {"customer_id": BC_ID, "billing_address": {"email": EMAIL, "first_name": GIVEN,
                                                         "last_name": FAMILY}}
        out = StringIO()
        call_command("import_bc_invoice", "743", bc_data=json.dumps(bc), stdout=out)
        self.assertIn("was erased", out.getvalue())
        self.assertNotIn(GIVEN, out.getvalue())
        self.assertEqual((Customer.objects.count(), Order.objects.count()), before)

    def test_a_new_customer_with_the_old_id_is_not_relinked(self):
        self.erase()
        Customer.objects.create(first_name="New", last_name="Person", email="new@example.test",
                                company=self.company, bc_id=BC_ID)
        self.assertFalse(ExternalRef.objects.filter(source="bigcommerce", source_id=BC_ID).exists())


def check_dangling():
    out = StringIO()
    try:
        call_command("check_external_refs", stdout=out)
    except SystemExit as exc:  # exits 1 when any ref dangles
        return [out.getvalue(), exc.code]
    return []


class SuppressionBlocksMarketingTests(_Seeded):
    def test_marketing_is_blocked_after_erasure_even_with_fresh_consent(self):
        self.assertTrue(can_contact(self.party, "email", "marketing", EMAIL).allowed)
        self.erase()
        # The same human comes back: a new party, consent granted, marketable.
        again = person(self.company, "New", "Signup", email=EMAIL)
        Party.objects.filter(pk=again.pk).update(marketable=True)
        again.refresh_from_db()
        ConsentRecord.objects.create(party=again, channel="email", purpose="marketing", state="granted",
                                     occurred_at="2026-10-08T12:00:00Z", source="portal")
        for address in (EMAIL, EMAIL_NORM, EXTRA_EMAIL):
            decision = can_contact(again, "email", "marketing", address)
            self.assertEqual((decision.allowed, decision.reason), (False, "suppressed:erasure"), address)
        self.assertFalse(can_contact(again, "sms", "marketing", PHONE).allowed)
        # A receipt for a new order still goes out (only hard_bounce / legal block those).
        self.assertTrue(can_contact(again, "email", "transactional", EMAIL).allowed)
        # Other tenants are unaffected.
        elsewhere = person(self.other_company, email=EMAIL)
        self.assertEqual(can_contact(elsewhere, "email", "marketing", EMAIL).reason, "no_consent")

    def test_suppressions_are_written_and_hold_no_hint(self):
        Suppression.objects.create(company=self.company, channel="email",
                                   address_hash=__import__("hashlib").sha256(EMAIL_NORM.encode()).hexdigest(),
                                   address_hint="z***@example.test", reason="opt_out", source="erp")
        result = self.erase()
        rows = Suppression.objects.filter(company=self.company)
        self.assertEqual(sorted(rows.values_list("channel", flat=True)), ["email", "email", "phone", "sms"])
        self.assertEqual(set(rows.values_list("address_hint", flat=True)), {""})
        self.assertEqual(result.counts["suppressions"], 3)  # the opt-out already covered one
        # The opt-out keeps its reason and is marked as an erasure, so an
        # importer matching by address still refuses to re-create the person.
        kept = rows.get(reason="opt_out")
        self.assertEqual(kept.source_detail, "erasure")
        from parties.erasure import erased_address

        self.assertTrue(erased_address(self.company.pk, EMAIL))


class SignalTests(_Seeded):
    def test_party_erased_sent_after_commit_with_ids_only(self):
        from core.plugins import signals

        if not hasattr(signals, "party_erased"):
            self.skipTest("party_erased arrives with CRM Phase 2 step 10")
        received = []

        def receiver(sender, **kwargs):
            received.append({k: v for k, v in kwargs.items() if k != "signal"})

        signals.party_erased.connect(receiver, weak=False)
        self.addCleanup(signals.party_erased.disconnect, receiver)
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            erase_party(self.party, self.staff, "why")
        self.assertEqual(received, [])  # nothing before commit
        for callback in callbacks:
            callback()
        self.assertEqual(received, [{"party_id": self.party.pk, "company_id": self.company.pk}])


class RefusalTests(_Seeded):
    def test_refusals_write_nothing(self):
        organisation = Party.objects.create(company=self.company, kind="organisation", display_name="Acme",
                                            origin="erp")
        cases = [
            (self.party, ""),  # no reason
            (organisation, "why"),
        ]
        for party, reason in cases:
            with self.subTest(party=party.display_name, reason=reason), self.assertRaises(ErasureError):
                erase_party(party, self.staff, reason)
        self.assertIsNone(Party.objects.get(pk=self.party.pk).erased_at)
        self.assertFalse(Suppression.objects.exists())
        self.erase()
        with self.assertRaisesMessage(ErasureError, "already been erased"):
            erase_party(Party.objects.get(pk=self.party.pk), self.staff, "again")

    def test_staff_login_refused(self):
        self.user.is_staff = True
        self.user.save()
        with self.assertRaisesMessage(ErasureError, "staff login"):
            erase_party(self.party, self.staff, "why")
        self.assertTrue(PartyEmail.objects.filter(party=self.party).exists())

    def test_login_shared_with_another_tenant_is_kept(self):
        other = person(self.other_company, "Zed", "Elsewhere", user=self.user)
        self.erase()
        user = User.objects.get(pk=self.user.pk)
        self.assertTrue(user.is_active)
        self.assertEqual(Party.objects.get(pk=other.pk).user_id, user.pk)
        self.assertIsNone(Party.objects.get(pk=self.party.pk).user_id)


class MergedPartyTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Tenant A")
        self.staff = make_staff_user("eraser", password="pw")
        self.survivor = person(self.company, "Sally", "Survivor", email="sally@example.test")
        self.loser = person(self.company, GIVEN, "", email=EMAIL)
        PartyPhone.objects.create(party=self.loser, number_raw=PHONE, purpose="personal", is_primary=True)
        self.customer = Customer.objects.bulk_create([Customer(
            company=self.company, first_name=GIVEN, last_name=FAMILY, email=EMAIL, party=self.loser)])[0]
        self.lead = Lead.objects.bulk_create([Lead(company=self.company, first_name=GIVEN, last_name=FAMILY,
                                                   email=EMAIL, lead_number="LD-M1", party=self.loser)])[0]
        # The merge took the loser's given name (a field pick) and moved everything.
        (self.log,) = merge(self.survivor, [self.loser], actor="tester",
                            field_picks={"given_name": str(self.loser.pk)})
        self.survivor.refresh_from_db()
        assert self.survivor.given_name == GIVEN

    def test_erasing_a_merged_away_party_erases_its_copies_on_the_survivor(self):
        with self.captureOnCommitCallbacks(execute=True):
            erase_party(Party._base_manager.get(pk=self.loser.pk), self.staff, "request")
        self.assertEqual(pii_hits((EMAIL_NORM, "202-555-0187", PHONE_E164, GIVEN, FAMILY)), [])
        survivor = Party.objects.get(pk=self.survivor.pk)
        self.assertIsNone(survivor.erased_at)
        self.assertEqual((survivor.given_name, survivor.family_name), ("Sally", "Survivor"))
        self.assertEqual(survivor.display_name, "Sally Survivor")
        self.assertEqual(list(survivor.emails.values_list("email_normalized", flat=True)), ["sally@example.test"])
        self.assertEqual(survivor.primary_email_normalized, "sally@example.test")
        self.assertEqual(survivor.primary_phone_e164, "")
        self.assertEqual(Customer.objects.get(pk=self.customer.pk).first_name, "Erased")
        self.assertEqual(Lead.objects.get(pk=self.lead.pk).first_name, "Erased")
        self.assertEqual(PartyMergeLog.objects.get(pk=self.log.pk).before, {})
        with self.assertRaisesMessage(MergeError, "erased"):
            unmerge(PartyMergeLog.objects.get(pk=self.log.pk), actor="tester")

    def test_erasing_the_survivor_erases_the_parties_merged_into_it(self):
        with self.captureOnCommitCallbacks(execute=True):
            result = erase_party(self.survivor, self.staff, "request")
        self.assertEqual(set(result.erased_party_ids), {self.survivor.pk, self.loser.pk})
        self.assertEqual(pii_hits((EMAIL_NORM, GIVEN, FAMILY, "sally@example.test", "Sally", "Survivor")), [])


@override_settings(API_KEY=API_KEY)
class ApiTests(_Seeded):
    def url(self, party=None):
        return f"{BASE}parties/{(party or self.party).pk}/erase/"

    def body(self, **kw):
        return {"confirm": self.party.display_name, "reason": "GDPR request", **kw}

    def test_with_the_permission(self):
        client = APIClient()
        client.force_authenticate(self.staff)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(self.url(), self.body(), format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["party_id"], str(self.party.pk))
        self.assertTrue(response.json()["kept"])
        self.assertIsNotNone(Party._base_manager.get(pk=self.party.pk).erased_at)
        self.assertEqual(Suppression.objects.filter(company=self.company).first().source, "api")

    def test_wrong_confirmation_is_400_and_erases_nothing(self):
        client = APIClient()
        client.force_authenticate(self.staff)
        response = client.post(self.url(), self.body(confirm="someone else"), format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(Party.objects.get(pk=self.party.pk).erased_at)

    def test_staff_without_the_permission_is_403(self):
        plain = make_staff_user("plain", password="pw")
        UserProfile.objects.update_or_create(user=plain, defaults={"company": self.company})
        client = APIClient()
        client.force_authenticate(plain)
        self.assertEqual(client.post(self.url(), self.body(), format="json").status_code, 403)
        self.assertIsNone(Party.objects.get(pk=self.party.pk).erased_at)

    def test_another_tenants_staff_gets_404(self):
        other = make_staff_user("other", password="pw")
        UserProfile.objects.update_or_create(user=other, defaults={"company": self.other_company})
        other.user_permissions.add(Permission.objects.get(codename="erase_party"))
        client = APIClient()
        client.force_authenticate(User.objects.get(pk=other.pk))
        self.assertEqual(client.post(self.url(), self.body(), format="json").status_code, 404)

    def test_storefront_identity_and_customers_get_403(self):
        from core.authentication import issue_api_jwt

        token = issue_api_jwt(sub="storefront")
        storefront = APIClient(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(storefront.post(self.url(), self.body(), format="json").status_code, 403)
        shopper = APIClient()
        shopper.force_login(self.user)
        self.assertEqual(shopper.post(self.url(), self.body(), format="json").status_code, 403)
        self.assertIsNone(Party.objects.get(pk=self.party.pk).erased_at)

    def test_patch_on_an_erased_party_is_refused(self):
        admin = User.objects.create_superuser("root", "root@example.com", "pw")
        client = APIClient()
        client.force_authenticate(admin)
        url = f"{BASE}parties/{self.party.pk}/"
        live = client.patch(url, {"tags": ["vip"]}, format="json")
        self.assertEqual(live.status_code, 200, live.content)
        self.erase()
        for body in ({"given_name": GIVEN}, {"metadata": {"note": EMAIL_NORM}}, {"tags": ["vip"]}):
            response = client.patch(url, body, format="json")
            self.assertEqual(response.status_code, 400, response.content)
            self.assertIn("erased", response.content.decode().lower())
        party = Party._base_manager.get(pk=self.party.pk)
        self.assertEqual((party.given_name, party.metadata, party.tags), ("", {}, []))

    def test_superuser(self):
        admin = User.objects.create_superuser("root", "root@example.com", "pw")
        client = APIClient()
        client.force_authenticate(admin)
        with self.captureOnCommitCallbacks(execute=True):
            response = client.post(self.url(), self.body(), format="json")
        self.assertEqual(response.status_code, 200, response.content)


class ErpDangerZoneTests(_Seeded):
    def detail(self):
        return reverse("erp-party-detail", args=[self.party.pk])

    def test_danger_zone_only_with_the_permission(self):
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(self.detail()), 'id="danger-zone"')
        plain = make_staff_user("plain", password="pw")
        UserProfile.objects.update_or_create(user=plain, defaults={"company": self.company})
        self.client.force_login(plain)
        self.assertNotContains(self.client.get(self.detail()), 'id="danger-zone"')
        response = self.client.post(reverse("erp-party-erase", args=[self.party.pk]),
                                    {"confirm": self.party.display_name, "reason": "x"})
        self.assertEqual(response.status_code, 403)

    def test_erase_with_typed_confirmation(self):
        self.client.force_login(self.staff)
        url = reverse("erp-party-erase", args=[self.party.pk])
        response = self.client.post(url, {"confirm": "nope", "reason": "x"})
        self.assertRedirects(response, self.detail() + "#danger-zone", fetch_redirect_response=False)
        self.assertIsNone(Party.objects.get(pk=self.party.pk).erased_at)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(url, {"confirm": self.party.display_name, "reason": "GDPR"}, follow=True)
        self.assertContains(response, "Erased on")
        self.assertNotContains(response, 'id="danger-zone"')
        self.assertIsNotNone(Party._base_manager.get(pk=self.party.pk).erased_at)


class CommandTests(_Seeded):
    def test_erase_party_command(self):
        out = StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("erase_party", party=str(self.party.pk), reason="GDPR #7", actor="eraser",
                         interactive=False, stdout=out)
        self.assertIn("Erased party", out.getvalue())
        self.assertNotIn(GIVEN, out.getvalue())
        self.assertIsNotNone(Party._base_manager.get(pk=self.party.pk).erased_at)

    def test_interactive_confirmation(self):
        with mock.patch("builtins.input", return_value="wrong"), self.assertRaises(CommandError):
            call_command("erase_party", party=str(self.party.pk), reason="x", stdout=StringIO())
        self.assertIsNone(Party.objects.get(pk=self.party.pk).erased_at)

    def test_errors(self):
        with self.assertRaisesMessage(CommandError, "No party"):
            call_command("erase_party", party="not-a-uuid", reason="x", interactive=False)
        org = Party.objects.create(company=self.company, kind="organisation", display_name="Acme", origin="erp")
        with self.assertRaisesMessage(CommandError, "Erasure is for people"):
            call_command("erase_party", party=str(org.pk), reason="x", interactive=False)


class ScopeTests(_Seeded):
    def test_other_tenants_audit_entries_are_untouched(self):
        # Per-tenant email uniqueness (step 11) lets another tenant hold the address.
        other_customer = Customer.objects.create(
            company=self.other_company, first_name="Same", last_name="Address", email=EMAIL_NORM)
        theirs = AuditLog.objects.create(action="update", model_name="customers.Customer",
                                         object_id=other_customer.pk, changes={"email": EMAIL_NORM})
        self.erase()
        self.assertEqual(AuditLog.objects.get(pk=theirs.pk).changes, {"email": EMAIL_NORM})

    def test_reason_may_not_repeat_the_persons_data(self):
        for reason in (f"{GIVEN} asked", f"mail from {EMAIL_NORM}"):
            with self.subTest(reason=reason), self.assertRaisesMessage(ErasureError, "must not repeat"):
                erase_party(self.party, self.staff, reason)
        self.assertIsNone(Party.objects.get(pk=self.party.pk).erased_at)

    def test_bigcommerce_guest_reimport_by_email_is_skipped(self):
        self.erase()
        before = Customer.objects.count()
        bc = {"customer_id": 0, "billing_address": {"email": EMAIL, "first_name": GIVEN, "last_name": FAMILY}}
        out = StringIO()
        call_command("import_bc_invoice", "744", bc_data=json.dumps(bc), stdout=out)
        self.assertIn("was erased", out.getvalue())
        self.assertEqual(Customer.objects.count(), before)


class BusinessAccountTests(TestCase):
    """A person who only holds a business account: the account stays open."""

    def setUp(self):
        self.company = Company.objects.create(name="Tenant A")
        self.staff = make_staff_user("eraser", password="pw")
        self.user = User.objects.create_user("holder", EMAIL_NORM, "pw")
        self.customer = Customer.objects.create(
            customer_type="business", company_name="Acme Solar", first_name=GIVEN, last_name=FAMILY,
            email=EMAIL, phone=PHONE, company=self.company, user=self.user, notes="Net 30 terms",
            is_tax_exempt=True, tax_exempt_reason="resale", tax_exempt_certificate_number="RESALE-1",
        )
        self.customer.refresh_from_db()
        self.person = Party.objects.get(pk=self.customer.account_holder_id)
        self.org = Party.objects.get(pk=self.customer.party_id)
        self.order = Order.objects.create(customer=self.customer, company=self.company,
                                          grand_total=Decimal("10.00"), customer_message="PO attached")

    def test_only_the_persons_fields_go(self):
        with self.captureOnCommitCallbacks(execute=True):
            erase_party(self.person, self.staff, "request")
        customer = Customer.objects.get(pk=self.customer.pk)
        self.assertTrue(customer.is_active)
        self.assertEqual((customer.company_name, customer.notes, customer.tax_exempt_certificate_number),
                         ("Acme Solar", "Net 30 terms", "RESALE-1"))
        self.assertEqual((customer.first_name, customer.last_name, customer.phone), ("Erased", "", ""))
        self.assertIsNone(customer.account_holder_id)
        self.assertIsNone(customer.user_id)
        self.assertEqual(Order.objects.get(pk=self.order.pk).customer_message, "PO attached")
        org = Party.objects.get(pk=self.org.pk)
        self.assertIsNone(org.erased_at)
        self.assertEqual(org.display_name, "Acme Solar")
        self.assertEqual(pii_hits((GIVEN, FAMILY, EMAIL_NORM, "202-555-0187", PHONE_E164)), [])


class LockOrderTests(_Seeded):
    """Erasure locks Customer → Party → User, the order portal login takes.

    Locking the Party first (as erasure once did) deadlocks against a
    concurrent login on Postgres, which aborts one side with an error.
    """

    @skipUnless(connection.features.has_select_for_update, "FOR UPDATE is a no-op on this backend")
    def test_erasure_locks_in_the_login_order(self):
        with CaptureQueriesContext(connection) as queries:
            erase_party(self.party, self.staff, reason="Art. 17 request")
        self.assertEqual(login_lock_order(queries), list(LOGIN_LOCK_ORDER))
        locks = [q["sql"] for q in queries if "FOR UPDATE" in q["sql"]]
        for sql in locks:
            self.assertNotIn(" JOIN ", sql)

    def test_a_footprint_that_grows_once_is_locked_again(self):
        from parties import erasure

        real, calls = erasure._lock_targets, []

        def growing(fp):
            calls.append(fp)
            customers, parties, users = real(fp)
            if len(calls) == 2:  # the re-collection under the first locks
                users = users | {-1}
            return customers, parties, users

        with mock.patch.object(erasure, "_lock_targets", side_effect=growing):
            erase_party(self.party, self.staff, reason="Art. 17 request")
        self.assertEqual(len(calls), 4)
        self.assertIsNotNone(Party.objects.get(pk=self.party.pk).erased_at)

    def test_a_footprint_that_keeps_moving_is_refused_whole(self):
        from parties import erasure

        real, calls = erasure._lock_targets, []

        def moving(fp):
            calls.append(fp)
            customers, parties, users = real(fp)
            return customers, parties, users | ({-len(calls)} if len(calls) % 2 == 0 else set())

        with mock.patch.object(erasure, "_lock_targets", side_effect=moving), \
                self.assertRaises(ErasureError):
            erase_party(self.party, self.staff, reason="Art. 17 request")
        self.assertEqual(len(calls), 2 * erasure.LOCK_ATTEMPTS)
        self.assertIsNone(Party.objects.get(pk=self.party.pk).erased_at)
        self.assertEqual(Customer.objects.get(pk=self.customer.pk).user, self.user)
