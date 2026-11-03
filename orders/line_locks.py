"""Order lines that must stay in their shipment group.

Shared by the order editor's save path (``orders.erp_views``) and the line
formset (``orders.forms``), which refuses to delete a locked line.
"""

from __future__ import annotations

from .models import OrderLineItem, SubOrder

#: SubOrder states in which the goods are labelled, picked or already gone.
_SUBORDER_LOCKED_STATES = frozenset({
    SubOrder.Status.LABEL_CREATED,
    SubOrder.Status.PICKED,
    SubOrder.Status.PACKED,
    SubOrder.Status.SHIPPED,
    SubOrder.Status.IN_TRANSIT,
    SubOrder.Status.DELIVERED,
    SubOrder.Status.RETURNED,
})


def _qty(value):
    """A quantity without trailing zeros: Decimal("2.000") -> "2"."""
    return f"{value.normalize():f}"


def order_line_move_locks(order):
    """Order lines that must stay in their shipment group, with the reason.

    Returns ``{line pk: {"reason": str, "group_id": pk or None}}``. A line is
    locked once fulfillment has started on it: some of it has shipped, its
    sub-order is labelled, picked or gone, its fulfillment status has left
    "pending", or it sits on a purchase order (not cancelled) or a pick
    ticket. Those documents were built from the shipment the line was in, so
    moving it would leave them describing a different shipment than the
    order does. Always read from the database, never from the POST.

    Three queries for an order with lines, one for an order without.
    """
    if order is None or order.pk is None or order._state.adding:
        return {}
    from fulfillment.models import PickTicketLine
    from purchasing.models import POLineItem, PurchaseOrder

    rows = list(
        OrderLineItem.objects.filter(order=order).values_list(
            "pk", "quantity", "quantity_shipped", "fulfillment_status",
            "shipment_group_id", "sub_order__status", "sub_order__sub_order_number",
        )
    )
    if not rows:
        return {}
    line_ids = [r[0] for r in rows]
    on_po = dict(
        POLineItem.objects.filter(order_line_item_id__in=line_ids)
        .exclude(purchase_order__status=PurchaseOrder.Status.CANCELLED)
        .values_list("order_line_item_id", "purchase_order__po_number")
    )
    on_pt = dict(
        PickTicketLine.objects.filter(order_line_item_id__in=line_ids)
        .values_list("order_line_item_id", "pick_ticket__ticket_number")
    )
    status_labels = dict(OrderLineItem.FulfillmentStatus.choices)
    suborder_labels = dict(SubOrder.Status.choices)

    locks = {}
    for pk, qty, shipped, ff_status, group_id, so_status, so_number in rows:
        if shipped and shipped > 0:
            reason = f"Already shipped ({_qty(shipped)} of {_qty(qty)})."
        elif so_status in _SUBORDER_LOCKED_STATES:
            label = suborder_labels.get(so_status, so_status).lower()
            reason = f"Its shipment {so_number} is {label}."
        elif ff_status and ff_status != OrderLineItem.FulfillmentStatus.PENDING:
            reason = f"Fulfillment has started ({status_labels.get(ff_status, ff_status)})."
        elif pk in on_po:
            reason = (
                f"It is on purchase order {on_po[pk]}, raised for this shipment. "
                "Take it off the PO to move it."
            )
        elif pk in on_pt:
            reason = (
                f"It is on pick ticket {on_pt[pk]}, printed for this shipment. "
                "Take it off the pick ticket to move it."
            )
        else:
            continue
        locks[pk] = {"reason": reason, "group_id": group_id}
    return locks


