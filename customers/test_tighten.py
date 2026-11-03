"""CRM Phase 2 step 11 (D13): the tightening and its pre-check.

phase2-party.md section 4 (D13) and section 8, step 11: every new constraint
is violated once; the pre-check refuses on seeded violations and lists them
(ids, masked emails); a clean database migrates. The constraint tests run on
whichever backend the settings select: SQLite under ``config.settings.test``,
Postgres under ``config.settings.dev`` (CI job ``migrations-postgres``).
"""

from __future__ import annotations

import io
import uuid

from django.core.exceptions import NON_FIELD_ERRORS, ValidationError
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from core.models import Company
from crm.models import ActivityLog, Lead
from customers.forms import CustomerForm, CustomerMoreInfoForm
from customers.models import Customer, CustomerContact
from customers.pre_tighten import relax, tighten
from customers.tighten import TightenRefused, find_violations, mask_email
from orders.tests.test_recalculate import company_user
from parties.merge import merge
from parties.models import Party


def _customer(company, email=None, **kw):
    kw.setdefault("first_name", "Ada")
    kw.setdefault("last_name", "Lovelace")
    return Customer.objects.create(company=company, email=email or f"{uuid.uuid4().hex[:8]}@example.com", **kw)


def _activity(**kw):
    # ActivityLog is frozen since CRM Phase 3 step 6 (save() refuses): rows as
    # legacy data has them.
    (row,) = ActivityLog.objects.bulk_create([ActivityLog(
        activity_type="note", subject="Called", activity_date=timezone.now(), **kw)])
    return row


class ConstraintTests(TestCase):
    """Each D13 constraint, violated once, on the database itself."""

    def setUp(self):
        self.company = Company.objects.create(name="Acme")
        self.customer = _customer(self.company, "ada@example.com")

    def assertRejected(self, model, pk, **values):
        with self.assertRaises(IntegrityError), transaction.atomic():
            model._base_manager.filter(pk=pk).update(**values)

    def test_customer_company_not_null(self):
        self.assertRejected(Customer, self.customer.pk, company=None)

    def test_customer_party_not_null(self):
        self.assertRejected(Customer, self.customer.pk, party=None)

    def test_contact_party_not_null(self):
        contact = CustomerContact.objects.create(customer=self.customer, first_name="Grace", email="g@example.com")
        self.assertIsNotNone(contact.party_id)
        self.assertRejected(CustomerContact, contact.pk, party=None)

    def test_activity_company_not_null(self):
        row = _activity(customer=self.customer, company=self.company)
        self.assertRejected(ActivityLog, row.pk, company=None)

    def test_email_unique_per_tenant_case_insensitively(self):
        other = _customer(self.company, "other@example.com")
        self.assertRejected(Customer, other.pk, email="ADA@Example.com")
        # Through save() too: the sync's parties roll back with the row.
        parties = Party.objects.count()
        with self.assertRaises(IntegrityError), transaction.atomic():
            _customer(self.company, "Ada@EXAMPLE.com")
        self.assertEqual(Party.objects.count(), parties)
        # Another tenant may hold the same address (the old unique=True could not).
        elsewhere = _customer(Company.objects.create(name="Other"), "ada@example.com")
        self.assertEqual(elsewhere.email, "ada@example.com")

    def test_model_validation_names_the_constraint(self):
        dup = Customer(company=self.company, first_name="A", last_name="B", email="ADA@example.com")
        with self.assertRaises(ValidationError) as caught:
            dup.validate_constraints()
        self.assertEqual(caught.exception.error_dict[NON_FIELD_ERRORS][0].code, "uniq_customer_email_ci_company")

    def test_account_holder_stays_nullable(self):
        # 4.1: set only while the customer has a portal login.
        self.assertIsNone(self.customer.account_holder_id)

    def test_lead_party_and_company_stay_nullable(self):
        lead = Lead.objects.create(first_name="Lee", last_name="Ead", email="lee@example.com", company=None)
        self.assertIsNone(lead.party_id)

    def test_constraint_is_an_expression_index_on_this_backend(self):
        with connection.cursor() as cursor:
            constraints = connection.introspection.get_constraints(cursor, Customer._meta.db_table)
        index = constraints["uniq_customer_email_ci_company"]
        self.assertTrue(index["unique"])
        self.assertTrue(index["index"])


