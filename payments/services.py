"""Payment orchestration: idempotent charging + ledger settlement."""

from __future__ import annotations

import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from accounting.posting import assert_period_open

from .gateways import get_gateway
from .models import Payment

logger = logging.getLogger("golden_erp.payments")


class GatewayChargeFailed(Exception):
    """Raised when the gateway declines or errors; no Payment row is stored."""


class PaymentRefused(Exception):
    """A payment that cannot be recorded as asked (amount, state); nothing stored."""


def _provider_for(gateway_id: str) -> str:
    mapping = {
        "stripe": Payment.PaymentProvider.STRIPE,
        "manual": Payment.PaymentProvider.MANUAL,
        "usdc": Payment.PaymentProvider.OTHER,
    }
    return mapping.get(gateway_id, Payment.PaymentProvider.OTHER)


def _settle_invoice_and_order(invoice, order) -> None:
    """Recompute amount_paid/balance_due from the settled payment ledger.

    Mirrors the sales-invoice reconcile path: totals derive from the
    captured/settled Payment rows, never from a single event's amount,
    so partial payments accumulate and duplicates cannot inflate.
    """
    from orders.models import Order as OrderModel
    from orders.models import SalesInvoice as InvoiceModel

    settled = (Payment.Status.CAPTURED, Payment.Status.SETTLED)
    applied = (
        invoice.payments.filter(status__in=settled).aggregate(
            total=Sum(F("amount") - F("convenience_fee"))
        )["total"]
        or Decimal("0.00")
    )
    invoice.amount_paid = applied
    invoice.balance_due = invoice.grand_total - applied
    if invoice.balance_due <= 0:
        invoice.status = InvoiceModel.Status.PAID
        if not invoice.paid_date:
            invoice.paid_date = date.today()
    else:
        invoice.status = InvoiceModel.Status.PARTIAL
        invoice.paid_date = None
    invoice.save(
        update_fields=["status", "amount_paid", "balance_due", "paid_date", "updated_at"]
    )

    order_applied = (
        order.payments.filter(status__in=settled).aggregate(
            total=Sum(F("amount") - F("convenience_fee"))
        )["total"]
        or Decimal("0.00")
    )
    order.amount_paid = order_applied
    order.balance_due = order.grand_total - order_applied
    order.payment_status = (
        OrderModel.PaymentStatus.PAID
        if order.balance_due <= 0
        else OrderModel.PaymentStatus.PARTIAL
    )
    if (
        order.payment_status == OrderModel.PaymentStatus.PAID
        and order.status == OrderModel.Status.AWAITING_PAYMENT
    ):
        order.status = OrderModel.Status.AWAITING_FULFILLMENT
    order.save(
        update_fields=[
            "payment_status", "amount_paid", "balance_due", "status", "updated_at",
        ]
    )


def charge_with_idempotency(
    *,
    gateway_id: str,
    order,
    invoice,
    amount: Decimal,
    payment_source: str,
    idempotency_key: str = "",
    currency: str = "USD",
    payment_method: str = "credit_card",
    metadata: Optional[Dict[str, Any]] = None,
) -> Payment:
    """Charge once per idempotency key; retried POSTs return the original row.

    A blank key skips dedup and always processes (back-compat path).
    Gateway failures raise GatewayChargeFailed with no Payment persisted
    and no ledger mutation.
    """
    if payment_method not in dict(Payment.PaymentMethod.choices):
        payment_method = Payment.PaymentMethod.OTHER

    with transaction.atomic():
        if idempotency_key:
            existing = (
                Payment.objects.select_for_update()
                .filter(idempotency_key=idempotency_key)
                .first()
            )
            if existing is not None:
                return existing

        gateway = get_gateway(gateway_id)
        result = gateway.charge(
            amount=amount,
            currency=currency,
            payment_source=payment_source,
            metadata=metadata,
        )
        if not result.success:
            raise GatewayChargeFailed(
                f"{gateway_id} charge failed: {result.error_message}"
            )

        now = timezone.now()
        payment = Payment.objects.create(
            order=order,
            invoice=invoice,
            payment_method=payment_method,
            payment_provider=_provider_for(gateway_id),
            transaction_id=result.transaction_id,
            amount=result.amount,
            currency=result.currency,
            net_amount=result.amount,
            status=Payment.Status.CAPTURED,
            payment_date=now,
            settled_date=now,
            gateway_response=result.raw_response,
            idempotency_key=idempotency_key or "",
            company=invoice.company if invoice is not None else order.company,
        )
        if invoice is not None:
            _settle_invoice_and_order(invoice, order)
        return payment


