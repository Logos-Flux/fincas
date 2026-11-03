"""Step 5 data steps D2-D10 (phase2-party.md section 4): forward, reverse,
idempotent, and no signals.

Test settings disable migrations, so the steps run against the live app
registry (the same models the migration state has at ``parties.0002``); the
real ``migrate`` path runs on Postgres in CI's ``migrations-postgres`` job.
Source rows are written with ``bulk_create`` (``parties.tests.factories``)
so no live sync runs and they look like pre-Phase-2 data.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib
import io

from django.apps import apps
from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.signals import post_save
from django.test import TestCase, override_settings

from core.models import Company, ExternalRef, UserProfile
from core.plugins import signals
from crm.models import Lead
from customers.models import Customer, CustomerAddress, CustomerContact
from customers.pre_tighten import PreTightenSchemaMixin
from parties.backfill import STEPS, BackfillRefused, format_line, run_step
from parties.models import ConsentRecord, Party, PartyAddress, PartyEmail, PartyPhone, PartyRelationship, PartyRole

from .factories import legacy_address, legacy_contact, legacy_customer, legacy_lead

MIGRATION = importlib.import_module("parties.migrations.0002_backfill")


def forward(*steps):
    return {n: run_step(apps, n) for n in steps}


def reverse(*steps):
    return {n: run_step(apps, n, reverse=True) for n in sorted(steps, reverse=True)}


def snapshot():
    """Row counts of everything the backfill writes."""
    return {
        m.__name__: m.objects.count()
        for m in (Party, PartyEmail, PartyPhone, PartyAddress, PartyRole, PartyRelationship, ConsentRecord, ExternalRef)
    } | {
        "customer_links": Customer.objects.filter(party__isnull=False).count(),
        "holders": Customer.objects.filter(account_holder__isnull=False).count(),
        "contact_links": CustomerContact.objects.filter(party__isnull=False).count(),
        "address_links": CustomerAddress.objects.filter(party_address__isnull=False).count(),
        "lead_links": Lead.objects.filter(party__isnull=False).count(),
        "party_users": Party.objects.filter(user__isnull=False).count(),
    }


class _Base(PreTightenSchemaMixin, TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")
        self.empty = snapshot()

    def assert_round_trip(self, *steps):
        """Forward twice == forward once; reverse restores the start; forward again works."""
        before = snapshot()
        forward(*steps)
        once = snapshot()
        forward(*steps)
        self.assertEqual(snapshot(), once, "a second forward pass changed something")
        reverse(*steps)
        self.assertEqual(snapshot(), before, "reverse did not restore the starting state")
        forward(*steps)
        self.assertEqual(snapshot(), once, "forward after reverse differs from the first forward")
        return once


class SelfPartyTests(_Base):
    def test_one_self_party_per_company(self):
        other = Company.objects.create(name="Other")
        self.assert_round_trip(2)
        for co in (self.company, other):
            p = Party.objects.get(company=co, is_self=True)
            self.assertEqual((p.kind, p.display_name, p.origin), ("organisation", co.name, "system"))

    def test_rename_follows_company(self):
        forward(2)
        self.company.name = "Acme Holdings"
        self.company.save()
        self.assertEqual(Party.objects.get(is_self=True).display_name, "Acme Holdings")


class CustomerPartyTests(_Base):
    def test_individual_without_login_has_no_account_holder(self):
        c = legacy_customer(self.company, first_name="Ada", last_name="Lovelace", email="Ada@Example.com",
                            phone="(212) 555-0143", is_active=True)
        forward(3)
        c.refresh_from_db()
        p = c.party
        self.assertEqual((p.kind, p.given_name, p.family_name, p.display_name, p.origin),
                         ("person", "Ada", "Lovelace", "Ada Lovelace", "backfill"))
        self.assertIsNone(c.account_holder_id)  # no portal login
        self.assertEqual(p.primary_email_normalized, "ada@example.com")
        self.assertEqual(p.primary_phone_e164, "+12125550143")
        email = p.emails.get()
        self.assertEqual((email.email, email.purpose, email.is_primary), ("Ada@Example.com", "billing", True))
        role = p.roles.get()
        self.assertEqual((role.role, role.valid_from, role.valid_to), ("customer", c.created_at.date(), None))

    def test_login_customer_is_its_own_account_holder(self):
        user = User.objects.create_user("portal-1")
        c = legacy_customer(self.company, user=user)
        forward(3)
        c.refresh_from_db()
        self.assertEqual(c.account_holder_id, c.party_id)

    def test_business_customer_gets_an_organisation(self):
        c = legacy_customer(self.company, customer_type="business", company_name="Widgets Inc.", job_title="Buyer")
        forward(3)
        c.refresh_from_db()
        org = c.party
        self.assertEqual((org.kind, org.display_name), ("organisation", "Widgets Inc."))
        self.assertIsNone(c.account_holder_id)
        rel = PartyRelationship.objects.get(to_party=org)
        self.assertEqual((rel.kind, rel.title, rel.from_party.kind, rel.source),
                         ("employment", "Buyer", "person", "customer-sync"))
        self.assertEqual(rel.metadata["customer"], str(c.pk))
        self.assertEqual(rel.from_party.primary_email_normalized, c.email)  # the commerce email is on the person
        self.assertTrue(org.roles.filter(role="customer", valid_to__isnull=True).exists())

    def test_business_with_login_holder_is_the_person(self):
        c = legacy_customer(self.company, customer_type="business", company_name="W", user=User.objects.create_user("u"))
        forward(3)
        c.refresh_from_db()
        self.assertEqual((c.party.kind, c.account_holder.kind), ("organisation", "person"))

    def test_blank_company_name_is_inferred_and_flagged(self):
        c = legacy_customer(self.company, customer_type="business", company_name="  ")
        forward(3)
        c.refresh_from_db()
        self.assertEqual(c.party.display_name, "Ada Lovelace")
        self.assertTrue(c.party.metadata["name_inferred"])

    def test_inactive_customer_gets_a_closed_role(self):
        c = legacy_customer(self.company, is_active=False)
        forward(3)
        c.refresh_from_db()
        self.assertIsNotNone(c.party.roles.get(role="customer").valid_to)

    def test_invalid_email_is_reported_not_written(self):
        legacy_customer(self.company, email="not-an-email")
        report = forward(3)[3]
        self.assertEqual(PartyEmail.objects.count(), 0)
        self.assertEqual(report["invalid emails (no PartyEmail)"], 1)

    def test_phone_parses_with_the_address_country(self):
        c = legacy_customer(self.company, phone="020 7946 0018")
        legacy_address(c, country="GB", is_default=True)
        forward(3)
        self.assertEqual(PartyPhone.objects.get().number_e164, "+442079460018")

    def test_unparseable_phone_keeps_raw(self):
        legacy_customer(self.company, phone="call me")
        forward(3)
        phone = PartyPhone.objects.get()
        self.assertEqual((phone.number_raw, phone.number_e164), ("call me", ""))

    def test_null_company_assigned_to_the_only_company_and_restored(self):
        c = legacy_customer(None)
        report = forward(3)[3]
        c.refresh_from_db()
        self.assertEqual(c.company_id, self.company.pk)
        self.assertEqual(c.party.company_id, self.company.pk)
        self.assertEqual(report["NULL-company customers assigned"], 1)
        reverse(3)
        c.refresh_from_db()
        self.assertIsNone(c.company_id)

    def test_null_company_with_two_companies_refuses(self):
        Company.objects.create(name="Second")
        c = legacy_customer(None)
        with self.assertRaisesMessage(BackfillRefused, str(c.pk)):
            forward(3)

    def test_null_company_uses_portal_company_setting(self):
        second = Company.objects.create(name="Second")
        c = legacy_customer(None)
        with override_settings(GOLDEN_PORTAL_COMPANY=str(second.pk)):
            forward(3)
        c.refresh_from_db()
        self.assertEqual(c.company_id, second.pk)

    def test_round_trip(self):
        legacy_customer(self.company, phone="2125550143", user=User.objects.create_user("x"))
        legacy_customer(self.company, customer_type="business", company_name="Org")
        legacy_customer(self.company, customer_type="business", company_name="")
        legacy_customer(None)
        self.assert_round_trip(3)


class ContactPartyTests(_Base):
    def setUp(self):
        super().setUp()
        self.customer = legacy_customer(self.company, customer_type="business", company_name="Org",
                                        email="boss@org.example")

    def test_a1_contact_with_the_customer_email_is_the_customer_person(self):
        ct = legacy_contact(self.customer, email="BOSS@org.example", phone="2125550101")
        forward(3, 4)
        ct.refresh_from_db()
        person = PartyRelationship.objects.get(source="customer-sync").from_party
        self.assertEqual(ct.party_id, person.pk)
        self.assertTrue(person.phones.filter(number_e164="+12125550101").exists())

    def test_a2_contacts_sharing_an_email_share_a_party(self):
        a = legacy_contact(self.customer, email="ap@org.example")
        b = legacy_contact(self.customer, email="AP@org.example", first_name="Other")
        report = forward(3, 4)[4]
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.party_id, b.party_id)
        self.assertEqual(report["folded by A2"], 1)

    def test_other_contact_is_employed_by_the_organisation(self):
        ct = legacy_contact(self.customer, email="clerk@org.example", job_title="Clerk", is_primary=True)
        forward(3, 4)
        ct.refresh_from_db()
        self.customer.refresh_from_db()
        rel = PartyRelationship.objects.get(from_party=ct.party)
        self.assertEqual((rel.kind, rel.to_party_id, rel.title, rel.is_primary),
                         ("employment", self.customer.party_id, "Clerk", True))

    def test_individual_customer_contact_is_contact_for(self):
        c = legacy_customer(self.company)
        ct = legacy_contact(c, email="spouse@example.com")
        forward(3, 4)
        ct.refresh_from_db()
        self.assertEqual(PartyRelationship.objects.get(from_party=ct.party).kind, "contact_for")

    def test_round_trip(self):
        legacy_contact(self.customer, email="boss@org.example", phone="2125550101")
        legacy_contact(self.customer, email="x@org.example")
        legacy_contact(self.customer, email="X@org.example")
        legacy_contact(self.customer, email="")
        forward(3)
        self.assert_round_trip(4)


class AddressTests(_Base):
    def test_addresses_land_on_the_countersigning_party(self):
        c = legacy_customer(self.company, customer_type="business", company_name="Org")
        both = legacy_address(c, address_type="both", is_default=True, label="HQ", country="us")
        bill = legacy_address(c, address_type="billing")
        forward(3, 5)
        c.refresh_from_db()
        both.refresh_from_db()
        bill.refresh_from_db()
        self.assertEqual(both.party_address.party_id, c.party_id)
        self.assertEqual((both.party_address.purpose, both.party_address.is_primary, both.party_address.country,
                          both.party_address.label), ("shipping", True, "US", "HQ"))
        self.assertEqual((bill.party_address.purpose, bill.party_address.is_primary), ("billing", False))

    def test_round_trip(self):
        c = legacy_customer(self.company)
        legacy_address(c, is_default=True)
        legacy_address(c, is_default=True)
        forward(3)
        self.assert_round_trip(5)


class StaffTests(_Base):
    def test_staff_party_with_role_and_employment(self):
        forward(2)
        user = User.objects.create_user("sam", "Sam@Acme.example", first_name="Sam", last_name="Staff")
        UserProfile.objects.create(user=user, company=self.company, display_name="Sam")
        UserProfile.objects.create(display_name="no user", company=self.company)
        self.assert_round_trip(6)
        p = Party.objects.get(user=user)
        self.assertEqual((p.display_name, p.primary_email_normalized), ("Sam Staff", "sam@acme.example"))
        self.assertTrue(p.roles.filter(role="staff", valid_to__isnull=True).exists())
        rel = PartyRelationship.objects.get(from_party=p)
        self.assertEqual((rel.kind, rel.to_party.is_self), ("employment", True))


class PortalLoginTests(_Base):
    def test_holder_gets_the_customer_login(self):
        user = User.objects.create_user("portal")
        c = legacy_customer(self.company, user=user)
        forward(3)
        self.assert_round_trip(7)
        c.refresh_from_db()
        self.assertEqual(c.account_holder.user_id, user.pk)

    def test_login_already_on_a_staff_party_is_a_conflict(self):
        user = User.objects.create_user("both")
        UserProfile.objects.create(user=user, company=self.company)
        c = legacy_customer(self.company, user=user)
        report = forward(2, 3, 6, 7)[7]
        c.refresh_from_db()
        self.assertIsNone(c.account_holder.user_id)
        self.assertEqual(report["login conflicts (not linked)"], 1)

    def test_holder_set_for_a_login_that_arrived_after_d3(self):
        c = legacy_customer(self.company)
        forward(3)
        user = User.objects.create_user("later")
        Customer.objects.filter(pk=c.pk).update(user=user)
        forward(7)
        c.refresh_from_db()
        self.assertEqual((c.account_holder_id, c.account_holder.user_id), (c.party_id, user.pk))


class ExternalRefTests(_Base):
    def test_legacy_ids_become_refs(self):
        c = legacy_customer(self.company, bc_id="17", erpnext_name="CUST-1", ebay_username="buyer9",
                            stripe_customer_id="cus_1")
        a = legacy_address(c, bc_address_id="88")
        forward(8)
        ct = ContentType.objects.get_for_model(Customer)
        got = set(ExternalRef.objects.filter(content_type=ct, object_id=c.pk).values_list("source", "object_type",
                                                                                          "source_id"))
        self.assertEqual(got, {("bigcommerce", "customer", "17"), ("erpnext", "customer", "CUST-1"),
                               ("ebay", "buyer", "buyer9"), ("stripe", "customer", "cus_1")})
        ref = ExternalRef.objects.get(source="bigcommerce", object_type="address")
        self.assertEqual(ref.object_id, a.pk)
        self.assertTrue(ref.metadata["backfill"])

    def test_duplicate_source_key_reported_not_written(self):
        legacy_customer(self.company, bc_id="5")
        legacy_customer(self.company, bc_id="5")
        report = forward(8)[8]
        self.assertFalse(ExternalRef.objects.filter(source="bigcommerce").exists())
        self.assertEqual(report["duplicate source keys (reported, not written)"], 1)

    def test_same_id_in_two_tenants_is_not_a_duplicate(self):
        other = Company.objects.create(name="Other")
        legacy_customer(self.company, bc_id="5")
        legacy_customer(other, bc_id="5")
        forward(8)
        self.assertEqual(ExternalRef.objects.filter(source="bigcommerce").count(), 2)

    def test_round_trip(self):
        legacy_customer(self.company, bc_id="1", stripe_customer_id="cus_x")
        self.assert_round_trip(8)


class ConsentTests(_Base):
    def test_opt_in_becomes_a_granted_record_and_marketable(self):
        c = legacy_customer(self.company, accepts_marketing=True)
        legacy_customer(self.company, accepts_marketing=False)
        forward(3, 9)
        c.refresh_from_db()
        rec = ConsentRecord.objects.get()
        self.assertEqual((rec.party_id, rec.channel, rec.purpose, rec.state, rec.source, rec.occurred_at),
                         (c.party_id, "email", "marketing", "granted", "backfill", c.created_at))
        self.assertEqual(rec.evidence, {"inferred_from": "accepts_marketing", "precision": "unknown"})
        self.assertTrue(c.party.marketable)
        self.assertEqual(c.party.marketable_source, "legacy:accepts_marketing")

    def test_business_consent_goes_on_the_person(self):
        c = legacy_customer(self.company, customer_type="business", company_name="O", accepts_marketing=True)
        forward(3, 9)
        self.assertEqual(ConsentRecord.objects.get().party.kind, "person")
        c.refresh_from_db()
        self.assertFalse(c.party.marketable)

    def test_round_trip(self):
        legacy_customer(self.company, accepts_marketing=True)
        forward(3)
        self.assert_round_trip(9)


class LeadTests(_Base):
    def test_lead_with_a_customer_email_links_to_the_customer_person(self):
        c = legacy_customer(self.company, email="buyer@example.com")
        lead = legacy_lead(self.company, email="Buyer@Example.com")
        forward(3, 10)
        lead.refresh_from_db()
        c.refresh_from_db()
        self.assertEqual(lead.party_id, c.party_id)
        self.assertTrue(c.party.roles.filter(role="lead", valid_to__isnull=True).exists())

    def test_new_lead_gets_a_person_party_and_open_role(self):
        lead = legacy_lead(self.company, email="new@example.com", phone="2125550199")
        forward(10)
        lead.refresh_from_db()
        p = lead.party
        self.assertEqual((p.kind, p.primary_email_normalized, p.primary_phone_e164), ("person", "new@example.com",
                                                                                      "+12125550199"))
        self.assertIsNone(p.roles.get(role="lead").valid_to)

    def test_two_leads_one_email_share_a_party(self):
        a = legacy_lead(self.company, email="same@example.com")
        b = legacy_lead(self.company, email="same@example.com")
        forward(10)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.party_id, b.party_id)
        self.assertEqual(PartyRole.objects.filter(role="lead", valid_to__isnull=True).count(), 1)

    def test_closed_lead_role_is_dated(self):
        lead = legacy_lead(self.company, status="closed_lost")
        lost = lead.created_at + datetime.timedelta(days=3)
        Lead.objects.filter(pk=lead.pk).update(lost_at=lost)
        forward(10)
        role = PartyRole.objects.get(role="lead")
        self.assertEqual((role.valid_from, role.valid_to), (lead.created_at.date(), lost.date()))

    def test_lead_without_company_is_skipped(self):
        legacy_lead(None)
        report = forward(10)[10]
        self.assertEqual(report["leads without a company (skipped)"], 1)
        self.assertFalse(Party.objects.exists())

    def test_ambiguous_lead_is_left_unlinked(self):
        a = legacy_customer(self.company, email="shared@example.com")
        b = legacy_customer(self.company, email="other@example.com")
        forward(3)
        b.refresh_from_db()
        PartyEmail.objects.create(party=b.party, email="shared@example.com", purpose="work")
        lead = legacy_lead(self.company, email="shared@example.com")
        report = forward(10)[10]
        lead.refresh_from_db()
        self.assertIsNone(lead.party_id)
        self.assertEqual(report["ambiguous (match-N, left unlinked)"], 1)
        del a

    def test_round_trip(self):
        legacy_customer(self.company, email="c@example.com")
        legacy_lead(self.company, email="c@example.com")
        legacy_lead(self.company, email="n@example.com", status="converted")
        forward(3)
        self.assert_round_trip(10)


class WholeBackfillTests(_Base):
    def _world(self):
        user = User.objects.create_user("p", "p@example.com")
        c = legacy_customer(self.company, user=user, bc_id="1", accepts_marketing=True, phone="2125550100")
        legacy_address(c, is_default=True)
        b = legacy_customer(self.company, customer_type="business", company_name="Org", email="o@org.example")
        legacy_contact(b, email="o@org.example")
        legacy_contact(b, email="k@org.example")
        legacy_lead(self.company, email="o@org.example")
        staff = User.objects.create_user("s", "s@acme.example")
        UserProfile.objects.create(user=staff, company=self.company)

    def test_all_steps_round_trip(self):
        self._world()
        once = self.assert_round_trip(*STEPS)
        self.assertEqual(once["holders"], 1)

    def test_backfill_emits_no_signals(self):
        self._world()
        seen = []

        def record(sender, **kwargs):
            seen.append(sender)

        post_save.connect(record, weak=False)
        self.addCleanup(post_save.disconnect, record)
        for sig in (signals.customer_registered,):
            sig.connect(record, weak=False)
            self.addCleanup(sig.disconnect, record)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            forward(*STEPS)
        self.assertEqual(seen, [])
        self.assertEqual(callbacks, [])
        self.assertTrue(Party.objects.exists())

    def test_summary_lines(self):
        self._world()
        lines = [format_line(n, r) for n, r in forward(*STEPS).items()]
        self.assertTrue(lines[1].startswith("parties backfill D3 customer parties: customers: 2;"), lines[1])
        self.assertIn("account holders (portal login): 1", lines[1])

    def test_migration_runs_every_step_both_ways(self):
        self._world()
        ops = MIGRATION.Migration.operations
        self.assertEqual(len(ops), len(STEPS))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            for op in ops:
                op.code(apps, None)
        self.assertIn("parties backfill D3 customer parties", out.getvalue())
        for op in reversed(ops):
            op.reverse_code(apps, None)
        self.assertEqual(snapshot(), self.empty)


class CommandTests(_Base):
    def test_backfill_parties_command(self):
        legacy_customer(self.company)
        out = io.StringIO()
        call_command("backfill_parties", "--step", "all", "--dry-run", stdout=out)
        self.assertIn("[dry run] parties backfill D3 customer parties: customers: 1", out.getvalue())
        self.assertFalse(Party.objects.exists())
        call_command("backfill_parties", "--step", "2", "--step", "3", stdout=out)
        self.assertEqual(Party.objects.count(), 2)
        call_command("backfill_parties", "--step", "3", "--reverse", stdout=out)
        self.assertEqual(Party.objects.count(), 1)

    def test_bad_step(self):
        with self.assertRaises(CommandError):
            call_command("backfill_parties", "--step", "11", stdout=io.StringIO())

    def test_refusal_is_a_command_error(self):
        Company.objects.create(name="Two")
        legacy_customer(None)
        with self.assertRaisesMessage(CommandError, "D3 refused"):
            call_command("backfill_parties", "--step", "3", stdout=io.StringIO())


class ReviewRegressionTests(_Base):
    def test_unparseable_customer_phone_then_parsed_a1_contact_phone(self):
        c = legacy_customer(self.company, email="b@example.com", phone="call me")
        legacy_contact(c, email="b@example.com", phone="2125550101")
        forward(3, 4)
        c.refresh_from_db()
        self.assertEqual(PartyPhone.objects.filter(party=c.party, is_primary=True).count(), 1)
        self.assertEqual(Party.objects.get(pk=c.party_id).primary_phone_e164, "+12125550101")

    def test_reverse_keeps_a_lead_party_a_business_customer_adopted(self):
        legacy_lead(self.company, email="adopt@example.com")
        forward(10)
        c = Customer.objects.create(first_name="A", last_name="B", email="adopt@example.com",
                                    customer_type="business", company_name="Org", company=self.company)
        lead_party = Lead.objects.get().party_id
        self.assertTrue(PartyRelationship.objects.filter(from_party_id=lead_party,
                                                         metadata__customer=str(c.pk)).exists())
        reverse(10)
        self.assertTrue(Party.objects.filter(pk=lead_party).exists())

    def test_reverse_leaves_links_the_live_sync_wrote(self):
        c = Customer.objects.create(first_name="A", last_name="B", email="live@example.com", company=self.company)
        ct = CustomerContact.objects.create(customer=c, first_name="K", email="k@example.com")
        a = CustomerAddress.objects.create(customer=c, address_line_1="1", city="X", state_province="IL",
                                           postal_code="1")
        lead = Lead.objects.create(first_name="L", last_name="D", email="l@example.com", company=self.company)
        reverse(*STEPS)
        ct.refresh_from_db()
        a.refresh_from_db()
        lead.refresh_from_db()
        self.assertIsNotNone(ct.party_id)
        self.assertIsNotNone(a.party_address_id)
        self.assertIsNotNone(lead.party_id)
