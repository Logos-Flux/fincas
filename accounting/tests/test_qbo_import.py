"""End-to-end QBO import behaviour.

The properties under test are the ones whose absence sank the first attempt at
this app: every write carries a company, re-running does not duplicate, an
unbalanced source group is never written, and negatives land on the right side.
"""

import datetime
import tempfile
from decimal import Decimal
from pathlib import Path

from django.test import TestCase

from accounting.models import Account, FiscalPeriod, JournalEntry, JournalLine
from accounting.qbo.importer import (
    ChartOfAccountsImporter,
    entry_fingerprint,
    import_chart_of_accounts,
    import_transactions,
)
from accounting.qbo.readers import QBOFormatError
from core.models import Company

COA_CSV = """Account Number,Account Name,Type,Detail Type,Description,Balance
1000,Checking,Bank,Checking,Operating account,15000.00
1200,Accounts Receivable,Accounts receivable (A/R),Accounts Receivable,,3200.00
1300,Inventory Asset,Other Current Assets,Inventory,,42000.00
2000,Accounts Payable,Accounts payable (A/P),Accounts Payable,,1800.00
2200,Sales Tax Payable,Other Current Liabilities,Sales Tax Payable,,410.00
3000,Opening Balance Equity,Equity,Opening Balance Equity,,0.00
4000,Sales,Income,Sales of Product Income,,0.00
4100,Shipping Income,Income,"Shipping, Freight & Delivery Income",,0.00
5000,Cost of Goods Sold,Cost of Goods Sold,Supplies & Materials - COGS,,0.00
6000,Bank Charges,Expenses,Bank Charges,,0.00
"""

JOURNAL_CSV = """Example Supply Inc
Journal
January 1 - January 31, 2025

Date,Transaction Type,Num,Name,Memo/Description,Account,Debit,Credit
01/15/2025,Invoice,1001,Acme Corp,Widget sale,Accounts Receivable,108.25,
01/15/2025,Invoice,1001,Acme Corp,Widget sale,Sales,,100.00
01/15/2025,Invoice,1001,Acme Corp,Sales tax,Sales Tax Payable,,8.25
01/20/2025,Payment,1002,Acme Corp,Payment received,Checking,108.25,
01/20/2025,Payment,1002,Acme Corp,Payment received,Accounts Receivable,,108.25
"""


