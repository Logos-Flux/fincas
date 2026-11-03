"""CRM Phase 2 step 1: tenancy correctness for ``crm.ActivityLog``.

Covers the company backfill (each source, in the spec's order: lead,
customer, performer, the single Company), its wiring into the migration and
``backfill_tenancy --also``, and the scoped viewset (another tenant's
activity is a 404).
"""

import importlib
import io
from contextlib import redirect_stdout

from django.apps import apps as global_apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIRequestFactory, force_authenticate

from communications.models import Communication
from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from crm.backfill import (
    backfill_activity_communications,
    backfill_activity_company,
    format_counts,
)
from crm.models import ActivityLog, ActivityLogFrozen, Lead
from crm.views import ActivityLogViewSet, LeadViewSet
from customers.models import Customer
from customers.pre_tighten import PreTightenSchemaMixin


def _lead(company, email="lead@example.com"):
    return Lead.objects.create(first_name="Lee", last_name="Ead", email=email, company=company)


def _customer(company, email="cust@example.com"):
    if company is None:
        # A pre-step-11 row (pre-tighten schema only): no save(), no party.
        (row,) = Customer.objects.bulk_create([Customer(first_name="Cus", last_name="Tomer", email=email)])
        return row
    return Customer.objects.create(first_name="Cus", last_name="Tomer", email=email, company=company)


def _profile(company, uid):
    return UserProfile.objects.create(company=company)


def _activity(**kwargs):
    """A pre-step-6 row: ``save()`` refuses since ActivityLog froze."""
    kwargs.setdefault("activity_type", ActivityLog.ActivityType.NOTE)
    kwargs.setdefault("subject", "Called")
    kwargs.setdefault("activity_date", timezone.now())
    row = ActivityLog(**kwargs)
    ActivityLog.objects.bulk_create([row])
    return row


def _legacy(**kwargs):
    """A row as written before step 1: company NULL whatever its sources."""
    row = _activity(**kwargs)
    ActivityLog.objects.filter(pk=row.pk).update(company=None)
    return row


def _company_of(activity):
    activity.refresh_from_db()
    return activity.company if activity.company_id else None  # NULL only on the pre-tighten schema


def _backfill(fallback=None):
    return backfill_activity_company(ActivityLog, Company, fallback=fallback)


