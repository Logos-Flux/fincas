"""``record_withdrawal`` / ``record_suppression``: what a provider may write about consent
(docs/crm/phase6-adapters.md 3.2). Withdrawals and suppressions only, idempotent, never a grant."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone as dt_timezone

from django.test import TestCase

from core.models import Company
from customers.models import Customer
from parties.consent import address_hash, can_contact, record_suppression, record_withdrawal
from parties.models import ConsentRecord, Party, PartyEmail, Suppression

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=dt_timezone.utc)


class ConsentWritebackTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Tenant")
        self.party = Party.objects.create(company=self.company, kind=Party.Kind.PERSON, given_name="Ada",
                                          family_name="Lovelace", origin=Party.Origin.ERP, marketable=True)
        PartyEmail.objects.create(party=self.party, email="Ada@Example.com", purpose="work")
        ConsentRecord.objects.create(party=self.party, company=self.company, channel="email", purpose="marketing",
                                     state="granted", occurred_at=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
                                     source="portal")

    def test_withdrawal_is_once_per_source_detail_and_never_a_grant(self):
        first, created = record_withdrawal(self.party, "email", "marketing", occurred_at=T0, source="api",
                                           source_detail="klaviyo:e1", address="Ada@Example.com")
        again, created_again = record_withdrawal(self.party, "email", "marketing", occurred_at=T0, source="api",
                                                 source_detail="klaviyo:e1", address="ada@example.com")
        self.assertTrue(created)
        self.assertFalse(created_again)
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(first.address_normalized, "ada@example.com")
        self.assertEqual(ConsentRecord.objects.filter(state="granted").count(), 1)
        self.assertEqual(can_contact(self.party, "email", "marketing", "ada@example.com").reason, "withdrawn")

    def test_withdrawal_clears_the_customer_flag_so_the_sync_cannot_regrant(self):
        [customer] = Customer.objects.bulk_create([Customer(company=self.company, email="ada@example.com",
                                                            accepts_marketing=True, party=self.party)])
        record_withdrawal(self.party, "email", "marketing", occurred_at=T0, source="api", source_detail="x:1")
        customer.refresh_from_db()
        self.assertFalse(customer.accepts_marketing)

    def test_suppression_is_idempotent_and_only_a_stronger_reason_supersedes(self):
        s1, c1 = record_suppression(self.company.pk, "email", "Ada@Example.com", "opt_out", source="api",
                                    source_detail="klaviyo:e1")
        s2, c2 = record_suppression(self.company.pk, "email", "ada@example.com", "complaint", source="api",
                                    source_detail="klaviyo:e2")
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(s1.pk, s2.pk)
        self.assertEqual(s1.address_hash, address_hash("ada@example.com"))
        self.assertEqual(s1.address_hint, "a***@example.com")
        s3, c3 = record_suppression(self.company.pk, "email", "ada@example.com", "hard_bounce", source="api",
                                    source_detail="klaviyo:e3")
        self.assertTrue(c3)
        s1.refresh_from_db()
        self.assertIsNotNone(s1.lifted_at)
        self.assertEqual(s1.lift_reason, "superseded:hard_bounce")
        self.assertEqual(Suppression.objects.filter(lifted_at__isnull=True).get().pk, s3.pk)
        _s4, c4 = record_suppression(self.company.pk, "email", "ada@example.com", "opt_out", source="api",
                                     source_detail="klaviyo:e4")
        self.assertFalse(c4)

    def test_an_erasure_suppression_is_never_replaced(self):
        Suppression.objects.create(company=self.company, channel="email", address_hash=address_hash("ada@example.com"),
                                   reason="erasure", source="erp", source_detail="erasure")
        _s, created = record_suppression(self.company.pk, "email", "ada@example.com", "hard_bounce", source="api",
                                         source_detail="klaviyo:e1")
        self.assertFalse(created)
        self.assertEqual(Suppression.objects.get(lifted_at__isnull=True).reason, "erasure")

    def test_an_unnormalisable_address_writes_nothing(self):
        self.assertEqual(record_suppression(self.company.pk, "email", "not an address", "opt_out", source="api",
                                            source_detail="x"), (None, False))
        self.assertFalse(Suppression.objects.exists())

    def test_withdrawal_clears_the_flag_on_a_business_account_the_person_holds(self):
        from parties.sync import _sync_consent

        org = Party.objects.create(company=self.company, kind=Party.Kind.ORGANISATION, legal_name="Acme",
                                   origin=Party.Origin.ERP)
        [customer] = Customer.objects.bulk_create([Customer(company=self.company, email="ada@example.com",
                                                            accepts_marketing=True, party=org, customer_type="business",
                                                            account_holder=self.party)])
        record_withdrawal(self.party, "email", "marketing", occurred_at=T0, source="api", source_detail="x:1")
        customer.refresh_from_db()
        self.assertFalse(customer.accepts_marketing)
        _sync_consent(customer, self.party, "erp")  # what the next customer save runs
        self.assertEqual(ConsentRecord.objects.filter(party=self.party).latest("recorded_at").state, "withdrawn")
