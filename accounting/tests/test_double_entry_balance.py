"""Double-entry balance pins for every posting path.

The GL has two writers — the QBO importer and the admin — plus direct ORM
use; all three must produce balanced entries (testing-plan §3 financial
integrity). This file pins:

- ``JournalEntry.validate_balanced``: balanced passes, unbalanced raises,
  line-less raises.
- Cent-level rounding discipline: a three-way $100 split must carry the
  remainder penny (33.33/33.33/33.34); three flat 33.33 legs do NOT balance
  against a 100.00 leg and are rejected rather than fudged.
- Rounding rule: money quantizes HALF_UP at the half-cent boundary
  (``accounting.qbo.importer._q2``), matching the order/PO recalculators —
  not banker's rounding, not gateway truncation.
- Canonical posting shapes balance: invoice (AR/Sales/Tax), payment
  (Cash/AR), refund (Sales/Cash reversal), landed cost (Inventory/AP +
  freight), discount (Discounts-given/AR).
- End to end: every entry the QBO importer writes is balanced from the DB.
"""

from __future__ import annotations

import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounting.models import (
    ZERO,
    Account,
    JournalEntry,
    JournalLine,
)
from accounting.qbo.importer import _q2, import_chart_of_accounts, import_transactions
from accounting.tests.test_qbo_import import COA_CSV, JOURNAL_CSV
from core.models import Company


class ValidateBalancedTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.ar = Account.objects.create(
            company=self.company,
            name="Accounts Receivable",
            account_type=Account.AccountType.ASSET,
            account_subtype=Account.AccountSubType.ACCOUNTS_RECEIVABLE,
        )
        self.sales = Account.objects.create(
            company=self.company,
            name="Sales",
            account_type=Account.AccountType.INCOME,
            account_subtype=Account.AccountSubType.SALES_INCOME,
        )

    def _entry(self):
        return JournalEntry.objects.create(
            company=self.company,
            entry_date="2025-03-01",
            entry_source=JournalEntry.EntrySource.MANUAL,
        )

    def _leg(self, entry, account, *, debit="0.00", credit="0.00"):
        return JournalLine.objects.create(
            entry=entry,
            account=account,
            debit_amount=Decimal(debit),
            credit_amount=Decimal(credit),
        )

    def test_balanced_entry_validates(self):
        entry = self._entry()
        self._leg(entry, self.ar, debit="108.25")
        self._leg(entry, self.sales, credit="108.25")
        entry.validate_balanced()
        self.assertTrue(entry.is_balanced)

    def test_unbalanced_entry_raises(self):
        entry = self._entry()
        self._leg(entry, self.ar, debit="108.25")
        self._leg(entry, self.sales, credit="108.24")
        with self.assertRaises(ValidationError):
            entry.validate_balanced()
        self.assertFalse(entry.is_balanced)

    def test_entry_with_no_lines_raises(self):
        entry = self._entry()
        with self.assertRaises(ValidationError):
            entry.validate_balanced()

    def test_three_way_split_must_carry_the_remainder_penny(self):
        # $100 split three ways: 33.33 x 3 = 99.99, which must NOT validate
        # against a 100.00 leg — the desequencing penny has to live on one
        # leg explicitly, never be fudged by the validator.
        entry = self._entry()
        for _ in range(3):
            self._leg(entry, self.ar, debit="33.33")
        self._leg(entry, self.sales, credit="100.00")
        with self.assertRaises(ValidationError):
            entry.validate_balanced()

        entry2 = self._entry()
        self._leg(entry2, self.ar, debit="33.33")
        self._leg(entry2, self.ar, debit="33.33")
        self._leg(entry2, self.ar, debit="33.34")
        self._leg(entry2, self.sales, credit="100.00")
        entry2.validate_balanced()

    def test_totals_return_zero_pair_without_lines(self):
        entry = self._entry()
        self.assertEqual(entry.totals(), (ZERO, ZERO))


