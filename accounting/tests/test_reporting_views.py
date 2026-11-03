"""Reporting-surface tests for the GL pages.

These assert on the *rendered response*, not on the querysets behind it. The
defects these pages can have are display defects — a footer total that quietly
becomes a per-page sum, a running balance that restarts on page 2 — and a test
that re-runs the view's own aggregate would agree with the bug.
"""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Company

from ..models import Account, JournalEntry, JournalLine


class GLReportingTestCase(TestCase):
    """A small but genuinely two-sided ledger across two account types."""

    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Example Supply, Inc.")
        cls.other = Company.objects.create(name="Other Co")

        cls.cash = Account.objects.create(
            company=cls.company, account_number="1000", name="Cash",
            full_name="Cash", account_type=Account.AccountType.ASSET,
            account_subtype=Account.AccountSubType.BANK,
        )
        cls.revenue = Account.objects.create(
            company=cls.company, account_number="4000", name="Sales",
            full_name="Sales", account_type=Account.AccountType.INCOME,
            account_subtype=Account.AccountSubType.SALES_INCOME,
        )
        cls.dormant = Account.objects.create(
            company=cls.company, account_number="9999", name="Dormant",
            full_name="Dormant", account_type=Account.AccountType.EXPENSE,
            account_subtype=Account.AccountSubType.OPERATING_EXPENSE,
        )

        # Three sales of 100, 200, 300 — DR cash / CR revenue.
        for i, amount in enumerate((100, 200, 300), start=1):
            entry = JournalEntry.objects.create(
                company=cls.company, entry_number=f"JE-{i}",
                entry_date=f"2026-0{i}-01",
                entry_source=JournalEntry.EntrySource.QBO_IMPORT,
            )
            JournalLine.objects.create(
                entry=entry, account=cls.cash, sort_order=0,
                debit_amount=Decimal(amount), credit_amount=Decimal("0"),
            )
            JournalLine.objects.create(
                entry=entry, account=cls.revenue, sort_order=1,
                debit_amount=Decimal("0"), credit_amount=Decimal(amount),
            )

        # A void entry — must not reach any balance on these pages.
        void = JournalEntry.objects.create(
            company=cls.company, entry_number="JE-VOID",
            entry_date="2026-04-01", status=JournalEntry.Status.VOID,
        )
        JournalLine.objects.create(
            entry=void, account=cls.cash, sort_order=0,
            debit_amount=Decimal("9999"), credit_amount=Decimal("0"),
        )
        JournalLine.objects.create(
            entry=void, account=cls.revenue, sort_order=1,
            debit_amount=Decimal("0"), credit_amount=Decimal("9999"),
        )

        # Two companies exist in this fixture, so naming one requires a
        # superuser — see resolve_company. `staff_only` below is the
        # non-superuser staff account used to prove that restriction.
        cls.staff = User.objects.create_user(
            "staffer", password="pw", is_staff=True, is_superuser=True
        )
        cls.staff_only = User.objects.create_user(
            "staffonly", password="pw", is_staff=True
        )
        cls.plain = User.objects.create_user("plain", password="pw")

    def setUp(self):
        self.client.force_login(self.staff)

    def url(self, name, *args):
        """Reverse with the company pinned.

        The fixture deliberately holds two companies, so these pages refuse to
        guess which books to show — that refusal is itself tested in
        ``test_ambiguous_company_is_refused_rather_than_guessed``. Every other
        test names the company the way a real multi-tenant caller would.
        """
        return f"{reverse(name, args=args)}?company={self.company.pk}"

    def test_ambiguous_company_is_refused_rather_than_guessed(self):
        """Two companies and no hint must show nothing, not one at random."""
        resp = self.client.get(reverse("erp-accounting-trial-balance"))
        self.assertContains(resp, "none was selected")
        self.assertNotContains(resp, "$600.00")

    # -- access ------------------------------------------------------------

    def test_non_staff_cannot_read_the_ledger(self):
        """The GL is staff-only; a logged-in non-staff user gets no rows."""
        self.client.force_login(self.plain)
        for name in (
            "erp-accounting-dashboard",
            "erp-accounting-trial-balance",
            "erp-accounting-entries",
        ):
            resp = self.client.get(self.url(name))
            self.assertNotEqual(
                resp.status_code, 200,
                f"{name} rendered for a non-staff user",
            )

    # -- trial balance -----------------------------------------------------

    def test_trial_balance_totals_exclude_void_entries(self):
        """$9,999.00 of void activity must not appear in the totals."""
        resp = self.client.get(self.url("erp-accounting-trial-balance"))
        self.assertContains(resp, "$600.00")
        self.assertNotContains(resp, "9,999")
        self.assertContains(resp, "in balance")

    def test_trial_balance_footer_is_ledger_wide_when_filtered_by_type(self):
        """A type filter narrows the rows, never the balance identity.

        If the footer silently became the sum of the visible rows, a filtered
        trial balance would appear wildly out of balance and mean nothing.
        """
        resp = self.client.get(
            self.url("erp-accounting-trial-balance") + "&account_type=asset"
        )
        self.assertContains(resp, "$600.00")
        self.assertContains(resp, "rows above are filtered")
        # Assert on the row's link, not the word "Sales" — the sidebar carries
        # a "Sales" section header on every page, so a bare text match here
        # passes whether or not the filter works.
        revenue_row = reverse(
            "erp-accounting-account-register", args=[self.revenue.pk]
        )
        self.assertNotContains(resp, revenue_row)
        cash_row = reverse(
            "erp-accounting-account-register", args=[self.cash.pk]
        )
        self.assertContains(resp, cash_row)

    def test_zero_activity_accounts_are_hidden_then_shown_on_request(self):
        hidden = self.client.get(self.url("erp-accounting-trial-balance"))
        self.assertNotContains(hidden, "Dormant")
        shown = self.client.get(self.url("erp-accounting-trial-balance") + "&show_empty=1")
        self.assertContains(shown, "Dormant")

    def test_credit_normal_accounts_get_a_positive_natural_balance(self):
        """Revenue of 600 reads as 600, not -600."""
        resp = self.client.get(self.url("erp-accounting-trial-balance"))
        body = resp.content.decode()
        self.assertNotIn("-600.00", body)

    def test_csv_export_carries_the_same_rows(self):
        resp = self.client.get(self.url("erp-accounting-trial-balance") + "&export=csv")
        self.assertEqual(resp["Content-Type"], "text/csv")
        body = resp.content.decode()
        self.assertIn("Cash", body)
        self.assertIn("Sales", body)
        self.assertNotIn("Dormant", body)

    # -- register ----------------------------------------------------------

    def test_register_running_balance_accumulates_in_date_order(self):
        resp = self.client.get(
            self.url("erp-accounting-account-register", self.cash.pk)
        )
        body = resp.content.decode()
        for figure in ("$100.00", "$300.00", "$600.00"):
            self.assertIn(figure, body)

    def test_register_excludes_void_and_reports_closing_balance(self):
        resp = self.client.get(
            self.url("erp-accounting-account-register", self.cash.pk)
        )
        self.assertContains(resp, "$600.00")
        self.assertNotContains(resp, "9,999")

    def test_register_refuses_another_companys_account(self):
        theirs = Account.objects.create(
            company=self.other, account_number="1000", name="Their Cash",
            full_name="Their Cash", account_type=Account.AccountType.ASSET,
        )
        resp = self.client.get(
            self.url("erp-accounting-account-register", theirs.pk)
        )
        self.assertEqual(resp.status_code, 404)

    # -- journal -----------------------------------------------------------

    def test_entry_list_search_matches_on_number(self):
        resp = self.client.get(self.url("erp-accounting-entries") + "&q=JE-2")
        self.assertContains(resp, "JE-2")
        self.assertNotContains(resp, "JE-1")

    def test_entry_detail_shows_both_legs_and_ties(self):
        entry = JournalEntry.objects.get(entry_number="JE-3")
        resp = self.client.get(
            self.url("erp-accounting-entry-detail", entry.pk)
        )
        self.assertContains(resp, "Cash")
        self.assertContains(resp, "Sales")
        self.assertContains(resp, "$300.00")
        self.assertNotContains(resp, "does not balance")

    def test_entry_detail_refuses_another_companys_entry(self):
        theirs = JournalEntry.objects.create(
            company=self.other, entry_number="X-1", entry_date="2026-01-01"
        )
        resp = self.client.get(
            self.url("erp-accounting-entry-detail", theirs.pk)
        )
        self.assertEqual(resp.status_code, 404)

    # -- dashboard ---------------------------------------------------------

    def test_dashboard_reports_the_ledger_ties_and_names_what_it_excluded(self):
        """The fixture holds one void entry, so the banner must say so.

        The banner is the page's only route for telling you a figure above it
        left something out; silence there is indistinguishable from having
        nothing to leave out.
        """
        resp = self.client.get(self.url("erp-accounting-dashboard"))
        self.assertContains(resp, "In balance")
        self.assertContains(resp, "draft/void")
        self.assertNotContains(resp, "unbalanced")

    def test_dashboard_shows_no_banner_when_there_is_nothing_to_report(self):
        JournalEntry.objects.filter(status=JournalEntry.Status.VOID).delete()
        resp = self.client.get(self.url("erp-accounting-dashboard"))
        self.assertContains(resp, "In balance")
        self.assertNotContains(resp, "alert-warning")

    def test_dashboard_warns_about_an_unbalanced_entry(self):
        """A one-legged entry is the thing the banner exists to surface."""
        broken = JournalEntry.objects.create(
            company=self.company, entry_number="JE-BAD", entry_date="2026-05-01"
        )
        JournalLine.objects.create(
            entry=broken, account=self.cash, sort_order=0,
            debit_amount=Decimal("50"), credit_amount=Decimal("0"),
        )
        resp = self.client.get(self.url("erp-accounting-dashboard"))
        self.assertContains(resp, "alert-warning")
        self.assertContains(resp, "unbalanced")

    def test_dashboard_counts_void_entries_as_excluded(self):
        resp = self.client.get(self.url("erp-accounting-dashboard"))
        self.assertEqual(resp.context["excluded_count"], 1)


