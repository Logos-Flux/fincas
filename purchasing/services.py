"""Receiving against a purchase order (R4.03).

One service for the back office ("Receive" on the purchase order) and the
API (``POST /api/v1/purchasing/purchase-orders/<pk>/receive/``): a posted
``POReceipt`` with a line per quantity received, the purchase-order lines'
``quantity_received`` / ``receiving_status`` moved on, stock on hand added
in the receiving warehouse (``WarehouseStock.save`` emits
``inventory_adjusted``), the purchase order's ``receiving_status`` and
``status`` (``receiving`` while short, ``received`` once every line is in,
``closed`` on request), and the goods / landed-cost journal entries
(``POReceipt.save`` posts a ``posted`` receipt through
``purchasing.posting.post_receipt``).

Until 2026-10-17 receiving existed only as plain CRUD on ``POReceipt`` /
``POReceiptLine`` (nothing moved the PO lines or stock), so the API
journey patched ``quantity_received`` and stock by hand and the back
office could not receive at all (release harness, docs/release-testing.md).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Iterable, Mapping

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from accounting.posting import assert_period_open
from inventory.models import Warehouse, WarehouseStock

from .models import POLineItem, POReceipt, POReceiptLine, PurchaseOrder

#: Statuses a purchase order can still be received against.
RECEIVABLE_STATUSES = frozenset(
    s for s in PurchaseOrder.Status.values
    if s not in (PurchaseOrder.Status.CLOSED, PurchaseOrder.Status.CANCELLED)
)


class ReceiptRefused(Exception):
    """The receipt cannot be recorded as asked (quantities, state, warehouse); nothing stored."""


def _q3(value) -> Decimal:
    return Decimal(value or 0).quantize(Decimal("0.001"))


def _fmt(value: Decimal) -> str:
    """A quantity for a message: ``4``, ``2.5``, never ``4.000``."""
    text = f"{value.normalize():f}"
    return text if text != "-0" else "0"


def remaining(line: POLineItem) -> Decimal:
    """Quantity still to come on a line (never negative)."""
    left = _q3(line.quantity_ordered) - _q3(line.quantity_received)
    return left if left > 0 else Decimal("0")


def receiving_warehouse(po: PurchaseOrder, warehouse: Warehouse | None = None) -> Warehouse:
    """The warehouse the goods land in: the one given, else the PO's, else
    the company's default, else its first active stock warehouse."""
    if warehouse is not None:
        if warehouse.company_id != po.company_id:
            raise ReceiptRefused("The warehouse belongs to another company.")
        return warehouse
    if po.warehouse_id and po.warehouse.company_id == po.company_id:
        return po.warehouse
    candidates = Warehouse.objects.filter(company=po.company, is_active=True)
    found = (
        candidates.filter(default_for_company=po.company).first()
        or candidates.filter(accepts_incoming=True).order_by("warehouse_code").first()
    )
    if found is None:
        raise ReceiptRefused(
            "No warehouse to receive into: give the purchase order a warehouse, or set one up "
            "under Inventory > Warehouses."
        )
    return found


