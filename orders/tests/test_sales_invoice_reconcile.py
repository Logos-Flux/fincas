"""Reconcile endpoint tests.

POST /api/v1/orders/sales-invoices/by-token/<uuid>/reconcile/ is called by
the Qwik Stripe webhook. Guards the reconciliation invariants:

- partial payment -> PARTIAL with amount_paid/balance_due accumulated from
  the captured payment ledger (never a single event's amount)
- a second payment completing the total -> PAID (+paid_date, order advances)
- duplicate webhook (same transaction_id) -> single Payment row, totals
  unchanged
- late "processing"/"failed" after PAID -> no downgrade

Migrations are disabled under test settings, so the DB-level partial unique
constraints (uniq_payment_txn / uniq_order_stripe_pi) don't exist in the
test DB — idempotency is exercised at the application level (get_or_create).
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from accounting.tests.test_posting import make_posting_rules
from core.models import Company
from core.tests.helpers import make_staff_user
from customers.models import Customer
from orders.models import Order, SalesInvoice
from orders.views import sales_invoice_reconcile
from payments.models import Payment


class SalesInvoiceReconcileTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            first_name="Web",
            last_name="Hook",
            email="hook@example.com",
            company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            status=Order.Status.AWAITING_PAYMENT,
            payment_status=Order.PaymentStatus.UNPAID,
            grand_total=Decimal("1000.00"),
            balance_due=Decimal("1000.00"),
        )
        self.invoice = SalesInvoice.objects.create(
            order=self.order,
            company=self.company,
            status=SalesInvoice.Status.SENT,
            grand_total=Decimal("1000.00"),
            balance_due=Decimal("1000.00"),
        )
        self.user = make_staff_user(username="worker", password="pw")
        self.factory = APIRequestFactory()

    def _reconcile(self, *, event="paid", amount_cents=0, fee_cents=0, pi="pi_test_1", method="card"):
        request = self.factory.post(
            f"/api/v1/orders/sales-invoices/by-token/{self.invoice.payment_token}/reconcile/",
            {
                "event": event,
                "amount_cents": amount_cents,
                "convenience_fee_cents": fee_cents,
                "stripe_payment_intent_id": pi,
                "method": method,
            },
            format="json",
        )
        force_authenticate(request, user=self.user)
        return sales_invoice_reconcile(request, token=self.invoice.payment_token)

    def _refresh(self):
        self.invoice.refresh_from_db()
        self.order.refresh_from_db()

    def test_partial_payment_marks_partial_with_accumulated_amounts(self):
        # $412.00 gross, $12.00 convenience fee -> $400.00 applied to invoice
        response = self._reconcile(amount_cents=41200, fee_cents=1200, pi="pi_partial")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], SalesInvoice.Status.PARTIAL)

        self._refresh()
        self.assertEqual(self.invoice.status, SalesInvoice.Status.PARTIAL)
        self.assertEqual(self.invoice.amount_paid, Decimal("400.00"))
        self.assertEqual(self.invoice.balance_due, Decimal("600.00"))
        self.assertIsNone(self.invoice.paid_date)

        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PARTIAL)
        self.assertEqual(self.order.amount_paid, Decimal("400.00"))
        self.assertEqual(self.order.balance_due, Decimal("600.00"))
        # Not fully paid — order must not advance out of AWAITING_PAYMENT.
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)
        self.assertEqual(self.order.stripe_payment_intent_id, "pi_partial")

        payment = Payment.objects.get(transaction_id="pi_partial")
        self.assertEqual(payment.status, Payment.Status.CAPTURED)
        self.assertEqual(payment.amount, Decimal("412.00"))
        self.assertEqual(payment.convenience_fee, Decimal("12.00"))
        self.assertRegex(payment.payment_number, re.compile(r"^PAY-\d{5}$"))

    def test_second_payment_completes_the_total(self):
        self._reconcile(amount_cents=41200, fee_cents=1200, pi="pi_first")
        # $618.00 gross, $18.00 fee -> $600.00 applied; 400 + 600 = 1000
        response = self._reconcile(amount_cents=61800, fee_cents=1800, pi="pi_second")
        self.assertEqual(response.data["status"], SalesInvoice.Status.PAID)

        self._refresh()
        self.assertEqual(self.invoice.status, SalesInvoice.Status.PAID)
        self.assertEqual(self.invoice.amount_paid, Decimal("1000.00"))
        self.assertEqual(self.invoice.balance_due, Decimal("0.00"))
        self.assertEqual(self.invoice.paid_date, date.today())

        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(self.order.amount_paid, Decimal("1000.00"))
        self.assertEqual(self.order.balance_due, Decimal("0.00"))
        self.assertEqual(self.order.status, Order.Status.AWAITING_FULFILLMENT)

        self.assertEqual(Payment.objects.count(), 2)

    def test_duplicate_webhook_is_idempotent(self):
        self._reconcile(amount_cents=103000, fee_cents=3000, pi="pi_full")
        self._refresh()
        first_paid_date = self.invoice.paid_date

        response = self._reconcile(amount_cents=103000, fee_cents=3000, pi="pi_full")
        self.assertEqual(response.status_code, 200)

        self._refresh()
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(self.invoice.status, SalesInvoice.Status.PAID)
        self.assertEqual(self.invoice.amount_paid, Decimal("1000.00"))
        self.assertEqual(self.invoice.balance_due, Decimal("0.00"))
        self.assertEqual(self.invoice.paid_date, first_paid_date)
        self.assertEqual(self.order.amount_paid, Decimal("1000.00"))
        self.assertEqual(self.order.balance_due, Decimal("0.00"))

    def test_duplicate_partial_webhook_does_not_double_count(self):
        self._reconcile(amount_cents=41200, fee_cents=1200, pi="pi_partial")
        self._reconcile(amount_cents=41200, fee_cents=1200, pi="pi_partial")

        self._refresh()
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(self.invoice.status, SalesInvoice.Status.PARTIAL)
        self.assertEqual(self.invoice.amount_paid, Decimal("400.00"))
        self.assertEqual(self.invoice.balance_due, Decimal("600.00"))
        self.assertEqual(self.order.amount_paid, Decimal("400.00"))
        self.assertEqual(self.order.balance_due, Decimal("600.00"))

    def test_late_processing_event_does_not_downgrade_paid(self):
        self._reconcile(amount_cents=100000, fee_cents=0, pi="pi_ach", method="ach")
        self._refresh()
        self.assertEqual(self.invoice.status, SalesInvoice.Status.PAID)

        response = self._reconcile(event="processing", amount_cents=100000, pi="pi_ach", method="ach")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], SalesInvoice.Status.PAID)

        self._refresh()
        self.assertEqual(self.invoice.status, SalesInvoice.Status.PAID)
        self.assertEqual(self.invoice.amount_paid, Decimal("1000.00"))
        self.assertEqual(self.invoice.paid_date, date.today())
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(Payment.objects.count(), 1)

    def test_late_failed_event_does_not_downgrade_paid(self):
        self._reconcile(amount_cents=103000, fee_cents=3000, pi="pi_full")
        response = self._reconcile(event="failed", amount_cents=103000, pi="pi_full")
        self.assertEqual(response.data["status"], SalesInvoice.Status.PAID)

        self._refresh()
        self.assertEqual(self.invoice.status, SalesInvoice.Status.PAID)

    def test_processing_then_paid_promotes_the_same_payment_row(self):
        self._reconcile(event="processing", amount_cents=100000, pi="pi_ach", method="ach")
        self._refresh()
        self.assertEqual(self.invoice.status, SalesInvoice.Status.PROCESSING)
        payment = Payment.objects.get(transaction_id="pi_ach")
        self.assertEqual(payment.status, Payment.Status.PENDING)

        self._reconcile(amount_cents=100000, fee_cents=0, pi="pi_ach", method="ach")
        self._refresh()
        self.assertEqual(Payment.objects.count(), 1)
        payment.refresh_from_db()
        self.assertEqual(payment.status, Payment.Status.CAPTURED)
        self.assertIsNotNone(payment.payment_date)
        self.assertIsNotNone(payment.settled_date)
        self.assertEqual(self.invoice.status, SalesInvoice.Status.PAID)
        self.assertEqual(self.invoice.amount_paid, Decimal("1000.00"))
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PAID)