class ActivityCompanyBackfillTests(PreTightenSchemaMixin, TestCase):
    def setUp(self):
        self.a = Company.objects.create(name="Tenant A")
        self.b = Company.objects.create(name="Tenant B")
        self.c = Company.objects.create(name="Tenant C")

    def test_lead_company_is_the_first_source(self):
        # Lead, customer and performer disagree: the lead wins.
        row = _legacy(
            lead=_lead(self.a), customer=_customer(self.b), performed_by=_profile(self.c, "u1")
        )
        counts = _backfill()
        self.assertEqual(_company_of(row), self.a)
        self.assertEqual(counts["lead"], 1)
        self.assertEqual(counts["customer"], 0)

    def test_customer_company_when_the_lead_has_none(self):
        row = _legacy(
            lead=_lead(None), customer=_customer(self.b), performed_by=_profile(self.c, "u1")
        )
        counts = _backfill()
        self.assertEqual(_company_of(row), self.b)
        self.assertEqual((counts["lead"], counts["customer"], counts["performed_by"]), (0, 1, 0))

    def test_performer_company_when_lead_and_customer_have_none(self):
        row = _legacy(customer=_customer(None), performed_by=_profile(self.c, "u1"))
        counts = _backfill()
        self.assertEqual(_company_of(row), self.c)
        self.assertEqual((counts["customer"], counts["performed_by"]), (0, 1))

    def test_single_company_is_the_last_source(self):
        from crm.models import Pipeline, Stage

        # Seeded pipelines are tenant data and PROTECT their company (Phase 3 1.2).
        Stage.objects.exclude(company=self.a).delete()
        Pipeline.objects.exclude(company=self.a).delete()
        Company.objects.exclude(pk=self.a.pk).delete()
        row = _legacy(performed_by=_profile(None, "u1"))
        counts = _backfill()
        self.assertEqual(_company_of(row), self.a)
        self.assertEqual(counts["single_company"], 1)
        self.assertEqual(counts["still_null"], 0)

    def test_several_companies_and_no_source_leave_the_row_null(self):
        row = _legacy()
        counts = _backfill()
        self.assertIsNone(_company_of(row))
        self.assertEqual(counts["single_company"], 0)
        self.assertEqual(counts["still_null"], 1)

    def test_explicit_fallback_replaces_the_single_company_rule(self):
        row = _legacy()
        counts = _backfill(fallback=self.b)
        self.assertEqual(_company_of(row), self.b)
        self.assertEqual(counts["single_company"], 1)

    def test_rows_with_a_company_are_never_changed(self):
        row = _activity(lead=_lead(self.a), company=self.b)
        counts = _backfill()
        self.assertEqual(_company_of(row), self.b)
        self.assertEqual(counts["lead"], 0)

    def test_rerun_is_a_no_op(self):
        _legacy(lead=_lead(self.a))
        _backfill()
        counts = _backfill()
        self.assertEqual(sum(counts.values()), 0)

    def test_summary_line_names_every_source_and_the_null_count(self):
        _legacy(lead=_lead(self.a))
        _legacy()
        line = format_counts(_backfill())
        self.assertEqual(
            line,
            "crm.ActivityLog company backfill: lead=1 customer=0 performed_by=0 "
            "single_company=0; still NULL: 1; lead/customer company conflicts: 0",
        )

    def test_lead_customer_conflicts_are_counted_not_changed(self):
        row = _legacy(lead=_lead(self.a), customer=_customer(self.b))
        counts = _backfill()
        self.assertEqual(_company_of(row), self.a)
        self.assertEqual(counts["lead_customer_conflicts"], 1)

    def test_migration_runs_the_backfill_and_prints_the_null_count(self):
        migration = importlib.import_module("crm.migrations.0005_backfill_activitylog_company")
        row = _legacy(customer=_customer(self.b))
        _legacy()
        out = io.StringIO()
        with redirect_stdout(out):
            migration._backfill(global_apps, None)
        self.assertEqual(_company_of(row), self.b)
        self.assertIn("customer=1", out.getvalue())
        self.assertIn("still NULL: 1", out.getvalue())


class BackfillTenancyAlsoActivityLogTests(PreTightenSchemaMixin, TestCase):
    def setUp(self):
        self.a = Company.objects.create(name="Tenant A")
        self.b = Company.objects.create(name="Tenant B")

    def _run(self, *args):
        out = io.StringIO()
        call_command("backfill_tenancy", "--company", str(self.a.pk), *args, stdout=out)
        return out.getvalue()

    def test_without_also_activity_rows_are_only_reported(self):
        row = _legacy(lead=_lead(self.b))
        out = self._run()
        self.assertIsNone(_company_of(row))
        self.assertIn("crm.ActivityLog: 1 NULL (not modified)", out)

    def test_also_uses_the_sources_before_the_company_flag(self):
        sourced = _legacy(lead=_lead(self.b))
        orphan = _legacy()
        out = self._run("--also", "crm.ActivityLog")
        self.assertEqual(_company_of(sourced), self.b)
        self.assertEqual(_company_of(orphan), self.a)
        self.assertIn("lead=1", out)
        self.assertIn("single_company=1; still NULL: 0", out)


