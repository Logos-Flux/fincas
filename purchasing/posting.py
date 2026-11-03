"""Purchase-receipt GL postings (Step 1a).

Goods value posts under PURCHASE (DR inventory / CR AP); the allocated
freight/duty slice posts separately under LANDED_COST so the landed-cost
trail is auditable. Per-line landed basis is ``landed_cost_per_unit`` minus
``unit_cost`` (see ``PurchaseOrder.recalculate_totals``).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional, Tuple

from accounting.models import DocumentPostingRule, JournalEntry
from accounting.posting import post_balanced_entry, rule_for


def _q2(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def post_receipt(receipt) -> Tuple[JournalEntry, Optional[JournalEntry]]:
    """Post goods entry; plus a landed-cost entry when the landed slice > 0."""
    po = receipt.purchase_order
    rule = rule_for(po.company, DocumentPostingRule.DocType.PURCHASE_RECEIPT)
    supplier = getattr(po, "supplier", None)
    entity = (
        getattr(supplier, "company_name", "") or str(supplier or "")
    )[:255]
    when = receipt.received_at.date() if receipt.received_at else date.today()

    goods = Decimal("0")
    landed = Decimal("0")
    for line in receipt.lines.select_related("po_line_item").all():
        qty = Decimal(line.quantity_received or 0)
        if qty <= 0:
            continue
        po_line = line.po_line_item
        ordered = Decimal(po_line.quantity_ordered or 0)
        if ordered > 0:
            goods += _q2(
                Decimal(po_line.line_total or 0) * qty / ordered
            )
        else:
            goods += _q2(Decimal(po_line.unit_cost or 0) * qty)
        landed += _q2(
            (Decimal(po_line.landed_cost_per_unit or 0)
             - Decimal(po_line.unit_cost or 0)) * qty
        )
    goods = _q2(goods)
    landed = _q2(landed)

    goods_entry = post_balanced_entry(
        company=po.company,
        entry_date=when,
        entry_source=JournalEntry.EntrySource.PURCHASE,
        source_reference=receipt.receipt_number,
        memo=f"Goods receipt {receipt.receipt_number}",
        entity_name=entity,
        legs=[
            (rule.inventory_account, goods, Decimal("0"), "Inventory received"),
            (rule.payable_account, Decimal("0"), goods, "AP accrued"),
        ],
    )
    landed_entry = None
    if landed > 0:
        landed_entry = post_balanced_entry(
            company=po.company,
            entry_date=when,
            entry_source=JournalEntry.EntrySource.LANDED_COST,
            source_reference=receipt.receipt_number,
            memo=f"Landed cost {receipt.receipt_number}",
            entity_name=entity,
            legs=[
                (
                    rule.inventory_account,
                    landed,
                    Decimal("0"),
                    "Landed cost capitalized",
                ),
                (
                    rule.payable_account,
                    Decimal("0"),
                    landed,
                    "Landed AP accrued",
                ),
            ],
        )
    return goods_entry, landed_entry
