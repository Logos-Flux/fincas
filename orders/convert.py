"""Document conversions: Quote → Order, and Order → SalesInvoice.

Both are idempotent and both return a `(document, created)` tuple so
callers can tell a fresh conversion from a benign repeat — a double-clicked
button must not mint a second order.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from crm.engagements import win_for_conversion

from . import people
from .models import Order, OrderLineItem, Quote, SalesInvoice, ShipmentGroup

# How many days each `Net N` enum represents. Used to compute due_date.
# Anything not in this map produces no due_date — the invoice is open-ended
# until staff fill one in or the customer pays.
_NET_DAYS = {
    SalesInvoice.PaymentTerms.NET_15: 15,
    SalesInvoice.PaymentTerms.NET_30: 30,
    SalesInvoice.PaymentTerms.NET_45: 45,
    SalesInvoice.PaymentTerms.NET_60: 60,
}


def _map_payment_terms(text: str) -> str:
    """Map free-text Order.payment_terms to SalesInvoice.PaymentTerms enum.

    Order stores `payment_terms` as a free CharField (admin types whatever
    the customer's contract says). SalesInvoice constrains it to a small
    enum because the payment-link flow needs deterministic due-date math.
    Falls back to `due_on_receipt` for unrecognised input — that's the
    safest default (treats the bill as currently owed).
    """
    t = (text or "").strip().lower().replace("-", " ")
    # Match longer prefixes first so "net 30" doesn't shadow "net 3" etc.
    if "net 60" in t or t in ("net60",):
        return SalesInvoice.PaymentTerms.NET_60
    if "net 45" in t or t in ("net45",):
        return SalesInvoice.PaymentTerms.NET_45
    if "net 30" in t or t in ("net30",):
        return SalesInvoice.PaymentTerms.NET_30
    if "net 15" in t or t in ("net15",):
        return SalesInvoice.PaymentTerms.NET_15
    if "prepaid" in t or "advance" in t:
        return SalesInvoice.PaymentTerms.PREPAID
    return SalesInvoice.PaymentTerms.DUE_ON_RECEIPT


def _due_date_for(invoice_date: date, payment_terms: str) -> date | None:
    days = _NET_DAYS.get(payment_terms)
    return invoice_date + timedelta(days=days) if days else None


def convert_order_to_invoice(order: Order) -> tuple[SalesInvoice, bool]:
    """Get-or-create the invoice for an order. Returns (invoice, created).

    If the order already has at least one invoice, the most recent one is
    returned with `created=False` — callers should treat this as a benign
    idempotent hit and surface the existing record. Otherwise, a fresh
    DRAFT invoice is built from the order's snapshot fields, and the order's
    unapplied customer deposits (settled payments with no invoice, posted to
    the payment rule's deposit account) are linked to it
    (``payments.services.attach_order_deposits``); they move to A/R when the
    invoice is issued.
    """
    existing = order.invoices.order_by("-invoice_date", "-created_at").first()
    if existing is not None:
        return existing, False

    invoice_date = date.today()
    payment_terms = _map_payment_terms(order.payment_terms)
    grand_total = order.grand_total or Decimal("0")

    with transaction.atomic():
        invoice = _create_invoice(order, invoice_date, payment_terms, grand_total)
        from payments.services import attach_order_deposits

        attach_order_deposits(invoice)  # re-settles this same object when it links any
    return invoice, True


def _create_invoice(order, invoice_date, payment_terms, grand_total):
    return SalesInvoice.objects.create(
        order=order,
        company=order.company,
        status=SalesInvoice.Status.DRAFT,
        invoice_date=invoice_date,
        payment_terms=payment_terms,
        due_date=_due_date_for(invoice_date, payment_terms),
        subtotal_ex_tax=order.subtotal_ex_tax or Decimal("0"),
        tax_total=order.tax_total or Decimal("0"),
        shipping_cost=order.shipping_cost_ex_tax or Decimal("0"),
        grand_total=grand_total,
        amount_paid=Decimal("0"),
        balance_due=grand_total,
        notes=order.customer_message or "",
        internal_notes=order.internal_notes or "",
    )


# ── Quote → Order ─────────────────────────────────────────

#: QuoteLineItem → OrderLineItem fields that carry across unchanged.
#: Verified identical in name, type and semantics on both models.
_LINE_COPY_FIELDS = (
    "sort_order",
    "variant",
    "item_name",
    "sku",
    "description",
    "image_id",
    "item_type",
    "quantity",
    "selling_uom",
    "unit_price_ex_tax",
    "unit_price_inc_tax",
    "list_price",
    "discount_percent",
    "discount_amount",
    "line_total_ex_tax",
    "line_total_inc_tax",
    "line_tax",
    "price_list",
    "unit_cost",
    "supplier_product",
    "margin_amount",
    "margin_percent",
    "item_weight",
    "tax_rate",
    "metadata",
)

#: ShipmentGroup fields to clone. `order`/`quote` are set by the caller.
_GROUP_COPY_FIELDS = ("name", "warehouse", "fulfillment_type", "supplier", "sort_order")


def _clone_shipment_groups(quote: Quote, order: Order) -> dict:
    """Copy the quote's shipment groups onto the order.

    Returns {old_group_pk: new_group}. The groups must be CLONED, not
    re-pointed: ShipmentGroup carries nullable `order` AND `quote` FKs, so
    setting `order` on the quote's own rows would leave `quote` set too and
    the group would belong to both documents. The next save of either edit
    page deletes any group of that parent it doesn't recognise, so the two
    documents would silently destroy each other's shipping plan.
    """
    mapping = {}
    for group in quote.shipment_groups.order_by("sort_order", "pk"):
        mapping[group.pk] = ShipmentGroup.objects.create(
            order=order,
            quote=None,
            **{f: getattr(group, f) for f in _GROUP_COPY_FIELDS},
        )
    return mapping


def _convertible_lines(quote: Quote):
    """The quote lines that belong on the resulting order.

    A quote can present optional add-ons and mutually-exclusive
    alternatives; `customer_selected` is the switch that records what the
    customer actually took, and it defaults to True, so an ordinary quote
    converts in full. Anything explicitly deselected is priced information
    the customer declined — putting it on the order would bill them for it.
    """
    return quote.line_items.filter(customer_selected=True).order_by("sort_order", "pk")


def convert_quote_to_order(quote: Quote, *, user=None) -> tuple[Order, bool]:
    """Get-or-create the Sales Order for a quote. Returns (order, created).

    Idempotent: a quote that already converted returns its existing order
    with `created=False` rather than minting a duplicate.

    The new order lands in PENDING deliberately. Order-confirmation email
    is triggered by the *view-level* PENDING → AWAITING_FULFILLMENT
    transition, never by creation, so converting cannot email a customer.
    Advancing the order stays an explicit, separate act.
    """
    if quote.converted_to_order_id:
        return quote.converted_to_order, False

    with transaction.atomic():
        subtotal = quote.subtotal_ex_tax or Decimal("0")
        tax_total = quote.tax_total or Decimal("0")
        shipping = quote.shipping_cost_ex_tax or Decimal("0")
        grand_total = quote.grand_total or Decimal("0")

        # shipping_terms_proposed has no Order column. Stash it rather than
        # drop it — it is a negotiated term the customer agreed to.
        metadata = dict(quote.metadata or {})
        if quote.shipping_terms_proposed:
            metadata["shipping_terms_proposed"] = quote.shipping_terms_proposed

        order = Order.objects.create(
            # Link both directions; Quote.converted_to_order is set below.
            quote=quote,
            customer=quote.customer,
            company=quote.company,
            billing_address=quote.billing_address,
            shipping_address=quote.shipping_address,
            contact=quote.contact,
            sales_rep=quote.sales_rep,
            commission_rate=quote.commission_rate,
            channel=quote.channel,
            # Attribution moved to the engagement (spec 1.6); a legacy quote
            # still hands its old values on until step 12 drops them.
            campaign=quote.legacy_campaign,
            lead_source=quote.legacy_lead_source,
            engagement_id=quote.engagement_id,
            currency=quote.currency,
            exchange_rate=quote.exchange_rate,
            price_list=quote.price_list,
            tax_rate=quote.tax_rate,
            tax_exempt=quote.tax_exempt,
            customer_po_number=quote.customer_po_number,
            include_standard_terms=quote.include_standard_terms,
            internal_notes=quote.internal_notes,
            # Renamed between the two models.
            customer_message=quote.customer_notes,
            payment_terms=quote.payment_terms_proposed,
            terms_text=quote.special_conditions,
            # Totals. shipping_cost_ex_tax MUST be set before any line is
            # created: recalculate_totals() reads it off the order row, so a
            # line save with it still zero produces a grand_total missing
            # shipping.
            subtotal_ex_tax=subtotal,
            subtotal_inc_tax=subtotal + tax_total,
            tax_total=tax_total,
            shipping_cost_ex_tax=shipping,
            shipping_cost_inc_tax=shipping,
            discount_amount=quote.discount_amount or Decimal("0"),
            discount_percent=quote.discount_percent or Decimal("0"),
            coupon_discount=quote.coupon_discount or Decimal("0"),
            grand_total=grand_total,
            balance_due=grand_total,
            # Inert starting state.
            status=Order.Status.PENDING,
            order_type=Order.OrderType.SALES,
            order_date=timezone.now(),
            metadata=metadata,
        )

        groups = _clone_shipment_groups(quote, order)

        # bulk_create deliberately: OrderLineItem's post_save fires a full
        # Order.recalculate_totals() per row, so saving lines one at a time
        # is O(n) recalcs of the same order. bulk_create skips post_save; we
        # recalculate once at the end instead.
        OrderLineItem.objects.bulk_create(
            [
                OrderLineItem(
                    order=order,
                    shipment_group=groups.get(line.shipment_group_id),
                    requested_ship_date=line.estimated_ship_date,
                    **{f: getattr(line, f) for f in _LINE_COPY_FIELDS},
                )
                for line in _convertible_lines(quote)
            ]
        )

        order.recalculate_totals()
        people.copy(quote, order)

        quote.converted_to_order = order
        quote.converted_at = timezone.now()
        quote.converted_by = user
        quote.status = Quote.Status.ACCEPTED
        if not quote.accepted_at:
            quote.accepted_at = quote.converted_at
        quote.save(
            update_fields=[
                "converted_to_order",
                "converted_at",
                "converted_by",
                "status",
                "accepted_at",
                "updated_at",
            ]
        )
        # The deal is won on the engagement, not on the quote (spec 1.6).
        # save() above ensured the quote has one; the order joins the thread.
        if order.engagement_id != quote.engagement_id:
            Order._base_manager.filter(pk=order.pk).update(engagement_id=quote.engagement_id)
            order.engagement_id = quote.engagement_id
        win_for_conversion(quote)

    return order, True
