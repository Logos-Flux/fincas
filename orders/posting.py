"""Sales-invoice GL postings (Step 1a).

Builds legs from the invoice snapshot and passes them into
``accounting.posting.post_balanced_entry`` — this module owns the debit/credit
design, the choke-point owns balance/period/company enforcement.
"""

from __future__ import annotations

from decimal import Decimal

from accounting.models import DocumentPostingRule, JournalEntry
from accounting.posting import post_balanced_entry, reverse_entry, rule_for


def _entity_name(invoice) -> str:
    order = getattr(invoice, "order", None)
    customer = getattr(order, "customer", None)
    return (
        getattr(customer, "company_name", "") or str(customer or "")
    )[:255]


def post_sales_invoice(invoice) -> JournalEntry | None:
    """DR AR (grand); CR revenue + tax + shipping; DR discount (if any).

    A zero-total invoice posts nothing (None) — a $0 entry would be rejected
    as lineless anyway, and zero-value documents have no financial effect.
    """
    rule = rule_for(invoice.company, DocumentPostingRule.DocType.SALES_INVOICE)
    subtotal = Decimal(invoice.subtotal_ex_tax or 0)
    tax = Decimal(invoice.tax_total or 0)
    shipping = Decimal(invoice.shipping_cost or 0)
    grand = Decimal(invoice.grand_total or 0)
    if not (subtotal or tax or shipping or grand):
        return None
    discount = subtotal + tax + shipping - grand

    legs = [
        (rule.receivable_account, grand, Decimal("0"), f"Invoice {invoice.invoice_number}"),
        (rule.revenue_account, Decimal("0"), subtotal, "Revenue"),
        (rule.tax_account, Decimal("0"), tax, "Sales tax payable"),
        (
            rule.shipping_income_account,
            Decimal("0"),
            shipping,
            "Shipping charged",
        ),
    ]
    if discount > 0:
        legs.append(
            (rule.discount_account, discount, Decimal("0"), "Order discounts")
        )
    elif discount < 0:
        legs.append(
            (
                rule.discount_account,
                Decimal("0"),
                -discount,
                "Rounding/credit adjustment",
            )
        )
    return post_balanced_entry(
        company=invoice.company,
        entry_date=invoice.invoice_date,
        entry_source=JournalEntry.EntrySource.ORDER,
        source_reference=invoice.invoice_number,
        memo=f"Sales invoice {invoice.invoice_number}",
        entity_name=_entity_name(invoice),
        legs=legs,
    )


def reverse_sales_invoice(invoice) -> JournalEntry:
    """Counter-entry for a voided/cancelled invoice. Never edits."""
    original = JournalEntry.objects.get(
        company=invoice.company,
        posting_key=f"{JournalEntry.EntrySource.ORDER}:{invoice.invoice_number}",
    )
    return reverse_entry(original, memo=f"Void of invoice {invoice.invoice_number}")
