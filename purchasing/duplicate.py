"""Duplicate helper for PurchaseOrder.

Mirrors the pattern in orders/duplicate.py: reset status/dates/receiving
counts to draft, mint a fresh PO number, link amended_from → source.
Copies every line item with received/rejected quantities zeroed.
"""

from __future__ import annotations

from django.db import transaction

from .models import POLineItem, PurchaseOrder

_PO_RESET = {
    "status": PurchaseOrder.Status.DRAFT,
    "status_note": "",
    "receiving_status": PurchaseOrder.ReceivingStatus.NOT_RECEIVED,
    "received_qty_total": 0,
    "payment_status": PurchaseOrder.PaymentStatus.UNPAID,
    "amount_paid": 0,
    "balance_due": 0,
    "vendor_invoice_number": "",
    "vendor_invoice_date": None,
    "vendor_invoice_url": "",
    "sent_at": None,
    "sent_via": "",
    "acknowledged_at": None,
    "actual_ship_date": None,
    "actual_delivery_date": None,
    "tracking_number": "",
    "tracking_url": "",
    "bol_number": "",
    "package_count": 0,
    "approved_by": None,
    "approved_at": None,
    "po_document_url": "",
    "vendor_quote_reference": "",
    "vendor_order_number": "",
    "receiving_notes": "",
    # Source-order links cleared — duplicate is independent of the
    # original demand it was raised against.
    "sub_order": None,
    "order": None,
    "shipment_group": None,
    "customer_name_snapshot": "",
}

_PO_LINE_RESET = {
    "quantity_received": 0,
    "quantity_rejected": 0,
    "quantity_returned": 0,
    "receiving_status": POLineItem.ReceivingStatus.NOT_RECEIVED,
    "actual_received_date": None,
    "order_line_item": None,
    "customer_order_number": "",
}


def duplicate_purchase_order(source: PurchaseOrder) -> PurchaseOrder:
    """Create a draft PurchaseOrder that mirrors `source`.

    `po_date` is auto_now_add on the model so it's set to "now"
    automatically when save() runs on the new row.
    """
    with transaction.atomic():
        new_po = PurchaseOrder.objects.get(pk=source.pk)
        new_po.pk = None
        new_po.id = None
        new_po._state.adding = True
        for field, value in _PO_RESET.items():
            setattr(new_po, field, value)
        new_po.amended_from = source
        new_po.po_number = ""  # let save() mint a fresh PO-NNNNN
        new_po.save()

        for line in source.line_items.all():
            new_line = POLineItem.objects.get(pk=line.pk)
            new_line.pk = None
            new_line.id = None
            new_line._state.adding = True
            for field, value in _PO_LINE_RESET.items():
                setattr(new_line, field, value)
            new_line.purchase_order = new_po
            new_line.save()

    return new_po