class _ImportTestBase(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.other = Company.objects.create(name="Voltage Labs")

    def write(self, name, content):
        path = Path(self.tmp.name) / name
        path.write_text(content, encoding="utf-8")
        return path


class ChartOfAccountsImportTest(_ImportTestBase):
    def test_imports_every_account_with_the_right_classification(self):
        path = self.write("coa.csv", COA_CSV)
        _, report = import_chart_of_accounts(self.company, path)

        self.assertEqual(report.accounts_created, 10)
        self.assertEqual(report.errors, [])
        self.assertEqual(Account.objects.for_company(self.company).count(), 10)

        checking = Account.objects.get(company=self.company, account_number="1000")
        self.assertEqual(checking.account_type, Account.AccountType.ASSET)
        self.assertEqual(checking.account_subtype, Account.AccountSubType.BANK)
        self.assertTrue(checking.is_debit_normal)

        sales = Account.objects.get(company=self.company, account_number="4000")
        self.assertEqual(sales.account_type, Account.AccountType.INCOME)
        self.assertFalse(sales.is_debit_normal)

        cogs = Account.objects.get(company=self.company, account_number="5000")
        self.assertEqual(cogs.account_type, Account.AccountType.EXPENSE)
        self.assertEqual(cogs.account_subtype, Account.AccountSubType.COST_OF_GOODS_SOLD)

    def test_every_account_lands_under_the_target_company(self):
        # NOT `filter(company__isnull=True)` — `company` is a non-null FK, so
        # that assertion can never fail and passes on an empty database. Assert
        # the positive count, which only holds if the import actually ran and
        # scoped correctly.
        path = self.write("coa.csv", COA_CSV)
        import_chart_of_accounts(self.company, path)

        self.assertEqual(Account.objects.count(), 10)
        self.assertEqual(Account.objects.for_company(self.company).count(), 10)
        self.assertEqual(Account.objects.for_company(self.other).count(), 0)
        self.assertEqual(
            set(Account.objects.values_list("company_id", flat=True)),
            {self.company.pk},
        )

    def test_opening_balances_are_parsed(self):
        path = self.write("coa.csv", COA_CSV)
        import_chart_of_accounts(self.company, path)
        checking = Account.objects.get(company=self.company, account_number="1000")
        self.assertEqual(checking.opening_balance, Decimal("15000.00"))

    def test_qbo_vocabulary_is_preserved_verbatim(self):
        path = self.write("coa.csv", COA_CSV)
        import_chart_of_accounts(self.company, path)
        ar = Account.objects.get(company=self.company, account_number="1200")
        self.assertEqual(ar.qbo_account_type, "Accounts receivable (A/R)")
        self.assertEqual(ar.qbo_detail_type, "Accounts Receivable")

    def test_rerunning_updates_rather_than_duplicating(self):
        path = self.write("coa.csv", COA_CSV)
        import_chart_of_accounts(self.company, path)
        _, second = import_chart_of_accounts(self.company, path)

        self.assertEqual(second.accounts_created, 0)
        self.assertEqual(second.accounts_updated, 10)
        self.assertEqual(Account.objects.for_company(self.company).count(), 10)

    def test_two_companies_import_the_same_chart_independently(self):
        path = self.write("coa.csv", COA_CSV)
        import_chart_of_accounts(self.company, path)
        import_chart_of_accounts(self.other, path)
        self.assertEqual(Account.objects.for_company(self.company).count(), 10)
        self.assertEqual(Account.objects.for_company(self.other).count(), 10)
        self.assertEqual(Account.objects.count(), 20)

    def test_sub_accounts_are_linked_to_their_parent(self):
        path = self.write(
            "nested.csv",
            "Account Name,Type,Detail Type,Full Name\n"
            "Expenses,Expenses,Office/General Administrative Expenses,Expenses\n"
            "Utilities,Expenses,Utilities,Expenses:Utilities\n"
            "Electric,Expenses,Utilities,Expenses:Utilities:Electric\n",
        )
        import_chart_of_accounts(self.company, path)
        electric = Account.objects.get(company=self.company, name="Electric")
        self.assertEqual(electric.full_name, "Expenses:Utilities:Electric")
        self.assertEqual(electric.parent.name, "Utilities")
        self.assertEqual(electric.parent.parent.name, "Expenses")

    def test_children_resolve_even_when_listed_before_their_parent(self):
        path = self.write(
            "unordered.csv",
            "Account Name,Type,Detail Type,Full Name\n"
            "Electric,Expenses,Utilities,Expenses:Utilities:Electric\n"
            "Expenses,Expenses,Utilities,Expenses\n"
            "Utilities,Expenses,Utilities,Expenses:Utilities\n",
        )
        import_chart_of_accounts(self.company, path)
        electric = Account.objects.get(company=self.company, name="Electric")
        self.assertIsNotNone(electric.parent)
        self.assertEqual(electric.parent.full_name, "Expenses:Utilities")

    def test_unknown_account_type_is_reported_not_guessed(self):
        path = self.write(
            "bad.csv",
            "Account Name,Type,Detail Type\n"
            "Checking,Bank,Checking\n"
            "Mystery,Cryptocurrency Vault,Whatever\n",
        )
        _, report = import_chart_of_accounts(self.company, path)
        self.assertEqual(report.accounts_created, 1)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("Cryptocurrency Vault", report.errors[0])
        self.assertFalse(Account.objects.filter(name="Mystery").exists())

    def test_unmapped_detail_type_is_recorded_but_not_fatal(self):
        path = self.write(
            "newdetail.csv",
            "Account Name,Type,Detail Type\nChecking,Bank,Some Brand New Detail Type\n",
        )
        _, report = import_chart_of_accounts(self.company, path)
        self.assertEqual(report.accounts_created, 1)
        self.assertEqual(report.errors, [])
        self.assertIn("Some Brand New Detail Type", report.unmapped_detail_types)
        account = Account.objects.get(company=self.company, name="Checking")
        # Fell back to the coarse bucket, and kept the raw string.
        self.assertEqual(account.account_subtype, Account.AccountSubType.BANK)
        self.assertEqual(account.qbo_detail_type, "Some Brand New Detail Type")


class TransactionImportTest(_ImportTestBase):
    def setUp(self):
        super().setUp()
        self.accounts, self.report = import_chart_of_accounts(
            self.company, self.write("coa.csv", COA_CSV)
        )

    def test_groups_rows_into_balanced_entries(self):
        path = self.write("journal.csv", JOURNAL_CSV)
        report = import_transactions(self.company, path, accounts=self.accounts)

        self.assertEqual(report.errors, [])
        self.assertEqual(report.skipped_unbalanced, [])
        self.assertEqual(report.entries_created, 2)
        self.assertEqual(report.lines_created, 5)

        invoice = JournalEntry.objects.get(company=self.company, source_reference="1001")
        self.assertEqual(invoice.entry_date, datetime.date(2025, 1, 15))
        self.assertEqual(invoice.qbo_txn_type, "Invoice")
        self.assertEqual(invoice.entry_source, JournalEntry.EntrySource.QBO_IMPORT)
        self.assertEqual(invoice.lines.count(), 3)
        invoice.validate_balanced()

        debit, credit = invoice.totals()
        self.assertEqual(debit, Decimal("108.25"))
        self.assertEqual(credit, Decimal("108.25"))

    def test_entity_name_is_captured_on_lines(self):
        path = self.write("journal.csv", JOURNAL_CSV)
        import_transactions(self.company, path, accounts=self.accounts)
        line = JournalLine.objects.filter(entity_name="Acme Corp").first()
        self.assertIsNotNone(line)

    def test_every_entry_lands_under_the_target_company(self):
        # Same reasoning as the account version: assert the positive count, not
        # a null check on a non-null column.
        path = self.write("journal.csv", JOURNAL_CSV)
        import_transactions(self.company, path, accounts=self.accounts)

        self.assertEqual(JournalEntry.objects.count(), 2)
        self.assertEqual(JournalEntry.objects.for_company(self.company).count(), 2)
        self.assertEqual(JournalEntry.objects.for_company(self.other).count(), 0)
        self.assertEqual(
            set(JournalEntry.objects.values_list("company_id", flat=True)),
            {self.company.pk},
        )
        # And no line reaches across into the other company's chart.
        self.assertEqual(
            set(JournalLine.objects.values_list("account__company_id", flat=True)),
            {self.company.pk},
        )

    def test_rerunning_does_not_duplicate_the_ledger(self):
        path = self.write("journal.csv", JOURNAL_CSV)
        import_transactions(self.company, path, accounts=self.accounts)
        second = import_transactions(self.company, path, accounts=self.accounts)

        self.assertEqual(second.entries_created, 0)
        self.assertEqual(second.entries_updated, 2)
        self.assertEqual(JournalEntry.objects.for_company(self.company).count(), 2)
        self.assertEqual(JournalLine.objects.count(), 5)

    def test_unbalanced_group_is_skipped_not_written(self):
        path = self.write(
            "unbalanced.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            "01/15/2025,Invoice,2001,Accounts Receivable,100.00,\n"
            "01/15/2025,Invoice,2001,Sales,,60.00\n"
            "01/16/2025,Invoice,2002,Accounts Receivable,50.00,\n"
            "01/16/2025,Invoice,2002,Sales,,50.00\n",
        )
        report = import_transactions(self.company, path, accounts=self.accounts)

        self.assertEqual(len(report.skipped_unbalanced), 1)
        self.assertIn("2001", report.skipped_unbalanced[0])
        # The good entry still landed; one bad group does not abort the import.
        self.assertEqual(report.entries_created, 1)
        self.assertFalse(
            JournalEntry.objects.filter(
                company=self.company, source_reference="2001"
            ).exists()
        )
        self.assertTrue(
            JournalEntry.objects.filter(
                company=self.company, source_reference="2002"
            ).exists()
        )

    def test_negative_debit_becomes_a_credit(self):
        path = self.write(
            "reversal.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            "01/15/2025,Credit Memo,3001,Accounts Receivable,(100.00),\n"
            "01/15/2025,Credit Memo,3001,Sales,,(100.00)\n",
        )
        report = import_transactions(self.company, path, accounts=self.accounts)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.entries_created, 1)

        entry = JournalEntry.objects.get(company=self.company, source_reference="3001")
        entry.validate_balanced()
        ar_line = entry.lines.get(account__account_number="1200")
        sales_line = entry.lines.get(account__account_number="4000")
        # The negative debit flipped to the credit side and vice versa.
        self.assertEqual(ar_line.credit_amount, Decimal("100.00"))
        self.assertEqual(ar_line.debit_amount, Decimal("0.00"))
        self.assertEqual(sales_line.debit_amount, Decimal("100.00"))

    def test_zero_amount_rows_are_skipped(self):
        path = self.write(
            "zeros.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            "01/15/2025,Invoice,4001,Sales,0.00,0.00\n"
            "01/15/2025,Invoice,4001,Accounts Receivable,100.00,\n"
            "01/15/2025,Invoice,4001,Sales,,100.00\n",
        )
        report = import_transactions(self.company, path, accounts=self.accounts)
        self.assertEqual(report.skipped_zero, 1)
        entry = JournalEntry.objects.get(company=self.company, source_reference="4001")
        self.assertEqual(entry.lines.count(), 2)

    def test_unknown_account_gets_a_deterministic_placeholder(self):
        path = self.write(
            "ghost.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            "01/15/2025,Expense,5001,Ghost Account,50.00,\n"
            "01/15/2025,Expense,5001,Checking,,50.00\n",
        )
        report = import_transactions(self.company, path, accounts=self.accounts)
        self.assertEqual(report.placeholders_created, ["Ghost Account"])

        ghost = Account.objects.get(company=self.company, name="Ghost Account")
        self.assertFalse(ghost.is_active)
        self.assertTrue(ghost.account_number.startswith("9999-"))
        first_number = ghost.account_number

        # A second run must reuse it rather than collide on the unique
        # constraint — this is why the suffix is a sha256, not hash().
        fresh = ChartOfAccountsImporter(self.company)
        fresh.load_existing()
        import_transactions(self.company, path, accounts=fresh)
        ghost.refresh_from_db()
        self.assertEqual(ghost.account_number, first_number)
        self.assertEqual(
            Account.objects.filter(company=self.company, name="Ghost Account").count(), 1
        )

    def test_blank_doc_number_with_an_entity_still_imports(self):
        # Deposits, bank fees, transfers and journal entries routinely ship
        # with no Num. Keying those per-row makes every leg a one-leg group,
        # unbalanced by construction and silently dropped — an export with a
        # sparse Num column would import as nothing at all.
        path = self.write(
            "deposit.csv",
            "Date,Transaction Type,Num,Name,Account,Debit,Credit\n"
            "01/15/2025,Deposit,,Chase Bank,Checking,500.00,\n"
            "01/15/2025,Deposit,,Chase Bank,Sales,,500.00\n",
        )
        report = import_transactions(self.company, path, accounts=self.accounts)

        self.assertEqual(report.errors, [])
        self.assertEqual(report.skipped_unbalanced, [])
        self.assertEqual(report.entries_created, 1)
        entry = JournalEntry.objects.get(company=self.company, qbo_txn_type="Deposit")
        self.assertEqual(entry.lines.count(), 2)
        entry.validate_balanced()

    def test_blank_doc_number_and_no_entity_is_not_fused(self):
        # With neither a reference nor an entity, nothing distinguishes two
        # unrelated same-day transactions. Fusing them would invent a
        # transaction that never happened, so they stay separate — and are
        # reported, not silently discarded.
        path = self.write(
            "noref.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            "01/15/2025,Expense,,Bank Charges,25.00,\n"
            "01/15/2025,Expense,,Checking,,99.00\n",
        )
        report = import_transactions(self.company, path, accounts=self.accounts)
        self.assertEqual(report.entries_created, 0)
        self.assertEqual(len(report.skipped_unbalanced), 2)

    def test_fiscal_period_is_resolved_when_one_exists(self):
        period = FiscalPeriod.objects.create(
            company=self.company, name="Jan 2025",
            start_date=datetime.date(2025, 1, 1),
            end_date=datetime.date(2025, 1, 31),
        )
        path = self.write("journal.csv", JOURNAL_CSV)
        import_transactions(self.company, path, accounts=self.accounts)
        entry = JournalEntry.objects.get(company=self.company, source_reference="1001")
        self.assertEqual(entry.fiscal_period, period)

    def test_missing_fiscal_period_is_left_null_not_invented(self):
        path = self.write("journal.csv", JOURNAL_CSV)
        import_transactions(self.company, path, accounts=self.accounts)
        entry = JournalEntry.objects.get(company=self.company, source_reference="1001")
        self.assertIsNone(entry.fiscal_period)
        self.assertEqual(FiscalPeriod.objects.count(), 0)

    def test_another_companys_period_is_never_used(self):
        FiscalPeriod.objects.create(
            company=self.other, name="Jan 2025",
            start_date=datetime.date(2025, 1, 1),
            end_date=datetime.date(2025, 1, 31),
        )
        path = self.write("journal.csv", JOURNAL_CSV)
        import_transactions(self.company, path, accounts=self.accounts)
        entry = JournalEntry.objects.get(company=self.company, source_reference="1001")
        self.assertIsNone(entry.fiscal_period)

    def test_dayfirst_changes_the_posting_date(self):
        path = self.write(
            "dates.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            "03/04/2025,Invoice,6001,Accounts Receivable,10.00,\n"
            "03/04/2025,Invoice,6001,Sales,,10.00\n",
        )
        import_transactions(self.company, path, accounts=self.accounts, dayfirst=True)
        entry = JournalEntry.objects.get(company=self.company, source_reference="6001")
        self.assertEqual(entry.entry_date, datetime.date(2025, 4, 3))


