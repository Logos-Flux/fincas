"""Translate QuickBooks Online's account vocabulary into ``Account`` choices.

QBO describes an account with two strings: a **Type** (15 values, stable for
years) and a **Detail Type** (~90 values, and Intuit adds to the list). This
module maps Type → ``AccountType`` exhaustively, and Detail Type →
``AccountSubType`` on a best-effort basis.

The asymmetry is deliberate. An unrecognised Type is a hard error: without a
classification the account cannot be placed on a P&L or balance sheet, and
guessing would silently corrupt every report built on it. An unrecognised
Detail Type is not — the raw string is preserved in ``Account.qbo_detail_type``
and the subtype is simply left blank, which loses no information and blocks
nothing.
"""

from ..models import Account

_AccountType = Account.AccountType
_SubType = Account.AccountSubType


def normalize(value):
    """Casefold and collapse whitespace/punctuation for lookup.

    QBO exports are inconsistent about spacing, case and the parenthetical in
    "Accounts receivable (A/R)", so every lookup goes through this.
    """
    if value is None:
        return ""
    text = str(value).strip().casefold()
    for ch in ("(", ")", "/", "-", "_", ",", ".", "&", "'", ":", "|"):
        text = text.replace(ch, " ")
    return " ".join(text.split())


#: QBO "Type" → accounting classification. Exhaustive over QBO's fixed list.
QBO_TYPE_TO_ACCOUNT_TYPE = {
    # Asset
    "bank": _AccountType.ASSET,
    "accounts receivable a r": _AccountType.ASSET,
    "accounts receivable": _AccountType.ASSET,
    "other current assets": _AccountType.ASSET,
    "other current asset": _AccountType.ASSET,
    "fixed assets": _AccountType.ASSET,
    "fixed asset": _AccountType.ASSET,
    "other assets": _AccountType.ASSET,
    "other asset": _AccountType.ASSET,
    # Liability
    "accounts payable a p": _AccountType.LIABILITY,
    "accounts payable": _AccountType.LIABILITY,
    "credit card": _AccountType.LIABILITY,
    "other current liabilities": _AccountType.LIABILITY,
    "other current liability": _AccountType.LIABILITY,
    "long term liabilities": _AccountType.LIABILITY,
    "long term liability": _AccountType.LIABILITY,
    # Equity
    "equity": _AccountType.EQUITY,
    # Income
    "income": _AccountType.INCOME,
    "other income": _AccountType.INCOME,
    # Expense
    "cost of goods sold": _AccountType.EXPENSE,
    "expenses": _AccountType.EXPENSE,
    "expense": _AccountType.EXPENSE,
    "other expense": _AccountType.EXPENSE,
}

#: Fallback when the Detail Type is unmapped: derive a coarse subtype from the
#: QBO Type alone. Every QBO Type resolves here, so an unknown detail type
#: still lands in a sensible bucket rather than blank.
QBO_TYPE_TO_SUBTYPE = {
    "bank": _SubType.BANK,
    "accounts receivable a r": _SubType.ACCOUNTS_RECEIVABLE,
    "accounts receivable": _SubType.ACCOUNTS_RECEIVABLE,
    "other current assets": _SubType.OTHER_CURRENT_ASSET,
    "other current asset": _SubType.OTHER_CURRENT_ASSET,
    "fixed assets": _SubType.FIXED_ASSET,
    "fixed asset": _SubType.FIXED_ASSET,
    "other assets": _SubType.OTHER_ASSET,
    "other asset": _SubType.OTHER_ASSET,
    "accounts payable a p": _SubType.ACCOUNTS_PAYABLE,
    "accounts payable": _SubType.ACCOUNTS_PAYABLE,
    "credit card": _SubType.CREDIT_CARD,
    "other current liabilities": _SubType.OTHER_CURRENT_LIABILITY,
    "other current liability": _SubType.OTHER_CURRENT_LIABILITY,
    "long term liabilities": _SubType.LONG_TERM_LIABILITY,
    "long term liability": _SubType.LONG_TERM_LIABILITY,
    "equity": _SubType.EQUITY,
    "income": _SubType.SALES_INCOME,
    "other income": _SubType.OTHER_INCOME,
    "cost of goods sold": _SubType.COST_OF_GOODS_SOLD,
    "expenses": _SubType.OPERATING_EXPENSE,
    "expense": _SubType.OPERATING_EXPENSE,
    "other expense": _SubType.OTHER_EXPENSE,
}

