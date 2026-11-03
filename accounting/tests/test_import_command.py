"""The ``import_qbo_export`` management command.

Focus: the guard rails around the import rather than the import itself —
company resolution (getting it wrong posts a ledger to the wrong books), the
dry-run rollback, and a non-zero exit when anything was skipped.
"""

import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from accounting.models import Account, JournalEntry
from core.models import Company

COA_CSV = """Account Number,Account Name,Type,Detail Type
1000,Checking,Bank,Checking
1200,Accounts Receivable,Accounts receivable (A/R),Accounts Receivable
4000,Sales,Income,Sales of Product Income
"""

JOURNAL_CSV = """Date,Transaction Type,Num,Name,Account,Debit,Credit
01/15/2025,Invoice,1001,Acme,Accounts Receivable,100.00,
01/15/2025,Invoice,1001,Acme,Sales,,100.00
"""


class ImportCommandTest(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.coa = self.write("coa.csv", COA_CSV)
        self.journal = self.write("journal.csv", JOURNAL_CSV)

    def write(self, name, content):
        path = Path(self.tmp.name) / name
        path.write_text(content, encoding="utf-8")
        return str(path)

    def run_command(self, *args, **kwargs):
        out = StringIO()
        call_command("import_qbo_export", *args, stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    # --- company resolution ---

    def test_resolves_company_by_name(self):
        self.run_command("--company", "Example Supply, Inc", "--chart-of-accounts", self.coa)
        self.assertEqual(Account.objects.for_company(self.company).count(), 3)

    def test_resolves_company_by_uuid(self):
        self.run_command(
            "--company", str(self.company.pk), "--chart-of-accounts", self.coa
        )
        self.assertEqual(Account.objects.for_company(self.company).count(), 3)

    def test_company_name_match_is_case_insensitive(self):
        self.run_command("--company", "example supply, inc", "--chart-of-accounts", self.coa)
        self.assertEqual(Account.objects.for_company(self.company).count(), 3)

    def test_unknown_company_is_a_clear_error_and_writes_nothing(self):
        with self.assertRaises(CommandError) as ctx:
            self.run_command("--company", "Nope Ltd", "--chart-of-accounts", self.coa)
        self.assertIn("No company matches", str(ctx.exception))
        self.assertEqual(Account.objects.count(), 0)

    def test_ambiguous_company_name_refuses_to_guess(self):
        Company.objects.create(name="example supply, inc")  # differs only in case
        with self.assertRaises(CommandError) as ctx:
            self.run_command("--company", "Example Supply, Inc", "--chart-of-accounts", self.coa)
        self.assertIn("matches 2 companies", str(ctx.exception))
        self.assertEqual(Account.objects.count(), 0)

    # --- arguments ---

    def test_company_is_required(self):
        with self.assertRaises(CommandError):
            self.run_command("--chart-of-accounts", self.coa)

    def test_no_input_file_is_an_error(self):
        with self.assertRaises(CommandError) as ctx:
            self.run_command("--company", "Example Supply, Inc")
        self.assertIn("Nothing to do", str(ctx.exception))

    def test_unsupported_file_type_is_reported_as_a_command_error(self):
        bad = self.write("data.txt", COA_CSV)
        with self.assertRaises(CommandError):
            self.run_command("--company", "Example Supply, Inc", "--chart-of-accounts", bad)

    # --- behaviour ---

    def test_imports_accounts_and_transactions_together(self):
        output = self.run_command(
            "--company", "Example Supply, Inc",
            "--chart-of-accounts", self.coa,
            "--transactions", self.journal,
        )
        self.assertEqual(Account.objects.for_company(self.company).count(), 3)
        self.assertEqual(JournalEntry.objects.for_company(self.company).count(), 1)
        self.assertIn("Import complete", output)

        entry = JournalEntry.objects.get(company=self.company)
        entry.validate_balanced()

    def test_dry_run_reports_but_writes_nothing(self):
        output = self.run_command(
            "--company", "Example Supply, Inc",
            "--chart-of-accounts", self.coa,
            "--transactions", self.journal,
            "--dry-run",
        )
        self.assertIn("DRY RUN", output)
        self.assertIn("3 created", output)
        self.assertEqual(Account.objects.count(), 0)
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_transactions_alone_reuse_the_existing_chart(self):
        self.run_command("--company", "Example Supply, Inc", "--chart-of-accounts", self.coa)
        self.run_command("--company", "Example Supply, Inc", "--transactions", self.journal)

        # No placeholders — every referenced account already existed.
        self.assertEqual(Account.objects.for_company(self.company).count(), 3)
        self.assertEqual(JournalEntry.objects.for_company(self.company).count(), 1)

    def test_unbalanced_source_exits_non_zero_and_says_so(self):
        journal = self.write(
            "bad.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            "01/15/2025,Invoice,9001,Accounts Receivable,100.00,\n"
            "01/15/2025,Invoice,9001,Sales,,60.00\n",
        )
        self.run_command("--company", "Example Supply, Inc", "--chart-of-accounts", self.coa)
        with self.assertRaises(CommandError) as ctx:
            self.run_command("--company", "Example Supply, Inc", "--transactions", journal)
        self.assertIn("unbalanced", str(ctx.exception))
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_rerun_is_idempotent(self):
        for _ in range(2):
            self.run_command(
                "--company", "Example Supply, Inc",
                "--chart-of-accounts", self.coa,
                "--transactions", self.journal,
            )
        self.assertEqual(Account.objects.for_company(self.company).count(), 3)
        self.assertEqual(JournalEntry.objects.for_company(self.company).count(), 1)

    def test_import_targets_only_the_named_company(self):
        other = Company.objects.create(name="Voltage Labs")
        self.run_command(
            "--company", "Example Supply, Inc",
            "--chart-of-accounts", self.coa,
            "--transactions", self.journal,
        )
        self.assertEqual(Account.objects.for_company(other).count(), 0)
        self.assertEqual(JournalEntry.objects.for_company(other).count(), 0)


class PartialCommitReportingTest(TestCase):
    """A non-zero exit must not imply that nothing was written.

    Anything that parsed cleanly is already committed by the time the command
    raises, so a caller reading only the status code would conclude the import
    did not happen and re-run it.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.coa = self.write("coa.csv", COA_CSV)

    def write(self, name, content):
        path = Path(self.tmp.name) / name
        path.write_text(content, encoding="utf-8")
        return str(path)

    def run_command(self, *args, **kwargs):
        out = StringIO()
        call_command("import_qbo_export", *args, stdout=out, stderr=out, **kwargs)
        return out.getvalue()

    def test_mixed_good_and_bad_says_the_import_was_partial(self):
        journal = self.write(
            "mixed.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            # good
            "01/15/2025,Invoice,9001,Accounts Receivable,100.00,\n"
            "01/15/2025,Invoice,9001,Sales,,100.00\n"
            # unbalanced
            "01/16/2025,Invoice,9002,Accounts Receivable,50.00,\n"
            "01/16/2025,Invoice,9002,Sales,,30.00\n",
        )
        self.run_command("--company", "Example Supply, Inc", "--chart-of-accounts", self.coa)
        with self.assertRaises(CommandError) as ctx:
            self.run_command("--company", "Example Supply, Inc", "--transactions", journal)

        message = str(ctx.exception)
        self.assertIn("PARTIAL IMPORT COMMITTED", message)
        self.assertIn("re-running is safe", message)
        # And the message is telling the truth: the good entry really is there.
        self.assertEqual(JournalEntry.objects.for_company(self.company).count(), 1)
        self.assertTrue(
            JournalEntry.objects.filter(
                company=self.company, source_reference="9001"
            ).exists()
        )
        self.assertFalse(
            JournalEntry.objects.filter(
                company=self.company, source_reference="9002"
            ).exists()
        )

    def test_dry_run_says_nothing_was_written(self):
        journal = self.write(
            "bad.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            "01/16/2025,Invoice,9002,Accounts Receivable,50.00,\n"
            "01/16/2025,Invoice,9002,Sales,,30.00\n",
        )
        with self.assertRaises(CommandError) as ctx:
            self.run_command(
                "--company", "Example Supply, Inc",
                "--chart-of-accounts", self.coa,
                "--transactions", journal,
                "--dry-run",
            )
        self.assertIn("Nothing was written (dry run)", str(ctx.exception))
        self.assertEqual(Account.objects.count(), 0)


class RequireStatusFlagTest(TestCase):
    """``--require-status`` on the command line (the importer is tested in
    test_qbo_import.StatusFilterTest)."""

    # Borrowed, not inherited: subclassing would re-run every parent test.
    setUp = ImportCommandTest.setUp
    write = ImportCommandTest.write
    run_command = ImportCommandTest.run_command

    STATUS_JOURNAL = (
        "Date,Num,Account,Debit (USD),Credit (USD),Status\n"
        "--- JE-1 --- POST ---,,,,,\n"
        "2025-12-31,JE-1,Accounts Receivable,100.00,,POST\n"
        "2025-12-31,JE-1,Sales,,100.00,POST\n"
        "2025-12-31,JE-2,Accounts Receivable,50.00,,PROPOSED - NOT POSTED\n"
        "2025-12-31,JE-2,Sales,,50.00,PROPOSED - NOT POSTED\n"
    )

    def test_flag_imports_only_approved_entries_and_echoes_the_gate(self):
        self.run_command("--company", "Example Supply, Inc", "--chart-of-accounts", self.coa)
        out = self.run_command(
            "--company", "Example Supply, Inc",
            "--transactions", self.write("je.csv", self.STATUS_JOURNAL),
            "--require-status", "POST",
        )
        self.assertIn("Status filter:", out)
        self.assertIn("'POST'", out)
        self.assertEqual(
            list(JournalEntry.objects.values_list("source_reference", flat=True)),
            ["JE-1"],
        )

    def test_flag_without_a_status_column_refuses_and_writes_nothing(self):
        self.run_command("--company", "Example Supply, Inc", "--chart-of-accounts", self.coa)
        with self.assertRaisesMessage(CommandError, "no status column"):
            self.run_command(
                "--company", "Example Supply, Inc",
                "--transactions", self.journal,
                "--require-status", "POST",
            )
        self.assertFalse(JournalEntry.objects.exists())
