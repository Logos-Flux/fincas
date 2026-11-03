from decimal import Decimal

from django.test import TestCase

from core.models import Company
from customers.models import Customer
from orders.duplicate import duplicate_order, duplicate_quote
from orders.models import Order, OrderLineItem, Quote, QuoteLineItem


class DuplicateOrderTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="dup@example.com", company=self.company,
        )
        self.source = Order.objects.create(
            customer=self.customer,
            company=self.company,
            status=Order.Status.SHIPPED,
            payment_status=Order.PaymentStatus.PAID,
            fulfillment_status=Order.FulfillmentStatus.FULFILLED,
            grand_total=Decimal("100.00"),
            amount_paid=Decimal("100.00"),
            refunded_amount=Decimal("5.00"),
            stripe_payment_intent_id="pi_abc",
            internal_notes="copy me",
            customer_message="thanks!",
            channel="web",
            currency="USD",
        )
        OrderLineItem.objects.create(
            order=self.source,
            item_name="Widget",
            sku="WDG-1",
            quantity=Decimal("2"),
            quantity_shipped=Decimal("2"),
            unit_price_ex_tax=Decimal("50.00"),
            line_total_ex_tax=Decimal("100.00"),
            fulfillment_status=OrderLineItem.FulfillmentStatus.SHIPPED,
        )

    def test_duplicate_order_resets_status_and_financials(self):
        new_order = duplicate_order(self.source)

        # Fresh identifiers
        self.assertNotEqual(new_order.id, self.source.id)
        self.assertNotEqual(new_order.order_number, self.source.order_number)
        self.assertTrue(new_order.order_number.startswith("SO-"))

        # Reset fields
        self.assertEqual(new_order.status, Order.Status.INCOMPLETE)
        self.assertEqual(new_order.payment_status, Order.PaymentStatus.UNPAID)
        self.assertEqual(new_order.fulfillment_status, Order.FulfillmentStatus.UNFULFILLED)
        self.assertEqual(new_order.amount_paid, Decimal("0"))
        self.assertEqual(new_order.refunded_amount, Decimal("0"))
        self.assertEqual(new_order.stripe_payment_intent_id, "")
        self.assertIsNone(new_order.confirmation_sent_at)

        # Copied fields
        self.assertEqual(new_order.customer, self.customer)
        self.assertEqual(new_order.internal_notes, "copy me")
        self.assertEqual(new_order.customer_message, "thanks!")
        self.assertEqual(new_order.channel, "web")
        self.assertEqual(new_order.grand_total, Decimal("100.00"))

        # Audit link
        self.assertEqual(new_order.amended_from_id, self.source.id)

    def test_duplicate_order_copies_line_items_with_shipping_reset(self):
        new_order = duplicate_order(self.source)

        lines = list(new_order.line_items.all())
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line.sku, "WDG-1")
        self.assertEqual(line.quantity, Decimal("2.000"))
        self.assertEqual(line.unit_price_ex_tax, Decimal("50.0000"))
        self.assertEqual(line.quantity_shipped, Decimal("0"))
        self.assertEqual(line.fulfillment_status, OrderLineItem.FulfillmentStatus.PENDING)
        self.assertNotEqual(line.id, self.source.line_items.first().id)

    def test_duplicate_is_idempotent(self):
        a = duplicate_order(self.source)
        b = duplicate_order(self.source)
        self.assertNotEqual(a.id, b.id)
        self.assertNotEqual(a.order_number, b.order_number)
        # Source untouched.
        self.source.refresh_from_db()
        self.assertEqual(self.source.status, Order.Status.SHIPPED)
        self.assertEqual(self.source.amount_paid, Decimal("100.00"))


class DuplicateQuoteTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="dupq@example.com", company=self.company,
        )
        self.source = Quote.objects.create(
            customer=self.customer,
            company=self.company,
            status=Quote.Status.ACCEPTED,
            customer_notes="customer-side text",
            internal_notes="staff-only text",
            grand_total=Decimal("250.00"),
        )
        # Pretend it was already accepted/converted.
        from products.models import Product, ProductVariant
        product = Product.objects.create(
            title="P", part_number="P-1", slug="p-1",
        )
        variant = ProductVariant.objects.create(
            product=product, sku="P-1-V1",
        )
        QuoteLineItem.objects.create(
            quote=self.source,
            variant=variant,
            item_name="Item",
            sku="P-1-V1",
            quantity=Decimal("3"),
            unit_price_ex_tax=Decimal("80.00"),
            is_optional=True,
            sort_order=2,
        )

    def test_duplicate_quote_resets_status_and_keeps_lines(self):
        new_quote = duplicate_quote(self.source)

        self.assertNotEqual(new_quote.id, self.source.id)
        self.assertTrue(new_quote.quote_number.startswith("QT-"))
        self.assertEqual(new_quote.status, Quote.Status.DRAFT)
        # A duplicate is a new deal: its own open engagement (CRM Phase 3, E5).
        self.assertEqual(self.source.engagement.status, "won")
        self.assertNotEqual(new_quote.engagement_id, self.source.engagement_id)
        self.assertEqual(new_quote.engagement.status, "open")
        with self.assertWarns(DeprecationWarning):
            self.assertEqual(new_quote.sales_stage, Quote.SalesStage.QUALIFICATION)
        self.assertIsNone(new_quote.converted_to_order)
        self.assertEqual(new_quote.amended_from_id, self.source.id)

        lines = list(new_quote.line_items.all())
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0].sku, "P-1-V1")
        self.assertEqual(lines[0].quantity, Decimal("3.000"))
        self.assertTrue(lines[0].is_optional)
        self.assertEqual(lines[0].sort_order, 2)