def record_payment(
    *,
    invoice,
    amount: Decimal,
    payment_date=None,
    payment_method: str = "other",
    reference: str = "",
    notes: str = "",
    received_by: str = "",
    idempotency_key: str = "",
) -> Payment:
    """Record money received against ``invoice`` outside any gateway.

    The back office's "Record payment" (R1.16) and any script that books a
    check, wire or cash receipt go through here: one settled, manual
    Payment on the invoice's order, posted to the ledger by
    ``Payment.save()`` (``payments.posting.post_payment``, under the
    company's payment DocumentPostingRule: ``accounting.posting.NoPostingRule``
    propagates, nothing is stored), then the invoice's and order's
    amount_paid / balance_due / status recomputed from the settled ledger
    (``_settle_invoice_and_order``), so a partial payment leaves the invoice
    ``partial`` and the one that clears the balance marks it ``paid``.

    ``idempotency_key`` names the attempt: a retry with the same key gets
    the original row (the form's ``_once`` token; API callers send their
    own). Refuses a non-positive amount or one over the balance due with
    ``PaymentRefused``.
    """
    amount = Decimal(amount or 0).quantize(Decimal("0.01"))
    if amount <= 0:
        raise PaymentRefused("The amount must be more than zero.")
    if payment_method not in dict(Payment.PaymentMethod.choices):
        payment_method = Payment.PaymentMethod.OTHER
    with transaction.atomic():
        if idempotency_key:
            existing = (
                Payment.objects.select_for_update()
                .filter(idempotency_key=idempotency_key)
                .first()
            )
            if existing is not None:
                return existing
        invoice = type(invoice).objects.select_for_update().select_related("order").get(pk=invoice.pk)
        balance = Decimal(invoice.balance_due or 0)
        if amount > balance:
            raise PaymentRefused(
                f"The amount ({amount}) is more than the balance due ({balance})."
            )
        when = payment_date or timezone.now()
        if not hasattr(when, "hour"):  # a date: midnight, server zone
            when = timezone.make_aware(
                datetime.combine(when, datetime.min.time()), timezone.get_current_timezone()
            )
        # The period is checked before the row exists (the posting inside
        # Payment.save would refuse too, but with the entry's wording).
        assert_period_open(invoice.company, when.date(), what=f"A payment on {invoice.invoice_number}")
        payment = Payment.objects.create(
            order=invoice.order,
            invoice=invoice,
            payment_method=payment_method,
            payment_provider=Payment.PaymentProvider.MANUAL,
            transaction_id=(reference or "")[:255],
            amount=amount,
            currency=invoice.order.currency or "USD",
            net_amount=amount,
            status=Payment.Status.CAPTURED,
            payment_date=when,
            settled_date=when,
            received_by=(received_by or "")[:255],
            notes=notes or "",
            idempotency_key=idempotency_key or "",
            company=invoice.company,
        )
        _settle_invoice_and_order(invoice, invoice.order)
        return payment


class RefundRefused(Exception):
    """A refund that cannot be issued as asked (amount, state); nothing stored."""


def refunded_total(payment) -> Decimal:
    """What has already been refunded against ``payment`` (its refund rows)."""
    return (
        payment.child_payments.filter(
            payment_type=Payment.PaymentType.REFUND, status=Payment.Status.REFUNDED,
        ).aggregate(total=Sum("amount"))["total"]
        or Decimal("0.00")
    )


def refundable_amount(payment) -> Decimal:
    """What can still be refunded on ``payment`` (zero for anything but a settled payment)."""
    settled = (Payment.Status.CAPTURED, Payment.Status.SETTLED, Payment.Status.REFUNDED)
    if payment.payment_type != Payment.PaymentType.PAYMENT or payment.status not in settled:
        return Decimal("0.00")
    left = Decimal(payment.amount or 0) - refunded_total(payment)
    return left if left > 0 else Decimal("0.00")


