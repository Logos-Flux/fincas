"""``reconcile_parties`` (phase2-party.md 2.1, 7.1) and the post-backfill
dedupe report (``party_dedupe_report --source parties``)."""

from __future__ import annotations

import io
import json
import os
import tempfile

from django.apps import apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core.models import Company
from customers.models import Customer, CustomerContact
from customers.pre_tighten import PreTightenSchemaMixin
from parties.backfill import STEPS, run_step
from parties.models import Party, PartyEmail
from parties.reconcile import CONFLICT, DRIFT, PENDING, check

from .factories import legacy_contact, legacy_customer, legacy_lead


def reconcile(*args):
    out = io.StringIO()
    call_command("reconcile_parties", *args, stdout=out)
    return out.getvalue()


class ReconcileTests(PreTightenSchemaMixin, TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")

    def test_seeded_demo_database_is_clean(self):
        call_command("seed_demo_data", stdout=io.StringIO())
        self.assertTrue(Customer.objects.exists())
        out = reconcile("--check")
        self.assertIn("drift: none", out)

    def test_backfilled_legacy_data_is_clean(self):
        c = legacy_customer(self.company, customer_type="business", company_name="O", bc_id="9",
                            accepts_marketing=True, user=User.objects.create_user("u"), phone="2125550100")
        legacy_contact(c, email=c.email)
        legacy_contact(c, email="other@example.com", phone="2125550111")
        legacy_lead(self.company, email="lead@example.com")
        for n in STEPS:
            run_step(apps, n)
        self.assertEqual([i for i in check() if i.level != CONFLICT], [])

    def test_queryset_update_is_drift_and_fix_repairs_it(self):
        c = Customer.objects.create(first_name="A", last_name="B", email="a@example.com", company=self.company)
        Customer.objects.filter(pk=c.pk).update(first_name="Changed", email="changed@example.com")
        issues = {(i.model, i.what) for i in check() if i.level == DRIFT}
        self.assertIn(("customer", "person name differs"), issues)
        self.assertIn(("customer", "email missing on the person party"), issues)
        with self.assertRaisesMessage(CommandError, "drift issue(s)"):
            reconcile("--check")
        out = reconcile("--fix")
        self.assertIn("Fixed: customers 1", out)
        self.assertEqual(Party.objects.get(pk=c.party_id).given_name, "Changed")
        reconcile("--check")

    def test_bulk_created_rows_are_drift(self):
        c = legacy_customer(self.company)
        legacy_contact(c)
        legacy_lead(self.company)
        models = {i.model for i in check() if i.level == DRIFT}
        self.assertEqual(models, {"customer", "lead"})  # the contact is reported once its customer has a party
        out = reconcile("--fix")
        self.assertIn("contacts 1", out)
        self.assertIn("customers 1", out)
        reconcile("--check")

    def test_stale_primary_columns_are_drift(self):
        c = Customer.objects.create(first_name="A", last_name="B", email="a@example.com", company=self.company)
        Party.objects.filter(pk=c.party_id).update(primary_email_normalized="")
        self.assertIn(("party", "primary email/phone columns are stale"),
                      {(i.model, i.what) for i in check() if i.level == DRIFT})
        reconcile("--fix")
        reconcile("--check")

    def test_conflicts_and_pending_do_not_fail_check(self):
        a = Customer.objects.create(first_name="A", last_name="B", email="a@example.com", company=self.company)
        b = Customer.objects.create(first_name="C", last_name="D", email="c@example.com", company=self.company)
        PartyEmail.objects.create(party_id=b.party_id, email="a@example.com", purpose="work")
        legacy_lead(self.company, email="a@example.com")
        issues = check()
        self.assertIn((CONFLICT, "lead"), {(i.level, i.model) for i in issues})
        self.assertIn((PENDING, "company"), {(i.level, i.model) for i in issues})
        out = reconcile("--check")
        self.assertIn("conflict: lead 1", out)
        del a

    def test_pending_self_and_staff_parties_fixed_by_backfill(self):
        from core.models import UserProfile

        UserProfile.objects.create(user=User.objects.create_user("s"), company=self.company)
        self.assertEqual({i.model for i in check() if i.level == PENDING}, {"company", "profile"})
        out = reconcile("--fix")
        self.assertIn("self parties 1", out)
        self.assertIn("staff parties 1", out)
        self.assertIn("pending: none", out)

    def test_company_scope(self):
        other = Company.objects.create(name="Other")
        legacy_customer(other)
        self.assertEqual([i for i in check(self.company) if i.level == DRIFT], [])
        self.assertTrue([i for i in check(other) if i.level == DRIFT])


class PostBackfillDedupeReportTests(PreTightenSchemaMixin, TestCase):
    """Step 5's account-holder definition, as the real-data report needed it."""

    def setUp(self):
        self.company = Company.objects.create(name="Acme")

    def report(self, *args):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(os.remove, path)
        call_command("party_dedupe_report", "--json", path, *args, stdout=io.StringIO())
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_customers_without_login_are_not_blocked_as_two_holders(self):
        legacy_customer(self.company, email="dup@example.com", first_name="Ann", last_name="Lee")
        c2 = legacy_customer(self.company, email="other@example.com", first_name="Ann", last_name="Lee")
        legacy_contact(c2, email="dup@example.com")
        for source in ("legacy", "parties"):
            if source == "parties":
                for n in STEPS:
                    run_step(apps, n)
            r = self.report("--source", source)
            self.assertEqual(r["source"], source)
            self.assertEqual(r["clusters_total"], 1, source)
            self.assertEqual(r["clusters"][0]["blocked"], [], source)
            self.assertTrue(r["clusters"][0]["bulk_approvable"], source)

    def test_two_login_holders_are_blocked(self):
        # Same phone and family name (R2), both with a portal login.
        legacy_customer(self.company, phone="2125550100", last_name="Lee", user=User.objects.create_user("a"))
        legacy_customer(self.company, phone="2125550100", last_name="Lee", user=User.objects.create_user("b"))
        for n in STEPS:
            run_step(apps, n)
        r = self.report()
        self.assertEqual(r["source"], "parties")  # auto picks parties once they exist
        self.assertIn("two_account_holders", r["clusters"][0]["blocked"])

    def test_auto_uses_legacy_before_the_backfill(self):
        legacy_customer(self.company)
        self.assertEqual(self.report()["source"], "legacy")

    def test_contact_counted_once_after_fold(self):
        c = legacy_customer(self.company, email="boss@example.com")
        legacy_contact(c, email="boss@example.com")
        for n in STEPS:
            run_step(apps, n)
        r = self.report("--source", "parties")
        self.assertEqual(r["clusters_total"], 0)
        self.assertEqual(r["rows"]["contacts"], 1)
        self.assertEqual(CustomerContact.objects.get().party_id, Customer.objects.get().party_id)