def receive_purchase_order(
    po: PurchaseOrder,
    quantities: Mapping | Iterable[tuple],
    *,
    warehouse: Warehouse | None = None,
    received_by=None,
    notes: str = "",
    carrier: str = "",
    tracking_number: str = "",
    close_when_complete: bool = False,
    idempotency_key: str = "",
) -> POReceipt:
    """Record a (partial) delivery against ``po``; return the posted receipt.

    ``quantities`` maps a ``POLineItem`` (or its pk) to the quantity received
    now; zero and missing lines are left alone. Refuses with
    ``ReceiptRefused`` when nothing is received, a line is not the PO's, a
    quantity is negative or exceeds what is still to come, the PO is closed
    or cancelled, or no warehouse can be found; a closed fiscal period
    raises ``ValidationError`` before anything is written.

    ``idempotency_key`` names the attempt (the form's ``_once`` token; API
    callers send their own): a retry returns the original receipt.
    """
    if hasattr(quantities, "items"):
        quantities = list(quantities.items())
    wanted = {}
    for line, qty in quantities:
        qty = _q3(qty)
        if qty < 0:
            raise ReceiptRefused("A received quantity cannot be negative.")
        if qty == 0:
            continue
        wanted[str(getattr(line, "pk", line))] = qty
    if not wanted:
        raise ReceiptRefused("Nothing received: enter a quantity on at least one line.")

    with transaction.atomic():
        if idempotency_key:
            existing = (
                POReceipt.objects.select_for_update()
                .filter(purchase_order=po, metadata__idempotency_key=idempotency_key)
                .first()
            )
            if existing is not None:
                return existing
        # Lock the row alone: warehouse and company are nullable FKs, and
        # Postgres refuses FOR UPDATE on the nullable side of an outer join.
        po = PurchaseOrder.objects.select_for_update().get(pk=po.pk)
        if po.status not in RECEIVABLE_STATUSES:
            raise ReceiptRefused(f"{po.po_number} is {po.get_status_display().lower()}; nothing can be received against it.")
        lines = {str(line.pk): line for line in po.line_items.select_for_update().select_related("variant")}
        unknown = set(wanted) - set(lines)
        if unknown:
            raise ReceiptRefused("A line is not on this purchase order.")
        for pk, qty in wanted.items():
            line = lines[pk]
            left = remaining(line)
            if qty > left:
                raise ReceiptRefused(
                    f"{line.our_sku or line.item_name}: {_fmt(qty)} received is more than the "
                    f"{_fmt(left)} still to come."
                )
        target = receiving_warehouse(po, warehouse)
        now = timezone.now()
        assert_period_open(po.company, now.date(), what=f"A receipt against {po.po_number}")

        receipt = POReceipt(
            purchase_order=po, warehouse=target, received_at=now, received_by=received_by,
            notes=notes or "", carrier=carrier or "", tracking_number=tracking_number or "",
            metadata={"idempotency_key": idempotency_key} if idempotency_key else {},
        )
        receipt.status = POReceipt.Status.DRAFT
        receipt.save()  # the number first; posting needs the lines
        for pk, qty in wanted.items():
            line = lines[pk]
            POReceiptLine.objects.create(
                receipt=receipt, po_line_item=line, quantity_received=qty,
                inspection_status=POReceiptLine.InspectionStatus.PASSED,
            )
            received = _q3(line.quantity_received) + qty
            ordered = _q3(line.quantity_ordered)
            if received > ordered:
                status = POLineItem.ReceivingStatus.OVER_RECEIVED
            elif received == ordered:
                status = POLineItem.ReceivingStatus.RECEIVED
            else:
                status = POLineItem.ReceivingStatus.PARTIAL
            # .update(): quantity_received is not a totals field, and the
            # line signal would only recalculate what has not changed.
            POLineItem.objects.filter(pk=pk).update(
                quantity_received=received, receiving_status=status, updated_at=now,
            )
            line.quantity_received, line.receiving_status = received, status
            stock, _ = WarehouseStock.objects.get_or_create(variant=line.variant, warehouse=target)
            stock.quantity = F("quantity") + qty
            stock.last_stock_update = now
            stock.save(update_fields=["quantity", "last_stock_update", "updated_at"])

        complete = all(remaining(line) == 0 for line in lines.values())
        po.receiving_status = (
            PurchaseOrder.ReceivingStatus.RECEIVED if complete else PurchaseOrder.ReceivingStatus.PARTIAL
        )
        po.status = PurchaseOrder.Status.RECEIVED if complete else PurchaseOrder.Status.RECEIVING
        po.save(update_fields=["receiving_status", "status", "updated_at"])
        # Posting last: the goods and landed-cost entries read the lines.
        receipt.status = POReceipt.Status.POSTED
        receipt.save(update_fields=["status", "updated_at"])
        if complete and close_when_complete:
            po.transition_to(PurchaseOrder.Status.CLOSED)
    return receipt
