"""Pick tickets: the warehouse's instruction to pick and pack one shipment."""

from __future__ import annotations

from typing import Any

from ._writable_fields import validate_fields
from .client import ERPClient, ERPValidationError

PICK_TICKETS_URL = "/api/v1/fulfillment/pick-tickets/"
SHIPMENT_GROUPS_URL = "/api/v1/orders/shipment-groups/"

PICK_TICKET_WRITABLE = frozenset({
    "assigned_to", "notes", "ship_to_address", "ship_to_label", "ship_to_attention",
    "status", "metadata",
})


async def list_pick_tickets(
    client: ERPClient,
    *,
    order: str | None = None,
    shipment_group: str | None = None,
    status: str | None = None,
    warehouse: str | None = None,
    search: str | None = None,
    limit: int | None = 20,
) -> list[dict[str, Any]]:
    """List pick tickets, newest first. `search` matches the ticket number."""
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    for key, value in (
        ("order", order), ("shipment_group", shipment_group), ("status", status),
        ("warehouse", warehouse), ("search", search),
    ):
        if value:
            params[key] = value
    return await client.list(PICK_TICKETS_URL, params=params, max_items=limit)


async def get_pick_ticket(client: ERPClient, pick_ticket_id: str) -> dict[str, Any]:
    """Fetch a pick ticket with its lines (sku, quantity, bin location)."""
    return await client.get(f"{PICK_TICKETS_URL}{pick_ticket_id}/")


async def create_pick_ticket(
    client: ERPClient,
    *,
    order: str,
    shipment_group: str,
    warehouse: str | None = None,
) -> dict[str, Any]:
    """Create the pick ticket for one shipment of an order, with its lines.

    One ticket per shipment and warehouse: asking again returns the ticket
    that exists (``already_existed`` true) and changes nothing. ``warehouse``
    defaults to the shipment's own. Only for an ``internal`` shipment.
    """
    group = await client.get(f"{SHIPMENT_GROUPS_URL}{shipment_group}/")
    if group.get("fulfillment_type") != "internal":
        raise ERPValidationError(
            "This shipment is a drop ship (a vendor ships it), so it gets a purchase order, "
            "not a pick ticket. Change it to internal with a warehouse first if we ship it.",
            details={"shipment_group": ["Shipment is not internal."]},
        )
    payload = {"order": order, "shipment_group": shipment_group}
    if warehouse:
        payload["warehouse"] = warehouse
    before = {
        str(t["id"]) for t in await list_pick_tickets(
            client, order=order, shipment_group=shipment_group, limit=None
        )
    }
    ticket = await client.post(f"{PICK_TICKETS_URL}for-shipment/", json=payload)
    ticket["already_existed"] = str(ticket["id"]) in before
    return ticket


async def transition_pick_ticket(
    client: ERPClient, pick_ticket_id: str, status: str
) -> dict[str, Any]:
    """Move a pick ticket one step: created, assigned, picking, picked, packing, packed, shipped."""
    return await client.post(
        f"{PICK_TICKETS_URL}{pick_ticket_id}/transition/", json={"status": status}
    )


async def update_pick_ticket(
    client: ERPClient, pick_ticket_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch a pick ticket (notes, assigned_to, ship-to). A ``status`` key goes
    through the guarded transition. Unknown fields raise."""
    validate_fields(fields, PICK_TICKET_WRITABLE, "pick ticket")
    fields = dict(fields)
    status = fields.pop("status", None)
    result = None
    if fields:
        result = await client.patch(f"{PICK_TICKETS_URL}{pick_ticket_id}/", json=fields)
    if status:
        result = await transition_pick_ticket(client, pick_ticket_id, status)
    return result if result is not None else await get_pick_ticket(client, pick_ticket_id)