class AccountLookupTest(_ImportTestBase):
    def test_lookup_never_crosses_companies(self):
        Account.objects.create(
            company=self.other, name="Checking", full_name="Checking",
            account_type=Account.AccountType.ASSET,
        )
        importer = ChartOfAccountsImporter(self.company)
        importer.load_existing()
        self.assertIsNone(importer.lookup("Checking"))

    def test_lookup_matches_full_path_then_bare_name(self):
        Account.objects.create(
            company=self.company, name="Electric",
            full_name="Expenses:Utilities:Electric",
            account_type=Account.AccountType.EXPENSE,
        )
        importer = ChartOfAccountsImporter(self.company)
        importer.load_existing()
        self.assertIsNotNone(importer.lookup("Expenses:Utilities:Electric"))
        self.assertIsNotNone(importer.lookup("Electric"))
        self.assertIsNotNone(importer.lookup("EXPENSES:UTILITIES:ELECTRIC"))


class FingerprintTest(TestCase):
    def test_is_stable_across_leg_ordering(self):
        legs_a = [("acct-1", Decimal("100.00"), Decimal("0"), "Acme"),
                  ("acct-2", Decimal("0"), Decimal("100.00"), "Acme")]
        legs_b = list(reversed(legs_a))
        date = datetime.date(2025, 1, 15)
        self.assertEqual(
            entry_fingerprint("co", date, "1001", "Invoice", legs_a),
            entry_fingerprint("co", date, "1001", "Invoice", legs_b),
        )

    def test_differs_when_an_amount_differs(self):
        date = datetime.date(2025, 1, 15)
        base = [("acct-1", Decimal("100.00"), Decimal("0"), "")]
        other = [("acct-1", Decimal("100.01"), Decimal("0"), "")]
        self.assertNotEqual(
            entry_fingerprint("co", date, "1001", "Invoice", base),
            entry_fingerprint("co", date, "1001", "Invoice", other),
        )

    def test_differs_across_companies(self):
        date = datetime.date(2025, 1, 15)
        legs = [("acct-1", Decimal("100.00"), Decimal("0"), "")]
        self.assertNotEqual(
            entry_fingerprint("co-a", date, "1001", "Invoice", legs),
            entry_fingerprint("co-b", date, "1001", "Invoice", legs),
        )

    def test_is_stable_across_processes_with_different_hash_seeds(self):
        """The property that makes re-imports idempotent at all.

        Python's builtin ``hash()`` is salted per process by PYTHONHASHSEED, so
        a fingerprint built on it would differ on every run and every import
        would duplicate the ledger instead of matching. Compute the same
        fingerprint in two subprocesses with opposing seeds and require
        agreement — testing the guarantee rather than pinning a magic digest.
        """
        import os
        import subprocess
        import sys

        script = (
            "import datetime, os, sys\n"
            "from decimal import Decimal\n"
            "os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.test')\n"
            "os.environ.setdefault('DJANGO_SECRET_KEY', 'django-insecure-test-only')\n"
            "import django; django.setup()\n"
            "from accounting.qbo.importer import entry_fingerprint\n"
            "legs = [('acct-1', Decimal('100.00'), Decimal('0'), 'Acme'),\n"
            "        ('acct-2', Decimal('0'), Decimal('100.00'), 'Acme')]\n"
            "print(entry_fingerprint('co', datetime.date(2025, 1, 15), "
            "'1001', 'Invoice', legs))\n"
        )

        digests = []
        for seed in ("0", "12345"):
            env = {**os.environ, "PYTHONHASHSEED": seed}
            result = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True, text=True, env=env, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            digests.append(result.stdout.strip())

        self.assertEqual(digests[0], digests[1])
        self.assertEqual(
            digests[0],
            entry_fingerprint(
                "co", datetime.date(2025, 1, 15), "1001", "Invoice",
                [("acct-1", Decimal("100.00"), Decimal("0"), "Acme"),
                 ("acct-2", Decimal("0"), Decimal("100.00"), "Acme")],
            ),
        )


