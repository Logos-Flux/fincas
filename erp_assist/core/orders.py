"""Quotes, orders, invoices, line items, and sub-orders.

Every document can be created here: a quote (``create_quote``), a sales
order (``create_order``) and an invoice (``create_invoice``). A header is
POSTed first, its lines are written one by one to the line endpoint (the
detail serializers treat ``line_items`` as read-only), and ``recalculate``
then derives the totals: totals are never caller-written.

There is no standalone invoice: ``SalesInvoice.order`` is required, so
``create_invoice`` creates the order and converts it.

Status changes go through the guarded ``POST .../transition/`` action
(never a bare PATCH): the model's transition map decides what's legal and
illegal moves return the DRF field-error envelope. An invoice is the
exception (it has no transition action); see ``update_invoice``.

Quote → Order conversion is POST /api/v1/orders/quotes/{id}/convert-to-order/
(``convert_quote_to_order``). It is idempotent — a quote that already
converted returns its existing order with `created: false` rather than
minting a duplicate — and it copies line items and shipment groups, so do
NOT hand-build the Order and set its `quote` field.
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
from .client import ERPClient, ERPValidationError
from .parties import TARGET_CUSTOMER, resolve_ref

QUOTES_URL = "/api/v1/orders/quotes/"
QUOTE_LINES_URL = "/api/v1/orders/quote-lines/"
ORDERS_URL = "/api/v1/orders/orders/"
ORDER_LINES_URL = "/api/v1/orders/order-lines/"
SUB_ORDERS_URL = "/api/v1/orders/sub-orders/"
SALES_INVOICES_URL = "/api/v1/orders/sales-invoices/"
CUSTOMERS_URL = "/api/v1/customers/customers/"
VARIANTS_URL = "/api/v1/products/variants/"

# SKU written on a line that names no catalog item and gives no SKU of its
# own (freight, a service, a one-off). The column is required and non-blank.
FREE_FORM_SKU = "MISC"

# What a caller may send on a new order or quote line. `order` / `quote` is
# set by the tool; totals are derived by recalculate.
_LINE_INPUT = frozenset({
    "variant", "item_name", "sku", "description", "item_type", "quantity",
    "selling_uom", "unit_price_ex_tax", "list_price", "discount_percent",
    "discount_amount", "unit_cost", "sort_order", "tax_rate", "price_list",
    "metadata",
})
_ORDER_LINE_INPUT = _LINE_INPUT | {"requested_ship_date", "shipping_origin", "lead_time_days"}
_QUOTE_LINE_INPUT = _LINE_INPUT | {
    "is_optional", "is_alternative", "alternative_group", "line_notes",
    "internal_line_notes", "estimated_ship_date",
}

INVOICE_WRITABLE = frozenset({
    "status", "invoice_date", "due_date", "paid_date", "payment_terms",
    "payment_instructions", "notes", "internal_notes", "metadata",
})


async def _line_payload(
    client: ERPClient, line: dict[str, Any], allowed: frozenset[str], what: str
) -> dict[str, Any]:
    """Validate one caller-supplied line and fill what the catalog can supply.

    A line either names a ``variant`` (UUID) or is free-form. For a variant
    line, ``item_name``, ``sku`` and ``unit_price_ex_tax`` default from the
    variant (sale price, else retail price); a free-form line must give
    ``item_name`` and ``unit_price_ex_tax`` and gets ``FREE_FORM_SKU`` when it
    names no SKU. ``quantity`` defaults to 1. Raises ERPValidationError before
    anything is written, so a bad line never leaves a half-built document.
    """
    payload = {k: v for k, v in line.items() if v is not None}
    if "notes" in payload and "line_notes" in allowed:
        payload["line_notes"] = payload.pop("notes")
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ERPValidationError(
            f"Unknown field(s) on {what}: {', '.join(unknown)}. "
            f"Allowed: {', '.join(sorted(allowed))}.",
            details={k: ["Unknown field."] for k in unknown},
        )
    payload.setdefault("quantity", 1)
    if payload.get("variant"):
        missing = [k for k in ("item_name", "sku", "unit_price_ex_tax") if k not in payload]
        if missing:
            variant = await client.get(f"{VARIANTS_URL}{payload['variant']}/")
            price = variant.get("sale_price") or variant.get("retail_price")
            defaults = {
                "item_name": variant.get("name") or variant.get("sku"),
                "sku": variant.get("sku"),
                "unit_price_ex_tax": price,
            }
            for key in missing:
                if defaults[key] is not None:
                    payload[key] = defaults[key]
    else:
        payload.setdefault("sku", FREE_FORM_SKU)
    still = [k for k in ("item_name", "unit_price_ex_tax") if payload.get(k) in (None, "")]
    if still:
        raise ERPValidationError(
            f"{what} needs {' and '.join(still)}"
            + ("" if payload.get("variant") else " (a line with no variant is free-form)")
            + ".",
            details={k: ["This field is required."] for k in still},
        )
    return payload


async def _customer_company(client: ERPClient, customer_id: str) -> str | None:
    """The customer's company, so a caller with no company of its own (the
    legacy API key) still creates the document in the right tenant."""
    customer = await client.get(f"{CUSTOMERS_URL}{customer_id}/")
    company = customer.get("company")
    return str(company) if company else None


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


async def create_order(
    client: ERPClient,
    *,
    customer: str,
    line_items: list[dict[str, Any]] | None = None,
    status: str | None = None,
    billing_address: str | None = None,
    shipping_address: str | None = None,
    sales_rep: str | None = None,
    price_list: str | None = None,
    currency: str | None = None,
    payment_terms: str | None = None,
    customer_po_number: str | None = None,
    requested_ship_date: str | None = None,
    shipping_cost_ex_tax: str | None = None,
    customer_message: str | None = None,
    internal_notes: str | None = None,
    idempotency_key: str | None = None,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a sales order, with its lines, and return the refreshed detail.

    The header is POSTed, each line goes to /order-lines/, then /recalculate/
    derives line and order totals (a new line reads ``line_total_ex_tax: 0``
    until then). Every line is validated before the header is written.

    ``idempotency_key`` is stored as the order's ``external_id``, the key the
    API dedups on: a retry with the same key gets the first order back and
    its lines are not added a second time.

    ``customer`` is a UUID, or free text resolved through the match envelope:
    only an exact match proceeds (ERPAmbiguousError otherwise, nothing
    written). ``order_number`` is server-assigned.
    """
    customer = await resolve_ref(client, customer, target=TARGET_CUSTOMER, what="customer")
    lines = [
        await _line_payload(client, line, _ORDER_LINE_INPUT, "order line")
        for line in (line_items or [])
    ]
    payload: dict[str, Any] = {"customer": customer}
    optional = {
        "status": status,
        "billing_address": billing_address,
        "shipping_address": shipping_address,
        "sales_rep": sales_rep,
        "price_list": price_list,
        "currency": currency,
        "payment_terms": payment_terms,
        "customer_po_number": customer_po_number,
        "requested_ship_date": requested_ship_date,
        "shipping_cost_ex_tax": shipping_cost_ex_tax,
        "customer_message": customer_message,
        "internal_notes": internal_notes,
        "external_id": idempotency_key,
    }
    payload.update({k: v for k, v in optional.items() if v is not None})
    if extra_fields:
        validate_fields(extra_fields, ORDER_WRITABLE, "order")
        payload.update(extra_fields)
    if "company" not in payload:
        company = await _customer_company(client, customer)
        if company:
            payload["company"] = company

    order = await client.post(ORDERS_URL, json=payload)
    order_id = order["id"]
    if lines and not order.get("line_items"):
        # An idempotent replay returns the first order, lines and all.
        for line in lines:
            await client.post(ORDER_LINES_URL, json={**line, "order": order_id})
    if lines:
        order = await client.post(f"{ORDERS_URL}{order_id}/recalculate/", json={})
    return order


