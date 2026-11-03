"""Milestone 3 Track G — S9: file upload (accounting import surface).

Product media and CMS artwork are URL references (Cloudinary-style
`cf_image_id` / `featured_image_url`), not file uploads — there is no
SVG-upload path to test. The only true file-upload surface is the
staff-only accounting QBO import. Pins:

- allow-list enforced: .csv/.xlsx/.xlsm accepted; .exe/.svg/.html/.js
  rejected with a field error (no 500)
- size: no explicit cap exists — a multi-MB file is accepted
  (characterized; DoS hardening is open)
- macro-capable .xlsm is accepted by extension (characterized; the
  reader only parses cell values, never executes macros — open note)
- uploaded cell values reaching the preview report render escaped
"""

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from accounting.forms import AccountingImportForm
from core.models import Company

IMPORT_URL = "/erp/accounting/import/"


def _csv_bytes(rows=3):
    lines = ["Number,Name,Type"] + [
        f"100{i},Account {i},Expense" for i in range(rows)
    ]
    return ("\n".join(lines) + "\n").encode()


class UploadValidationTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="upl-staff", password="pw", is_staff=True
        )
        self.company = Company.objects.create(name="Upl Co")
        self.client.force_login(self.staff)

    def _post(self, name, content, **extra):
        data = {
            "company": str(self.company.pk),
            "chart_of_accounts": SimpleUploadedFile(
                name, content, content_type="application/octet-stream"
            ),
        }
        data.update(extra)
        return self.client.post(IMPORT_URL, data, follow=True)

    def test_csv_accepted(self):
        response = self._post("coa.csv", _csv_bytes())
        self.assertEqual(response.status_code, 200)

    def test_exe_rejected_with_field_error(self):
        response = self._post("evil.exe", b"MZ...")
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertTrue(form.errors)
        self.assertIn("chart_of_accounts", form.errors)

    def test_svg_rejected(self):
        response = self._post(
            "evil.svg", b"<svg onload=alert(1)>", dry_run="on"
        )
        form = response.context["form"]
        self.assertTrue(form.errors)
        self.assertIn("chart_of_accounts", form.errors)

    def test_html_rejected(self):
        response = self._post("evil.html", b"<script>alert(1)</script>")
        form = response.context["form"]
        self.assertTrue(form.errors)

    def test_large_file_accepted_no_cap(self):
        # Characterize: no MaxSize validator exists on the form.
        self.assertFalse(hasattr(AccountingImportForm, "MAX_UPLOAD_SIZE"))
        big = _csv_bytes(rows=60000)  # ~1MB
        self.assertGreater(len(big), 500_000)
        response = self._post("big.csv", big)
        self.assertEqual(response.status_code, 200)
        print(
            f"\n[S9-characterize] {len(big)}-byte upload accepted "
            "(no size cap on AccountingImportForm)"
        )

    def test_xlsm_macro_capable_extension_accepted(self):
        # Characterize: .xlsm (macro-capable) passes the extension gate.
        # openpyxl/data_only-style readers parse values, never run macros,
        # but the gate itself does not distinguish .xlsx from .xlsm.
        self.assertIn(".xlsm", AccountingImportForm.ALLOWED_EXTENSIONS)
        print("\n[S9-characterize] .xlsm in ALLOWED_EXTENSIONS (macros not executed by reader)")

    def test_script_cell_values_escaped_in_report(self):
        evil = (
            "Number,Name,Type\n"
            '1000,"<script>alert(\'csv\')</script>",Expense\n'
        ).encode()
        response = self._post("evil.csv", evil)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn("<script>alert('csv')</script>", content)