class MissingAccountColumnTest(_ImportTestBase):
    """An export with no resolvable account column must fail loudly.

    The silent-failure shape this guards: a journal whose only name-ish column
    is "Name" (the customer). Reading that as the account would mint a
    placeholder account per customer and file real money against it.
    """

    def setUp(self):
        super().setUp()
        self.accounts, _ = import_chart_of_accounts(
            self.company, self.write("coa.csv", COA_CSV)
        )

    def test_customer_name_is_never_used_as_an_account(self):
        path = self.write(
            "nameonly.csv",
            "Date,Transaction Type,Num,Name,Debit,Credit\n"
            "01/15/2025,Invoice,1001,Acme Solar,100.00,\n"
            "01/15/2025,Invoice,1001,Acme Solar,,100.00\n",
        )
        report = import_transactions(self.company, path, accounts=self.accounts)

        self.assertEqual(report.entries_created, 0)
        self.assertEqual(report.placeholders_created, [])
        self.assertEqual(len(report.errors), 2)
        self.assertIn("no account column resolved", report.errors[0])
        self.assertFalse(
            Account.objects.filter(company=self.company, name="Acme Solar").exists()
        )

    def test_placeholder_factory_refuses_an_empty_name(self):
        with self.assertRaises(ValueError):
            self.accounts.get_or_create_placeholder("")
        with self.assertRaises(ValueError):
            self.accounts.get_or_create_placeholder("   ")


class CorrectedReimportTest(_ImportTestBase):
    """Re-importing a *corrected* export must update, not double-count.

    Keying idempotency on a fingerprint that covers the amounts means any
    correction changes the key, so `update_or_create` inserts a second entry
    and leaves the stale one behind — the revenue is then counted twice.
    """

    def setUp(self):
        super().setUp()
        self.accounts, _ = import_chart_of_accounts(
            self.company, self.write("coa.csv", COA_CSV)
        )

    def _journal(self, name, amount):
        return self.write(
            name,
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            f"01/15/2025,Invoice,1001,Accounts Receivable,{amount},\n"
            f"01/15/2025,Invoice,1001,Sales,,{amount}\n",
        )

    def test_a_corrected_amount_updates_the_same_entry(self):
        import_transactions(self.company, self._journal("v1.csv", "100.00"),
                            accounts=self.accounts)
        report = import_transactions(self.company, self._journal("v2.csv", "120.00"),
                                     accounts=self.accounts)

        entries = JournalEntry.objects.filter(
            company=self.company, source_reference="1001"
        )
        self.assertEqual(entries.count(), 1)
        self.assertEqual(report.entries_created, 0)
        self.assertEqual(report.entries_updated, 1)

        entry = entries.get()
        debit, credit = entry.totals()
        self.assertEqual(debit, Decimal("120.00"))
        self.assertEqual(credit, Decimal("120.00"))
        entry.validate_balanced()

    def test_a_correction_leaves_no_stale_lines_behind(self):
        import_transactions(self.company, self._journal("v1.csv", "100.00"),
                            accounts=self.accounts)
        import_transactions(self.company, self._journal("v2.csv", "120.00"),
                            accounts=self.accounts)
        self.assertEqual(JournalLine.objects.count(), 2)
        self.assertEqual(
            set(JournalLine.objects.values_list("debit_amount", flat=True)),
            {Decimal("120.00"), Decimal("0.00")},
        )

    def test_different_doc_numbers_stay_separate(self):
        # The guard against over-merging: same date and type, different Num.
        path = self.write(
            "two.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            "01/15/2025,Invoice,1001,Accounts Receivable,100.00,\n"
            "01/15/2025,Invoice,1001,Sales,,100.00\n"
            "01/15/2025,Invoice,1002,Accounts Receivable,200.00,\n"
            "01/15/2025,Invoice,1002,Sales,,200.00\n",
        )
        import_transactions(self.company, path, accounts=self.accounts)
        self.assertEqual(JournalEntry.objects.for_company(self.company).count(), 2)


class FiscalPeriodResolutionTest(_ImportTestBase):
    """Periods must be matched by date, not cached per calendar month.

    A (year, month) cache assigns every entry in a month to whichever period
    the first lookup hit — wrong for semi-monthly and 4-4-5 fiscal calendars,
    and wrong with no error at all.
    """

    def setUp(self):
        super().setUp()
        self.accounts, _ = import_chart_of_accounts(
            self.company, self.write("coa.csv", COA_CSV)
        )
        self.first = FiscalPeriod.objects.create(
            company=self.company, name="Jan 1-15",
            start_date=datetime.date(2025, 1, 1), end_date=datetime.date(2025, 1, 15),
        )
        self.second = FiscalPeriod.objects.create(
            company=self.company, name="Jan 16-31",
            start_date=datetime.date(2025, 1, 16), end_date=datetime.date(2025, 1, 31),
        )

    def test_two_periods_in_one_month_are_told_apart(self):
        path = self.write(
            "split.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            "01/10/2025,Invoice,2001,Accounts Receivable,10.00,\n"
            "01/10/2025,Invoice,2001,Sales,,10.00\n"
            "01/20/2025,Invoice,2002,Accounts Receivable,20.00,\n"
            "01/20/2025,Invoice,2002,Sales,,20.00\n",
        )
        import_transactions(self.company, path, accounts=self.accounts)

        early = JournalEntry.objects.get(company=self.company, source_reference="2001")
        late = JournalEntry.objects.get(company=self.company, source_reference="2002")
        self.assertEqual(early.fiscal_period, self.first)
        self.assertEqual(late.fiscal_period, self.second)


