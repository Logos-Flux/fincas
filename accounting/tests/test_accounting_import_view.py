"""Accounting import view — the ERP's upload surface for QBO exports.

Covers the properties the task calls out explicitly: staff-only, company is
mandatory (never guessed), .csv/.xlsx/.xlsm accepted with .xls refused, dry-run
default + rollback, and the importer's full report rendered.
"""


from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from accounting.models import Account, JournalEntry
from core.models import Company

# Keep the reader honest — same fixtures as the importer tests, but built as
# uploads rather than temp-file paths so the view's chunk-handling is exercised.
COA_CSV = b"""Account Number,Account Name,Type,Detail Type
1000,Checking,Bank,Checking
1200,Accounts Receivable,Accounts receivable (A/R),Accounts Receivable
4000,Sales,Income,Sales of Product Income
"""

JOURNAL_CSV = b"""Date,Transaction Type,Num,Name,Account,Debit,Credit
01/15/2025,Invoice,1001,Acme,Accounts Receivable,100.00,
01/15/2025,Invoice,1001,Acme,Sales,,100.00
"""

UNBALANCED_CSV = b"""Date,Transaction Type,Num,Account,Debit,Credit
01/15/2025,Invoice,9001,Accounts Receivable,100.00,
01/15/2025,Invoice,9001,Sales,,60.00
"""


def _upload(name, content):
    return SimpleUploadedFile(name, content, content_type="text/csv")


def _minimal_xlsx_bytes(csv_content):
    """Build a minimal .xlsx from CSV bytes so xlsx uploads are exercised too."""
    import csv
    import io

    try:
        import openpyxl  # noqa: F401
    except ImportError:
        return None

    import openpyxl as _oxl

    lines = csv_content.decode("utf-8").strip().splitlines()
    wb = _oxl.Workbook()
    ws = wb.active
    for row in csv.reader(lines):
        ws.append(row)
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


class AccountingImportViewTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.other = Company.objects.create(name="Voltage Labs")
        self.staff = User.objects.create_user(
            username="staffer", password="pw", is_staff=True
        )
        self.non_staff = User.objects.create_user(
            username="regular", password="pw", is_staff=False
        )
        self.url = reverse("erp-accounting-import")

    # --- access control ---

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/erp/login", resp["Location"])

    def test_non_staff_is_forbidden(self):
        self.client.force_login(self.non_staff)
        resp = self.client.get(self.url)
        # Authenticated but not staff -> UserPassesTestMixin gives 403
        # (Django 5+ returns 403 rather than a login redirect when the user
        # is already authenticated).
        self.assertEqual(resp.status_code, 403)

    def test_staff_can_reach_the_form(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Import QBO exports")

    def test_staff_sees_company_choices(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.url)
        self.assertContains(resp, "Example Supply, Inc")

    # --- validation ---

    def test_company_is_required(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.url,
            {"chart_of_accounts": _upload("coa.csv", COA_CSV), "dry_run": "on"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "This field is required")
        self.assertEqual(Account.objects.count(), 0)

    def test_at_least_one_file_is_required(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.url,
            {"company": str(self.company.pk), "dry_run": "on"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Upload at least one file")
        self.assertEqual(Account.objects.count(), 0)

    def test_legacy_xls_is_refused(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.url,
            {
                "company": str(self.company.pk),
                "chart_of_accounts": _upload("coa.xls", COA_CSV),
                "dry_run": "on",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Unsupported file type")
        self.assertEqual(Account.objects.count(), 0)

    def test_xlsx_is_accepted(self):
        xlsx = _minimal_xlsx_bytes(COA_CSV)
        if xlsx is None:
            self.skipTest("openpyxl not installed in this env")
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.url,
            {
                "company": str(self.company.pk),
                "chart_of_accounts": SimpleUploadedFile(
                    "coa.xlsx",
                    xlsx,
                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ),
                "dry_run": "on",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Import report")

    def test_xlsm_is_accepted(self):
        xlsx = _minimal_xlsx_bytes(COA_CSV)
        if xlsx is None:
            self.skipTest("openpyxl not installed in this env")
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.url,
            {
                "company": str(self.company.pk),
                "chart_of_accounts": SimpleUploadedFile(
                    "coa.xlsm",
                    xlsx,
                    content_type="application/vnd.ms-excel.sheet.macroEnabled.12",
                ),
                "dry_run": "on",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Import report")

    # --- dry-run is the safety story ---

    def test_dry_run_reports_but_writes_nothing(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.url,
            {
                "company": str(self.company.pk),
                "chart_of_accounts": _upload("coa.csv", COA_CSV),
                "transactions": _upload("journal.csv", JOURNAL_CSV),
                "dry_run": "on",
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "DRY RUN")
        self.assertContains(resp, "Import report")
        # Nothing committed — the outer test transaction sees no rows (the view's
        # inner atomic was rolled back), same guarantee as --dry-run on the CLI.
        self.assertEqual(Account.objects.count(), 0)
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_dry_run_off_actually_writes(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.url,
            {
                "company": str(self.company.pk),
                "chart_of_accounts": _upload("coa.csv", COA_CSV),
                "transactions": _upload("journal.csv", JOURNAL_CSV),
                # dry_run omitted => False
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Import report")
        self.assertNotContains(resp, "DRY RUN — rolled back")
        self.assertEqual(Account.objects.for_company(self.company).count(), 3)
        self.assertEqual(JournalEntry.objects.for_company(self.company).count(), 1)

    def test_form_renders_dry_run_checked_by_default(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.url)
        # The checkbox input should carry `checked` on initial render.
        self.assertContains(resp, 'name="dry_run"')
        # Look for checked attribute near the dry_run input — the form's
        # initial is True so the widget renders checked.
        content = resp.content.decode()
        # Find the dry_run input tag and assert it is checked.
        import re

        m = re.search(r'<input[^>]*name="dry_run"[^>]*>', content)
        self.assertIsNotNone(m, "dry_run input not found in form")
        self.assertIn("checked", m.group(0))

    # --- import is company-scoped ---

    def test_import_targets_only_the_named_company(self):
        self.client.force_login(self.staff)
        self.client.post(
            self.url,
            {
                "company": str(self.company.pk),
                "chart_of_accounts": _upload("coa.csv", COA_CSV),
                "transactions": _upload("journal.csv", JOURNAL_CSV),
            },
        )
        self.assertEqual(Account.objects.for_company(self.other).count(), 0)
        self.assertEqual(JournalEntry.objects.for_company(self.other).count(), 0)

    # --- report shows what happened ---

    def test_report_shows_counts(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.url,
            {
                "company": str(self.company.pk),
                "chart_of_accounts": _upload("coa.csv", COA_CSV),
            },
        )
        self.assertContains(resp, "Accounts")
        self.assertContains(resp, "created")

    def test_report_shows_skipped_unbalanced(self):
        # Need the chart first so the accounts exist for the journal.
        self.client.force_login(self.staff)
        self.client.post(
            self.url,
            {
                "company": str(self.company.pk),
                "chart_of_accounts": _upload("coa.csv", COA_CSV),
            },
        )
        resp = self.client.post(
            self.url,
            {
                "company": str(self.company.pk),
                "transactions": _upload("journal.csv", UNBALANCED_CSV),
            },
        )
        self.assertContains(resp, "Unbalanced")
        self.assertEqual(JournalEntry.objects.for_company(self.company).count(), 0)

    def test_malformed_file_surfaces_as_report_error(self):
        # A file with no recognised header triggers QBOFormatError inside the
        # view's atomic block — it should appear as an error, not a 500.
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.url,
            {
                "company": str(self.company.pk),
                "chart_of_accounts": _upload("coa.csv", b"foo,bar,baz\n1,2,3\n"),
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "No header row found")

    def test_chart_only_without_transactions(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.url,
            {
                "company": str(self.company.pk),
                "chart_of_accounts": _upload("coa.csv", COA_CSV),
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(Account.objects.for_company(self.company).count(), 3)
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_transactions_only_without_chart(self):
        # Chart already exists — transactions alone should reuse it (no placeholders).
        self.client.force_login(self.staff)
        self.client.post(
            self.url,
            {
                "company": str(self.company.pk),
                "chart_of_accounts": _upload("coa.csv", COA_CSV),
            },
        )
        resp = self.client.post(
            self.url,
            {
                "company": str(self.company.pk),
                "transactions": _upload("journal.csv", JOURNAL_CSV),
            },
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(JournalEntry.objects.for_company(self.company).count(), 1)
        # No placeholders — every referenced account already existed.
        self.assertEqual(Account.objects.for_company(self.company).count(), 3)
