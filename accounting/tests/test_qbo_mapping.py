"""QBO vocabulary → Account choices.

The asymmetry under test: an unknown *Type* must fail loudly (an unclassified
account silently breaks every report), while an unknown *Detail Type* must not
(the raw string survives on the model, so nothing is lost).
"""

from django.test import TestCase

from accounting.models import Account
from accounting.qbo.mapping import (
    QBO_TYPE_TO_ACCOUNT_TYPE,
    QBO_TYPE_TO_SUBTYPE,
    UnknownAccountTypeError,
    normalize,
    resolve_account_subtype,
    resolve_account_type,
)


class NormalizeTest(TestCase):
    def test_strips_case_punctuation_and_extra_space(self):
        self.assertEqual(normalize("  Accounts Receivable (A/R) "), "accounts receivable a r")
        self.assertEqual(normalize("Cost of Goods Sold"), "cost of goods sold")
        self.assertEqual(normalize("Other  Current   Assets"), "other current assets")

    def test_none_and_blank_become_empty(self):
        self.assertEqual(normalize(None), "")
        self.assertEqual(normalize("   "), "")


class ResolveAccountTypeTest(TestCase):
    def test_every_qbo_type_maps_to_a_valid_classification(self):
        valid = set(Account.AccountType.values)
        for qbo_type, account_type in QBO_TYPE_TO_ACCOUNT_TYPE.items():
            self.assertIn(account_type, valid, f"{qbo_type} → {account_type}")

    def test_the_fifteen_documented_qbo_types_all_resolve(self):
        expected = {
            "Bank": Account.AccountType.ASSET,
            "Accounts receivable (A/R)": Account.AccountType.ASSET,
            "Other Current Assets": Account.AccountType.ASSET,
            "Fixed Assets": Account.AccountType.ASSET,
            "Other Assets": Account.AccountType.ASSET,
            "Accounts payable (A/P)": Account.AccountType.LIABILITY,
            "Credit Card": Account.AccountType.LIABILITY,
            "Other Current Liabilities": Account.AccountType.LIABILITY,
            "Long Term Liabilities": Account.AccountType.LIABILITY,
            "Equity": Account.AccountType.EQUITY,
            "Income": Account.AccountType.INCOME,
            "Other Income": Account.AccountType.INCOME,
            "Cost of Goods Sold": Account.AccountType.EXPENSE,
            "Expenses": Account.AccountType.EXPENSE,
            "Other Expense": Account.AccountType.EXPENSE,
        }
        for qbo_type, account_type in expected.items():
            self.assertEqual(resolve_account_type(qbo_type), account_type, qbo_type)

    def test_matching_is_case_and_punctuation_insensitive(self):
        for spelling in ("BANK", "bank", "  Bank  "):
            self.assertEqual(resolve_account_type(spelling), Account.AccountType.ASSET)
        self.assertEqual(
            resolve_account_type("accounts receivable (a/r)"), Account.AccountType.ASSET
        )

    def test_unknown_type_raises_rather_than_guessing(self):
        with self.assertRaises(UnknownAccountTypeError):
            resolve_account_type("Cryptocurrency Vault")

    def test_blank_type_raises(self):
        for blank in ("", "   ", None):
            with self.assertRaises(UnknownAccountTypeError):
                resolve_account_type(blank)


class ResolveAccountSubTypeTest(TestCase):
    def test_every_type_fallback_is_a_valid_subtype(self):
        valid = set(Account.AccountSubType.values)
        for qbo_type, subtype in QBO_TYPE_TO_SUBTYPE.items():
            self.assertIn(subtype, valid, f"{qbo_type} → {subtype}")

    def test_every_qbo_type_has_a_subtype_fallback(self):
        # Otherwise an unmapped detail type would leave the subtype blank even
        # when the coarse bucket is perfectly well known.
        for qbo_type in QBO_TYPE_TO_ACCOUNT_TYPE:
            self.assertIn(qbo_type, QBO_TYPE_TO_SUBTYPE, qbo_type)

    def test_known_detail_type_wins(self):
        self.assertEqual(
            resolve_account_subtype("Bank", "Checking"), Account.AccountSubType.BANK
        )
        self.assertEqual(
            resolve_account_subtype("Other Current Assets", "Inventory"),
            Account.AccountSubType.INVENTORY,
        )
        self.assertEqual(
            resolve_account_subtype("Expenses", "Shipping, Freight & Delivery"),
            Account.AccountSubType.SHIPPING_EXPENSE,
        )

    def test_unknown_detail_type_falls_back_to_the_type(self):
        self.assertEqual(
            resolve_account_subtype("Bank", "Some New Intuit Detail Type"),
            Account.AccountSubType.BANK,
        )
        self.assertEqual(
            resolve_account_subtype("Cost of Goods Sold", "Brand New Thing"),
            Account.AccountSubType.COST_OF_GOODS_SOLD,
        )

    def test_unknown_type_and_detail_yields_blank_without_raising(self):
        self.assertEqual(resolve_account_subtype("Nonsense", "Nonsense"), "")

    def test_subtype_never_raises(self):
        for args in (("", ""), (None, None), ("Bank", None), (None, "Checking")):
            resolve_account_subtype(*args)