class ChartOfAccountsResilienceTest(_ImportTestBase):
    def test_a_duplicate_account_number_is_reported_not_fatal(self):
        # An uncaught IntegrityError here aborts the caller's whole
        # transaction with a traceback instead of a report line.
        path = self.write(
            "dupnum.csv",
            "Account Number,Account Name,Type,Detail Type,Full Name\n"
            "1000,First,Bank,Checking,First\n"
            "1000,Second,Bank,Savings,Second\n"
            "2000,Third,Bank,Checking,Third\n",
        )
        _, report = import_chart_of_accounts(self.company, path)

        self.assertEqual(len(report.errors), 1)
        # The report must name both the account and the colliding number, or
        # the operator cannot find the offending row in a 400-line export.
        self.assertIn("Second", report.errors[0])
        self.assertIn("1000", report.errors[0])
        # The other rows still landed.
        self.assertTrue(
            Account.objects.filter(company=self.company, name="First").exists()
        )
        self.assertTrue(
            Account.objects.filter(company=self.company, name="Third").exists()
        )

    def test_two_accounts_flattening_to_one_path_are_reported(self):
        # QBO's default export has no Full Name column, so "Insurance" under
        # COGS and under Expenses both flatten to "Insurance". Overwriting one
        # with the other loses an account silently.
        path = self.write(
            "collide.csv",
            "Account Name,Type,Detail Type\n"
            "Insurance,Cost of Goods Sold,Supplies & Materials - COGS\n"
            "Insurance,Expenses,Insurance\n",
        )
        _, report = import_chart_of_accounts(self.company, path)

        self.assertEqual(
            Account.objects.filter(company=self.company, name="Insurance").count(), 1
        )
        self.assertEqual(len(report.errors), 1)
        self.assertIn("duplicate account path", report.errors[0])

    def test_a_parent_of_a_different_type_is_not_bound_by_leaf_name(self):
        # "Sales" exists as income; a leaf-name fallback would attach an
        # expense sub-account to it, violating the parent/type invariant.
        path = self.write(
            "leaf.csv",
            "Account Name,Type,Detail Type,Full Name\n"
            "Sales,Income,Sales of Product Income,Sales\n"
            "Widget,Expenses,Utilities,Costs:Sales:Widget\n",
        )
        _, report = import_chart_of_accounts(self.company, path)

        widget = Account.objects.filter(company=self.company, name="Widget").first()
        if widget is not None:
            self.assertIsNone(
                widget.parent,
                "expense account must not inherit the income 'Sales' as parent",
            )
        sales = Account.objects.get(company=self.company, name="Sales")
        self.assertEqual(sales.account_type, Account.AccountType.INCOME)


class SharedDocumentNumberTest(_ImportTestBase):
    """Document numbers get reused, and fusing the transactions is invisible.

    Two vendors paid with the same check number on the same day would group
    into one entry — which still *balances*, so no validation catches it, and
    the ledger silently shows one transaction where there were two.
    """

    def setUp(self):
        super().setUp()
        self.accounts, _ = import_chart_of_accounts(
            self.company, self.write("coa.csv", COA_CSV)
        )

    def test_two_entities_sharing_a_doc_number_stay_separate(self):
        path = self.write(
            "shared.csv",
            "Date,Transaction Type,Num,Name,Account,Debit,Credit\n"
            "01/15/2025,Expense,1001,Vendor A,Bank Charges,50.00,\n"
            "01/15/2025,Expense,1001,Vendor A,Checking,,50.00\n"
            "01/15/2025,Expense,1001,Vendor B,Bank Charges,75.00,\n"
            "01/15/2025,Expense,1001,Vendor B,Checking,,75.00\n",
        )
        report = import_transactions(self.company, path, accounts=self.accounts)

        entries = JournalEntry.objects.filter(
            company=self.company, source_reference="1001"
        )
        self.assertEqual(report.entries_created, 2)
        self.assertEqual(entries.count(), 2)
        self.assertEqual(
            {e.entity_name for e in entries}, {"Vendor A", "Vendor B"}
        )
        self.assertEqual(
            sorted(e.totals()[0] for e in entries),
            [Decimal("50.00"), Decimal("75.00")],
        )
        for entry in entries:
            entry.validate_balanced()

    def test_one_invoice_with_a_partly_blank_name_column_is_not_split(self):
        # The guard against over-splitting: an ordinary invoice carries the
        # customer on some legs and not on the tax leg. Splitting on a partial
        # match would shred every real invoice.
        path = self.write(
            "invoice.csv",
            "Date,Transaction Type,Num,Name,Account,Debit,Credit\n"
            "01/15/2025,Invoice,2001,Acme Corp,Accounts Receivable,108.25,\n"
            "01/15/2025,Invoice,2001,Acme Corp,Sales,,100.00\n"
            "01/15/2025,Invoice,2001,,Sales Tax Payable,,8.25\n",
        )
        report = import_transactions(self.company, path, accounts=self.accounts)

        self.assertEqual(report.entries_created, 1)
        entry = JournalEntry.objects.get(company=self.company, source_reference="2001")
        self.assertEqual(entry.lines.count(), 3)
        self.assertEqual(entry.entity_name, "Acme Corp")
        entry.validate_balanced()

    def test_a_shared_doc_number_re_imports_idempotently(self):
        path = self.write(
            "shared.csv",
            "Date,Transaction Type,Num,Name,Account,Debit,Credit\n"
            "01/15/2025,Expense,1001,Vendor A,Bank Charges,50.00,\n"
            "01/15/2025,Expense,1001,Vendor A,Checking,,50.00\n"
            "01/15/2025,Expense,1001,Vendor B,Bank Charges,75.00,\n"
            "01/15/2025,Expense,1001,Vendor B,Checking,,75.00\n",
        )
        import_transactions(self.company, path, accounts=self.accounts)
        report = import_transactions(self.company, path, accounts=self.accounts)

        self.assertEqual(report.entries_created, 0)
        self.assertEqual(report.entries_updated, 2)
        self.assertEqual(JournalEntry.objects.for_company(self.company).count(), 2)


class FingerprintStabilityTest(_ImportTestBase):
    """The fingerprint must survive the chart of accounts being reloaded.

    It keys the entries that have no document number. Building it from account
    primary keys means deleting and re-importing the chart changes every UUID,
    so those entries stop matching and duplicate on the next run.
    """

    def _chart(self):
        importer, _ = import_chart_of_accounts(
            self.company, self.write("coa.csv", COA_CSV)
        )
        return importer

    def test_hash_survives_the_chart_being_deleted_and_reloaded(self):
        deposit = self.write(
            "deposit.csv",
            "Date,Transaction Type,Num,Name,Account,Debit,Credit\n"
            "01/16/2025,Deposit,,Chase Bank,Checking,500.00,\n"
            "01/16/2025,Deposit,,Chase Bank,Sales,,500.00\n",
        )
        import_transactions(self.company, deposit, accounts=self._chart())
        first = JournalEntry.objects.get(
            company=self.company, qbo_txn_type="Deposit"
        ).import_hash

        JournalLine.objects.all().delete()
        JournalEntry.objects.all().delete()
        Account.objects.filter(company=self.company).delete()

        import_transactions(self.company, deposit, accounts=self._chart())
        second = JournalEntry.objects.get(
            company=self.company, qbo_txn_type="Deposit"
        ).import_hash

        self.assertEqual(first, second)