class WritePathTests(TestCase):
    """Writers that leave ``company`` / ``party`` unset still produce valid rows,
    and duplicate emails are a 400 / form error, never a 500."""

    def setUp(self):
        self.company = Company.objects.create(name="Acme")

    def test_new_customer_gets_the_portal_tenant_and_its_party_at_insert(self):
        c = Customer.objects.create(first_name="Ada", last_name="L", email="ada@example.com")
        c.refresh_from_db()
        self.assertEqual(c.company_id, self.company.pk)
        self.assertEqual(c.party.kind, "person")
        self.assertTrue(c.party.roles.filter(role="customer", valid_to__isnull=True).exists())

    def test_new_business_customer_links_one_person_and_one_organisation(self):
        c = _customer(self.company, customer_type="business", company_name="Org Ltd")
        c.refresh_from_db()
        self.assertEqual(c.party.kind, "organisation")
        self.assertEqual(Party.objects.filter(kind="person").count(), 1)

    def test_new_contact_gets_its_party_at_insert(self):
        c = _customer(self.company, customer_type="business", company_name="Org Ltd")
        contact = CustomerContact.objects.create(customer=c, first_name="Grace", email="grace@example.com")
        contact.refresh_from_db()
        self.assertEqual(contact.party.given_name, "Grace")

    def test_form_reports_a_case_duplicate(self):
        _customer(self.company, "ada@example.com")
        form = CustomerForm(data={
            "first_name": "A", "last_name": "B", "email": "ADA@example.com", "customer_type": "individual",
            "pricing_tier": "retail", "default_currency": "USD",
        })
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors.as_data()["email"][0].code, "uniq_customer_email_ci_company")

    def test_form_allows_the_own_email_in_another_case(self):
        c = _customer(self.company, "ada@example.com")
        form = CustomerForm(instance=c, data={
            "first_name": "A", "last_name": "B", "email": "Ada@Example.com", "customer_type": "individual",
            "pricing_tier": "retail", "default_currency": "USD",
        })
        self.assertTrue(form.is_valid(), form.errors)

    def test_moving_tenant_onto_a_held_address_is_a_form_error(self):
        other = Company.objects.create(name="Other")
        _customer(other, "ada@example.com")
        c = _customer(self.company, "ADA@example.com")
        form = CustomerMoreInfoForm(instance=c, data={"company": str(other.pk), "default_currency": "USD"})
        self.assertFalse(form.is_valid())
        self.assertIn("company", form.errors)

    def test_api_answers_400_with_the_constraint_code(self):
        _customer(self.company, "ada@example.com")
        api = APIClient()
        api.force_authenticate(user=company_user(self.company))
        response = api.post("/api/v1/customers/customers/",
                            {"first_name": "A", "last_name": "B", "email": "Ada@Example.COM"}, format="json")
        self.assertEqual(response.status_code, 400, response.data)
        self.assertEqual(response.data["email"][0].code, "uniq_customer_email_ci_company")

        response = api.post("/api/v1/customers/customers/",
                            {"first_name": "A", "last_name": "B", "email": "new@example.com"}, format="json")
        self.assertEqual(response.status_code, 201, response.data)


class ReviewRegressionTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")

    def test_a_failed_insert_leaves_the_instance_retryable(self):
        _customer(self.company, "ada@example.com")
        c = Customer(company=self.company, first_name="A", last_name="B", email="ADA@example.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            c.save()
        self.assertIsNone(c.party_id)  # not a rolled-back party
        c.email = "grace@example.com"
        c.save()
        self.assertTrue(Party.objects.filter(pk=c.party_id).exists())

    def test_no_tenant_is_a_400_not_a_500(self):
        # The cross-company service account, several Companies, no portal tenant.
        from django.contrib.auth.models import User

        Company.objects.create(name="Second")
        api = APIClient()
        api.force_authenticate(user=User.objects.create_superuser("svc", "svc@example.com", "pw"))
        response = api.post("/api/v1/customers/customers/",
                            {"first_name": "A", "last_name": "B", "email": "x@example.com"}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.data["company"][0].code, "no_tenant")

    def test_form_without_a_tenant_is_invalid(self):
        Company.objects.create(name="Second")
        form = CustomerForm(data={
            "first_name": "A", "last_name": "B", "email": "x@example.com", "customer_type": "individual",
            "pricing_tier": "retail", "default_currency": "USD",
        })
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors.as_data()["email"][0].code, "no_tenant")

    def test_backfill_reverse_refuses_on_the_tightened_schema(self):
        from django.apps import apps

        from parties.backfill import STEPS, BackfillRefused

        for step in (3, 4):
            with self.subTest(step=step), self.assertRaisesMessage(BackfillRefused, "0007_tighten"):
                STEPS[step][2](apps)


