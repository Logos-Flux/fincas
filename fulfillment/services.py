"""Pick ticket creation, shared by the back office and the API."""

from __future__ import annotations

from django.db import IntegrityError, transaction

from .models import PickTicket, PickTicketLine


def _ship_to(order):
    """The order's shipping address as the three ship-to fields of a ticket."""
    address = getattr(order, "shipping_address", None)
    if address is None:
        return {"ship_to_address": "", "ship_to_label": "", "ship_to_attention": ""}
    return {
        "ship_to_label": address.label or "",
        "ship_to_attention": " ".join(filter(None, [address.first_name, address.last_name])),
        "ship_to_address": "\n".join(filter(None, [
            address.company_name,
            address.address_line_1,
            address.address_line_2,
            f"{address.city}, {address.state_province} {address.postal_code}",
        ])),
    }


class PickTicketRefused(Exception):
    """The ticket cannot be made as asked. ``field`` names the input at fault."""

    def __init__(self, field, message):
        super().__init__(message)
        self.field = field
        self.message = message


def pick_ticket_for_shipment(order, shipment_group, warehouse=None):
    """Find or create the pick ticket for one shipment group of an order.

    Returns ``(ticket, created)``. There is one ticket per order, shipment
    group and warehouse: a second call returns the first ticket untouched
    (the shipment row is locked while that is decided, so two callers at once
    get one ticket). With no ``warehouse`` the group's own is used, else the
    first active one of the order's company; a ticket may have none. A new
    ticket gets the order's ship-to and one line per order line in the group,
    with the warehouse's bin location for each variant.

    Raises ``PickTicketRefused`` when the shipment is not the order's, or the
    warehouse belongs to another company than the order.
    """
    from inventory.models import Warehouse, WarehouseStock
    from orders.models import OrderLineItem, ShipmentGroup

    with transaction.atomic():
        # The row alone: the lock is the "one ticket per shipment" guard.
        group = ShipmentGroup.objects.select_for_update().filter(pk=shipment_group.pk).first()
        if group is None or group.order_id != order.pk:
            raise PickTicketRefused("shipment_group", "This shipment is not on the order.")

        if warehouse is None:
            warehouse = group.warehouse or (
                Warehouse.objects.filter(is_active=True, company_id=order.company_id).first()
                if order.company_id else None
            )
        if warehouse is not None and warehouse.company_id != order.company_id:
            raise PickTicketRefused(
                "warehouse", "The warehouse belongs to another company than the order."
            )

        lookup = {"order": order, "shipment_group": group}
        if warehouse is not None:
            lookup["warehouse"] = warehouse
        existing = PickTicket.objects.filter(**lookup).first()
        if existing is not None:
            return existing, False

        ticket = _create_numbered(order=order, shipment_group=group, warehouse=warehouse, **_ship_to(order))
        order_lines = list(
            OrderLineItem.objects.filter(order=order, shipment_group=group).select_related("variant")
        )
        variant_ids = [line.variant_id for line in order_lines if line.variant_id]
        bins = {}
        if variant_ids and warehouse is not None:
            bins = dict(
                WarehouseStock.objects.filter(warehouse=warehouse, variant_id__in=variant_ids)
                .values_list("variant_id", "bin_location")
            )
        PickTicketLine.objects.bulk_create([
            PickTicketLine(
                pick_ticket=ticket,
                order_line_item=line,
                variant=line.variant,
                item_name=line.item_name,
                sku=line.sku,
                quantity=line.quantity,
                unit_cost=line.unit_cost or 0,
                bin_location=bins.get(line.variant_id, ""),
                sort_order=position,
            )
            for position, line in enumerate(order_lines)
        ])
    return ticket, True


def _create_numbered(**fields):
    """Create a ticket, retrying when another shipment's ticket took the number.

    ``PickTicket.save()`` numbers from the current maximum, so two tickets
    made at once for different shipments can pick the same PT-nnnnn.
    """
    for attempt in range(5):
        try:
            with transaction.atomic():
                return PickTicket.objects.create(**fields)
        except IntegrityError:
            if attempt == 4:
                raise
    raise AssertionError("unreachable")