class TransactionGroupingEdgeCaseTest(_ImportTestBase):
    """Grouping fails silently in two opposite directions, so both are pinned.

    Under-splitting fuses distinct transactions into one entry that still
    balances, so nothing downstream can tell. Over-splitting shreds a genuine
    entry into unbalanced fragments and then blames the source file for an
    imbalance that does not exist.
    """

    def setUp(self):
        super().setUp()
        self.accounts, _ = import_chart_of_accounts(
            self.company, self.write("coa.csv", COA_CSV)
        )

    def test_a_multi_party_journal_entry_is_not_shredded(self):
        # One entry naming a different party per line — an inter-customer
        # reclass or payroll JE. Cutting on entity leaves one-leg fragments,
        # unbalanced by construction, and the whole entry disappears.
        path = self.write(
            "je.csv",
            "Date,Transaction Type,Num,Name,Account,Debit,Credit\n"
            "01/10/2025,Journal Entry,JE-1,Customer A,Accounts Receivable,500.00,\n"
            "01/10/2025,Journal Entry,JE-1,Customer B,Sales,,500.00\n",
        )
        report = import_transactions(self.company, path, accounts=self.accounts)

        self.assertEqual(report.skipped_unbalanced, [])
        self.assertEqual(report.entries_created, 1)
        entry = JournalEntry.objects.get(company=self.company, source_reference="JE-1")
        self.assertEqual(entry.lines.count(), 2)
        entry.validate_balanced()

    def test_two_vendors_sharing_a_check_number_are_still_split(self):
        # The counter-case: each party's legs balance on their own, so these
        # really are two transactions.
        path = self.write(
            "chk.csv",
            "Date,Transaction Type,Num,Name,Account,Debit,Credit\n"
            "01/11/2025,Expense,1001,Vendor A,Bank Charges,50.00,\n"
            "01/11/2025,Expense,1001,Vendor A,Checking,,50.00\n"
            "01/11/2025,Expense,1001,Vendor B,Bank Charges,75.00,\n"
            "01/11/2025,Expense,1001,Vendor B,Checking,,75.00\n",
        )
        report = import_transactions(self.company, path, accounts=self.accounts)
        self.assertEqual(report.entries_created, 2)
        self.assertEqual(
            sorted(
                e.totals()[0]
                for e in JournalEntry.objects.filter(company=self.company)
            ),
            [Decimal("50.00"), Decimal("75.00")],
        )

    def test_two_blank_num_transactions_are_cut_into_balanced_runs(self):
        # Same day, same payee, no reference. Grouping them together yields one
        # four-line entry that balances — so nothing raises, and per-transaction
        # bank reconciliation quietly stops working.
        path = self.write(
            "fees.csv",
            "Date,Transaction Type,Num,Name,Account,Debit,Credit\n"
            "01/07/2025,Expense,,Bank Co,Bank Charges,25.00,\n"
            "01/07/2025,Expense,,Bank Co,Checking,,25.00\n"
            "01/07/2025,Expense,,Bank Co,Bank Charges,40.00,\n"
            "01/07/2025,Expense,,Bank Co,Checking,,40.00\n",
        )
        report = import_transactions(self.company, path, accounts=self.accounts)

        self.assertEqual(report.entries_created, 2)
        entries = JournalEntry.objects.filter(company=self.company)
        self.assertEqual(sorted(e.lines.count() for e in entries), [2, 2])
        self.assertEqual(
            sorted(e.totals()[0] for e in entries),
            [Decimal("25.00"), Decimal("40.00")],
        )

    def test_a_genuine_multi_leg_blank_num_deposit_stays_whole(self):
        # The counter-case for run-splitting: no prefix of this balances until
        # the last leg, so it is one transaction.
        path = self.write(
            "dep3.csv",
            "Date,Transaction Type,Num,Name,Account,Debit,Credit\n"
            "01/08/2025,Deposit,,Chase,Checking,300.00,\n"
            "01/08/2025,Deposit,,Chase,Sales,,200.00\n"
            "01/08/2025,Deposit,,Chase,Accounts Receivable,,100.00\n",
        )
        report = import_transactions(self.company, path, accounts=self.accounts)
        self.assertEqual(report.entries_created, 1)
        entry = JournalEntry.objects.get(company=self.company, qbo_txn_type="Deposit")
        self.assertEqual(entry.lines.count(), 3)
        entry.validate_balanced()


class AmbiguousAccountReferenceTest(_ImportTestBase):
    """"Insurance" under both COGS and Expenses is an ordinary QBO chart.

    Resolving a bare reference to whichever was indexed first moves money
    between cost of sales and operating expense — changing reported gross
    margin — with no error and nothing in the report.
    """

    def _chart(self):
        importer, _ = import_chart_of_accounts(
            self.company,
            self.write(
                "dupleaf.csv",
                "Account Number,Account Name,Type,Detail Type,Full Name\n"
                "5010,Insurance,Expenses,Insurance,Expenses:Insurance\n"
                "5011,Insurance,Cost of Goods Sold,Supplies & Materials - COGS,"
                "Cost of Goods Sold:Insurance\n"
                "1000,Checking,Bank,Checking,Checking\n",
            ),
        )
        return importer

    def test_an_ambiguous_bare_reference_is_refused(self):
        accounts = self._chart()
        path = self.write(
            "amb.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            "01/05/2025,Expense,7001,Insurance,100.00,\n"
            "01/05/2025,Expense,7001,Checking,,100.00\n",
        )
        report = import_transactions(self.company, path, accounts=accounts)

        self.assertEqual(report.entries_created, 0)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("matches more than one account", report.errors[0])
        self.assertEqual(JournalLine.objects.count(), 0)

    def test_the_full_path_still_resolves(self):
        accounts = self._chart()
        path = self.write(
            "exact.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            "01/06/2025,Expense,7002,Expenses:Insurance,100.00,\n"
            "01/06/2025,Expense,7002,Checking,,100.00\n",
        )
        report = import_transactions(self.company, path, accounts=accounts)

        self.assertEqual(report.errors, [])
        self.assertEqual(report.entries_created, 1)
        line = JournalLine.objects.get(debit_amount=Decimal("100.00"))
        self.assertEqual(line.account.account_number, "5010")


