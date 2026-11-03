"""Fiscal-period guard: closed/locked periods reject writes.

``FiscalPeriod.is_editable`` exists but nothing enforces it — a JournalEntry
can be posted, edited, or imported into a CLOSED or LOCKED period and the
write lands silently (testing-plan §3 financial integrity, journey J4).
These tests pin the rejection at every writer:

- ORM ``JournalEntry.save()`` raises on a closed/locked ``fiscal_period``.
- ``full_clean()`` (admin/forms path) rejects the same way.
- Moving an existing open-period entry into a closed period is rejected.
- The QBO importer skips groups dated inside a closed/locked period and
  reports them instead of writing.
- OPEN periods and period-less entries still write fine.
"""

from __future__ import annotations

import datetime
import tempfile
from decimal import Decimal
from pathlib import Path

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounting.models import (
    Account,
    FiscalPeriod,
    JournalEntry,
    JournalLine,
)
from accounting.qbo.importer import import_chart_of_accounts, import_transactions
from accounting.tests.test_qbo_import import COA_CSV, JOURNAL_CSV
from core.models import Company


def _period(company, name, status, end_month=12):
    return FiscalPeriod.objects.create(
        company=company,
        name=name,
        start_date=datetime.date(2024, 1, 1),
        end_date=datetime.date(2024, end_month, 30 if end_month == 6 else 31),
        status=status,
    )


class JournalEntryFiscalGuardTests(TestCase):
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
        self.closed = _period(self.company, "EDGE-CLOSED-2024", FiscalPeriod.Status.CLOSED)
        self.locked = _period(
            self.company, "EDGE-LOCKED-2024", FiscalPeriod.Status.LOCKED, end_month=6
        )
        self.open = FiscalPeriod.objects.create(
            company=self.company,
            name="EDGE-OPEN",
            start_date=datetime.date(2025, 1, 1),
            end_date=datetime.date(2025, 12, 31),
            status=FiscalPeriod.Status.OPEN,
        )

    def _entry(self, period):
        return JournalEntry(
            company=self.company,
            fiscal_period=period,
            entry_date=datetime.date(2024, 6, 15),
            entry_source=JournalEntry.EntrySource.MANUAL,
        )

    def _balanced_lines(self, entry):
        entry.save()
        JournalLine.objects.create(
            entry=entry, account=self.ar, debit_amount=Decimal("100.00")
        )
        JournalLine.objects.create(
            entry=entry, account=self.sales, credit_amount=Decimal("100.00")
        )
        return entry

    def test_post_to_closed_period_rejected(self):
        with self.assertRaises(ValidationError):
            self._balanced_lines(self._entry(self.closed))
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_post_to_locked_period_rejected(self):
        with self.assertRaises(ValidationError):
            self._balanced_lines(self._entry(self.locked))
        self.assertEqual(JournalEntry.objects.count(), 0)

    def test_full_clean_rejects_closed_period(self):
        entry = self._entry(self.closed)
        with self.assertRaises(ValidationError):
            entry.full_clean()

    def test_moving_entry_into_closed_period_rejected(self):
        entry = self._balanced_lines(self._entry(self.open))
        entry.fiscal_period = self.closed
        with self.assertRaises(ValidationError):
            entry.save()
        entry.refresh_from_db()
        self.assertEqual(entry.fiscal_period_id, self.open.pk)

    def test_post_to_open_period_allowed(self):
        entry = self._balanced_lines(self._entry(self.open))
        entry.validate_balanced()

    def test_post_without_period_allowed(self):
        entry = self._balanced_lines(self._entry(None))
        entry.validate_balanced()


class ImporterFiscalGuardTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.company = Company.objects.create(name="Example Supply, Inc")

    def test_import_skips_entries_dated_in_closed_period(self):
        coa = Path(self.tmp.name) / "coa.csv"
        coa.write_text(COA_CSV, encoding="utf-8")
        import_chart_of_accounts(self.company, coa)
        FiscalPeriod.objects.create(
            company=self.company,
            name="EDGE-CLOSED-2024",
            start_date=datetime.date(2025, 1, 1),
            end_date=datetime.date(2025, 12, 31),
            status=FiscalPeriod.Status.CLOSED,
        )
        journal = Path(self.tmp.name) / "journal.csv"
        journal.write_text(JOURNAL_CSV, encoding="utf-8")
        report = import_transactions(self.company, journal)

        self.assertEqual(JournalEntry.objects.count(), 0)
        self.assertEqual(len(report.skipped_closed_period), 2)
