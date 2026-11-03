"""``parties.sync``: Customer / Contact / Address / Lead ``save()`` keep their
parties in step (phase2-party.md 2.1, 2.5, decision P2)."""

from __future__ import annotations

import uuid
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from core.models import Company, ExternalRef
from core.plugins import signals
from crm.models import Lead
from customers.models import Customer, CustomerAddress, CustomerContact
from parties.models import ConsentRecord, Party, PartyAddress, PartyEmail, PartyRelationship, PartyRole


class Recorder:
    def __init__(self, test, signal):
        self.calls = []
        signal.connect(self, weak=False)
        test.addCleanup(signal.disconnect, self)

    def __call__(self, sender, signal=None, **kwargs):
        self.calls.append(kwargs)


class _Base(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")

    def customer(self, **kw):
        kw.setdefault("first_name", "Ada")
        kw.setdefault("last_name", "Lovelace")
        kw.setdefault("email", f"{uuid.uuid4().hex[:8]}@example.com")
        kw.setdefault("company", self.company)
        return Customer.objects.create(**kw)


class CustomerSyncTests(_Base):
    def test_customer_registered_fires_once_per_create(self):
        registered = Recorder(self, signals.customer_registered)
        with self.captureOnCommitCallbacks(execute=True):
            c = self.customer()
            c.first_name = "Augusta"
            c.save()
        self.assertEqual(len(registered.calls), 1)
        self.assertEqual(registered.calls[0]["customer"].pk, c.pk)

    def test_individual_gets_a_person_and_no_holder_without_login(self):
        c = self.customer(phone="2125550143", email="Ada@Example.com")
        c.refresh_from_db()
        self.assertEqual((c.party.kind, c.party.display_name, c.party.origin), ("person", "Ada Lovelace", "erp"))
        self.assertIsNone(c.account_holder_id)
        self.assertEqual(c.party.primary_email_normalized, "ada@example.com")
        self.assertEqual(c.party.primary_phone_e164, "+12125550143")
        self.assertTrue(c.party.roles.filter(role="customer", valid_to__isnull=True).exists())

    def test_login_makes_the_person_the_account_holder_with_the_user(self):
        user = User.objects.create_user("portal")
        c = self.customer(user=user)
        c.refresh_from_db()
        self.assertEqual(c.account_holder_id, c.party_id)
        self.assertEqual(c.account_holder.user_id, user.pk)

    def test_mirrored_fields_follow_the_customer(self):
        c = self.customer(email="old@example.com", phone="2125550100")
        c.first_name, c.last_name, c.email, c.phone = "Augusta", "King", "new@example.com", "2125550199"
        c.save()
        p = Party.objects.get(pk=c.party_id)
        self.assertEqual((p.given_name, p.family_name, p.display_name), ("Augusta", "King", "Augusta King"))
        self.assertEqual(list(p.emails.values_list("email_normalized", flat=True)), ["new@example.com"])
        self.assertEqual(list(p.phones.values_list("number_e164", flat=True)), ["+12125550199"])
        self.assertEqual((p.primary_email_normalized, p.primary_phone_e164), ("new@example.com", "+12125550199"))

    def test_business_gets_organisation_and_person(self):
        c = self.customer(customer_type="business", company_name="Widgets", job_title="CEO")
        c.refresh_from_db()
        self.assertEqual((c.party.kind, c.party.display_name), ("organisation", "Widgets"))
        rel = PartyRelationship.objects.get(to_party=c.party)
        self.assertEqual((rel.kind, rel.title, rel.metadata["customer"]), ("employment", "CEO", str(c.pk)))
        c.company_name = "Widgets Ltd"
        c.save()
        self.assertEqual(Party.objects.get(pk=c.party_id).display_name, "Widgets Ltd")

    def test_type_switch_moves_the_role(self):
        c = self.customer()
        person_id = c.party_id
        c.customer_type, c.company_name = "business", "Org"
        c.save()
        c.refresh_from_db()
        self.assertEqual(c.party.kind, "organisation")
        self.assertTrue(PartyRole.objects.filter(party_id=person_id, role="customer").exclude(
            valid_to__isnull=True).exists())
        c.customer_type = "individual"
        c.save()
        c.refresh_from_db()
        self.assertEqual(c.party_id, person_id)
        self.assertFalse(PartyRelationship.objects.filter(metadata__customer=str(c.pk), valid_to__isnull=True).exists())

    def test_deactivation_closes_and_reactivation_reopens_the_role(self):
        c = self.customer()
        c.is_active = False
        c.save()
        self.assertFalse(PartyRole.objects.filter(party_id=c.party_id, valid_to__isnull=True).exists())
        c.is_active = True
        c.save()
        self.assertTrue(PartyRole.objects.filter(party_id=c.party_id, valid_to__isnull=True).exists())

    def test_marketing_flag_appends_consent(self):
        c = self.customer()
        self.assertFalse(ConsentRecord.objects.exists())  # False = unknown: nothing written
        c.accepts_marketing = True
        c.save()
        c.accepts_marketing = False
        c.save()
        states = list(ConsentRecord.objects.order_by("recorded_at").values_list("state", "source"))
        self.assertEqual(states, [("granted", "erp"), ("withdrawn", "erp")])
        self.assertTrue(Party.objects.get(pk=c.party_id).marketable)

    def test_legacy_ids_dual_written_and_replaced(self):
        c = self.customer(bc_id="10")
        ct = ContentType.objects.get_for_model(Customer)
        self.assertTrue(ExternalRef.objects.filter(content_type=ct, object_id=c.pk, source_id="10").exists())
        c.bc_id = "11"
        c.save()
        self.assertEqual(list(ExternalRef.objects.filter(content_type=ct, object_id=c.pk).values_list(
            "source_id", flat=True)), ["11"])

    def test_duplicate_legacy_id_is_not_linked(self):
        self.customer(bc_id="10")
        with self.assertLogs("parties.sync", "WARNING"):
            second = self.customer(bc_id="10")
        ct = ContentType.objects.get_for_model(Customer)
        self.assertFalse(ExternalRef.objects.filter(content_type=ct, object_id=second.pk).exists())

    def test_new_customer_reuses_a_lead_party(self):
        lead = Lead.objects.create(first_name="Lee", last_name="Ead", email="lee@example.com", company=self.company)
        c = self.customer(email="LEE@example.com", first_name="Lee", last_name="Ead")
        lead.refresh_from_db()
        self.assertEqual(c.party_id, lead.party_id)

    def test_new_customer_never_reuses_another_customers_party(self):
        a = self.customer(email="same@example.com")
        Customer.objects.filter(pk=a.pk).update(email="moved@example.com")  # frees the unique email
        b = self.customer(email="same@example.com")
        self.assertNotEqual(a.party_id, b.party_id)

    def test_new_customer_never_reuses_a_staff_party(self):
        staff = Party.objects.create(company=self.company, kind="person", display_name="S", origin="backfill")
        PartyEmail.objects.create(party=staff, email="s@example.com", purpose="work")
        PartyRole.objects.create(party=staff, role="staff", valid_from="2026-01-01")
        c = self.customer(email="s@example.com")
        self.assertNotEqual(c.party_id, staff.pk)

    def test_null_company_gets_the_only_company(self):
        c = self.customer(company=None)
        c.refresh_from_db()
        self.assertEqual((c.company_id, c.party.company_id), (self.company.pk, self.company.pk))

    def test_null_company_with_two_companies_is_refused(self):
        # Step 11: company is NOT NULL and no tenant can be chosen.
        Company.objects.create(name="Other")
        parties = Party.objects.count()
        with self.assertRaisesMessage(IntegrityError, "GOLDEN_PORTAL_COMPANY"):
            self.customer(company=None)
        self.assertEqual(Party.objects.count(), parties)
        self.assertFalse(Customer.objects.exists())

    def test_unrelated_update_fields_skip_the_sync(self):
        c = self.customer()
        with mock.patch("parties.sync.sync_from_customer") as sync:
            c.save(update_fields=["last_order_at"])
            sync.assert_not_called()
            c.save(update_fields=["email"])
            sync.assert_called_once()

    @override_settings(GOLDEN_PARTY_SYNC_STRICT=False)
    def test_a_sync_failure_never_blocks_a_customer_update(self):
        c = self.customer()
        with mock.patch("parties.sync.customer_person", side_effect=RuntimeError("boom")), \
                self.assertLogs("parties.sync", "ERROR"):
            c.first_name = "Augusta"
            c.save()
        self.assertTrue(Customer.objects.filter(pk=c.pk, first_name="Augusta", party_id=c.party_id).exists())

    @override_settings(GOLDEN_PARTY_SYNC_STRICT=False)
    def test_a_sync_failure_blocks_a_new_customer(self):
        # Step 11: party is NOT NULL, so a create needs the sync to succeed.
        with mock.patch("parties.sync.customer_person", side_effect=RuntimeError("boom")), \
                self.assertRaises(RuntimeError), transaction.atomic():
            self.customer()
        self.assertFalse(Customer.objects.exists())
        self.assertFalse(Party.objects.filter(kind="person").exists())

    def test_queryset_update_is_drift_not_sync(self):
        c = self.customer()
        Customer.objects.filter(pk=c.pk).update(first_name="Changed")
        self.assertEqual(Party.objects.get(pk=c.party_id).given_name, "Ada")


class ContactSyncTests(_Base):
    def setUp(self):
        super().setUp()
        self.c = self.customer(customer_type="business", company_name="Org", email="boss@org.example")

    def test_a1_contact_is_the_customer_person(self):
        ct = CustomerContact.objects.create(customer=self.c, first_name="B", email="Boss@org.example",
                                            phone="2125550101")
        person = PartyRelationship.objects.get(metadata__customer=str(self.c.pk)).from_party
        self.assertEqual(ct.party_id, person.pk)
        self.assertEqual(person.given_name, "Ada")  # an A1 contact never overwrites the customer's names
        self.assertTrue(person.phones.filter(number_e164="+12125550101").exists())

    def test_a2_and_own_party(self):
        a = CustomerContact.objects.create(customer=self.c, first_name="A", email="ap@org.example",
                                           job_title="Clerk")
        b = CustomerContact.objects.create(customer=self.c, first_name="B", email="AP@org.example")
        self.assertEqual(a.party_id, b.party_id)
        rel = PartyRelationship.objects.get(from_party_id=a.party_id)
        self.assertEqual((rel.kind, rel.to_party_id, rel.title), ("employment", self.c.party_id, "Clerk"))
        a.first_name, a.job_title = "Alice", "Head clerk"
        a.save()
        self.assertEqual(Party.objects.get(pk=a.party_id).given_name, "Alice")
        rel.refresh_from_db()
        self.assertEqual(rel.title, "Head clerk")
        b.first_name = "Bob"
        b.save()  # not the owner: names stay
        self.assertEqual(Party.objects.get(pk=a.party_id).given_name, "Alice")


class AddressSyncTests(_Base):
    def test_create_update_delete(self):
        c = self.customer()
        a = CustomerAddress.objects.create(customer=c, address_line_1="1 Main", city="X", state_province="IL",
                                           postal_code="1", country="us", address_type="billing", is_default=True)
        pa = PartyAddress.objects.get(pk=a.party_address_id)
        self.assertEqual((pa.party_id, pa.purpose, pa.country, pa.is_primary), (c.party_id, "billing", "US", True))
        a.city = "Y"
        a.save()
        pa.refresh_from_db()
        self.assertEqual(pa.city, "Y")
        a.delete()
        self.assertFalse(PartyAddress.objects.filter(pk=pa.pk).exists())


class LeadSyncTests(_Base):
    def test_new_lead_creates_a_person_with_an_open_role(self):
        lead = Lead.objects.create(first_name="Lee", last_name="Ead", email="lee@example.com", company=self.company)
        self.assertEqual((lead.party.kind, lead.party.origin), ("person", "lead_form"))
        role = PartyRole.objects.get(party=lead.party, role="lead")
        self.assertIsNone(role.valid_to)
        lead.status = "converted"
        lead.save()
        role.refresh_from_db()
        self.assertIsNotNone(role.valid_to)

    def test_lead_matching_a_customer_links_to_it(self):
        c = self.customer(email="buyer@example.com")
        lead = Lead.objects.create(first_name="B", last_name="U", email="buyer@example.com", company=self.company)
        self.assertEqual(lead.party_id, c.party_id)

    def test_lead_without_company_stays_unlinked(self):
        lead = Lead.objects.create(first_name="N", last_name="C", email="n@example.com")
        self.assertIsNone(lead.party_id)


class SelfPartyRenameTests(_Base):
    def test_company_rename(self):
        Party.objects.create(company=self.company, kind="organisation", display_name="Acme", is_self=True,
                             origin="system")
        self.company.name = "Acme 2"
        self.company.save()
        self.assertEqual(Party.objects.get(is_self=True).display_name, "Acme 2")


class ImportPathTests(_Base):
    def bc(self, email="bc@example.com"):
        import json

        return json.dumps({
            "customer_id": 77, "date_created": "Wed, 06 Aug 2025 18:38:09 +0000",
            "subtotal_ex_tax": "10", "shipping_cost_ex_tax": "0", "total_tax": "0", "total_inc_tax": "10",
            "billing_address": {"first_name": "Bea", "last_name": "Cee", "email": email, "phone": "2125550123",
                                "street_1": "1 Main", "city": "X", "state": "IL", "zip": "62701",
                                "country_iso2": "US"},
        })

    def test_import_bc_invoice_creates_and_resyncs_the_party(self):
        import io

        from django.core.management import call_command

        from accounting.tests.test_posting import make_posting_rules

        make_posting_rules(self.company)
        call_command("import_bc_invoice", "901", "--bc-data", self.bc(), stdout=io.StringIO())
        c = Customer.objects.get(email="bc@example.com")
        self.assertEqual((c.party.origin, c.party.primary_phone_e164), ("import", "+12125550123"))
        self.assertTrue(ExternalRef.objects.filter(source="bigcommerce", object_type="customer", source_id="77",
                                                   object_id=c.pk).exists())
        self.assertTrue(PartyAddress.objects.filter(party=c.party, postal_code="62701").exists())
        # A bypassing write, then a second import of the same customer re-syncs it.
        Customer.objects.filter(pk=c.pk).update(first_name="Beatrice")
        call_command("import_bc_invoice", "902", "--bc-data", self.bc(), stdout=io.StringIO())
        self.assertEqual(Party.objects.get(pk=c.party_id).given_name, "Beatrice")


class ContactOwnedPartyTests(_Base):
    def test_new_customer_never_adopts_a_contact_owned_party(self):
        owner = self.customer(customer_type="business", company_name="Org")
        ct = CustomerContact.objects.create(customer=owner, first_name="K", email="k@example.com")
        c = self.customer(email="k@example.com", first_name="Other")
        self.assertNotEqual(c.party_id, ct.party_id)
