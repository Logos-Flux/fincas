"""GL model invariants: normal balance, hierarchy, and the DB constraints.

The constraint tests matter more than they look. A journal line that can hold
zero on both sides, or a negative amount, unbalances every report built on top
of it — and nothing upstream would raise. These pin the guarantee at the layer
that actually enforces it.
"""

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounting.models import Account, FiscalPeriod, JournalEntry, JournalLine
from core.models import Company


class AccountTypeTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")

    def _account(self, name, account_type, **kwargs):
        return Account.objects.create(
            company=self.company, name=name, account_type=account_type, **kwargs
        )

    def test_asset_and_expense_are_debit_normal(self):
        for account_type in (Account.AccountType.ASSET, Account.AccountType.EXPENSE):
            account = self._account(f"a-{account_type}", account_type)
            self.assertTrue(account.is_debit_normal)
            self.assertEqual(account.normal_balance, "debit")

    def test_liability_equity_income_are_credit_normal(self):
        for account_type in (
            Account.AccountType.LIABILITY,
            Account.AccountType.EQUITY,
            Account.AccountType.INCOME,
        ):
            account = self._account(f"a-{account_type}", account_type)
            self.assertFalse(account.is_debit_normal)
            self.assertEqual(account.normal_balance, "credit")


class AccountHierarchyTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.other = Company.objects.create(name="Voltage Labs")

    def test_full_name_built_from_parent_chain(self):
        expenses = Account.objects.create(
            company=self.company, name="Expenses",
            account_type=Account.AccountType.EXPENSE,
        )
        utilities = Account.objects.create(
            company=self.company, name="Utilities", parent=expenses,
            account_type=Account.AccountType.EXPENSE,
        )
        electric = Account.objects.create(
            company=self.company, name="Electric", parent=utilities,
            account_type=Account.AccountType.EXPENSE,
        )
        self.assertEqual(expenses.full_name, "Expenses")
        self.assertEqual(utilities.full_name, "Expenses:Utilities")
        self.assertEqual(electric.full_name, "Expenses:Utilities:Electric")

    def test_explicit_full_name_is_not_overwritten(self):
        account = Account.objects.create(
            company=self.company, name="Electric",
            full_name="Expenses:Utilities:Electric",
            account_type=Account.AccountType.EXPENSE,
        )
        self.assertEqual(account.full_name, "Expenses:Utilities:Electric")

    def test_parent_must_share_account_type(self):
        asset = Account.objects.create(
            company=self.company, name="Assets",
            account_type=Account.AccountType.ASSET,
        )
        child = Account(
            company=self.company, name="Bad", parent=asset,
            account_type=Account.AccountType.EXPENSE,
        )
        with self.assertRaises(ValidationError):
            child.clean()

    def test_parent_must_be_same_company(self):
        foreign = Account.objects.create(
            company=self.other, name="Expenses",
            account_type=Account.AccountType.EXPENSE,
        )
        child = Account(
            company=self.company, name="Utilities", parent=foreign,
            account_type=Account.AccountType.EXPENSE,
        )
        with self.assertRaises(ValidationError):
            child.clean()

    def test_account_cannot_be_its_own_parent(self):
        account = Account.objects.create(
            company=self.company, name="Expenses",
            account_type=Account.AccountType.EXPENSE,
        )
        account.parent = account
        with self.assertRaises(ValidationError):
            account.clean()

    def test_build_full_name_detects_a_cycle(self):
        a = Account.objects.create(
            company=self.company, name="A", account_type=Account.AccountType.EXPENSE
        )
        b = Account.objects.create(
            company=self.company, name="B", parent=a,
            account_type=Account.AccountType.EXPENSE,
        )
        # Force the cycle past clean(), as a bad data migration could.
        Account.objects.filter(pk=a.pk).update(parent=b)
        a.refresh_from_db()
        with self.assertRaises(ValidationError):
            a.build_full_name()


class AccountConstraintTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.other = Company.objects.create(name="Voltage Labs")

    def test_account_number_unique_within_company(self):
        Account.objects.create(
            company=self.company, name="Checking", account_number="1000",
            account_type=Account.AccountType.ASSET,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Account.objects.create(
                company=self.company, name="Savings", account_number="1000",
                account_type=Account.AccountType.ASSET,
            )

    def test_same_account_number_allowed_in_another_company(self):
        Account.objects.create(
            company=self.company, name="Checking", account_number="1000",
            account_type=Account.AccountType.ASSET,
        )
        Account.objects.create(
            company=self.other, name="Checking", account_number="1000",
            account_type=Account.AccountType.ASSET,
        )
        self.assertEqual(Account.objects.filter(account_number="1000").count(), 2)

    def test_blank_account_numbers_do_not_collide(self):
        # The uniqueness is partial — QBO leaves numbers off by default, and a
        # whole chart of blank-numbered accounts must still import.
        for name in ("One", "Two", "Three"):
            Account.objects.create(
                company=self.company, name=name,
                account_type=Account.AccountType.EXPENSE,
            )
        self.assertEqual(Account.objects.filter(account_number="").count(), 3)

    def test_full_name_unique_within_company(self):
        Account.objects.create(
            company=self.company, name="Electric",
            full_name="Expenses:Utilities:Electric",
            account_type=Account.AccountType.EXPENSE,
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            Account.objects.create(
                company=self.company, name="Electric",
                full_name="Expenses:Utilities:Electric",
                account_type=Account.AccountType.EXPENSE,
            )


class CompanyScopingTest(TestCase):
    """Cross-tenant reads are the failure mode that silently corrupts books."""

    def setUp(self):
        self.a = Company.objects.create(name="Example Supply, Inc")
        self.b = Company.objects.create(name="Voltage Labs")
        Account.objects.create(
            company=self.a, name="Checking", account_type=Account.AccountType.ASSET
        )
        Account.objects.create(
            company=self.b, name="Checking", account_type=Account.AccountType.ASSET
        )

    def test_for_company_isolates_accounts(self):
        self.assertEqual(Account.objects.for_company(self.a).count(), 1)
        self.assertEqual(Account.objects.for_company(self.b).count(), 1)
        self.assertEqual(Account.objects.count(), 2)

    def test_for_company_isolates_journal_entries(self):
        for company in (self.a, self.b):
            JournalEntry.objects.create(
                company=company, entry_date=datetime.date(2025, 1, 1)
            )
        self.assertEqual(JournalEntry.objects.for_company(self.a).count(), 1)
        self.assertEqual(JournalEntry.objects.for_company(self.b).count(), 1)


class FiscalPeriodTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")

    def test_end_date_must_not_precede_start_date(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            FiscalPeriod.objects.create(
                company=self.company, name="Broken",
                start_date=datetime.date(2025, 2, 1),
                end_date=datetime.date(2025, 1, 1),
            )

    def test_single_day_period_is_allowed(self):
        period = FiscalPeriod.objects.create(
            company=self.company, name="One day",
            start_date=datetime.date(2025, 1, 1),
            end_date=datetime.date(2025, 1, 1),
        )
        self.assertTrue(period.contains(datetime.date(2025, 1, 1)))

    def test_contains_is_inclusive_of_both_ends(self):
        period = FiscalPeriod.objects.create(
            company=self.company, name="Jan 2025",
            start_date=datetime.date(2025, 1, 1),
            end_date=datetime.date(2025, 1, 31),
        )
        self.assertTrue(period.contains(datetime.date(2025, 1, 1)))
        self.assertTrue(period.contains(datetime.date(2025, 1, 31)))
        self.assertFalse(period.contains(datetime.date(2025, 2, 1)))

    def test_name_unique_within_company(self):
        FiscalPeriod.objects.create(
            company=self.company, name="Jan 2025",
            start_date=datetime.date(2025, 1, 1),
            end_date=datetime.date(2025, 1, 31),
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            FiscalPeriod.objects.create(
                company=self.company, name="Jan 2025",
                start_date=datetime.date(2025, 3, 1),
                end_date=datetime.date(2025, 3, 31),
            )


class JournalLineConstraintTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.cash = Account.objects.create(
            company=self.company, name="Cash", account_type=Account.AccountType.ASSET
        )
        self.entry = JournalEntry.objects.create(
            company=self.company, entry_date=datetime.date(2025, 1, 15)
        )

    def _line(self, debit, credit):
        return JournalLine.objects.create(
            entry=self.entry, account=self.cash,
            debit_amount=Decimal(debit), credit_amount=Decimal(credit),
        )

    def test_debit_only_line_is_accepted(self):
        line = self._line("100.00", "0.00")
        self.assertEqual(line.signed_amount, Decimal("100.00"))

    def test_credit_only_line_is_accepted(self):
        line = self._line("0.00", "100.00")
        self.assertEqual(line.signed_amount, Decimal("-100.00"))

    def test_zero_on_both_sides_is_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._line("0.00", "0.00")

    def test_amount_on_both_sides_is_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._line("100.00", "100.00")

    def test_negative_amount_is_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._line("-100.00", "0.00")


class JournalEntryBalanceTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.cash = Account.objects.create(
            company=self.company, name="Cash", account_type=Account.AccountType.ASSET
        )
        self.revenue = Account.objects.create(
            company=self.company, name="Sales", account_type=Account.AccountType.INCOME
        )
        self.entry = JournalEntry.objects.create(
            company=self.company, entry_date=datetime.date(2025, 1, 15)
        )

    def test_balanced_entry_validates(self):
        JournalLine.objects.create(
            entry=self.entry, account=self.cash, debit_amount=Decimal("100.00")
        )
        JournalLine.objects.create(
            entry=self.entry, account=self.revenue, credit_amount=Decimal("100.00")
        )
        self.assertTrue(self.entry.is_balanced)
        self.entry.validate_balanced()  # does not raise

    def test_unbalanced_entry_raises(self):
        JournalLine.objects.create(
            entry=self.entry, account=self.cash, debit_amount=Decimal("100.00")
        )
        JournalLine.objects.create(
            entry=self.entry, account=self.revenue, credit_amount=Decimal("60.00")
        )
        self.assertFalse(self.entry.is_balanced)
        with self.assertRaises(ValidationError):
            self.entry.validate_balanced()

    def test_entry_with_no_lines_raises(self):
        with self.assertRaises(ValidationError):
            self.entry.validate_balanced()

    def test_multi_leg_entry_balances(self):
        tax = Account.objects.create(
            company=self.company, name="Sales Tax Payable",
            account_type=Account.AccountType.LIABILITY,
        )
        JournalLine.objects.create(
            entry=self.entry, account=self.cash, debit_amount=Decimal("108.25")
        )
        JournalLine.objects.create(
            entry=self.entry, account=self.revenue, credit_amount=Decimal("100.00")
        )
        JournalLine.objects.create(
            entry=self.entry, account=tax, credit_amount=Decimal("8.25")
        )
        self.entry.validate_balanced()

    def test_deleting_an_entry_removes_its_lines(self):
        JournalLine.objects.create(
            entry=self.entry, account=self.cash, debit_amount=Decimal("100.00")
        )
        JournalLine.objects.create(
            entry=self.entry, account=self.revenue, credit_amount=Decimal("100.00")
        )
        self.entry.delete()
        self.assertEqual(JournalLine.objects.count(), 0)

    def test_account_in_use_cannot_be_deleted(self):
        JournalLine.objects.create(
            entry=self.entry, account=self.cash, debit_amount=Decimal("100.00")
        )
        # PROTECT: losing an account out from under posted history would
        # silently rewrite the books.
        from django.db.models import ProtectedError

        with self.assertRaises(ProtectedError), transaction.atomic():
            self.cash.delete()


class SubtypeFamilyTest(TestCase):
    """`account_type` and `account_subtype` must describe the same account.

    Nothing at the DB level ties them together, so without a check an
    "income / bank" account is storable — and no report can render it sensibly.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")

    def test_every_subtype_declares_a_family(self):
        for subtype in Account.AccountSubType.values:
            self.assertIn(subtype, Account.SUBTYPE_FAMILY, subtype)

    def test_every_declared_family_is_a_real_account_type(self):
        valid = set(Account.AccountType.values)
        for subtype, family in Account.SUBTYPE_FAMILY.items():
            self.assertIn(family, valid, f"{subtype} -> {family}")

    def test_a_matching_pair_validates(self):
        account = Account(
            company=self.company, name="Checking",
            account_type=Account.AccountType.ASSET,
            account_subtype=Account.AccountSubType.BANK,
        )
        account.clean()  # does not raise

    def test_a_mismatched_pair_is_rejected(self):
        account = Account(
            company=self.company, name="Nonsense",
            account_type=Account.AccountType.INCOME,
            account_subtype=Account.AccountSubType.BANK,
        )
        with self.assertRaises(ValidationError):
            account.clean()

    def test_a_blank_subtype_is_always_allowed(self):
        for account_type in Account.AccountType.values:
            Account(
                company=self.company, name=f"x-{account_type}",
                account_type=account_type, account_subtype="",
            ).clean()


class AccountRenameTest(TestCase):
    """`full_name` is the importer's lookup key AND a unique constraint.

    A rename that leaves it stale makes the next import miss the account and
    build a duplicate tree beside it — so the path has to follow the rename,
    for the account and for everything under it.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.parent = Account.objects.create(
            company=self.company, name="Expenses",
            account_type=Account.AccountType.EXPENSE,
        )
        self.child = Account.objects.create(
            company=self.company, name="Utilities", parent=self.parent,
            account_type=Account.AccountType.EXPENSE,
        )
        self.grandchild = Account.objects.create(
            company=self.company, name="Electric", parent=self.child,
            account_type=Account.AccountType.EXPENSE,
        )

    def test_renaming_updates_the_accounts_own_path(self):
        self.parent.name = "Operating Expenses"
        self.parent.save()
        self.parent.refresh_from_db()
        self.assertEqual(self.parent.full_name, "Operating Expenses")

    def test_renaming_cascades_to_descendants(self):
        self.parent.name = "Operating Expenses"
        self.parent.save()
        self.child.refresh_from_db()
        self.grandchild.refresh_from_db()
        self.assertEqual(self.child.full_name, "Operating Expenses:Utilities")
        self.assertEqual(
            self.grandchild.full_name, "Operating Expenses:Utilities:Electric"
        )

    def test_renaming_a_middle_node_cascades(self):
        self.child.name = "Power"
        self.child.save()
        self.grandchild.refresh_from_db()
        self.assertEqual(self.grandchild.full_name, "Expenses:Power:Electric")

    def test_reparenting_moves_the_subtree(self):
        new_parent = Account.objects.create(
            company=self.company, name="Overheads",
            account_type=Account.AccountType.EXPENSE,
        )
        self.child.parent = new_parent
        self.child.save()
        self.child.refresh_from_db()
        self.grandchild.refresh_from_db()
        self.assertEqual(self.child.full_name, "Overheads:Utilities")
        self.assertEqual(self.grandchild.full_name, "Overheads:Utilities:Electric")

    def test_saving_without_a_rename_leaves_paths_alone(self):
        self.parent.description = "touched"
        self.parent.save()
        self.grandchild.refresh_from_db()
        self.assertEqual(self.grandchild.full_name, "Expenses:Utilities:Electric")

    def test_a_descendant_with_a_drifted_path_is_repaired_not_mangled(self):
        # Rebuilding by string-replacing the old prefix would fail the prefix
        # test on a drifted path and fall back to "<new parent>:<leaf>",
        # discarding every intermediate segment. Deriving from the parent chain
        # cannot do that — and repairs the drift on the way past.
        Account.objects.filter(pk=self.child.pk).update(full_name="Wrong:Path")
        self.parent.name = "Operating Expenses"
        self.parent.save()

        self.child.refresh_from_db()
        self.grandchild.refresh_from_db()
        self.assertEqual(self.child.full_name, "Operating Expenses:Utilities")
        self.assertEqual(
            self.grandchild.full_name, "Operating Expenses:Utilities:Electric"
        )

    def test_a_colliding_rename_leaves_the_tree_intact(self):
        # If the rewrite is not atomic with the rename, a unique-path collision
        # partway down leaves the parent renamed and half the subtree pointing
        # at a path that no longer exists — and the next import duplicates it.
        Account.objects.create(
            company=self.company, name="Blocker",
            full_name="Overheads:Utilities:Electric",
            account_type=Account.AccountType.EXPENSE,
        )
        Account.objects.create(
            company=self.company, name="Overheads",
            full_name="Overheads", account_type=Account.AccountType.EXPENSE,
        )
        self.parent.name = "Overheads"
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.parent.save()

        # Nothing moved.
        self.parent.refresh_from_db()
        self.child.refresh_from_db()
        self.grandchild.refresh_from_db()
        self.assertEqual(self.parent.full_name, "Expenses")
        self.assertEqual(self.child.full_name, "Expenses:Utilities")
        self.assertEqual(self.grandchild.full_name, "Expenses:Utilities:Electric")



class JournalLineCompanyTest(TestCase):
    """A line whose account belongs to another company posts one company's
    money into another's books, and both ledgers are then wrong. The database
    cannot see it — the company is two FK hops away."""

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.other = Company.objects.create(name="Voltage Labs")
        self.entry = JournalEntry.objects.create(
            company=self.company, entry_date=datetime.date(2025, 1, 15)
        )
        self.own = Account.objects.create(
            company=self.company, name="Cash", account_type=Account.AccountType.ASSET
        )
        self.foreign = Account.objects.create(
            company=self.other, name="Cash", account_type=Account.AccountType.ASSET
        )

    def test_same_company_line_validates(self):
        line = JournalLine(
            entry=self.entry, account=self.own, debit_amount=Decimal("10.00")
        )
        line.full_clean()  # does not raise

    def test_cross_company_line_is_rejected(self):
        line = JournalLine(
            entry=self.entry, account=self.foreign, debit_amount=Decimal("10.00")
        )
        with self.assertRaises(ValidationError) as ctx:
            line.full_clean()
        self.assertIn("account", ctx.exception.message_dict)
