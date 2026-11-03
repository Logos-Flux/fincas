"""Payment GL postings (Step 1a).

Settlement: DR cash (net) + DR processing fee; CR AR (gross). Refunds post the
mirror with source REFUND. Legs go through ``post_balanced_entry``. The cash
and fee accounts come from the rule, or from the company's per-provider
override for ``payment.payment_provider`` (``accounting.posting.payment_accounts``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from accounting.models import DocumentPostingRule, JournalEntry
from accounting.posting import payment_accounts, post_balanced_entry, reverse_entry


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
    if is_refund and not provider:
        parent = getattr(payment, "parent_payment", None)
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
        legs = [
            (rule.receivable_account, gross, Decimal("0"), f"Refund {payment.payment_number}"),
            (cash_account, Decimal("0"), net, "Cash refunded"),
        ]
        if fee > 0:
            legs.append((cash_account, Decimal("0"), fee, "Fee refunded"))
    else:
        legs = [
            (cash_account, net, Decimal("0"), f"Payment {payment.payment_number}"),
            (rule.receivable_account, Decimal("0"), gross, "AR settled"),
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
        posting_key=f"{JournalEntry.EntrySource.PAYMENT}:{payment.payment_number}",
    )
    return reverse_entry(original, memo=f"Void of payment {payment.payment_number}")