class PreCheckTests(TransactionTestCase):
    """The pre-check, on the pre-step-11 schema (the tightening unapplied)."""

    def setUp(self):
        relax()
        self.addCleanup(self._restore)
        self.company = Company.objects.create(name="Acme")

    def _restore(self):
        # The tightening's own pre-check runs here, so the tables must be clean.
        # The rest goes with TransactionTestCase's flush.
        for model in (ActivityLog, CustomerContact, Customer):
            model._base_manager.all().delete()
        tighten()

    def check_tighten(self):
        out = io.StringIO()
        try:
            call_command("check_tighten", stdout=out)
        except SystemExit as exc:
            return exc.code, out.getvalue()
        return 0, out.getvalue()

    def seed_case_duplicates(self):
        """The downstream rehearsal's one finding: two customers of one tenant
        whose emails differ only by case (both with parties, nothing else)."""
        a = _customer(self.company, "alice.smith@example.com")
        b = _customer(self.company, "Alice.Smith@Example.com")
        return a, b

    def test_refuses_on_seeded_violations_and_lists_them(self):
        a, b = self.seed_case_duplicates()
        (no_company,) = Customer.objects.bulk_create([
            Customer(first_name="No", last_name="Tenant", email="nobody@example.com")])
        (no_party,) = Customer.objects.bulk_create([
            Customer(company=self.company, first_name="No", last_name="Party", email="np@example.com")])
        (contact,) = CustomerContact.objects.bulk_create([CustomerContact(customer=a, first_name="Grace")])
        activity = _activity(customer=a)  # no company: legal on the pre-step-11 schema

        with self.assertRaises(TightenRefused) as caught:
            tighten()
        message = str(caught.exception)
        for row in (a, b, no_company, no_party, contact, activity):
            self.assertIn(str(row.pk), message)
        self.assertIn("a…@example.com", message)
        self.assertNotIn("alice.smith", message.lower())  # masked: no full address in logs
        self.assertIn("customers with no company: 1", message)
        # no_company has no party either: listed under both.
        self.assertIn("customers with no party: 2", message)
        self.assertIn("customer contacts with no party: 1", message)
        self.assertIn("activity log rows with no company: 1", message)
        self.assertIn("differ only by case within a tenant: 1", message)

        # Nothing was altered: the refusal rolled the migration back.
        Customer.objects.bulk_create([Customer(company=self.company, first_name="X", last_name="Y",
                                               email="another-null-party@example.com")])

        code, out = self.check_tighten()
        self.assertEqual(code, 1)
        for row in (a, b, no_company, no_party, contact, activity):
            self.assertIn(str(row.pk), out)
        self.assertNotIn("alice.smith", out.lower())

    def test_case_duplicates_alone_refuse_until_resolved(self):
        """The operator path for the downstream's finding (phase2-party.md
        4.2): merge the two people in the queue, give the duplicate commerce
        account its own address, then migrate."""
        a, b = self.seed_case_duplicates()
        report = find_violations()
        self.assertEqual(report["customer_email_case_duplicates"], [{
            "company": str(self.company.pk), "email": mask_email("alice.smith@example.com"),
            "customers": sorted([str(a.pk), str(b.pk)]),
        }])
        with self.assertRaises(TightenRefused):
            tighten()

        merge(Party.objects.get(pk=a.party_id), [Party.objects.get(pk=b.party_id)], actor="tester")
        with self.assertRaises(TightenRefused):
            tighten()  # a party merge alone leaves two commerce accounts on one address

        b.refresh_from_db()
        b.email = "alice.smith+old-account@example.com"
        b.save()
        self.assertEqual(self.check_tighten()[0], 0)
        tighten()
        relax()  # the cleanup re-applies it

    def test_clean_database_migrates(self):
        _customer(self.company, "ada@example.com")
        _customer(Company.objects.create(name="Other"), "Ada@example.com")  # other tenant: fine
        code, out = self.check_tighten()
        self.assertEqual(code, 0)
        self.assertIn("clean", out)
        tighten()
        relax()  # the cleanup re-applies it

    def test_mask_email(self):
        self.assertEqual(mask_email("alice@example.com"), "a…@example.com")
        self.assertEqual(mask_email("not-an-address"), "…")