#: QBO "Detail Type" → finer subtype. Best-effort; misses are not errors.
QBO_DETAIL_TYPE_TO_SUBTYPE = {
    # --- Asset ---
    "cash on hand": _SubType.BANK,
    "checking": _SubType.BANK,
    "money market": _SubType.BANK,
    "rents held in trust": _SubType.BANK,
    "savings": _SubType.BANK,
    "trust accounts": _SubType.BANK,
    "accounts receivable a r": _SubType.ACCOUNTS_RECEIVABLE,
    "accounts receivable": _SubType.ACCOUNTS_RECEIVABLE,
    "allowance for bad debts": _SubType.OTHER_CURRENT_ASSET,
    "development costs": _SubType.OTHER_CURRENT_ASSET,
    "employee cash advances": _SubType.OTHER_CURRENT_ASSET,
    "inventory": _SubType.INVENTORY,
    "investment mortgage real estate loans": _SubType.OTHER_CURRENT_ASSET,
    "investment other": _SubType.OTHER_CURRENT_ASSET,
    "investment tax exempt securities": _SubType.OTHER_CURRENT_ASSET,
    "investment us government obligations": _SubType.OTHER_CURRENT_ASSET,
    "loans to officers": _SubType.OTHER_CURRENT_ASSET,
    "loans to others": _SubType.OTHER_CURRENT_ASSET,
    "loans to stockholders": _SubType.OTHER_CURRENT_ASSET,
    "prepaid expenses": _SubType.OTHER_CURRENT_ASSET,
    "retainage": _SubType.OTHER_CURRENT_ASSET,
    "undeposited funds": _SubType.UNDEPOSITED_FUNDS,
    "other current assets": _SubType.OTHER_CURRENT_ASSET,
    "accumulated depletion": _SubType.ACCUMULATED_DEPRECIATION,
    "accumulated depreciation": _SubType.ACCUMULATED_DEPRECIATION,
    "accumulated depreciation on property for sale": _SubType.ACCUMULATED_DEPRECIATION,
    "buildings": _SubType.FIXED_ASSET,
    "depletable assets": _SubType.FIXED_ASSET,
    "furniture and fixtures": _SubType.FIXED_ASSET,
    "land": _SubType.FIXED_ASSET,
    "leasehold improvements": _SubType.FIXED_ASSET,
    "machinery and equipment": _SubType.FIXED_ASSET,
    "vehicles": _SubType.FIXED_ASSET,
    "other fixed assets": _SubType.FIXED_ASSET,
    "accumulated amortization": _SubType.OTHER_ASSET,
    "goodwill": _SubType.OTHER_ASSET,
    "intangible assets": _SubType.OTHER_ASSET,
    "lease buyout": _SubType.OTHER_ASSET,
    "licenses": _SubType.OTHER_ASSET,
    "organizational costs": _SubType.OTHER_ASSET,
    "security deposits": _SubType.OTHER_ASSET,
    "other long term assets": _SubType.OTHER_ASSET,
    # --- Liability ---
    "accounts payable a p": _SubType.ACCOUNTS_PAYABLE,
    "accounts payable": _SubType.ACCOUNTS_PAYABLE,
    "credit card": _SubType.CREDIT_CARD,
    "direct deposit payable": _SubType.OTHER_CURRENT_LIABILITY,
    "federal income tax payable": _SubType.OTHER_CURRENT_LIABILITY,
    "insurance payable": _SubType.OTHER_CURRENT_LIABILITY,
    "line of credit": _SubType.OTHER_CURRENT_LIABILITY,
    "loan payable": _SubType.OTHER_CURRENT_LIABILITY,
    "payroll clearing": _SubType.OTHER_CURRENT_LIABILITY,
    "payroll tax payable": _SubType.OTHER_CURRENT_LIABILITY,
    "prepaid expenses payable": _SubType.OTHER_CURRENT_LIABILITY,
    "rents in trust liability": _SubType.OTHER_CURRENT_LIABILITY,
    "sales tax payable": _SubType.SALES_TAX_PAYABLE,
    "state local income tax payable": _SubType.OTHER_CURRENT_LIABILITY,
    "trust accounts liabilities": _SubType.OTHER_CURRENT_LIABILITY,
    "other current liabilities": _SubType.OTHER_CURRENT_LIABILITY,
    "notes payable": _SubType.LONG_TERM_LIABILITY,
    "shareholder notes payable": _SubType.LONG_TERM_LIABILITY,
    "other long term liabilities": _SubType.LONG_TERM_LIABILITY,
    # --- Equity ---
    "accumulated adjustment": _SubType.EQUITY,
    "common stock": _SubType.EQUITY,
    "estimated taxes": _SubType.EQUITY,
    "health insurance premium": _SubType.EQUITY,
    "health savings account contribution": _SubType.EQUITY,
    "opening balance equity": _SubType.OPENING_BALANCE_EQUITY,
    "owners equity": _SubType.EQUITY,
    "paid in capital or surplus": _SubType.EQUITY,
    "partner contributions": _SubType.EQUITY,
    "partner distributions": _SubType.EQUITY,
    "partners equity": _SubType.EQUITY,
    "personal expense": _SubType.EQUITY,
    "personal income": _SubType.EQUITY,
    "preferred stock": _SubType.EQUITY,
    "retained earnings": _SubType.RETAINED_EARNINGS,
    "treasury stock": _SubType.EQUITY,
    # --- Income ---
    "discounts refunds given": _SubType.DISCOUNTS_REFUNDS,
    "non profit income": _SubType.SALES_INCOME,
    "other primary income": _SubType.SALES_INCOME,
    "sales of product income": _SubType.SALES_INCOME,
    "service fee income": _SubType.SERVICE_INCOME,
    "shipping freight delivery income": _SubType.SHIPPING_INCOME,
    "unapplied cash payment income": _SubType.OTHER_INCOME,
    "dividend income": _SubType.OTHER_INCOME,
    "interest earned": _SubType.OTHER_INCOME,
    "other investment income": _SubType.OTHER_INCOME,
    "other miscellaneous income": _SubType.OTHER_INCOME,
    "tax exempt interest": _SubType.OTHER_INCOME,
    # --- Expense ---
    "cost of labor": _SubType.COST_OF_GOODS_SOLD,
    "equipment rental cos": _SubType.COST_OF_GOODS_SOLD,
    "other costs of services cos": _SubType.COST_OF_GOODS_SOLD,
    "shipping freight delivery cos": _SubType.COST_OF_GOODS_SOLD,
    "supplies materials cogs": _SubType.COST_OF_GOODS_SOLD,
    "advertising promotional": _SubType.OPERATING_EXPENSE,
    "auto": _SubType.OPERATING_EXPENSE,
    "bad debts": _SubType.OPERATING_EXPENSE,
    "bank charges": _SubType.PROCESSING_FEE_EXPENSE,
    "charitable contributions": _SubType.OPERATING_EXPENSE,
    "dues subscriptions": _SubType.OPERATING_EXPENSE,
    "entertainment": _SubType.OPERATING_EXPENSE,
    "entertainment meals": _SubType.OPERATING_EXPENSE,
    "equipment rental": _SubType.OPERATING_EXPENSE,
    "finance costs": _SubType.PROCESSING_FEE_EXPENSE,
    "insurance": _SubType.OPERATING_EXPENSE,
    "interest paid": _SubType.OPERATING_EXPENSE,
    "legal professional fees": _SubType.OPERATING_EXPENSE,
    "office general administrative expenses": _SubType.OPERATING_EXPENSE,
    "other business expenses": _SubType.OPERATING_EXPENSE,
    "other miscellaneous service cost": _SubType.OPERATING_EXPENSE,
    "payroll expenses": _SubType.PAYROLL_EXPENSE,
    "payroll tax expenses": _SubType.PAYROLL_EXPENSE,
    "payroll wage expenses": _SubType.PAYROLL_EXPENSE,
    "promotional meals": _SubType.OPERATING_EXPENSE,
    "rent or lease of buildings": _SubType.OPERATING_EXPENSE,
    "repair maintenance": _SubType.OPERATING_EXPENSE,
    "shipping freight delivery": _SubType.SHIPPING_EXPENSE,
    "supplies materials": _SubType.OPERATING_EXPENSE,
    "taxes paid": _SubType.TAX_EXPENSE,
    "travel": _SubType.OPERATING_EXPENSE,
    "travel meals": _SubType.OPERATING_EXPENSE,
    "unapplied cash bill payment expense": _SubType.OPERATING_EXPENSE,
    "utilities": _SubType.OPERATING_EXPENSE,
    "amortization": _SubType.DEPRECIATION_EXPENSE,
    "depreciation": _SubType.DEPRECIATION_EXPENSE,
    "exchange gain or loss": _SubType.OTHER_EXPENSE,
    "gas and fuel": _SubType.OPERATING_EXPENSE,
    "home office": _SubType.OPERATING_EXPENSE,
    "homeowner rental insurance": _SubType.OPERATING_EXPENSE,
    "other home office expenses": _SubType.OPERATING_EXPENSE,
    "mortgage interest home office": _SubType.OPERATING_EXPENSE,
    "other vehicle expenses": _SubType.OPERATING_EXPENSE,
    "parking and tolls": _SubType.OPERATING_EXPENSE,
    "penalties settlements": _SubType.OTHER_EXPENSE,
    "repairs and maintenance": _SubType.OPERATING_EXPENSE,
    "rent and lease home office": _SubType.OPERATING_EXPENSE,
    "utilities home office": _SubType.OPERATING_EXPENSE,
    "wash and road services": _SubType.OPERATING_EXPENSE,
}