def refund_payment(
    payment,
    amount: Decimal,
    *,
    reason: str = "",
    refunded_by: str = "",
    idempotency_key: str = "",
) -> Payment:
    """Refund ``amount`` of a settled payment; return the refund row (R5.02).

    The back office's "Refund" on the invoice page and the API go through
    here: one ``Payment`` of ``payment_type=refund`` under the parent
    (``parent_payment``), status ``refunded``, posted to the ledger by
    ``Payment.save()`` under the company's refund DocumentPostingRule
    (``accounting.posting.NoPostingRule`` propagates, nothing stored); the
    parent is marked ``refunded`` once its refunds reach its amount (its own
    entry is not reversed: the refund rows carry the money back); the order's
    ``refunded_amount`` and ``payment_status`` follow (``refunded`` once the
    refunds reach what was paid), and a shipped order moves to
    ``partially_refunded``, the order's one refund state. The invoice keeps
    its status (``SalesInvoice`` has no refund state): the refund is a row in
    its payments. Stock is not touched: a refund returns money, not goods.

    ``idempotency_key`` names the attempt (the form's ``_once`` token; API
    callers send their own): a retry returns the original row. Refuses a
    non-positive amount, one over what is still refundable, or a parent that
    is not a settled payment, with ``RefundRefused``; a closed fiscal period
    raises ``ValidationError`` before anything is written.
    """
    from orders.models import Order as OrderModel

    amount = Decimal(amount or 0).quantize(Decimal("0.01"))
    if amount <= 0:
        raise RefundRefused("The amount must be more than zero.")
    with transaction.atomic():
        if idempotency_key:
            existing = (
                Payment.objects.select_for_update()
                .filter(idempotency_key=idempotency_key, payment_type=Payment.PaymentType.REFUND)
                .first()
            )
            if existing is not None:
                return existing
        # Lock the row alone (invoice is nullable: no select_related under FOR UPDATE on Postgres).
        parent = Payment.objects.select_for_update().get(pk=payment.pk)
        left = refundable_amount(parent)
        if left <= 0:
            raise RefundRefused(f"{parent.payment_number} is not a settled payment with anything left to refund.")
        if amount > left:
            raise RefundRefused(f"The amount ({amount}) is more than the {left} still refundable on {parent.payment_number}.")
        now = timezone.now()
        assert_period_open(parent.company or parent.order.company, now.date(), what=f"A refund of {parent.payment_number}")
        refund = Payment.objects.create(
            order=parent.order,
            invoice=parent.invoice,
            parent_payment=parent,
            payment_type=Payment.PaymentType.REFUND,
            payment_method=parent.payment_method,
            payment_provider=parent.payment_provider,
            transaction_id="",
            amount=amount,
            currency=parent.currency,
            net_amount=amount,
            status=Payment.Status.REFUNDED,
            payment_date=now,
            settled_date=now,
            refund_reason=(reason or "")[:255],
            notes=reason or "",
            received_by=(refunded_by or "")[:255],
            idempotency_key=idempotency_key or "",
            company=parent.company or parent.order.company,
        )
        if refunded_total(parent) >= Decimal(parent.amount or 0) and parent.status != Payment.Status.REFUNDED:
            parent.status = Payment.Status.REFUNDED
            parent.save(update_fields=["status", "updated_at"])
        order = OrderModel.objects.select_for_update().get(pk=parent.order_id)
        refunded = (
            order.payments.filter(payment_type=Payment.PaymentType.REFUND, status=Payment.Status.REFUNDED)
            .aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        )
        order.refunded_amount = refunded
        # What was paid, from the ledger: a seeded or imported order may carry
        # payment_status=paid with amount_paid never filled in.
        paid = (
            order.payments.filter(
                payment_type=Payment.PaymentType.PAYMENT,
                status__in=(Payment.Status.CAPTURED, Payment.Status.SETTLED, Payment.Status.REFUNDED),
            ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
        )
        if paid > 0 and refunded >= paid:
            order.payment_status = OrderModel.PaymentStatus.REFUNDED
        update = ["refunded_amount", "payment_status", "updated_at"]
        if OrderModel.Status.PARTIALLY_REFUNDED in OrderModel.VALID_TRANSITIONS.get(order.status, []):
            order.status = OrderModel.Status.PARTIALLY_REFUNDED
            update.append("status")
        order.save(update_fields=update)
        return refund
