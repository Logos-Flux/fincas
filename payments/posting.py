"""Payment GL postings (Step 1a, customer deposits 0.4.4).

Settlement: DR cash (net) + DR processing fee; CR AR (gross). Refunds post the
mirror with source REFUND. Legs go through ``post_balanced_entry``. The cash
and fee accounts come from the rule, or from the company's per-provider
override for ``payment.payment_provider`` (``accounting.posting.payment_accounts``).

Customer deposits (optional, ``DocumentPostingRule.deposit_account`` on the
payment rule): a payment with no invoice credits the deposit liability
instead of A/R. When the payment's invoice is issued (linked and out of
draft), :func:`sync_deposit_application` moves the deposit to A/R with its
own entry, DR deposits / CR A/R, and reverses that entry again if the
payment is voided, unlinked or the invoice voided. A refund of a payment
whose money is still in deposits debits deposits instead of A/R.

Application rule: a payment applies whole (its gross less any refunds that
already came out of deposits) to the one invoice it is linked to. An
overpayment leaves a credit on A/R, exactly as a payment recorded against
the invoice directly does; a payment linked to no issued invoice stays in
deposits. No deposit account on the rule: every payment credits A/R, as
before 0.4.4, and nothing here posts.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from accounting.models import Account, DocumentPostingRule, JournalEntry, JournalLine
from accounting.posting import (
    payment_accounts,
    post_balanced_entry,
    reverse_entry,
    rule_for,
)

ZERO = Decimal("0")

#: Invoice statuses with no A/R entry (nothing to apply a deposit to).
_UNISSUED_INVOICE = ("draft", "void", "cancelled")


def _payment_key(payment) -> str:
    return f"{JournalEntry.EntrySource.PAYMENT}:{payment.payment_number}"


def _reversal_key(entry) -> str:
    return f"{JournalEntry.EntrySource.SYSTEM}:reversal:{entry.posting_key or entry.pk}"


def _is_reversed(entry) -> bool:
    return JournalEntry.objects.filter(
        company_id=entry.company_id, posting_key=_reversal_key(entry)
    ).exists()


def deposit_account_of(payment) -> Account | None:
    """The deposit liability ``payment``'s own entry credited, or None.

    None when the payment was posted to A/R (the pre-0.4.4 path, or it had an
    invoice when it settled), never posted, or its entry has been reversed
    (voided). Read from the entry itself, not the rule, so a later change to
    the rule never reclassifies money already booked.
    """
    if not payment.payment_number or payment.company_id is None:
        return None
    entry = JournalEntry.objects.filter(
        company_id=payment.company_id, posting_key=_payment_key(payment)
    ).first()
    if entry is None:
        return None
    line = (
        entry.lines.filter(
            credit_amount__gt=0, account__account_type=Account.AccountType.LIABILITY
        )
        .select_related("account")
        .first()
    )
    if line is None or _is_reversed(entry):
        return None
    return line.account


def _refunds_from_deposits(payment, deposit: Account) -> Decimal:
    """What refund rows of ``payment`` took back out of ``deposit``."""
    numbers = list(
        payment.child_payments.filter(payment_type=payment.PaymentType.REFUND)
        .values_list("payment_number", flat=True)
    )
    if not numbers:
        return ZERO
    keys = [f"{JournalEntry.EntrySource.REFUND}:{n}" for n in numbers]
    total = sum(
        JournalLine.objects.filter(
            entry__company_id=payment.company_id,
            entry__posting_key__in=keys,
            account=deposit,
        ).values_list("debit_amount", flat=True),
        ZERO,
    )
    return Decimal(total)


def post_payment(payment) -> JournalEntry:
    """Post a captured/settled payment, or a refund mirror."""
    is_refund = payment.payment_type == payment.PaymentType.REFUND
    doc_type = (
        DocumentPostingRule.DocType.REFUND if is_refund
        else DocumentPostingRule.DocType.PAYMENT
    )
    # A refund row may carry no provider of its own; the money goes back out
    # of the account the parent payment put it in.
    provider = payment.payment_provider
    parent = getattr(payment, "parent_payment", None) if is_refund else None
    if is_refund and not provider:
        provider = getattr(parent, "payment_provider", "") or ""
    rule, cash_account, fee_account = payment_accounts(
        payment.company, doc_type, provider
    )
    gross = Decimal(payment.amount or 0)
    fee = Decimal(payment.convenience_fee or 0)
    net = gross - fee
    source = (
        JournalEntry.EntrySource.REFUND if is_refund
        else JournalEntry.EntrySource.PAYMENT
    )

    if is_refund:
        # A refund of money still held as a customer deposit (parent posted
        # to deposits, never applied to an issued invoice) comes out of
        # deposits; once applied, the money is on A/R and the refund is too.
        debit_account, debit_label = rule.receivable_account, f"Refund {payment.payment_number}"
        if parent is not None:
            deposit = deposit_account_of(parent)
            if deposit is not None and active_application(parent) is None:
                debit_account = deposit
                debit_label = f"Refund {payment.payment_number} of customer deposit"
        legs = [
            (debit_account, gross, Decimal("0"), debit_label),
            (cash_account, Decimal("0"), net, "Cash refunded"),
        ]
        if fee > 0:
            legs.append((cash_account, Decimal("0"), fee, "Fee refunded"))
    else:
        credit_account, credit_label = rule.receivable_account, "AR settled"
        if payment.invoice_id is None and rule.deposit_account_id is not None:
            credit_account, credit_label = rule.deposit_account, "Customer deposit received"
        legs = [
            (cash_account, net, Decimal("0"), f"Payment {payment.payment_number}"),
            (credit_account, Decimal("0"), gross, credit_label),
        ]
        if fee > 0:
            legs.append(
                (fee_account, fee, Decimal("0"), "Processing fee")
            )
    order = getattr(payment, "order", None)
    customer = getattr(order, "customer", None)
    entity = (
        getattr(customer, "company_name", "") or str(customer or "")
    )[:255]
    paid_at = getattr(payment, "payment_date", None)
    if hasattr(paid_at, "date"):
        paid_at = paid_at.date()
    return post_balanced_entry(
        company=payment.company,
        entry_date=paid_at or date.today(),
        entry_source=source,
        source_reference=payment.payment_number,
        memo=f"Payment {payment.payment_number}",
        entity_name=entity,
        legs=legs,
    )


def reverse_payment(payment) -> JournalEntry:
    """Counter-entry for a voided/cancelled payment. Never edits."""
    original = JournalEntry.objects.get(
        company=payment.company,
        posting_key=_payment_key(payment),
    )
    return reverse_entry(original, memo=f"Void of payment {payment.payment_number}")


# --- customer deposit application --------------------------------------------------


def _applications(payment) -> list[JournalEntry]:
    """Every deposit-application entry ever posted for ``payment``, oldest first.

    Their ``source_reference`` is ``<payment_number>:<invoice_number>:<n>``;
    ``n`` counts applications of this payment, so a payment unlinked and
    linked again gets a fresh key while each one stays idempotent.
    """
    return list(
        JournalEntry.objects.filter(
            company_id=payment.company_id,
            entry_source=JournalEntry.EntrySource.DEPOSIT_APPLICATION,
            source_reference__startswith=f"{payment.payment_number}:",
        ).order_by("created_at", "entry_number")
    )


def _applied_amount(entry) -> Decimal:
    return Decimal(
        sum(entry.lines.values_list("debit_amount", flat=True), ZERO)
    )


def active_application(payment) -> JournalEntry | None:
    """The application entry of ``payment`` that has not been reversed, if any."""
    if not payment.payment_number or payment.company_id is None:
        return None
    for entry in reversed(_applications(payment)):
        if not _is_reversed(entry):
            return entry
    return None


def sync_deposit_application(payment) -> bool:
    """Make the GL agree with where ``payment``'s deposit should be. Idempotent.

    Wanted: the payment was posted to a deposit account, is not voided, is
    linked to an invoice that has been issued (not draft, void or
    canceled), and something is left after deposit refunds. Then one live
    application entry for that invoice and amount (DR deposits / CR A/R) must
    exist; otherwise none may. A live entry that no longer matches is reversed
    (never edited) and, when wanted, a new one posted. Returns True when it
    wrote an entry.
    """
    if payment.payment_type != payment.PaymentType.PAYMENT or not payment.payment_number:
        return False
    deposit = deposit_account_of(payment)
    entries = _applications(payment) if payment.company_id else []
    if deposit is None and not entries:
        return False  # an A/R payment (or never posted): nothing to do

    invoice = payment.invoice if payment.invoice_id else None
    wanted = None
    held = (payment.Status.CAPTURED, payment.Status.SETTLED, payment.Status.REFUNDED)
    if (
        deposit is not None
        and invoice is not None
        and invoice.status not in _UNISSUED_INVOICE
        and payment.status in held
    ):
        amount = Decimal(payment.amount or 0) - _refunds_from_deposits(payment, deposit)
        if amount > 0:
            wanted = (invoice, amount)

    wrote = False
    kept = None
    for entry in entries:
        if _is_reversed(entry):
            continue
        if (
            kept is None
            and wanted is not None
            and entry.source_reference.split(":")[1] == wanted[0].invoice_number
            and _applied_amount(entry) == wanted[1]
        ):
            kept = entry
            continue
        reverse_entry(
            entry, memo=f"Deposit {payment.payment_number} no longer applied ({entry.source_reference})"
        )
        wrote = True
    if wanted is None or kept is not None:
        return wrote

    invoice, amount = wanted
    receivable = rule_for(payment.company, DocumentPostingRule.DocType.PAYMENT).receivable_account
    paid_at = getattr(payment, "payment_date", None)
    if hasattr(paid_at, "date"):
        paid_at = paid_at.date()
    # The deposit becomes A/R when both exist: the later of invoice and payment.
    dates = [d for d in (invoice.invoice_date, paid_at) if d]
    entry_date = max(dates) if dates else date.today()
    order = getattr(payment, "order", None)
    customer = getattr(order, "customer", None)
    entity = (getattr(customer, "company_name", "") or str(customer or ""))[:255]
    post_balanced_entry(
        company=payment.company,
        entry_date=entry_date,
        entry_source=JournalEntry.EntrySource.DEPOSIT_APPLICATION,
        source_reference=f"{payment.payment_number}:{invoice.invoice_number}:{len(entries) + 1}",
        memo=f"Deposit {payment.payment_number} applied to {invoice.invoice_number}",
        entity_name=entity,
        legs=[
            (deposit, amount, ZERO, f"Deposit {payment.payment_number} applied"),
            (receivable, ZERO, amount, f"Invoice {invoice.invoice_number} settled"),
        ],
    )
    return True
