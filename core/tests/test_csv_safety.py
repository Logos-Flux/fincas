"""CSV formula escaping (release-hardening-plan.md 9.8, 6.3).

One rule in ``core.csv_safety``, used by every CSV writer: a string cell that
starts with ``= + - @``, a tab or a carriage return is prefixed with ``'``
unless it is a plain number; non-string values are written as the number.
"""

from __future__ import annotations

import csv
import io
from decimal import Decimal

from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.csv_safety import csv_safe, csv_safe_row
from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from customers.models import Customer
from parties.models import Party

HYPERLINK = '=HYPERLINK("http://evil.example", "click")'


def table(response):
    if hasattr(response, "streaming_content"):
        body = b"".join(response.streaming_content).decode()
    else:
        body = response.content.decode()
    return list(csv.reader(io.StringIO(body)))


class CsvSafeRuleTests(TestCase):
    def test_formula_triggers_are_prefixed(self):
        for text in (HYPERLINK, "+1+1", "-1+1", "@SUM(A1)", "\t=cmd", "\r=cmd"):
            with self.subTest(text=text):
                self.assertEqual(csv_safe(text), "'" + text)

    def test_plain_numbers_keep_their_sign(self):
        for text in ("-12.50", "-5", "12", "0.5"):
            with self.subTest(text=text):
                self.assertEqual(csv_safe(text), text)

    def test_formatted_negative_money_is_text_and_is_prefixed(self):
        # The rule, documented in core.csv_safety: format to text and accept
        # the prefix, or pass the number through.
        self.assertEqual(csv_safe("-$12.50"), "'-$12.50")
        self.assertEqual(csv_safe("-1,200.00"), "'-1,200.00")

    def test_non_strings_pass_through_as_numbers(self):
        for value in (Decimal("-12.50"), -5, 0, None, True):
            with self.subTest(value=value):
                self.assertIs(csv_safe(value), value)

    def test_ordinary_text_is_untouched(self):
        self.assertEqual(csv_safe("Gold Org"), "Gold Org")
        self.assertEqual(csv_safe(""), "")
        self.assertEqual(csv_safe(" =not-a-formula"), " =not-a-formula")

    def test_row_helper(self):
        self.assertEqual(csv_safe_row(["=a", Decimal("-1"), "b"]), ["'=a", Decimal("-1"), "b"])


class ExportFixture(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.staff = make_staff_user("csv-staff", password="pw")
        UserProfile.objects.update_or_create(user=cls.staff, defaults={"company": cls.company})

    def setUp(self):
        self.client.force_login(self.staff)


class ListExportEscapingTests(ExportFixture):
    """The streaming canonical export (``core.list_export``)."""

    URL = reverse("erp-party-list")

    def test_cells_are_prefixed_and_headers_unchanged(self):
        for name in (HYPERLINK, "+1+1", "-cmd|calc", "@SUM(A1)"):
            Party.objects.create(company=self.company, kind="organisation", display_name=name, origin="erp")
        before = table(self.client.get(self.URL, {"export": "csv"}))[0]
        rows = table(self.client.get(self.URL, {"export": "csv"}))
        self.assertEqual(rows[0], before)
        self.assertIn("display_name", rows[0])  # canonical column ids, never labels
        self.assertEqual(rows[0], [h for h in rows[0] if not h.startswith("'")])
        names = sorted(r[rows[0].index("display_name")] for r in rows[1:])
        self.assertEqual(names, sorted(["'" + HYPERLINK, "'+1+1", "'-cmd|calc", "'@SUM(A1)"]))
        response = self.client.get(self.URL, {"export": "csv"})
        self.assertTrue(hasattr(response, "streaming_content"))  # still streamed


class CommerceCsvEscapingTests(ExportFixture):
    """The non-canonical commerce lists (``ERPListView.export_csv``)."""

    URL = reverse("erp-customer-list")

    def test_cells_are_prefixed_and_headers_unchanged(self):
        Customer.objects.create(company=self.company, first_name=HYPERLINK, last_name="x",
                                email="a@example.com", company_name="+1+1")
        Customer.objects.create(company=self.company, first_name="-cmd", last_name="y",
                                email="b@example.com", company_name="@SUM(A1)")
        rows = table(self.client.get(self.URL, {"export": "csv"}))
        self.assertEqual(rows[0][:3], ["Name", "Email", "Company"])
        by_email = {r[1]: r for r in rows[1:]}
        self.assertEqual(by_email["a@example.com"][0], "'" + HYPERLINK + " x")
        self.assertEqual(by_email["a@example.com"][2], "'+1+1")
        self.assertEqual(by_email["b@example.com"][0], "'-cmd y")
        self.assertEqual(by_email["b@example.com"][2], "'@SUM(A1)")


class AccountingCsvEscapingTests(TestCase):
    """The reconciliation export writes labels straight from account names."""

    def test_reconciliation_rows_are_escaped_and_numbers_stay_numbers(self):
        from accounting.erp_views import ReconciliationView

        view = ReconciliationView()
        view.request = RequestFactory().get("/erp/accounting/reconciliation/?export=csv")
        response = view._csv([
            {"label": "=HYPERLINK(1)", "gl": Decimal("-10.00"), "subledger": Decimal("0"),
             "diff": Decimal("-10.00"), "note": "@note"},
        ])
        self.assertEqual(response["Content-Type"], "text/csv")
        rows = table(response)
        self.assertEqual(rows[0], ["account", "gl", "subledger", "difference", "note"])
        self.assertEqual(rows[1], ["'=HYPERLINK(1)", "-10.00", "0", "-10.00", "'@note"])
