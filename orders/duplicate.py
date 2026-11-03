"""Duplicate helpers for Order and Quote.

Each function returns a freshly-saved copy with status fields reset to
draft/initial, the auto-generated number minted by the model's save()
override, and `amended_from` pointing back at the source. Wrapped in
`transaction.atomic` so partial inserts can't leak on failure.

These live in their own module (rather than as model methods) so the
field-reset rules sit in one obvious place — the duplicate spec is the
source of truth, not the model class.
"""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from . import people
from .models import Order, OrderLineItem, Quote, QuoteLineItem

# Fields cleared on the new Order so it starts fresh (financials zeroed,
# status back to incomplete (a draft), external/payment refs blank, email tracking reset).
# Anything not listed here is copied verbatim from the source.
_ORDER_RESET = {
    # INCOMPLETE, not PENDING: a copy is a draft, and leaving INCOMPLETE is
    # what announces order_placed to plugins.
    "status": Order.Status.INCOMPLETE,
    "payment_status": Order.PaymentStatus.UNPAID,
    "fulfillment_status": Order.FulfillmentStatus.UNFULFILLED,
    "status_note": "",
    "booked_at": None,
    "shipped_date": None,
    "amount_paid": 0,
    "balance_due": 0,
    "refunded_amount": 0,
    "stripe_payment_intent_id": "",
    "confirmation_sent_at": None,
    "external_source": "",
    "external_id": "",
    "bc_id": "",
    "erpnext_name": "",
    "d1_id": "",
    # quote link cleared — duplicate isn't tied to the source's quote chain,
    # nor to its sales thread (CRM Phase 3)
    "quote": None,
    "engagement": None,
}

_ORDER_LINE_RESET = {
    "quantity_shipped": 0,
    "quantity_returned": 0,
    "refunded_amount": 0,
    "fulfillment_status": OrderLineItem.FulfillmentStatus.PENDING,
    "sub_order": None,
    "purchase_order_line": None,
    "pick_ticket_line": None,
    "return_id": "",
}


_QUOTE_RESET = {
    "status": Quote.Status.DRAFT,
    # A duplicate is an independent quote, so it starts its own sales thread
    # (spec 1.6, E5): Quote.save() -> crm.engagements.ensure_for_quote.
    "engagement": None,
    "status_note": "",
    "revision_number": 1,
    "revision_reason": "",
    "superseded_by": None,
    "original_quote": None,
    "converted_to_order": None,
    "converted_at": None,
    "converted_by": None,
    "sent_at": None,
    "sent_via": "",
    "sent_to_email": "",
    "viewed_at": None,
    "accepted_at": None,
    "accepted_by_name": "",
    "accepted_by_title": "",
    "acceptance_method": "",
    "acceptance_document_url": "",
    "approval_status": "",
    "approved_by": None,
    "approved_at": None,
    "approval_notes": "",
    # The legacy pipeline columns are history of the source's deal; nothing
    # writes them after the step-5 backfill, so the copy starts at defaults.
    **{
        f.attname: f.get_default()
        for f in Quote._meta.concrete_fields
        if f.attname.startswith("legacy_")
    },
}

# QuoteLineItem doesn't carry per-line fulfillment state, so nothing to reset
# beyond clearing audit timestamps populated when a quote is sent/accepted.
_QUOTE_LINE_RESET = {
    "availability_checked_at": None,
}


def _copy_instance(source, reset_fields: dict, *, amended_from):
    """Clone a Django model instance: shallow copy, reset overrides, fresh PK.

    Returns the unsaved new instance. Caller saves so the model's save()
    can mint identifiers (order_number, quote_number, po_number).
    """
    # Re-fetch as a fresh, plain instance so we don't accidentally carry
    # over related-object caches from prefetch.
    new = source.__class__.objects.get(pk=source.pk)
    new.pk = None
    new.id = None
    new._state.adding = True
    for field, value in reset_fields.items():
        setattr(new, field, value)
    new.amended_from = amended_from
    return new


def duplicate_order(source: Order) -> Order:
    """Create a draft Order that mirrors `source` field-for-field.

    Resets status/payment/fulfillment, zeros financials, clears stripe and
    confirmation refs, mints a new order_number, links amended_from →
    source. Copies every line item with shipping/return state cleared.
    """
    with transaction.atomic():
        new_order = _copy_instance(source, _ORDER_RESET, amended_from=source)
        new_order.order_number = ""  # let save() mint a fresh SO-NNNNN
        new_order.slug = ""
        new_order.order_date = timezone.now()
        new_order.save()

        for line in source.line_items.all():
            new_line = OrderLineItem.objects.get(pk=line.pk)
            new_line.pk = None
            new_line.id = None
            new_line._state.adding = True
            for field, value in _ORDER_LINE_RESET.items():
                setattr(new_line, field, value)
            new_line.order = new_order
            new_line.save()
        people.copy(source, new_order)

    return new_order


def duplicate_quote(source: Quote, *, engagement=None) -> Quote:
    """Create a draft Quote that mirrors `source`.

    Resets status to draft, clears revision-tree links and conversion
    audit fields, mints a new quote_number, links amended_from → source.
    Copies every line item. The copy gets a new engagement unless the caller
    passes one (a revision, unlike a duplicate, stays on its chain's).
    """
    with transaction.atomic():
        new_quote = _copy_instance(source, _QUOTE_RESET, amended_from=source)
        new_quote.quote_number = ""  # let save() mint a fresh QT-NNNNN
        if engagement is not None:
            from crm.engagements import ensure_for_quote

            # Refuses another tenant's engagement.
            new_quote.engagement = ensure_for_quote(new_quote, engagement=engagement)
        new_quote.save()

        for line in source.line_items.all():
            new_line = QuoteLineItem.objects.get(pk=line.pk)
            new_line.pk = None
            new_line.id = None
            new_line._state.adding = True
            for field, value in _QUOTE_LINE_RESET.items():
                setattr(new_line, field, value)
            new_line.quote = new_quote
            new_line.save()
        people.copy(source, new_quote)

    return new_quote