class RoundingRuleTests(TestCase):
    def test_half_cent_rounds_up_not_to_even(self):
        # HALF_UP everywhere in the ERP (orders/purchasing/importer share it):
        # 2.675 -> 2.68, and 2.665 -> 2.67. Banker's rounding would give
        # 2.67 for the first case — pin that it does NOT apply here.
        self.assertEqual(_q2("2.675"), Decimal("2.68"))
        self.assertEqual(_q2("2.665"), Decimal("2.67"))
        self.assertEqual(_q2("100.005"), Decimal("100.01"))

    def test_gateway_cents_arrive_exact(self):
        # Stripe speaks integer cents; the cent -> dollar conversion must be
        # exact, never a float round-trip (0.1 + 0.2 territory).
        amount = Decimal(10825) / Decimal(100)
        self.assertEqual(amount, Decimal("108.25"))


class PostingShapeTests(TestCase):
    """Canonical postings for each operational path balance by construction."""

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")

    def _account(self, name, atype, subtype=""):
        return Account.objects.create(
            company=self.company, name=name, account_type=atype,
            account_subtype=subtype,
        )

    def _post(self, legs):
        entry = JournalEntry.objects.create(
            company=self.company,
            entry_date="2025-03-01",
            entry_source=JournalEntry.EntrySource.MANUAL,
        )
        for account, debit, credit in legs:
            JournalLine.objects.create(
                entry=entry, account=account,
                debit_amount=Decimal(debit), credit_amount=Decimal(credit),
            )
        entry.validate_balanced()
        return entry

    def test_invoice_posting_balances(self):
        ar = self._account("AR", Account.AccountType.ASSET,
                           Account.AccountSubType.ACCOUNTS_RECEIVABLE)
        sales = self._account("Sales", Account.AccountType.INCOME,
                              Account.AccountSubType.SALES_INCOME)
        tax = self._account("Tax", Account.AccountType.LIABILITY,
                            Account.AccountSubType.SALES_TAX_PAYABLE)
        entry = self._post([
            (ar, "108.25", "0.00"),
            (sales, "0.00", "100.00"),
            (tax, "0.00", "8.25"),
        ])
        debit, credit = entry.totals()
        self.assertEqual((debit, credit), (Decimal("108.25"), Decimal("108.25")))

    def test_payment_posting_balances(self):
        cash = self._account("Cash", Account.AccountType.ASSET,
                             Account.AccountSubType.BANK)
        ar = self._account("AR", Account.AccountType.ASSET,
                           Account.AccountSubType.ACCOUNTS_RECEIVABLE)
        self._post([(cash, "108.25", "0.00"), (ar, "0.00", "108.25")])

    def test_refund_posting_balances(self):
        sales = self._account("Sales", Account.AccountType.INCOME,
                              Account.AccountSubType.SALES_INCOME)
        cash = self._account("Cash", Account.AccountType.ASSET,
                             Account.AccountSubType.BANK)
        self._post([(sales, "50.00", "0.00"), (cash, "0.00", "50.00")])

    def test_landed_cost_posting_balances(self):
        inventory = self._account("Inventory", Account.AccountType.ASSET,
                                  Account.AccountSubType.INVENTORY)
        ap = self._account("AP", Account.AccountType.LIABILITY,
                           Account.AccountSubType.ACCOUNTS_PAYABLE)
        # Goods 500 + freight allocation 75 + duty 12.50, all inventoried.
        self._post([
            (inventory, "587.50", "0.00"),
            (ap, "0.00", "587.50"),
        ])

    def test_discount_posting_balances(self):
        contra = self._account("Discounts", Account.AccountType.INCOME,
                               Account.AccountSubType.DISCOUNTS_REFUNDS)
        ar = self._account("AR", Account.AccountType.ASSET,
                           Account.AccountSubType.ACCOUNTS_RECEIVABLE)
        self._post([(contra, "25.00", "0.00"), (ar, "0.00", "25.00")])


class ImportedEntriesBalancedTests(TestCase):
    def test_every_imported_entry_is_balanced_from_the_db(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        company = Company.objects.create(name="Example Supply, Inc")
        coa = Path(tmp.name) / "coa.csv"
        coa.write_text(COA_CSV, encoding="utf-8")
        import_chart_of_accounts(company, coa)
        journal = Path(tmp.name) / "journal.csv"
        journal.write_text(JOURNAL_CSV, encoding="utf-8")
        import_transactions(company, journal)

        entries = JournalEntry.objects.for_company(company)
        self.assertGreater(entries.count(), 0)
        for entry in entries:
            with self.subTest(entry=entry.pk):
                entry.validate_balanced()
