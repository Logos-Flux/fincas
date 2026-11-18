"""Suppliers, purchase orders, PO line items, and receipts."""

from __future__ import annotations

from typing import Any

from ._writable_fields import (
    PO_LINE_WRITABLE,
    PO_RECEIPT_WRITABLE,
    PURCHASE_ORDER_WRITABLE,
    SUPPLIER_WRITABLE,
    validate_fields,
)
from .client import ERPClient, ERPNotFoundError, ERPValidationError

SUPPLIERS_URL = "/api/v1/purchasing/suppliers/"
PURCHASE_ORDERS_URL = "/api/v1/purchasing/purchase-orders/"
PO_LINES_URL = "/api/v1/purchasing/po-lines/"
RECEIPTS_URL = "/api/v1/purchasing/receipts/"


# ---- Suppliers -------------------------------------------------------------


async def list_suppliers(
    client: ERPClient,
    *,
    search: str | None = None,
    supplier_type: str | None = None,
    is_active: bool | None = None,
    is_approved: bool | None = None,
    company: str | None = None,
    ordering: str | None = None,
    limit: int | None = 50,
) -> list[dict[str, Any]]:
    """List suppliers. `search` matches supplier_code and company_name."""
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if search:
        params["search"] = search
    if supplier_type:
        params["supplier_type"] = supplier_type
    if is_active is not None:
        params["is_active"] = str(is_active).lower()
    if is_approved is not None:
        params["is_approved"] = str(is_approved).lower()
    if company:
        params["company"] = company
    if ordering:
        params["ordering"] = ordering
    return await client.list(SUPPLIERS_URL, params=params, max_items=limit)


async def find_supplier(client: ERPClient, query: str) -> dict[str, Any]:
    """Find a supplier by code or company name. Returns the match envelope."""
    hits = await client.list(
        SUPPLIERS_URL, params={"search": query, "page_size": 10}, max_items=10
    )
    if not hits:
        raise ERPNotFoundError(f"No supplier found matching '{query}'")
    q = query.lower()
    for h in hits:
        if (
            (h.get("supplier_code") or "").lower() == q
            or (h.get("company_name") or "").lower() == q
        ):
            return {"match_quality": "exact", "item": h}
    return {"match_quality": "fuzzy", "item": hits[0]}


async def get_supplier(client: ERPClient, supplier_id: str) -> dict[str, Any]:
    return await client.get(f"{SUPPLIERS_URL}{supplier_id}/")