class ActivityLogScopingTests(TestCase):
    """The compat endpoint (Phase 3 step 6) keeps the step-1 scoping rules."""

    def setUp(self):
        self.a = Company.objects.create(name="Tenant A")
        self.b = Company.objects.create(name="Tenant B")
        self.user_a = make_staff_user("staff-a", password="pw")
        self.user_a.userprofile = _profile(self.a, "uid-a")
        self.mine = _activity(company=self.a, customer=_customer(self.a, "a@example.com"))
        self.theirs = _activity(company=self.b, lead=_lead(self.b, "b@example.com"))
        backfill_activity_communications()
        self.factory = APIRequestFactory()

    def _call(self, actions, method, user, pk=None, data=None):
        url = "/api/v1/crm/activities/" + (f"{pk}/" if pk else "")
        request = getattr(self.factory, method)(url, data, format="json")
        force_authenticate(request, user=user)
        kwargs = {"pk": str(pk)} if pk else {}
        return ActivityLogViewSet.as_view(actions)(request, **kwargs)

    def test_another_tenants_activity_is_404(self):
        response = self._call({"get": "retrieve"}, "get", self.user_a, pk=self.theirs.pk)
        self.assertEqual(response.status_code, 404)

    def test_own_activity_without_a_lead_is_visible(self):
        # Scoping used to traverse lead__company, which hid customer-only rows.
        response = self._call({"get": "retrieve"}, "get", self.user_a, pk=self.mine.pk)
        self.assertEqual(response.status_code, 200)

    def test_list_shows_only_own_tenant(self):
        response = self._call({"get": "list"}, "get", self.user_a)
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data["results"]], [str(self.mine.pk)])

    def test_create_stamps_the_requesters_company(self):
        response = self._call({"post": "create"}, "post", self.user_a, data={
            "activity_type": "call", "subject": "Hi", "activity_date": timezone.now().isoformat(),
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Communication.objects.get(pk=response.data["id"]).company, self.a)

    def test_create_on_another_tenants_lead_or_customer_is_400(self):
        for field, parent in (
            ("lead", self.theirs.lead),
            ("customer", _customer(self.b, "b2@example.com")),
        ):
            with self.subTest(field=field):
                response = self._call({"post": "create"}, "post", self.user_a, data={
                    "activity_type": "call", "subject": "Hi",
                    "activity_date": timezone.now().isoformat(), field: str(parent.pk),
                })
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)

    def test_superuser_sees_every_tenant(self):
        su = User.objects.create_superuser("root", "root@example.com", "pw")
        response = self._call({"get": "list"}, "get", su)
        self.assertEqual(response.data["count"], 2)

    def _update(self, pk, data):
        return self._call({"patch": "partial_update"}, "patch", self.user_a, pk=pk, data=data)

    def test_update_cannot_null_or_move_the_company(self):
        for value in (None, str(self.b.pk)):
            with self.subTest(company=value):
                response = self._update(self.mine.pk, {"company": value})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(Communication.objects.get(pk=self.mine.pk).company, self.a)

    def test_update_onto_another_tenants_lead_is_400(self):
        response = self._update(self.mine.pk, {"lead": str(self.theirs.lead.pk)})
        self.assertEqual(response.status_code, 400)
        self.assertIn("lead", response.data)

    def test_create_with_another_tenants_performer_is_400(self):
        response = self._call({"post": "create"}, "post", self.user_a, data={
            "activity_type": "call", "subject": "Hi", "activity_date": timezone.now().isoformat(),
            "performed_by": str(_profile(self.b, "uid-b").pk),
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn("performed_by", response.data)

    def test_superuser_create_takes_the_company_from_the_lead(self):
        # The service account stamps no company; the row must still reach
        # the lead's tenant rather than vanish with a NULL company.
        su = User.objects.create_superuser("root", "root@example.com", "pw")
        lead = _lead(self.a, "svc@example.com")
        response = self._call({"post": "create"}, "post", su, data={
            "activity_type": "call", "subject": "Hi",
            "activity_date": timezone.now().isoformat(), "lead": str(lead.pk),
        })
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(Communication.objects.get(pk=response.data["id"]).company, self.a)

    def test_lead_detail_hides_another_tenants_activity(self):
        lead = _lead(self.a, "shared@example.com")
        _activity(lead=lead, company=self.a, subject="ours")
        _activity(lead=lead, company=self.b, subject="theirs")
        backfill_activity_communications()
        request = self.factory.get(f"/api/v1/crm/leads/{lead.pk}/")
        force_authenticate(request, user=self.user_a)
        response = LeadViewSet.as_view({"get": "retrieve"})(request, pk=str(lead.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([a["subject"] for a in response.data["activities"]], ["ours"])


class ActivityLogFrozenTests(TestCase):
    def test_save_refuses_since_step_6(self):
        a = Company.objects.create(name="Tenant A")
        with self.assertRaises(ActivityLogFrozen):
            ActivityLog.objects.create(company=a, activity_type="call", subject="Hi",
                                       activity_date=timezone.now())
        row = _activity(company=a)
        row.subject = "changed"
        with self.assertRaises(ActivityLogFrozen):
            row.save()
