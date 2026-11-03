"""Refunds from the invoice page (R5.02, R5.03, R5.04).

Found by the release harness on 2026-10-17 (docs/release-testing.md): no
back-office screen issued a refund; refunds existed only as API-created
``Payment`` rows of ``payment_type=refund``, with the order patched by hand.

``payments.services.refund_payment`` is the one path: a refund row under
the parent payment, posted under the refund rule by ``Payment.save()``;
the parent marked refunded once its refunds reach its amount (its own
entry is NOT reversed again: the refund rows carry the money back); the
order's refunded amount and payment status; the invoice keeps its status.
``RefundView`` posts it from the invoice page behind the ``_once`` guard.
Stock is untouched: a refund returns money, not goods.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.urls import reverse

from accounting.models import FiscalPeriod, JournalEntry
from accounting.tests.test_posting import make_posting_rules
from inventory.models import Warehouse, WarehouseStock
from orders.models import Order, OrderLineItem, SalesInvoice
from payments.services import RefundRefused, record_payment, refund_payment, refundable_amount
from payments.tests.test_record_payment import RecordPaymentBase, once_token
from products.models import Product, ProductVariant

from ..models import Payment


class RefundBase(RecordPaymentBase):
    def setUp(self):
        super().setUp()
        make_posting_rules(self.company)
        self.order.status = Order.Status.SHIPPED
        self.order.save(update_fields=["status"])
        product = Product.objects.create(title="Widget", part_number="WID-1", slug="wid-1")
        self.variant = ProductVariant.objects.create(product=product, sku="WID-1")
        OrderLineItem.objects.create(
            order=self.order, variant=self.variant, sku="WID-1", item_name="Widget",
            quantity=Decimal("2"), unit_price_ex_tax=Decimal("50"), line_total_ex_tax=Decimal("100"),
        )
        warehouse = Warehouse.objects.create(warehouse_code="WH-1", name="Main", company=self.company)
        WarehouseStock.objects.create(variant=self.variant, warehouse=warehouse, quantity=Decimal("8"))
        self.payment = record_payment(invoice=self.invoice, amount=Decimal("100"), payment_method="credit_card")
        self.invoice.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.invoice.status, SalesInvoice.Status.PAID)
        self.refund_url = reverse("erp-invoice-refund", args=[self.invoice.pk])

    def on_hand(self):
        return sum((s.quantity for s in WarehouseStock.objects.filter(variant=self.variant)), Decimal("0"))

    def refresh(self):
        self.invoice.refresh_from_db()
        self.order.refresh_from_db()
        self.payment.refresh_from_db()


class RefundServiceTests(RefundBase):
    def test_a_full_refund(self):
        refund = refund_payment(self.payment, Decimal("100"), reason="Customer returned both units.")
        self.refresh()
        self.assertEqual(refund.payment_type, Payment.PaymentType.REFUND)
        self.assertEqual(refund.status, Payment.Status.REFUNDED)
        self.assertEqual(refund.parent_payment_id, self.payment.pk)
        self.assertEqual(refund.invoice_id, self.invoice.pk)
        self.assertEqual(refund.amount, Decimal("100.00"))
        self.assertEqual(refund.refund_reason, "Customer returned both units.")
        self.assertEqual(self.payment.status, Payment.Status.REFUNDED)
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.REFUNDED)
        self.assertEqual(self.order.refunded_amount, Decimal("100.00"))
        self.assertEqual(self.order.status, Order.Status.PARTIALLY_REFUNDED)  # the order's one refund state
        # The invoice keeps its status: SalesInvoice has no refund state.
        self.assertEqual(self.invoice.status, SalesInvoice.Status.PAID)
        self.assertEqual(self.invoice.amount_paid, Decimal("100.00"))
        # The books: the invoice's entry (it posted on leaving draft), the
        # payment's, the refund's mirror, and NO second reversal for the
        # parent; every entry balanced.
        keys = sorted(JournalEntry.objects.values_list("posting_key", flat=True))
        self.assertEqual(keys, sorted([
            f"order:{self.invoice.invoice_number}", f"payment:{self.payment.payment_number}",
            f"refund:{refund.payment_number}",
        ]))
        for entry in JournalEntry.objects.all():
            debit, credit = entry.totals()
            self.assertEqual(debit, credit, entry.posting_key)
        refund_entry = JournalEntry.objects.get(posting_key=f"refund:{refund.payment_number}")
        self.assertEqual(refund_entry.totals()[0], Decimal("100.00"))
        # Stock is not a refund's business.
        self.assertEqual(self.on_hand(), Decimal("8"))
        self.assertEqual(refundable_amount(self.payment), Decimal("0.00"))

    def test_an_order_paid_without_amount_paid_still_reads_refunded(self):
        # A seeded or imported order: payment_status=paid, amount_paid never
        # filled in (core.release_test.seed_paid_shipped_order). "Paid" is
        # read from the ledger, not the column.
        Order.objects.filter(pk=self.order.pk).update(amount_paid=Decimal("0"))
        refund_payment(self.payment, Decimal("100"))
        self.refresh()
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.REFUNDED)
        self.assertEqual(self.order.refunded_amount, Decimal("100.00"))

    def test_a_partial_refund_then_the_rest(self):
        refund_payment(self.payment, Decimal("30"), reason="Damaged unit")
        self.refresh()
        self.assertEqual(self.payment.status, Payment.Status.CAPTURED)
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(self.order.refunded_amount, Decimal("30.00"))
        self.assertEqual(refundable_amount(self.payment), Decimal("70.00"))
        with self.assertRaisesMessage(RefundRefused, "more than the 70.00 still refundable"):
            refund_payment(self.payment, Decimal("70.01"))
        refund_payment(self.payment, Decimal("70"), reason="The rest")
        self.refresh()
        self.assertEqual(self.payment.status, Payment.Status.REFUNDED)
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.REFUNDED)
        self.assertEqual(self.order.refunded_amount, Decimal("100.00"))
        self.assertEqual(JournalEntry.objects.filter(entry_source="refund").count(), 2)
        self.assertEqual(JournalEntry.objects.count(), 4)  # invoice, payment, two refunds

    def test_refusals_store_nothing(self):
        with self.assertRaisesMessage(RefundRefused, "more than zero"):
            refund_payment(self.payment, Decimal("0"))
        with self.assertRaisesMessage(RefundRefused, "more than the 100.00 still refundable"):
            refund_payment(self.payment, Decimal("100.01"))
        self.assertEqual(Payment.objects.filter(payment_type=Payment.PaymentType.REFUND).count(), 0)
        self.refresh()
        self.assertEqual(self.order.refunded_amount, Decimal("0"))

    def test_the_same_idempotency_key_returns_the_original_refund(self):
        first = refund_payment(self.payment, Decimal("100"), idempotency_key="k-1")
        again = refund_payment(self.payment, Decimal("100"), idempotency_key="k-1")
        self.assertEqual(first.pk, again.pk)
        self.assertEqual(Payment.objects.filter(payment_type=Payment.PaymentType.REFUND).count(), 1)

    def test_a_closed_period_refuses_before_anything_is_written(self):
        today = datetime.date.today()
        FiscalPeriod.objects.create(
            company=self.company, name=f"FY{today.year}", status=FiscalPeriod.Status.CLOSED,
            start_date=datetime.date(today.year, 1, 1), end_date=datetime.date(today.year, 12, 31),
        )
        with self.assertRaises(ValidationError):
            refund_payment(self.payment, Decimal("100"))
        self.refresh()
        self.assertEqual(Payment.objects.filter(payment_type=Payment.PaymentType.REFUND).count(), 0)
        self.assertEqual(self.payment.status, Payment.Status.CAPTURED)
        self.assertEqual(self.order.refunded_amount, Decimal("0"))


class RefundScreenTests(RefundBase):
    def refund(self, amount, token=None, **extra):
        if token is None:
            token = once_token(self.client.get(self.page).content.decode())
        data = {"_once": token, "payment": str(self.payment.pk), "amount": str(amount), "reason": "Returned"}
        data.update(extra)
        return self.client.post(self.refund_url, data), token

    def test_the_pages_offer_refund_and_the_post_issues_one(self):
        order_page = self.client.get(reverse("erp-order-edit", args=[self.order.pk])).content.decode()
        self.assertIn(f'{self.page}#refund', order_page)
        invoice_page = self.client.get(self.page).content.decode()
        self.assertIn('id="refund-form"', invoice_page)
        self.assertIn(self.payment.payment_number, invoice_page)
        response, token = self.refund("100.00")
        self.assertRedirects(response, self.page + "#refund", fetch_redirect_response=False)
        refund = Payment.objects.get(payment_type=Payment.PaymentType.REFUND)
        self.assertEqual(refund.amount, Decimal("100.00"))
        self.assertEqual(refund.idempotency_key, f"erp-once:{token}")
        self.assertEqual(refund.refund_reason, "Returned")
        self.refresh()
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.REFUNDED)
        page = self.client.get(self.page)
        self.assertContains(page, refund.payment_number)
        self.assertContains(page, 'id="invoice-refunded"')
        self.assertContains(page, "Refunded in full")
        self.assertNotContains(page, 'id="refund-form"')

    def test_a_double_submit_issues_one_refund(self):
        response, token = self.refund("40.00")
        self.assertEqual(response.status_code, 302)
        again, _ = self.refund("40.00", token=token)
        self.assertEqual(again.status_code, 302)
        shown = " ".join(str(m) for m in again.wsgi_request._messages)
        self.assertIn("already issued", shown)
        self.assertEqual(Payment.objects.filter(payment_type=Payment.PaymentType.REFUND).count(), 1)
        self.refresh()
        self.assertEqual(self.order.refunded_amount, Decimal("40.00"))

    def test_an_over_refund_is_a_message_and_stores_nothing(self):
        response, _ = self.refund("100.01")
        self.assertRedirects(response, self.page + "#refund", fetch_redirect_response=False)
        shown = " ".join(str(m) for m in response.wsgi_request._messages)
        self.assertIn("still refundable", shown)
        self.assertEqual(Payment.objects.filter(payment_type=Payment.PaymentType.REFUND).count(), 0)

    def test_a_closed_period_is_a_message(self):
        today = datetime.date.today()
        period = FiscalPeriod.objects.create(
            company=self.company, name=f"FY{today.year}", status=FiscalPeriod.Status.CLOSED,
            start_date=datetime.date(today.year, 1, 1), end_date=datetime.date(today.year, 12, 31),
        )
        response, _ = self.refund("100.00")
        shown = " ".join(str(m) for m in response.wsgi_request._messages)
        self.assertIn("Not refunded", shown)
        self.assertIn(period.name, shown)
        self.assertEqual(Payment.objects.filter(payment_type=Payment.PaymentType.REFUND).count(), 0)
