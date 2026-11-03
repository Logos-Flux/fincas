"""Quote → order → invoice → payment → shipment chain totals.

Each conversion must carry the money forward exactly: the order matches the
quote, the invoice snapshots the order, the reconcile settles both ledgers
to the same paid/owing figures, and fulfilling/ship­ping the goods never
moves the money. Any drift between steps is a wrong-total (S1) bug.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from accounting.tests.test_posting import make_posting_rules
from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from customers.models import Customer
from orders.convert import convert_order_to_invoice, convert_quote_to_order
from orders.models import (
    Order,
    Quote,
    QuoteLineItem,
    SalesInvoice,
    SubOrder,
)
from orders.views import sales_invoice_reconcile
from payments.models import Payment

_user_seq = [0]


def company_user(company):
    _user_seq[0] += 1
    n = _user_seq[0]
    user = make_staff_user(username=f"chain-{n}", password="pw")
    user.userprofile = UserProfile.objects.create(
        company=company
    )
    return user


class FullChainTotalsTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="chain@example.com",
            company=self.company,
        )
        self.quote = Quote.objects.create(
            customer=self.customer,
            company=self.company,
            shipping_cost_ex_tax=Decimal("60.00"),
            discount_percent=Decimal("10.00"),
            status=Quote.Status.SENT,
        )
        # 4 @ 25 = 100 (tax 8) + 2 @ 100 = 200 (tax 16):
        # subtotal 300, 10% -> 30 off, tax 24, ship 60 -> grand 354.
        QuoteLineItem.objects.create(
            quote=self.quote, item_name="Widget", sku="W-1",
            quantity=Decimal("4"), unit_price_ex_tax=Decimal("25.00"),
            line_total_ex_tax=Decimal("100.00"), line_tax=Decimal("8.00"),
        )
        QuoteLineItem.objects.create(
            quote=self.quote, item_name="Gadget", sku="G-1",
            quantity=Decimal("2"), unit_price_ex_tax=Decimal("100.00"),
            line_total_ex_tax=Decimal("200.00"), line_tax=Decimal("16.00"),
        )
        self.quote.recalculate_totals()
        self.quote.refresh_from_db()

    def _reconcile(self, invoice, *, event, amount_cents, pi_id="pi_chain_1"):
        factory = APIRequestFactory()
        request = factory.post(
            f"/api/v1/orders/sales-invoices/reconcile/{invoice.payment_token}/",
            {
                "event": event,
                "amount_cents": amount_cents,
                "convenience_fee_cents": 0,
                "stripe_payment_intent_id": pi_id,
                "method": "card",
                "transaction_id": pi_id,
            },
            format="json",
        )
        force_authenticate(request, user=company_user(self.company))
        return sales_invoice_reconcile(request, token=str(invoice.payment_token))

    def test_quote_to_order_carries_totals(self):
        order, created = convert_quote_to_order(self.quote)
        self.assertTrue(created)
        for field in (
            "subtotal_ex_tax", "tax_total", "shipping_cost_ex_tax",
            "discount_amount", "grand_total",
        ):
            with self.subTest(field=field):
                self.assertEqual(getattr(order, field), getattr(self.quote, field))
        # A fresh order owes its full total (quotes carry no balance_due leg).
        self.assertEqual(order.balance_due, self.quote.grand_total)
        self.assertEqual(order.grand_total, Decimal("354.00"))
        # Quote is marked converted, not left open.
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.ACCEPTED)
        self.assertEqual(self.quote.converted_to_order_id, order.pk)

    def test_order_to_invoice_snapshots_totals(self):
        order, _ = convert_quote_to_order(self.quote)
        invoice, created = convert_order_to_invoice(order)
        self.assertTrue(created)
        self.assertEqual(invoice.subtotal_ex_tax, order.subtotal_ex_tax)
        self.assertEqual(invoice.tax_total, order.tax_total)
        self.assertEqual(invoice.shipping_cost, order.shipping_cost_ex_tax)
        self.assertEqual(invoice.grand_total, order.grand_total)
        self.assertEqual(invoice.balance_due, order.grand_total)
        self.assertEqual(invoice.amount_paid, Decimal("0"))

    def test_full_chain_to_paid_and_shipped(self):
        order, _ = convert_quote_to_order(self.quote)
        invoice, _ = convert_order_to_invoice(order)
        order.status = Order.Status.AWAITING_PAYMENT
        order.save(update_fields=["status", "updated_at"])

        response = self._reconcile(invoice, event="paid", amount_cents=35400)
        self.assertEqual(response.status_code, 200)

        invoice.refresh_from_db()
        order.refresh_from_db()
        # Invoice and order ledgers agree with each other and the quote.
        self.assertEqual(invoice.status, SalesInvoice.Status.PAID)
        self.assertEqual(invoice.amount_paid, Decimal("354.00"))
        self.assertEqual(invoice.balance_due, Decimal("0.00"))
        self.assertEqual(order.payment_status, Order.PaymentStatus.PAID)
        self.assertEqual(order.amount_paid, Decimal("354.00"))
        self.assertEqual(order.balance_due, Decimal("0.00"))
        self.assertEqual(order.status, Order.Status.AWAITING_FULFILLMENT)
        self.assertEqual(
            Payment.objects.filter(order=order, status=Payment.Status.CAPTURED).count(), 1
        )

        # Shipment leg: walk a sub-order to delivered; the money must not
        # move and the order must complete.
        sub = SubOrder.objects.create(
            sub_order_number="SUB-CHAIN-1", order=order,
            status=SubOrder.Status.PACKED,
        )
        for nxt in (
            SubOrder.Status.SHIPPED, SubOrder.Status.IN_TRANSIT,
            SubOrder.Status.DELIVERED,
        ):
            sub.transition_to(nxt)
        order.refresh_from_db()
        invoice.refresh_from_db()
        self.assertEqual(order.status, Order.Status.COMPLETED)
        self.assertEqual(order.fulfillment_status, Order.FulfillmentStatus.FULFILLED)
        self.assertEqual(order.grand_total, Decimal("354.00"))
        self.assertEqual(invoice.grand_total, Decimal("354.00"))

    def test_partial_payment_splits_invoice_and_order_evenly(self):
        order, _ = convert_quote_to_order(self.quote)
        invoice, _ = convert_order_to_invoice(order)
        order.status = Order.Status.AWAITING_PAYMENT
        order.save(update_fields=["status", "updated_at"])

        response = self._reconcile(invoice, event="paid", amount_cents=15400)
        self.assertEqual(response.status_code, 200)
        invoice.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoice.Status.PARTIAL)
        self.assertEqual(invoice.amount_paid, Decimal("154.00"))
        self.assertEqual(invoice.balance_due, Decimal("200.00"))
        self.assertEqual(order.payment_status, Order.PaymentStatus.PARTIAL)
        self.assertEqual(order.balance_due, Decimal("200.00"))

    def test_duplicate_webhook_does_not_double_count(self):
        order, _ = convert_quote_to_order(self.quote)
        invoice, _ = convert_order_to_invoice(order)
        order.status = Order.Status.AWAITING_PAYMENT
        order.save(update_fields=["status", "updated_at"])

        self._reconcile(invoice, event="paid", amount_cents=35400)
        self._reconcile(invoice, event="paid", amount_cents=35400)
        invoice.refresh_from_db()
        self.assertEqual(invoice.amount_paid, Decimal("354.00"))
        self.assertEqual(
            Payment.objects.filter(
                order=order, payment_type=Payment.PaymentType.PAYMENT
            ).count(),
            1,
        )