async def create_supplier(
    client: ERPClient,
    *,
    company_name: str,
    supplier_code: str,
    supplier_type: str = "manufacturer",
    is_active: bool = True,
    primary_contact_email: str | None = None,
    primary_contact_name: str | None = None,
    primary_contact_phone: str | None = None,
    website: str | None = None,
    payment_terms: str | None = None,
    default_lead_time_days: int | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new supplier."""
    payload: dict[str, Any] = {
        "company_name": company_name,
        "supplier_code": supplier_code,
        "supplier_type": supplier_type,
        "is_active": is_active,
    }
    optional = {
        "primary_contact_email": primary_contact_email,
        "primary_contact_name": primary_contact_name,
        "primary_contact_phone": primary_contact_phone,
        "website": website,
        "payment_terms": payment_terms,
        "default_lead_time_days": default_lead_time_days,
    }
    for k, v in optional.items():
        if v is not None:
            payload[k] = v
    if extra_fields:
        payload.update(extra_fields)
    return await client.post(SUPPLIERS_URL, json=payload)


async def update_supplier(
    client: ERPClient, supplier_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on a supplier. Unknown fields raise."""
    validate_fields(fields, SUPPLIER_WRITABLE, "supplier")
    return await client.patch(f"{SUPPLIERS_URL}{supplier_id}/", json=fields)


# ---- Purchase orders -------------------------------------------------------


async def list_purchase_orders(
    client: ERPClient,
    *,
    search: str | None = None,
    supplier: str | None = None,
    status: str | None = None,
    po_type: str | None = None,
    receiving_status: str | None = None,
    company: str | None = None,
    ordering: str | None = None,
    limit: int | None = 20,
) -> list[dict[str, Any]]:
    """List purchase orders.

    `search` matches po_number, supplier company_name, and tracking_number.
    `ordering` examples: "-po_date", "-grand_total".
    """
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if search:
        params["search"] = search
    if supplier:
        params["supplier"] = supplier
    if status:
        params["status"] = status
    if po_type:
        params["po_type"] = po_type
    if receiving_status:
        params["receiving_status"] = receiving_status
    if company:
        params["company"] = company
    if ordering:
        params["ordering"] = ordering
    return await client.list(PURCHASE_ORDERS_URL, params=params, max_items=limit)


async def get_purchase_order(client: ERPClient, po_id: str) -> dict[str, Any]:
    """Fetch full PO detail (includes nested line items)."""
    return await client.get(f"{PURCHASE_ORDERS_URL}{po_id}/")


async def update_purchase_order(
    client: ERPClient, po_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on an existing PO. Unknown fields raise.

    Common edits: status_note, vendor_estimated_ship_date, tracking_number,
    carrier, actual_ship_date, actual_delivery_date, internal_notes, vendor_notes.

    A ``status`` key is NOT patched directly: it is routed through the
    guarded transition action (``POST .../transition/``), so illegal moves
    fail with the DRF field-error envelope instead of silently applying.
    """
    fields = dict(fields)
    status = fields.pop("status", None)
    validate_fields(fields, PURCHASE_ORDER_WRITABLE, "purchase order")
    result = None
    if status is not None:
        result = await transition_purchase_order(client, po_id, status)
    if fields:
        result = await client.patch(f"{PURCHASE_ORDERS_URL}{po_id}/", json=fields)
    return result if result is not None else await get_purchase_order(client, po_id)


async def transition_purchase_order(
    client: ERPClient, po_id: str, status: str
) -> dict[str, Any]:
    """Apply a guarded status transition to a purchase order.

    Calls POST /api/v1/purchasing/purchase-orders/{id}/transition/ with
    body ``{"status": <target>}``. The target must be a legal move per the
    PurchaseOrder transition map (draft → sent → acknowledged → ...,
    with closed/cancelled terminal); illegal moves raise
    ERPValidationError with the server's field errors. Returns the
    refreshed PO detail.
    """
    return await client.post(
        f"{PURCHASE_ORDERS_URL}{po_id}/transition/", json={"status": status}
    )


async def recalculate_purchase_order_totals(
    client: ERPClient, po_id: str
) -> dict[str, Any]:
    """Trigger a server-side recalculation of aggregate fields on a PO.

    Calls POST /api/v1/purchasing/purchase-orders/{id}/recalculate/.
    Re-derives line totals and sums to subtotal, grand_total, and
    balance_due. Returns the refreshed PO detail.
    """
    return await client.post(
        f"{PURCHASE_ORDERS_URL}{po_id}/recalculate/", json={}
    )


_PO_LINE_INPUT = frozenset({
    "variant", "supplier_product", "item_name", "supplier_sku", "our_sku",
    "description", "quantity_ordered", "purchasing_uom", "qty_per_uom",
    "unit_cost", "list_cost", "discount_percent", "discount_amount",
    "duty_amount", "order_line_item", "customer_order_number",
    "requested_delivery_date", "sort_order", "notes", "metadata",
})


async def _po_line_payload(client: ERPClient, line: dict[str, Any]) -> dict[str, Any]:
    """Validate one PO line before anything is written.

    A PO line must name a catalog ``variant`` (the column is required: there
    is no free-form PO line), ``quantity_ordered`` and ``unit_cost``.
    ``item_name`` and ``our_sku`` default from the variant.
    """
    payload = {k: v for k, v in line.items() if v is not None}
    if "quantity" in payload and "quantity_ordered" not in payload:
        payload["quantity_ordered"] = payload.pop("quantity")
    unknown = sorted(set(payload) - _PO_LINE_INPUT)
    if unknown:
        raise ERPValidationError(
            f"Unknown field(s) on PO line: {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(_PO_LINE_INPUT))}.",
            details={k: ["Unknown field."] for k in unknown},
        )
    missing = [k for k in ("variant", "quantity_ordered", "unit_cost") if payload.get(k) in (None, "")]
    if missing:
        raise ERPValidationError(
            f"PO line needs {', '.join(missing)}. A PO line always names a catalog "
            "variant; create the product first if the item is new.",
            details={k: ["This field is required."] for k in missing},
        )
    if not payload.get("item_name") or not payload.get("our_sku"):
        variant = await client.get(f"/api/v1/products/variants/{payload['variant']}/")
        payload.setdefault("item_name", variant.get("name") or variant.get("sku"))
        payload.setdefault("our_sku", variant.get("sku"))
    return payload


async def create_purchase_order(
    client: ERPClient,
    *,
    supplier: str,
    line_items: list[dict[str, Any]] | None = None,
    po_type: str | None = None,
    warehouse: str | None = None,
    order: str | None = None,
    requested_delivery_date: str | None = None,
    vendor_payment_terms: str | None = None,
    shipping_cost: str | None = None,
    ship_to_address: str | None = None,
    vendor_notes: str | None = None,
    internal_notes: str | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a draft purchase order, with its lines, and return the detail.

    ``supplier`` is a supplier UUID (resolve a name with ``find_supplier``
    first). The PO is created in the supplier's company and ``po_number`` is
    server-assigned. Every line is validated before the header is written;
    lines go to /po-lines/ and /recalculate/ then derives the totals. The PO
    stays in draft: sending, approving and receiving are separate steps.
    """
    lines = [await _po_line_payload(client, line) for line in (line_items or [])]
    payload: dict[str, Any] = {"supplier": supplier}
    optional = {
        "po_type": po_type,
        "warehouse": warehouse,
        "order": order,
        "requested_delivery_date": requested_delivery_date,
        "vendor_payment_terms": vendor_payment_terms,
        "shipping_cost": shipping_cost,
        "ship_to_address": ship_to_address,
        "vendor_notes": vendor_notes,
        "internal_notes": internal_notes,
    }
    payload.update({k: v for k, v in optional.items() if v is not None})
    if extra_fields:
        validate_fields(extra_fields, PURCHASE_ORDER_WRITABLE, "purchase order")
        payload.update(extra_fields)
    if "company" not in payload:
        company = (await get_supplier(client, supplier)).get("company")
        if company:
            payload["company"] = str(company)

    po = await client.post(PURCHASE_ORDERS_URL, json=payload)
    po_id = po["id"]
    for line in lines:
        await client.post(PO_LINES_URL, json={**line, "purchase_order": po_id})
    if lines:
        po = await client.post(f"{PURCHASE_ORDERS_URL}{po_id}/recalculate/", json={})
    return po


async def create_po_line(
    client: ERPClient, po_id: str, line: dict[str, Any]
) -> dict[str, Any]:
    """Add one line to an existing purchase order and recalculate. Returns the PO."""
    payload = await _po_line_payload(client, line)
    await client.post(PO_LINES_URL, json={**payload, "purchase_order": po_id})
    return await client.post(f"{PURCHASE_ORDERS_URL}{po_id}/recalculate/", json={})


async def delete_po_line(client: ERPClient, line_id: str) -> dict[str, Any]:
    """Delete one PO line and recalculate its purchase order. Returns the PO."""
    line = await get_po_line(client, line_id)
    await client.delete(f"{PO_LINES_URL}{line_id}/")
    return await client.post(
        f"{PURCHASE_ORDERS_URL}{line['purchase_order']}/recalculate/", json={}
    )


async def duplicate_purchase_order(client: ERPClient, po_id: str) -> dict[str, Any]:
    """Duplicate a PO. Returns the new PO with a fresh po_number.

    Calls POST /api/v1/purchasing/purchase-orders/{id}/duplicate/. The new
    PO starts as a draft with received/rejected counts zeroed, no tracking
    or invoice refs, and amended_from pointing back at the source.
    """
    return await client.post(f"{PURCHASE_ORDERS_URL}{po_id}/duplicate/", json={})


# ---- PO line items ---------------------------------------------------------


async def list_po_lines(
    client: ERPClient,
    *,
    purchase_order: str | None = None,
    receiving_status: str | None = None,
    search: str | None = None,
    limit: int | None = 50,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if purchase_order:
        params["purchase_order"] = purchase_order
    if receiving_status:
        params["receiving_status"] = receiving_status
    if search:
        params["search"] = search
    return await client.list(PO_LINES_URL, params=params, max_items=limit)


async def get_po_line(client: ERPClient, line_id: str) -> dict[str, Any]:
    return await client.get(f"{PO_LINES_URL}{line_id}/")


async def update_po_line(
    client: ERPClient, line_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on a PO line. Unknown fields raise.

    Common edits: quantity_received, quantity_rejected, actual_received_date,
    receiving_status, unit_cost, duty_amount, notes. Landed allocation
    (freight_allocation, other_landed_costs, landed_cost_per_unit) is
    server-computed by recalculate_totals() — patch the inputs and call
    POST .../recalculate/ instead.
    """
    validate_fields(fields, PO_LINE_WRITABLE, "PO line")
    return await client.patch(f"{PO_LINES_URL}{line_id}/", json=fields)


# ---- Receipts --------------------------------------------------------------


async def list_receipts(
    client: ERPClient,
    *,
    purchase_order: str | None = None,
    status: str | None = None,
    condition_on_arrival: str | None = None,
    search: str | None = None,
    limit: int | None = 20,
) -> list[dict[str, Any]]:
    """List PO receipts."""
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if purchase_order:
        params["purchase_order"] = purchase_order
    if status:
        params["status"] = status
    if condition_on_arrival:
        params["condition_on_arrival"] = condition_on_arrival
    if search:
        params["search"] = search
    return await client.list(RECEIPTS_URL, params=params, max_items=limit)


async def get_receipt(client: ERPClient, receipt_id: str) -> dict[str, Any]:
    """Fetch full receipt detail including nested receipt lines."""
    return await client.get(f"{RECEIPTS_URL}{receipt_id}/")


async def create_receipt(
    client: ERPClient,
    *,
    purchase_order: str,
    warehouse: str | None = None,
    status: str = "received",
    carrier: str | None = None,
    tracking_number: str | None = None,
    bol_number: str | None = None,
    package_count: int | None = None,
    condition_on_arrival: str | None = None,
    notes: str | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new PO receipt.

    This is a standard POST to /receipts/ — not a custom action. Link to the PO
    via the `purchase_order` UUID. Receipt lines can be created separately or
    via the same payload if the backend supports nested writes.
    """
    payload: dict[str, Any] = {
        "purchase_order": purchase_order,
        "status": status,
    }
    optional = {
        "warehouse": warehouse,
        "carrier": carrier,
        "tracking_number": tracking_number,
        "bol_number": bol_number,
        "package_count": package_count,
        "condition_on_arrival": condition_on_arrival,
        "notes": notes,
    }
    for k, v in optional.items():
        if v is not None:
            payload[k] = v
    if extra_fields:
        payload.update(extra_fields)
    return await client.post(RECEIPTS_URL, json=payload)


async def update_receipt(
    client: ERPClient, receipt_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on a receipt. Unknown fields raise."""
    validate_fields(fields, PO_RECEIPT_WRITABLE, "PO receipt")
    return await client.patch(f"{RECEIPTS_URL}{receipt_id}/", json=fields)
