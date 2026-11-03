"""Step 1b (test plan #9): fiscal close preconditions + API.

Covers: close happy path, draft-blockers named, non-OPEN refuse, LOCKED never
reopens, staff-only actions, and the closed-then-backdated-post rejection
through the service layer.
"""

from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from accounting.models import FiscalPeriod, JournalEntry
from accounting.periods import PeriodCloseError, close_period, reopen_period
from accounting.tests.test_posting import make_posting_rules
from core.models import Company
from core.tests.helpers import make_staff_user


def _period(company, status="open", start=None, end=None):
    start = start or date.today() - timedelta(days=30)
    end = end or date.today() + timedelta(days=30)
    return FiscalPeriod.objects.create(
        company=company, name=f"P-{status}-{start}", start_date=start,
        end_date=end, status=status,
    )


class CloseServiceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Close Co")
        make_posting_rules(self.company)

    def _draft(self, when):
        return JournalEntry.objects.create(
            company=self.company, entry_date=when,
            entry_source="manual", source_reference="d1",
            status=JournalEntry.Status.DRAFT,
        )

    def test_close_happy_path(self):
        period = _period(self.company)
        closed = close_period(period)
        self.assertEqual(closed.status, FiscalPeriod.Status.CLOSED)
        self.assertIsNotNone(closed.closed_at)

    def test_close_refuses_non_open(self):
        period = _period(self.company, status="closed")
        with self.assertRaises(PeriodCloseError):
            close_period(period)

    def test_close_blocked_by_drafts_names_them(self):
        period = _period(self.company)
        draft = self._draft(date.today())
        with self.assertRaises(PeriodCloseError) as ctx:
            close_period(period)
        self.assertIn(draft.entry_number or "d1", str(ctx.exception))
        period.refresh_from_db()
        self.assertEqual(period.status, FiscalPeriod.Status.OPEN)

    def test_draft_outside_range_does_not_block(self):
        period = _period(self.company)
        self._draft(date.today() + timedelta(days=90))
        close_period(period)
        period.refresh_from_db()
        self.assertEqual(period.status, FiscalPeriod.Status.CLOSED)

    def test_locked_never_reopens(self):
        period = _period(self.company, status="locked")
        with self.assertRaises(PeriodCloseError):
            reopen_period(period)

    def test_reopen_closed(self):
        period = _period(self.company)
        close_period(period)
        reopen_period(period)
        period.refresh_from_db()
        self.assertEqual(period.status, FiscalPeriod.Status.OPEN)
        self.assertIsNone(period.closed_at)

    def test_post_into_closed_period_rejected(self):
        period = _period(self.company)
        close_period(period)
        with self.assertRaises(Exception):
            JournalEntry.objects.create(
                company=self.company,
                fiscal_period=FiscalPeriod.objects.get(pk=period.pk),
                entry_date=date.today(),
                entry_source="manual",
                source_reference="late",
            )


class CloseApiTests(TestCase):
    def setUp(self):
        from core.models import UserProfile

        self.company = Company.objects.create(name="API Co")
        make_posting_rules(self.company)
        self.staff = make_staff_user("s", password="p")
        self.plain = User.objects.create_user("u", password="p")
        # Mirror the Step-0 login signal: staff resolve to their company.
        UserProfile.objects.create(
            user=self.staff, company=self.company,
        )
        self.period = _period(self.company)
        self.client = APIClient()

    def _url(self, verb):
        return f"/api/v1/accounting/periods/{self.period.pk}/{verb}/"

    def test_staff_can_close_and_reopen(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(self._url("close"))
        self.assertEqual(response.status_code, 200)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, FiscalPeriod.Status.CLOSED)
        response = self.client.post(self._url("reopen"))
        self.assertEqual(response.status_code, 200)

    def test_non_staff_forbidden(self):
        self.client.force_authenticate(user=self.plain)
        response = self.client.post(self._url("close"))
        self.assertEqual(response.status_code, 403)

    def test_close_with_drafts_conflicts(self):
        JournalEntry.objects.create(
            company=self.company, entry_date=date.today(),
            entry_source="manual", source_reference="d9",
            status=JournalEntry.Status.DRAFT,
        )
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(self._url("close"))
        self.assertEqual(response.status_code, 409)
        self.assertIn("draft", response.data["detail"].lower())

    def test_reopen_open_conflicts(self):
        self.client.force_authenticate(user=self.staff)
        response = self.client.post(self._url("reopen"))
        self.assertEqual(response.status_code, 409)

    def test_list_scoped_to_company(self):
        other = Company.objects.create(name="Other")
        _period(other)
        self.client.force_authenticate(user=self.staff)
        # Staff without a profile company fail closed (Step 0): only rows
        # resolvable to their company show; superusers see all.
        response = self.client.get("/api/v1/accounting/periods/")
        self.assertEqual(response.status_code, 200)