class HierarchyPreservationTest(_ImportTestBase):
    """QBO's default account-list export carries no Full Name or Parent column.

    Writing those flat rows over an existing chart nulls every parent and
    shortens every path — silently dismantling a tree a richer export built,
    and reporting it as a clean "3 updated".
    """

    NESTED = (
        "Account Number,Account Name,Type,Detail Type,Full Name,ID\n"
        "5000,Expenses,Expenses,Insurance,Expenses,A1\n"
        "5010,Insurance,Expenses,Insurance,Expenses:Insurance,A2\n"
        "5011,Auto,Expenses,Insurance,Expenses:Insurance:Auto,A3\n"
    )
    FLAT = (
        "Account Number,Account Name,Type,Detail Type,ID\n"
        "5000,Expenses,Expenses,Insurance,A1\n"
        "5010,Insurance,Expenses,Insurance,A2\n"
        "5011,Auto,Expenses,Insurance,A3\n"
    )

    def test_a_flat_reimport_does_not_flatten_an_existing_tree(self):
        import_chart_of_accounts(self.company, self.write("nested.csv", self.NESTED))
        before = {
            a.name: (a.full_name, a.parent_id)
            for a in Account.objects.for_company(self.company)
        }

        _, report = import_chart_of_accounts(
            self.company, self.write("flat.csv", self.FLAT)
        )
        after = {
            a.name: (a.full_name, a.parent_id)
            for a in Account.objects.for_company(self.company)
        }

        self.assertEqual(report.errors, [])
        self.assertEqual(before, after)
        self.assertEqual(
            Account.objects.get(company=self.company, name="Auto").full_name,
            "Expenses:Insurance:Auto",
        )

    def test_a_flat_import_into_an_empty_chart_still_works(self):
        _, report = import_chart_of_accounts(
            self.company, self.write("flat.csv", self.FLAT)
        )
        self.assertEqual(report.accounts_created, 3)
        self.assertEqual(
            Account.objects.get(company=self.company, name="Auto").full_name, "Auto"
        )


class ImportDiagnosticsTest(_ImportTestBase):
    def setUp(self):
        super().setUp()
        self.accounts, _ = import_chart_of_accounts(
            self.company, self.write("coa.csv", COA_CSV)
        )

    def test_an_unrecognised_date_column_is_an_error_not_a_silent_no_op(self):
        # Otherwise the import writes nothing, reports nothing and exits 0 —
        # indistinguishable to an automated caller from a real no-op.
        path = self.write(
            "nodate.csv",
            "Posted On,Transaction Type,Num,Account,Debit,Credit\n"
            "01/05/2025,Expense,9001,Bank Charges,25.00,\n"
            "01/05/2025,Expense,9001,Checking,,25.00\n",
        )
        report = import_transactions(self.company, path, accounts=self.accounts)

        self.assertEqual(report.entries_created, 0)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("date column was not recognized", report.errors[0])

    def test_a_bad_amount_is_a_per_row_error_not_a_whole_pass_abort(self):
        # Dates already behave this way; amounts must too, or one malformed
        # cell rolls back every good row in the file.
        path = self.write(
            "badamt.csv",
            "Date,Transaction Type,Num,Account,Debit,Credit\n"
            "01/05/2025,Expense,8001,Bank Charges,25.00,\n"
            "01/05/2025,Expense,8001,Checking,,25.00\n"
            '01/06/2025,Expense,8002,Bank Charges,"1.234,56",\n',
        )
        report = import_transactions(self.company, path, accounts=self.accounts)

        self.assertEqual(report.entries_created, 1)  # the good one survived
        self.assertTrue(report.errors)
        self.assertTrue(
            JournalEntry.objects.filter(
                company=self.company, source_reference="8001"
            ).exists()
        )


class QboIdIdentityTest(_ImportTestBase):
    """`qbo_id` is an account's durable identity across imports.

    A flat re-export with no ID column must not blank it, and an export naming
    a different id for the same account must not quietly take it over — either
    way the next rich import stops matching and builds a duplicate account.
    """

    RICH = (
        "Account Number,Account Name,Type,Detail Type,Full Name,ID\n"
        "5010,Insurance,Expenses,Insurance,Expenses:Insurance,QBO-AAA\n"
    )
    FLAT_NO_ID = (
        "Account Number,Account Name,Type,Detail Type\n"
        "5010,Insurance,Expenses,Insurance\n"
    )
    FLAT_OTHER_ID = (
        "Account Number,Account Name,Type,Detail Type,ID\n"
        "5010,Insurance,Expenses,Insurance,QBO-ZZZ\n"
    )

    def test_a_flat_reimport_does_not_blank_an_existing_qbo_id(self):
        import_chart_of_accounts(self.company, self.write("rich.csv", self.RICH))
        _, report = import_chart_of_accounts(
            self.company, self.write("flat.csv", self.FLAT_NO_ID)
        )

        account = Account.objects.get(company=self.company, account_number="5010")
        self.assertEqual(account.qbo_id, "QBO-AAA")
        self.assertEqual(account.full_name, "Expenses:Insurance")
        self.assertEqual(report.errors, [])

    def test_a_conflicting_qbo_id_is_reported_not_silently_taken(self):
        import_chart_of_accounts(self.company, self.write("rich.csv", self.RICH))
        _, report = import_chart_of_accounts(
            self.company, self.write("other.csv", self.FLAT_OTHER_ID)
        )

        account = Account.objects.get(company=self.company, account_number="5010")
        self.assertEqual(account.qbo_id, "QBO-AAA")  # unchanged
        self.assertTrue(report.errors)

    def test_a_first_import_still_records_the_qbo_id(self):
        _, report = import_chart_of_accounts(
            self.company, self.write("rich.csv", self.RICH)
        )
        self.assertEqual(report.errors, [])
        self.assertEqual(
            Account.objects.get(company=self.company, account_number="5010").qbo_id,
            "QBO-AAA",
        )


class RunSplitReferenceTest(_ImportTestBase):
    """Split runs must not leak the internal grouping key into stored data.

    `source_reference` is read back by humans and matched by external systems;
    a Python repr in that column would corrupt both.
    """

    def setUp(self):
        super().setUp()
        self.accounts, _ = import_chart_of_accounts(
            self.company, self.write("coa.csv", COA_CSV)
        )

    def test_run_split_entries_carry_a_clean_reference(self):
        path = self.write(
            "fees.csv",
            "Date,Transaction Type,Num,Name,Account,Debit,Credit\n"
            "01/07/2025,Expense,,Bank Co,Bank Charges,25.00,\n"
            "01/07/2025,Expense,,Bank Co,Checking,,25.00\n"
            "01/07/2025,Expense,,Bank Co,Bank Charges,40.00,\n"
            "01/07/2025,Expense,,Bank Co,Checking,,40.00\n",
        )
        import_transactions(self.company, path, accounts=self.accounts)

        entries = JournalEntry.objects.filter(company=self.company)
        self.assertEqual(entries.count(), 2)
        for entry in entries:
            self.assertEqual(entry.source_reference, "")
            self.assertNotIn("(", entry.source_reference)
            self.assertNotIn("datetime", entry.source_reference)
            self.assertEqual(entry.entity_name, "Bank Co")


