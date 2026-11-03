"""Quotes, orders, line items, and sub-orders.

SalesInvoice isn't exposed in the DRF API, so we skip it here. Writes are
limited to note/misc updates and order confirmation — full order creation
usually goes through the storefront checkout or quote→order conversion flow
and shouldn't be done via natural-language tooling. Status changes go
through the guarded ``POST .../transition/`` action (never a bare PATCH):
the model's transition map decides what's legal and illegal moves return
the DRF field-error envelope.

Quote → Order conversion IS a custom DRF action as of 2026-08-21:
POST /api/v1/orders/quotes/{id}/convert-to-order/. It is idempotent — a
quote that already converted returns its existing order with
`created: false` rather than minting a duplicate — and it copies line
items and shipment groups, so do NOT hand-build the Order and set its
`quote` field, which is what this docstring used to advise.
"""

from __future__ import annotations

from typing import Any

from ._writable_fields import (
    ORDER_LINE_WRITABLE,
    ORDER_WRITABLE,
    QUOTE_WRITABLE,
    SUB_ORDER_WRITABLE,
    validate_fields,
)
from .client import ERPClient
from .parties import TARGET_CUSTOMER, resolve_ref

QUOTES_URL = "/api/v1/orders/quotes/"
QUOTE_LINES_URL = "/api/v1/orders/quote-lines/"
ORDERS_URL = "/api/v1/orders/orders/"
ORDER_LINES_URL = "/api/v1/orders/order-lines/"
SUB_ORDERS_URL = "/api/v1/orders/sub-orders/"
SALES_INVOICES_URL = "/api/v1/orders/sales-invoices/"


# ---- Orders ----------------------------------------------------------------


async def list_orders(
    client: ERPClient,
    *,
    search: str | None = None,
    customer: str | None = None,
    status: str | None = None,
    payment_status: str | None = None,
    fulfillment_status: str | None = None,
    order_type: str | None = None,
    sales_rep: str | None = None,
    company: str | None = None,
    ordering: str | None = None,
    limit: int | None = 20,
) -> list[dict[str, Any]]:
    """List orders.

    `search` matches order_number, customer email, and external_id.
    Status/payment_status/fulfillment_status are independent filters — you
    can have a paid order that hasn't shipped yet.
    `ordering` examples: "-order_date", "-grand_total".
    """
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if search:
        params["search"] = search
    if customer:
        params["customer"] = customer
    if status:
        params["status"] = status
    if payment_status:
        params["payment_status"] = payment_status
    if fulfillment_status:
        params["fulfillment_status"] = fulfillment_status
    if order_type:
        params["order_type"] = order_type
    if sales_rep:
        params["sales_rep"] = sales_rep
    if company:
        params["company"] = company
    if ordering:
        params["ordering"] = ordering
    return await client.list(ORDERS_URL, params=params, max_items=limit)


async def get_order(client: ERPClient, order_id: str) -> dict[str, Any]:
    """Fetch full order detail by UUID (includes nested line items and sub-orders)."""
    return await client.get(f"{ORDERS_URL}{order_id}/")