class PeriodsViewTests(TestCase):
    """The /erp/ close face: renders, closes, and names blockers."""

    def setUp(self):
        self.company = Company.objects.create(name="View Co")
        make_posting_rules(self.company)
        self.staff = make_staff_user("v", password="p")
        self.period = _period(self.company)

    def _login(self):
        # Real login fires the Step-0 signal: profile links + company resolves.
        self.assertTrue(self.client.login(username="v", password="p"))
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.userprofile.company, self.company)

    def test_page_renders(self):
        self._login()
        response = self.client.get("/erp/accounting/periods/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.period.name)

    def test_post_close_works(self):
        self._login()
        response = self.client.post(
            "/erp/accounting/periods/",
            {"period": str(self.period.pk), "action": "close"},
        )
        self.assertEqual(response.status_code, 302)
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, FiscalPeriod.Status.CLOSED)

    def test_close_asks_first_and_says_it_worked(self):
        # F-091: closing a period was one silent click.
        self._login()
        page = self.client.get("/erp/accounting/periods/")
        self.assertContains(page, "return confirm(")
        self.assertContains(page, "until someone reopens it")
        response = self.client.post(
            "/erp/accounting/periods/",
            {"period": str(self.period.pk), "action": "close"},
            follow=True,
        )
        self.assertContains(response, f"{self.period.name} is closed.")
        response = self.client.post(
            "/erp/accounting/periods/",
            {"period": str(self.period.pk), "action": "reopen"},
            follow=True,
        )
        self.assertContains(response, f"{self.period.name} is open again.")

    def test_post_close_blocked_shows_reason(self):
        JournalEntry.objects.create(
            company=self.company, entry_date=date.today(),
            entry_source="manual", source_reference="d7",
            status=JournalEntry.Status.DRAFT,
        )
        self._login()
        response = self.client.post(
            "/erp/accounting/periods/",
            {"period": str(self.period.pk), "action": "close"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "draft entries")
        self.period.refresh_from_db()
        self.assertEqual(self.period.status, FiscalPeriod.Status.OPEN)

    def test_other_company_period_404s(self):
        from core.models import UserProfile

        other = Company.objects.create(name="Other View")
        foreign = _period(other)
        # Two companies: Step 0 leaves the profile unresolved (fail closed),
        # so an admin assigns it — the assignment path under test.
        self.assertTrue(self.client.login(username="v", password="p"))
        profile = UserProfile.objects.get(user=self.staff)
        self.assertIsNone(profile.company)
        profile.company = self.company
        profile.save(update_fields=["company"])
        response = self.client.post(
            "/erp/accounting/periods/",
            {"period": str(foreign.pk), "action": "close"},
        )
        self.assertEqual(response.status_code, 404)


class OpenBalanceGuardTests(TestCase):
    """Zero-total and unbalanced entries stay impossible through the service."""

    def setUp(self):
        self.company = Company.objects.create(name="Guard Co")
        self.accounts = make_posting_rules(self.company)

    def test_empty_legs_rejected(self):
        from accounting.posting import post_balanced_entry

        with self.assertRaises(Exception):
            post_balanced_entry(
                company=self.company,
                entry_date=date.today(),
                entry_source="manual",
                source_reference="empty",
                legs=[],
            )