class UnknownAccountTypeError(ValueError):
    """Raised when a QBO Type has no accounting classification.

    Hard-fails the import: an unclassified account cannot be reported on, and a
    guess would corrupt the P&L silently.
    """


def resolve_account_type(qbo_type):
    """Map a QBO Type string to an ``Account.AccountType``.

    Raises ``UnknownAccountTypeError`` when it is unrecognised or blank.
    """
    key = normalize(qbo_type)
    if not key:
        raise UnknownAccountTypeError("QBO account type is blank.")
    try:
        return QBO_TYPE_TO_ACCOUNT_TYPE[key]
    except KeyError:
        raise UnknownAccountTypeError(
            f"Unrecognized QBO account type {qbo_type!r}. Add it to "
            f"QBO_TYPE_TO_ACCOUNT_TYPE in accounting/qbo/mapping.py."
        ) from None


def resolve_account_subtype(qbo_type, qbo_detail_type):
    """Map a QBO Detail Type to an ``Account.AccountSubType``, best effort.

    Falls back to the coarse subtype implied by the Type, and finally to "" if
    even that is unknown. Never raises — the raw strings are preserved on the
    model, so nothing is lost by declining to classify.
    """
    detail_key = normalize(qbo_detail_type)
    if detail_key in QBO_DETAIL_TYPE_TO_SUBTYPE:
        return QBO_DETAIL_TYPE_TO_SUBTYPE[detail_key]
    return QBO_TYPE_TO_SUBTYPE.get(normalize(qbo_type), "")