class HandMaintainedJournalTest(_ImportTestBase):
    """A human-readable correcting-entries sheet, imported unmodified.

    Real shape, from a year-end close package: currency-qualified money
    headers, ``--- JE-X# --- ... ---`` section dividers, and a self-cancelling
    ``BALANCE CHECK`` footer under each block. All three used to conspire into
    a silent no-op — the money columns resolved to nothing, so every leg read
    0.00 and was dropped as a zero-amount row, while the only errors raised
    were about dates.
    """

    JOURNAL = (
        "Date,Num,Account,Debit (USD),Credit (USD),Status,Memo\n"
        "--- JE-X1 --- 2025-12-31 --- POST ---,,,,,,\n"
        "2025-12-31,JE-X1,General business expenses,11000.00,,PROPOSED,Reverse phantom deposit\n"
        "2025-12-31,JE-X1,Checking,,11000.00,PROPOSED,Removes the duplicate\n"
        ",JE-X1,BALANCE CHECK,11000.00,11000.00,,BALANCED\n"
        "--- JE-X2 --- 2025-12-31 --- POST ---,,,,,,\n"
        "2025-12-31,JE-X2,Sales,2500.00,,PROPOSED,Financing draw booked to revenue\n"
        "2025-12-31,JE-X2,Accounts Payable,,2500.00,PROPOSED,Creates the liability\n"
        ",JE-X2,BALANCE CHECK,2500.00,2500.00,,BALANCED\n"
    )

    def test_imports_without_editing_the_file(self):
        report = import_transactions(
            self.company, self.write("je.csv", self.JOURNAL)
        )
        self.assertEqual(report.errors, [])
        self.assertEqual(report.entries_created, 2)
        self.assertEqual(report.lines_created, 4)
        self.assertEqual(report.skipped_annotation, 2)

        total = sum(
            line.debit_amount for line in JournalLine.objects.filter(entry__company=self.company)
        )
        self.assertEqual(total, Decimal("13500.00"))

    def test_balance_check_rows_never_reach_the_ledger(self):
        """They carry an equal debit AND credit, so they net to nothing."""
        import_transactions(self.company, self.write("je.csv", self.JOURNAL))
        self.assertFalse(
            Account.objects.filter(
                company=self.company, name__icontains="BALANCE CHECK"
            ).exists()
        )

    def test_a_divider_row_never_hides_an_unmapped_money_column(self):
        """The annotation rule counts raw cells, so it cannot swallow real rows.

        If it counted *resolved* fields instead, a file whose Debit/Credit
        headers failed to map would read as a page of annotations and vanish
        with no error at all.
        """
        journal = (
            "Date,Num,Account,Wibble,Wobble\n"
            "--- JE-X1 --- 2025-12-31 --- POST ---,,,,\n"
            "2025-12-31,JE-X1,General business expenses,11000.00,\n"
            "2025-12-31,JE-X1,Checking,,11000.00\n"
        )
        report = import_transactions(self.company, self.write("bad.csv", journal))
        self.assertEqual(report.skipped_annotation, 1)
        self.assertEqual(report.entries_created, 0)
        # The two real rows are accounted for as zero-amount, not silently gone.
        self.assertEqual(report.skipped_zero, 2)


class StatusFilterTest(_ImportTestBase):
    """``--require-status``: post only what a reviewer approved."""

    JOURNAL = (
        "Date,Num,Account,Debit (USD),Credit (USD),Status,Memo\n"
        "--- JE-X1 --- 2025-12-31 --- POST ---,,,,,,\n"
        "2025-12-31,JE-X1,General business expenses,11000.00,,POST,Approved\n"
        "2025-12-31,JE-X1,Checking,,11000.00,POST,Approved\n"
        "--- JE-X3 --- 2025-12-31 --- OPTIONAL ---,,,,,,\n"
        "2025-12-31,JE-X3,Sales,2088.00,,PROPOSED - NOT POSTED,Ask the preparer\n"
        "2025-12-31,JE-X3,Accounts Payable,,2088.00,PROPOSED - NOT POSTED,Ask the preparer\n"
    )

    def test_only_approved_entries_are_written(self):
        report = import_transactions(
            self.company, self.write("je.csv", self.JOURNAL), require_status=["POST"]
        )
        self.assertEqual(report.errors, [])
        self.assertEqual(report.entries_created, 1)
        self.assertEqual(report.skipped_status, 2)
        self.assertEqual(
            list(
                JournalEntry.objects.filter(company=self.company).values_list(
                    "source_reference", flat=True
                )
            ),
            ["JE-X1"],
        )

    def test_post_does_not_match_not_posted(self):
        """The substring trap: "POST" is inside "PROPOSED - NOT POSTED".

        A substring test would post precisely the rows a reviewer marked as
        not for posting — the most expensive possible way to be wrong here.
        """
        journal = (
            "Date,Num,Account,Debit,Credit,Status\n"
            "2025-12-31,JE-X9,Sales,100.00,,PROPOSED - NOT POSTED\n"
            "2025-12-31,JE-X9,Checking,,100.00,PROPOSED - NOT POSTED\n"
        )
        report = import_transactions(
            self.company, self.write("np.csv", journal), require_status=["POST"]
        )
        self.assertEqual(report.entries_created, 0)
        self.assertEqual(report.skipped_status, 2)

    def test_matching_ignores_case_and_punctuation(self):
        journal = (
            "Date,Num,Account,Debit,Credit,Status\n"
            "2025-12-31,JE-A,Sales,100.00,, post \n"
            "2025-12-31,JE-A,Checking,,100.00,Post\n"
        )
        report = import_transactions(
            self.company, self.write("case.csv", journal), require_status=["POST"]
        )
        self.assertEqual(report.entries_created, 1)

    def test_several_approved_values_may_be_allowed(self):
        journal = (
            "Date,Num,Account,Debit,Credit,Status\n"
            "2025-12-31,JE-A,Sales,100.00,,POST\n"
            "2025-12-31,JE-A,Checking,,100.00,APPROVED\n"
        )
        report = import_transactions(
            self.company,
            self.write("multi.csv", journal),
            require_status=["POST", "APPROVED"],
        )
        self.assertEqual(report.errors, [])
        self.assertEqual(report.entries_created, 1)

    def test_a_mixed_entry_is_an_error_not_a_partial_write(self):
        """Half an approved correcting entry is worse than none of it."""
        journal = (
            "Date,Num,Account,Debit,Credit,Status\n"
            "2025-12-31,JE-M,Sales,100.00,,POST\n"
            "2025-12-31,JE-M,Checking,,100.00,PROPOSED - NOT POSTED\n"
        )
        report = import_transactions(
            self.company, self.write("mixed.csv", journal), require_status=["POST"]
        )
        self.assertEqual(report.entries_created, 0)
        self.assertEqual(len(report.errors), 1)
        self.assertIn("mixed status", report.errors[0])
        self.assertFalse(JournalEntry.objects.filter(company=self.company).exists())

    def test_a_missing_status_column_is_fatal(self):
        """Never silently import everything because the gate is unanswerable."""
        journal = (
            "Date,Num,Account,Debit,Credit\n"
            "2025-12-31,JE-A,Sales,100.00,\n"
            "2025-12-31,JE-A,Checking,,100.00\n"
        )
        with self.assertRaises(QBOFormatError) as ctx:
            import_transactions(
                self.company, self.write("nostatus.csv", journal), require_status=["POST"]
            )
        self.assertIn("no status column", str(ctx.exception))
        self.assertFalse(JournalEntry.objects.filter(company=self.company).exists())

    def test_without_the_flag_every_entry_imports(self):
        report = import_transactions(self.company, self.write("je.csv", self.JOURNAL))
        self.assertEqual(report.entries_created, 2)
        self.assertEqual(report.skipped_status, 0)