async def create_invoice(
    client: ERPClient,
    *,
    customer: str,
    line_items: list[dict[str, Any]],
    payment_terms: str | None = None,
    due_date: str | None = None,
    notes: str | None = None,
    internal_notes: str | None = None,
    idempotency_key: str | None = None,
    order_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a draft invoice for work that has no sales order yet.

    An invoice always belongs to an order, so this creates the order
    (``create_order``), converts it (``convert_order_to_invoice``) and, when
    ``due_date`` / ``notes`` / ``internal_notes`` are given, patches them onto
    the invoice. Returns ``{"order": <detail>, "invoice": <detail>}``. The
    invoice stays in draft: moving it out of draft posts it to the ledger
    (``update_invoice``). For an order that already exists, call
    ``convert_order_to_invoice`` instead.
    """
    if not line_items:
        raise ERPValidationError(
            "An invoice needs at least one line item.",
            details={"line_items": ["This field is required."]},
        )
    order = await create_order(
        client,
        customer=customer,
        line_items=line_items,
        payment_terms=payment_terms,
        idempotency_key=idempotency_key,
        extra_fields=order_fields,
    )
    converted = await convert_order_to_invoice(client, order["id"])
    invoice = converted.get("invoice", converted)
    patch = {
        k: v
        for k, v in {"due_date": due_date, "notes": notes, "internal_notes": internal_notes}.items()
        if v is not None
    }
    if patch:
        invoice = await client.patch(f"{SALES_INVOICES_URL}{invoice['id']}/", json=patch)
    return {"order": await get_order(client, order["id"]), "invoice": invoice}


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


async def create_order_line(
    client: ERPClient, order_id: str, line: dict[str, Any]
) -> dict[str, Any]:
    """Add one line to an existing order and recalculate. Returns the order.

    ``line`` names a ``variant`` (item_name, sku and price default from it) or
    is free-form (``item_name`` + ``unit_price_ex_tax``; freight, a service).
    """
    payload = await _line_payload(client, line, _ORDER_LINE_INPUT, "order line")
    await client.post(ORDER_LINES_URL, json={**payload, "order": order_id})
    return await client.post(f"{ORDERS_URL}{order_id}/recalculate/", json={})


async def delete_order_line(client: ERPClient, line_id: str) -> dict[str, Any]:
    """Delete one order line and recalculate its order. Returns the order."""
    line = await get_order_line(client, line_id)
    await client.delete(f"{ORDER_LINES_URL}{line_id}/")
    return await client.post(f"{ORDERS_URL}{line['order']}/recalculate/", json={})


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
    if "company" not in payload:
        # A caller with no company of its own (the legacy API key) is
        # otherwise refused: `company` is required on the quote.
        company = await _customer_company(client, customer)
        if company:
            payload["company"] = company

    # Validate every line (and default name / SKU / price from its variant)
    # before the quote exists, so a bad line never leaves an empty quote.
    # Friendly alias: callers say "notes", the model field is line_notes.
    lines = [
        await _line_payload(client, line, _QUOTE_LINE_INPUT, "quote line")
        for line in (line_items or [])
    ]

    quote = await client.post(QUOTES_URL, json=payload)

    if lines:
        quote_id = quote["id"]
        for line_payload in lines:
            await client.post(QUOTE_LINES_URL, json={**line_payload, "quote": quote_id})
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


async def convert_quote_to_order(client: ERPClient, quote_id: str) -> dict[str, Any]:
    """Convert a quote to a sales order. Idempotent.

    Calls POST /api/v1/orders/quotes/{id}/convert-to-order/. Lines and
    shipment groups are copied and the quote's engagement is won. A quote
    that already converted returns its existing order (`"created": false`).
    """
    return await client.post(f"{QUOTES_URL}{quote_id}/convert-to-order/", json={})


async def create_quote_line(
    client: ERPClient, quote_id: str, line: dict[str, Any]
) -> dict[str, Any]:
    """Add one line to an existing quote and recalculate. Returns the quote."""
    payload = await _line_payload(client, line, _QUOTE_LINE_INPUT, "quote line")
    await client.post(QUOTE_LINES_URL, json={**payload, "quote": quote_id})
    return await client.post(f"{QUOTES_URL}{quote_id}/recalculate/", json={})


async def delete_quote_line(client: ERPClient, line_id: str) -> dict[str, Any]:
    """Delete one quote line and recalculate its quote. Returns the quote."""
    line = await get_quote_line(client, line_id)
    await client.delete(f"{QUOTE_LINES_URL}{line_id}/")
    return await client.post(f"{QUOTES_URL}{line['quote']}/recalculate/", json={})


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


async def list_invoices(
    client: ERPClient,
    *,
    search: str | None = None,
    status: str | None = None,
    order: str | None = None,
    payment_terms: str | None = None,
    company: str | None = None,
    ordering: str | None = None,
    limit: int | None = 20,
) -> list[dict[str, Any]]:
    """List sales invoices.

    `search` matches invoice_number and the order number. `ordering`
    examples: "-invoice_date", "due_date", "-grand_total".
    """
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    for key, value in {
        "search": search, "status": status, "order": order,
        "payment_terms": payment_terms, "company": company, "ordering": ordering,
    }.items():
        if value:
            params[key] = value
    return await client.list(SALES_INVOICES_URL, params=params, max_items=limit)


async def get_invoice(client: ERPClient, invoice_id: str) -> dict[str, Any]:
    """Fetch one sales invoice by UUID."""
    return await client.get(f"{SALES_INVOICES_URL}{invoice_id}/")


async def update_invoice(
    client: ERPClient, invoice_id: str, fields: dict[str, Any]
) -> dict[str, Any]:
    """Patch fields on a sales invoice. Unknown fields raise.

    Amounts are a snapshot of the order and are not writable: change the
    order's lines, then ``recalculate_invoice_totals``. ``status`` IS written
    here (an invoice has no transition action), and a move out of ``draft``
    posts the invoice to the ledger: a closed fiscal period refuses it with
    the period named.
    """
    validate_fields(fields, INVOICE_WRITABLE, "invoice")
    return await client.patch(f"{SALES_INVOICES_URL}{invoice_id}/", json=fields)


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