async def update_order(
    client: ERPClient, order_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on an existing order. Unknown fields raise.

    Typical fields: payment_status, fulfillment_status, status_note,
    internal_notes, sales_rep, requested_ship_date, customer_message. Full
    field list is large — see the DRF serializer for writable fields.

    A ``status`` key is NOT patched directly: it is routed through the
    guarded transition action (``POST .../transition/``), so illegal moves
    fail with the DRF field-error envelope instead of silently applying.
    The remaining fields are validated first, then the transition applies,
    then the rest is PATCHed — a typo in the other fields never leaves a
    half-transitioned order.
    """
    fields = dict(fields)
    status = fields.pop("status", None)
    validate_fields(fields, ORDER_WRITABLE, "order")
    result = None
    if status is not None:
        result = await transition_order(client, order_id, status)
    if fields:
        result = await client.patch(f"{ORDERS_URL}{order_id}/", json=fields)
    return result if result is not None else await get_order(client, order_id)


async def transition_order(
    client: ERPClient, order_id: str, status: str
) -> dict[str, Any]:
    """Apply a guarded status transition to an order.

    Calls POST /api/v1/orders/orders/{id}/transition/ with body
    ``{"status": <target>}``. The target must be a legal move per the
    Order's transition map (e.g. pending → awaiting_payment); illegal
    moves raise ERPValidationError with the server's field errors.
    Returns the refreshed order detail.
    """
    return await client.post(
        f"{ORDERS_URL}{order_id}/transition/", json={"status": status}
    )


async def update_order_status(
    client: ERPClient,
    order_id: str,
    *,
    status: str,
    status_note: str | None = None,
) -> dict[str, Any]:
    """Shortcut for the most common order write: change status (+ optional note).

    The status change goes through the guarded transition action (see
    ``transition_order``); the note, when given, is PATCHed separately
    afterwards. Returns the refreshed order detail.
    """
    result = await transition_order(client, order_id, status)
    if status_note is not None:
        result = await client.patch(
            f"{ORDERS_URL}{order_id}/", json={"status_note": status_note}
        )
    return result


async def send_order_confirmation(client: ERPClient, order_id: str) -> Any:
    """Trigger an order confirmation email.

    Uses the custom DRF action at POST /api/v1/orders/orders/{id}/send-confirmation/.
    """
    return await client.post(f"{ORDERS_URL}{order_id}/send-confirmation/", json={})


async def duplicate_order(client: ERPClient, order_id: str) -> dict[str, Any]:
    """Duplicate an order. Returns the new order with a fresh order_number.

    Calls POST /api/v1/orders/orders/{id}/duplicate/. The new order is in
    draft state with all financials zeroed and no link to the source's
    fulfillment / payment history. amended_from points back at the source.
    """
    return await client.post(f"{ORDERS_URL}{order_id}/duplicate/", json={})


async def recalculate_order_totals(
    client: ERPClient, order_id: str
) -> dict[str, Any]:
    """Trigger a server-side recalculation of aggregate fields on an Order.

    Calls POST /api/v1/orders/orders/{id}/recalculate/. Re-derives line
    totals from quantity * unit_price (- discount), then sums to subtotal,
    grand_total, and balance_due. Returns the refreshed order detail.
    """
    return await client.post(f"{ORDERS_URL}{order_id}/recalculate/", json={})


async def convert_order_to_invoice(client: ERPClient, order_id: str) -> dict[str, Any]:
    """Convert a Sales Order to a draft SalesInvoice. Idempotent.

    Calls POST /api/v1/orders/orders/{id}/convert-to-invoice/. If the order
    already has an invoice, the existing one is returned (response body
    has `"created": false`). Otherwise a new draft invoice is minted with
    a fresh invoice_number, financials snapshotted from the order, and
    payment_terms mapped from the order's free-text terms to the invoice
    enum (Net-N, Prepaid, Due on Receipt). due_date is computed from
    payment_terms when applicable.
    """
    return await client.post(
        f"{ORDERS_URL}{order_id}/convert-to-invoice/", json={}
    )


# ---- Order line items ------------------------------------------------------


async def list_order_lines(
    client: ERPClient,
    *,
    order: str | None = None,
    fulfillment_status: str | None = None,
    sub_order: str | None = None,
    search: str | None = None,
    limit: int | None = 50,
) -> list[dict[str, Any]]:
    """List order line items, optionally scoped to a specific order or sub-order."""
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if order:
        params["order"] = order
    if fulfillment_status:
        params["fulfillment_status"] = fulfillment_status
    if sub_order:
        params["sub_order"] = sub_order
    if search:
        params["search"] = search
    return await client.list(ORDER_LINES_URL, params=params, max_items=limit)


async def get_order_line(client: ERPClient, line_id: str) -> dict[str, Any]:
    return await client.get(f"{ORDER_LINES_URL}{line_id}/")


async def update_order_line(
    client: ERPClient, line_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on an order line. Unknown fields raise."""
    validate_fields(fields, ORDER_LINE_WRITABLE, "order line")
    return await client.patch(f"{ORDER_LINES_URL}{line_id}/", json=fields)


# ---- Sub-orders (shipments) ------------------------------------------------


async def list_sub_orders(
    client: ERPClient,
    *,
    order: str | None = None,
    status: str | None = None,
    is_drop_ship: bool | None = None,
    search: str | None = None,
    limit: int | None = 50,
) -> list[dict[str, Any]]:
    """List sub-orders (individual shipments from an order)."""
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if order:
        params["order"] = order
    if status:
        params["status"] = status
    if is_drop_ship is not None:
        params["is_drop_ship"] = str(is_drop_ship).lower()
    if search:
        params["search"] = search
    return await client.list(SUB_ORDERS_URL, params=params, max_items=limit)


async def get_sub_order(client: ERPClient, sub_order_id: str) -> dict[str, Any]:
    return await client.get(f"{SUB_ORDERS_URL}{sub_order_id}/")


async def update_sub_order(
    client: ERPClient, sub_order_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on a sub-order (tracking, carrier, ship dates). Unknown fields raise.

    A ``status`` key is routed through the guarded transition action
    (``POST .../transition/``) rather than patched directly.
    """
    fields = dict(fields)
    status = fields.pop("status", None)
    validate_fields(fields, SUB_ORDER_WRITABLE, "sub-order")
    result = None
    if status is not None:
        result = await transition_sub_order(client, sub_order_id, status)
    if fields:
        result = await client.patch(f"{SUB_ORDERS_URL}{sub_order_id}/", json=fields)
    return result if result is not None else await get_sub_order(client, sub_order_id)


async def transition_sub_order(
    client: ERPClient, sub_order_id: str, status: str
) -> dict[str, Any]:
    """Apply a guarded status transition to a sub-order.

    Calls POST /api/v1/orders/sub-orders/{id}/transition/. Legal moves
    follow the SubOrder transition map and roll up to the parent order;
    illegal moves raise ERPValidationError with the server's field errors.
    Returns the refreshed sub-order detail.
    """
    return await client.post(
        f"{SUB_ORDERS_URL}{sub_order_id}/transition/", json={"status": status}
    )


# ---- Quotes ----------------------------------------------------------------


async def list_quotes(
    client: ERPClient,
    *,
    search: str | None = None,
    customer: str | None = None,
    status: str | None = None,
    sales_stage: str | None = None,
    sales_rep: str | None = None,
    company: str | None = None,
    ordering: str | None = None,
    limit: int | None = 20,
) -> list[dict[str, Any]]:
    """List quotes.

    `search` matches quote_number and customer email.
    `sales_stage` filters by the deal stage on the quote's engagement, in the
    old vocabulary: prospecting / qualification / needs_analysis /
    proposal_sent / negotiation / verbal_commit / closed_won / closed_lost
    (deprecated with the rest of the quote pipeline fields, CRM Phase 3).
    """
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if search:
        params["search"] = search
    if customer:
        params["customer"] = customer
    if status:
        params["status"] = status
    if sales_stage:
        params["sales_stage"] = sales_stage
    if sales_rep:
        params["sales_rep"] = sales_rep
    if company:
        params["company"] = company
    if ordering:
        params["ordering"] = ordering
    return await client.list(QUOTES_URL, params=params, max_items=limit)


async def get_quote(client: ERPClient, quote_id: str) -> dict[str, Any]:
    """Fetch full quote detail including nested line items."""
    return await client.get(f"{QUOTES_URL}{quote_id}/")


async def create_quote(
    client: ERPClient,
    *,
    customer: str,
    status: str = "draft",
    billing_address: str | None = None,
    shipping_address: str | None = None,
    sales_rep: str | None = None,
    price_list: str | None = None,
    currency: str = "USD",
    payment_terms: str | None = None,
    customer_notes: str | None = None,
    internal_notes: str | None = None,
    line_items: list[dict[str, Any]] | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a new quote, optionally with nested line items.

    The Quote DRF serializer treats `line_items` as read-only, so each line
    is written separately to /quote-lines/ after the parent quote exists.
    Once all lines are persisted we trigger /recalculate/ so subtotal,
    grand_total, and per-line line_total_ex_tax reflect the new rows.

    `payment_terms` maps to the model's `payment_terms_proposed` (Quote
    payment terms are proposals until acceptance — Order is where they
    become binding). `notes` on a line item maps to `line_notes`.

    `quote_number` is auto-generated in Quote.save() — don't pass one.
    Returns the refreshed detail (same shape as `get_quote`).

    `customer` is a UUID, or free text resolved through the match envelope
    (phase2-party.md 2.7): only an exact match proceeds; an ambiguous or
    fuzzy one raises ERPAmbiguousError before anything is written.
    """
    customer = await resolve_ref(client, customer, target=TARGET_CUSTOMER, what="customer")
    payload: dict[str, Any] = {
        "customer": customer,
        "status": status,
        "currency": currency,
    }
    optional = {
        "billing_address": billing_address,
        "shipping_address": shipping_address,
        "sales_rep": sales_rep,
        "price_list": price_list,
        "payment_terms_proposed": payment_terms,
        "customer_notes": customer_notes,
        "internal_notes": internal_notes,
    }
    for k, v in optional.items():
        if v is not None:
            payload[k] = v
    if extra_fields:
        payload.update(extra_fields)

    quote = await client.post(QUOTES_URL, json=payload)

    if line_items:
        quote_id = quote["id"]
        for line in line_items:
            line_payload = {k: v for k, v in line.items() if v is not None}
            # Friendly alias: callers say "notes", model field is line_notes.
            if "notes" in line_payload:
                line_payload["line_notes"] = line_payload.pop("notes")
            line_payload["quote"] = quote_id
            await client.post(QUOTE_LINES_URL, json=line_payload)
        # Refresh totals from the line items we just inserted.
        quote = await client.post(
            f"{QUOTES_URL}{quote_id}/recalculate/", json={}
        )

    return quote


async def update_quote(
    client: ERPClient, quote_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on an existing quote. Unknown fields raise.

    A ``status`` key is routed through the guarded transition action
    (``POST .../transition/``) rather than patched directly: legal moves
    follow the Quote transition map (draft → sent → accepted, ...).
    """
    fields = dict(fields)
    status = fields.pop("status", None)
    validate_fields(fields, QUOTE_WRITABLE, "quote")
    result = None
    if status is not None:
        result = await transition_quote(client, quote_id, status)
    if fields:
        result = await client.patch(f"{QUOTES_URL}{quote_id}/", json=fields)
    return result if result is not None else await get_quote(client, quote_id)


async def transition_quote(
    client: ERPClient, quote_id: str, status: str
) -> dict[str, Any]:
    """Apply a guarded status transition to a quote.

    Calls POST /api/v1/orders/quotes/{id}/transition/ with body
    ``{"status": <target>}``. Illegal moves (including any move out of a
    terminal state) raise ERPValidationError with the server's field
    errors. Returns the refreshed quote detail.
    """
    return await client.post(
        f"{QUOTES_URL}{quote_id}/transition/", json={"status": status}
    )


async def duplicate_quote(client: ERPClient, quote_id: str) -> dict[str, Any]:
    """Duplicate a quote. Returns the new quote with a fresh quote_number.

    Calls POST /api/v1/orders/quotes/{id}/duplicate/. The new quote starts
    in draft on a new engagement, with conversion + acceptance fields
    cleared and amended_from pointing back at the source.
    """
    return await client.post(f"{QUOTES_URL}{quote_id}/duplicate/", json={})


async def recalculate_quote_totals(
    client: ERPClient, quote_id: str
) -> dict[str, Any]:
    """Trigger a server-side recalculation of aggregate fields on a Quote.

    Calls POST /api/v1/orders/quotes/{id}/recalculate/. Returns the
    refreshed quote detail.
    """
    return await client.post(f"{QUOTES_URL}{quote_id}/recalculate/", json={})


async def delete_quote(client: ERPClient, quote_id: str) -> dict[str, Any]:
    """Permanently delete a quote.

    Safe before conversion to an order. After conversion the `quote` FK on
    the resulting Order will go dangling, so the DRF layer should reject
    the delete if a linked order exists — but do NOT call this on a
    quote that's been accepted.
    """
    await client.delete(f"{QUOTES_URL}{quote_id}/")
    return {"deleted": True, "resource": "quote", "id": quote_id}


async def list_quote_lines(
    client: ERPClient,
    *,
    quote: str | None = None,
    is_optional: bool | None = None,
    is_alternative: bool | None = None,
    limit: int | None = 50,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    if quote:
        params["quote"] = quote
    if is_optional is not None:
        params["is_optional"] = str(is_optional).lower()
    if is_alternative is not None:
        params["is_alternative"] = str(is_alternative).lower()
    return await client.list(QUOTE_LINES_URL, params=params, max_items=limit)


async def get_quote_line(client: ERPClient, line_id: str) -> dict[str, Any]:
    return await client.get(f"{QUOTE_LINES_URL}{line_id}/")


# ---- Sales invoices --------------------------------------------------------


async def recalculate_invoice_totals(
    client: ERPClient, invoice_id: str
) -> dict[str, Any]:
    """Trigger a server-side recalculation on a Sales Invoice.

    Calls POST /api/v1/orders/sales-invoices/{id}/recalculate/. Re-snapshots
    financials from the parent Order. No-op for PAID / VOID / CANCELLED
    invoices. Returns the refreshed invoice detail.
    """
    return await client.post(
        f"{SALES_INVOICES_URL}{invoice_id}/recalculate/", json={}
    )