class GLSecurityAndAccuracyTestCase(GLReportingTestCase):
    """Regressions for defects found in adversarial review, 2026-09-08."""

    def test_staff_user_cannot_read_another_companys_ledger_by_uuid(self):
        """?company= is not a tenant selector for ordinary staff.

        There is no user-to-company binding to authorise it against
        (UserProfile once keyed off a legacy uid, not auth.User), so honouring the
        parameter would let any staff account read every company's books by
        pasting a UUID.
        """
        self.client.force_login(self.staff_only)
        resp = self.client.get(
            reverse("erp-accounting-trial-balance"),
            {"company": str(self.company.pk)},
        )
        self.assertNotContains(resp, "$600.00")
        self.assertContains(resp, "none was selected")

    def test_superuser_may_still_name_a_company(self):
        resp = self.client.get(self.url("erp-accounting-trial-balance"))
        self.assertContains(resp, "$600.00")

    def test_malformed_company_id_is_404_not_500(self):
        resp = self.client.get(
            reverse("erp-accounting-trial-balance"), {"company": "not-a-uuid"}
        )
        self.assertEqual(resp.status_code, 404)

    def test_rendered_links_carry_the_company_forward(self):
        """Follow the page's own links; they must not land on a 404.

        Pinning the parameter in the test URL proves nothing about the hrefs
        the template actually emits — which is how every link on these pages
        came to drop it.
        """
        import re

        resp = self.client.get(self.url("erp-accounting-trial-balance"))
        hrefs = re.findall(r'href="([^"]+)"', resp.content.decode())
        followed = 0
        for href in hrefs:
            if "/erp/accounting/" not in href and not href.startswith("?"):
                continue
            if href.startswith("?"):
                href = reverse("erp-accounting-trial-balance") + href
            followed += 1
            self.assertEqual(
                self.client.get(href.replace("&amp;", "&")).status_code, 200,
                f"link 404s: {href}",
            )
        self.assertGreater(followed, 1, "no accounting links were exercised")

    def test_draft_entries_do_not_trigger_the_unbalanced_alarm(self):
        """A draft is allowed to be half-written; it is not a broken ledger."""
        draft = JournalEntry.objects.create(
            company=self.company, entry_number="JE-DRAFT",
            entry_date="2026-06-01", status=JournalEntry.Status.DRAFT,
        )
        JournalLine.objects.create(
            entry=draft, account=self.cash, sort_order=0,
            debit_amount=Decimal("5"), credit_amount=Decimal("0"),
        )
        resp = self.client.get(self.url("erp-accounting-dashboard"))
        self.assertEqual(resp.context["unbalanced_count"], 0)
        self.assertNotContains(resp, "unbalanced")
        self.assertContains(resp, "draft/void")

    def test_date_filtered_register_carries_the_opening_balance(self):
        """Closing balance must mean closing, not period movement.

        Cash holds 100 (Jan) + 200 (Feb) + 300 (Mar). Asked for March only,
        the account still closes at 600, not 300.
        """
        resp = self.client.get(
            self.url("erp-accounting-account-register", self.cash.pk)
            + "&date_from=2026-03-01"
        )
        self.assertEqual(resp.context["opening_balance"], Decimal("300.00"))
        self.assertEqual(resp.context["period_movement"], Decimal("300.00"))
        self.assertEqual(resp.context["closing_balance"], Decimal("600.00"))
        self.assertContains(resp, "brought forward")
        # The running balance on the visible row is offset by the opening
        # figure, so the register reads 600 and not 300.
        self.assertContains(resp, "$600.00")

    def test_csv_export_neutralises_spreadsheet_formulas(self):
        """Account names come from a third-party export."""
        Account.objects.create(
            company=self.company, account_number="=cmd|calc",
            name="=HYPERLINK(1)", full_name="=HYPERLINK(1)",
            account_type=Account.AccountType.ASSET,
        )
        entry = JournalEntry.objects.create(
            company=self.company, entry_number="JE-CSV", entry_date="2026-07-01"
        )
        evil = Account.objects.get(full_name="=HYPERLINK(1)")
        JournalLine.objects.create(
            entry=entry, account=evil, sort_order=0,
            debit_amount=Decimal("1"), credit_amount=Decimal("0"),
        )
        JournalLine.objects.create(
            entry=entry, account=self.revenue, sort_order=1,
            debit_amount=Decimal("0"), credit_amount=Decimal("1"),
        )
        body = self.client.get(
            self.url("erp-accounting-trial-balance") + "&export=csv"
        ).content.decode()
        self.assertIn("'=HYPERLINK(1)", body)
        self.assertNotIn(",=HYPERLINK(1)", body)


class MoneyFilterTests(TestCase):
    """DD-14: one money filter — no bare `$` in any back-office template."""

    def test_no_hardcoded_dollar_signs(self):
        from golden_erp.paths import TEMPLATES_DIR

        paths = list(TEMPLATES_DIR.rglob("*.html"))
        # A moved/renamed templates dir must fail here, not scan nothing.
        self.assertGreater(len(paths), 0, f"no templates found under {TEMPLATES_DIR}")
        offenders = [str(path) for path in paths if "${{" in path.read_text()]
        self.assertEqual(offenders, [])

    def test_money_filter_renders(self):
        from core.templatetags.erp_tags import money

        self.assertEqual(money("100"), "$100.00")
        self.assertEqual(money("1234.5"), "$1,234.50")
        self.assertEqual(money("-1234.5"), "($1,234.50)")
        self.assertEqual(money("100", "USD"), "$100.00")
        # Another currency keeps its code, so it never reads as dollars.
        self.assertEqual(money("-5.5", "cad"), "(CAD 5.50)")
        self.assertNotIn("USD", money("100"))
        self.assertEqual(money("0"), "$0.00")
        self.assertEqual(money(None), "—")
        self.assertEqual(money("junk"), "—")
