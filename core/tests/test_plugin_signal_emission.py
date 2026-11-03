"""Lifecycle signals fire from the real service paths, once, after commit.

One integration test per signal in core/plugins/signals.py, each driving a
real flow (DRF endpoint or service function), plus failure isolation: a
receiver that raises never fails or rolls back the emitting transaction.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.test import TestCase, TransactionTestCase
from rest_framework.test import APIClient

from accounting.tests.test_posting import make_posting_rules
from core.models import Company
from core.plugins import signals
from customers.models import Customer
from inventory.models import Warehouse, WarehouseStock
from orders.models import Order, SalesInvoice, SubOrder
from orders.tests.test_recalculate import company_user
from payments.models import Payment
from payments.services import charge_with_idempotency
from products.models import Product, ProductVariant


class Recorder:
    """Connects to one signal for a test; collects (sender, kwargs)."""

    def __init__(self, test, signal):
        self.calls = []
        signal.connect(self, weak=False)
        test.addCleanup(signal.disconnect, self)

    def __call__(self, sender, signal=None, **kwargs):
        self.calls.append((sender, kwargs))


class _Fixtures:
    def make_world(self):
        self.company = Company.objects.create(name="Signal Co")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            first_name="Sig", last_name="Nal", email="signal@example.com",
            company=self.company,
        )
        self.api = APIClient()
        self.api.force_authenticate(user=company_user(self.company))

    def make_order(self, status=Order.Status.PENDING, total="200.00"):
        return Order.objects.create(
            customer=self.customer, company=self.company, status=status,
            grand_total=Decimal(total),
        )


class LifecycleSignalEmissionTests(_Fixtures, TestCase):
    def setUp(self):
        self.make_world()

    def test_order_placed_on_api_create(self):
        placed = Recorder(self, signals.order_placed)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.api.post(
                "/api/v1/orders/orders/", {"customer": str(self.customer.pk)}, format="json",
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(len(placed.calls), 1)
        sender, kwargs = placed.calls[0]
        self.assertIs(sender, Order)
        self.assertEqual(str(kwargs["order"].pk), response.data["id"])

    def test_order_created_cancelled_is_not_placed(self):
        # An imported historical order that arrives already cancelled or
        # declined was never "placed" from a receiver's point of view.
        placed = Recorder(self, signals.order_placed)
        with self.captureOnCommitCallbacks(execute=True):
            self.make_order(status=Order.Status.CANCELLED)
            self.make_order(status=Order.Status.DECLINED)
        self.assertEqual(placed.calls, [])

    def test_duplicate_is_a_draft_until_moved_to_pending(self):
        from orders.duplicate import duplicate_order

        source = self.make_order()
        placed = Recorder(self, signals.order_placed)
        with self.captureOnCommitCallbacks(execute=True):
            copy = duplicate_order(source)
        self.assertEqual(placed.calls, [])
        with self.captureOnCommitCallbacks(execute=True):
            copy.status = Order.Status.PENDING
            copy.save()
        self.assertEqual([kw["order"].pk for _, kw in placed.calls], [copy.pk])

    def test_order_status_changed_on_transition(self):
        order = self.make_order()
        changed = Recorder(self, signals.order_status_changed)
        cancelled = Recorder(self, signals.order_cancelled)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.api.post(
                f"/api/v1/orders/orders/{order.pk}/transition/",
                {"status": "awaiting_payment"}, format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(changed.calls), 1)
        sender, kwargs = changed.calls[0]
        self.assertIs(sender, Order)
        self.assertEqual(kwargs["order"].pk, order.pk)
        self.assertEqual(kwargs["old_status"], "pending")
        self.assertEqual(kwargs["new_status"], "awaiting_payment")
        self.assertEqual(cancelled.calls, [])

    def test_order_cancelled_on_transition(self):
        order = self.make_order()
        changed = Recorder(self, signals.order_status_changed)
        cancelled = Recorder(self, signals.order_cancelled)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.api.post(
                f"/api/v1/orders/orders/{order.pk}/transition/",
                {"status": "cancelled"}, format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(cancelled.calls), 1)
        sender, kwargs = cancelled.calls[0]
        self.assertIs(sender, Order)
        self.assertEqual(kwargs["order"].pk, order.pk)
        self.assertEqual(kwargs["reason"], "")
        self.assertEqual(len(changed.calls), 1)

    def test_payment_completed_on_charge(self):
        order = self.make_order(status=Order.Status.AWAITING_PAYMENT)
        invoice = SalesInvoice.objects.create(
            invoice_number="INV-SIG1", order=order, company=self.company,
            grand_total=Decimal("200.00"), balance_due=Decimal("200.00"),
        )
        completed = Recorder(self, signals.payment_completed)
        with self.captureOnCommitCallbacks(execute=True):
            payment = charge_with_idempotency(
                gateway_id="manual", order=order, invoice=invoice,
                amount=Decimal("200.00"), payment_source="CHECK-1",
                idempotency_key="sig-key-1",
            )
            # A retried charge returns the original row and must not re-emit.
            charge_with_idempotency(
                gateway_id="manual", order=order, invoice=invoice,
                amount=Decimal("200.00"), payment_source="CHECK-1",
                idempotency_key="sig-key-1",
            )
        self.assertEqual(len(completed.calls), 1)
        sender, kwargs = completed.calls[0]
        self.assertIs(sender, Payment)
        self.assertEqual(kwargs["payment"].pk, payment.pk)

    def test_payment_refunded_on_api_status_update(self):
        order = self.make_order()
        payment = Payment.objects.create(
            order=order, company=self.company, amount=Decimal("50.00"),
            net_amount=Decimal("50.00"), status=Payment.Status.CAPTURED,
        )
        refunded = Recorder(self, signals.payment_refunded)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.api.patch(
                f"/api/v1/payments/payments/{payment.pk}/",
                {"status": "refunded"}, format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(refunded.calls), 1)
        sender, kwargs = refunded.calls[0]
        self.assertIs(sender, Payment)
        self.assertEqual(kwargs["payment"].pk, payment.pk)
        self.assertIsNone(kwargs["refund"])

    def _sub_order(self, status):
        order = self.make_order(status=Order.Status.AWAITING_SHIPMENT)
        return SubOrder.objects.create(
            sub_order_number="SUB-SIG-1", order=order, status=status,
            tracking_number="1Z999",
        )

    def test_shipment_dispatched_on_transition(self):
        sub = self._sub_order(SubOrder.Status.PACKED)
        dispatched = Recorder(self, signals.shipment_dispatched)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.api.post(
                f"/api/v1/orders/sub-orders/{sub.pk}/transition/",
                {"status": "shipped"}, format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(dispatched.calls), 1)
        sender, kwargs = dispatched.calls[0]
        self.assertIs(sender, SubOrder)
        self.assertEqual(kwargs["shipment"].pk, sub.pk)
        self.assertEqual(kwargs["tracking_number"], "1Z999")

    def test_shipment_delivered_on_transition(self):
        sub = self._sub_order(SubOrder.Status.SHIPPED)
        dispatched = Recorder(self, signals.shipment_dispatched)
        delivered = Recorder(self, signals.shipment_delivered)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.api.post(
                f"/api/v1/orders/sub-orders/{sub.pk}/transition/",
                {"status": "delivered"}, format="json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(delivered.calls), 1)
        sender, kwargs = delivered.calls[0]
        self.assertIs(sender, SubOrder)
        self.assertEqual(kwargs["shipment"].pk, sub.pk)
        self.assertEqual(dispatched.calls, [])

    def test_inventory_adjusted_on_api_quantity_update(self):
        warehouse = Warehouse.objects.create(
            warehouse_code="WH-SIG", name="Sig WH", company=self.company,
        )
        product = Product.objects.create(
            title="Panel", part_number="P-SIG", slug="p-sig", company=self.company,
        )
        variant = ProductVariant.objects.create(product=product, sku="P-SIG-A")
        stock = WarehouseStock.objects.create(
            warehouse=warehouse, variant=variant, quantity=Decimal("42"),
        )
        adjusted = Recorder(self, signals.inventory_adjusted)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.api.patch(
                f"/api/v1/inventory/stock/{stock.pk}/", {"quantity": "50"}, format="json",
            )
            # A save that leaves quantity alone is not an adjustment.
            self.api.patch(
                f"/api/v1/inventory/stock/{stock.pk}/", {"bin_location": "A-1"}, format="json",
            )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(adjusted.calls), 1)
        sender, kwargs = adjusted.calls[0]
        self.assertIs(sender, WarehouseStock)
        self.assertEqual(kwargs["warehouse_stock"].pk, stock.pk)
        self.assertEqual(kwargs["delta"], Decimal("8"))
        self.assertEqual(kwargs["reason"], "")

    def test_inventory_adjusted_with_f_expression_quantity(self):
        from django.db.models import F

        warehouse = Warehouse.objects.create(
            warehouse_code="WH-F", name="F WH", company=self.company,
        )
        product = Product.objects.create(
            title="Panel F", part_number="P-F", slug="p-f", company=self.company,
        )
        variant = ProductVariant.objects.create(product=product, sku="P-F-A")
        stock = WarehouseStock.objects.create(
            warehouse=warehouse, variant=variant, quantity=Decimal("10"),
        )
        adjusted = Recorder(self, signals.inventory_adjusted)
        with self.captureOnCommitCallbacks(execute=True):
            stock.quantity = F("quantity") - 3
            stock.save()
        self.assertEqual(stock.quantity, Decimal("7"))
        self.assertEqual([kw["delta"] for _, kw in adjusted.calls], [Decimal("-3")])

    def test_customer_registered_on_api_create(self):
        registered = Recorder(self, signals.customer_registered)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.api.post(
                "/api/v1/customers/customers/",
                {"first_name": "New", "last_name": "Buyer", "email": "new@example.com",
                 "company": str(self.company.pk)},
                format="json",
            )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(len(registered.calls), 1)
        sender, kwargs = registered.calls[0]
        self.assertIs(sender, Customer)
        self.assertEqual(str(kwargs["customer"].pk), response.data["id"])

    def test_rolled_back_transition_never_emits(self):
        order = self.make_order()
        changed = Recorder(self, signals.order_status_changed)
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    order.transition_to(Order.Status.AWAITING_PAYMENT)
                    raise RuntimeError("abort")
            except RuntimeError:
                pass
        self.assertEqual(changed.calls, [])


class ReceiverFailureIsolationTests(_Fixtures, TransactionTestCase):
    """Real commits: a raising receiver cannot fail the emitting transaction."""

    def setUp(self):
        self.make_world()

    def _raising_receiver(self, signal, label):
        def receiver(sender, **kwargs):
            # Write first, then raise: the write must roll back with the
            # receiver, the emitter's work must not.
            Company.objects.create(name=f"receiver-write-{label}")
            raise RuntimeError(f"{label} receiver blew up")

        receiver.__qualname__ = f"exploding_{label}_receiver"
        signal.connect(receiver, weak=False)
        self.addCleanup(signal.disconnect, receiver)
        return receiver

    def test_payment_completed_receiver_raises(self):
        order = self.make_order(status=Order.Status.AWAITING_PAYMENT)
        invoice = SalesInvoice.objects.create(
            invoice_number="INV-ISO1", order=order, company=self.company,
            grand_total=Decimal("200.00"), balance_due=Decimal("200.00"),
        )
        self._raising_receiver(signals.payment_completed, "payment")
        after = Recorder(self, signals.payment_completed)

        with self.assertLogs("golden_erp.plugins.signals", level="ERROR") as logs:
            payment = charge_with_idempotency(
                gateway_id="manual", order=order, invoice=invoice,
                amount=Decimal("200.00"), payment_source="CHECK-ISO",
                idempotency_key="iso-key-1",
            )

        # The emitting transaction committed, and the DB is usable after.
        self.assertTrue(Payment.objects.filter(pk=payment.pk).exists())
        invoice.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(invoice.status, SalesInvoice.Status.PAID)
        self.assertEqual(order.status, Order.Status.AWAITING_FULFILLMENT)
        self.assertFalse(Company.objects.filter(name="receiver-write-payment").exists())
        # Later receivers still run.
        self.assertEqual(len(after.calls), 1)

        record = logs.records[0]
        message = record.getMessage()
        self.assertIn("payment_completed", message)
        self.assertIn("exploding_payment_receiver", message)
        self.assertIn(str(payment.pk), message)
        self.assertIsNotNone(record.exc_info)

    def test_order_status_receiver_raises(self):
        order = self.make_order()
        self._raising_receiver(signals.order_status_changed, "order")
        api = APIClient()
        api.force_authenticate(user=company_user(self.company, username="iso", uid="uid-iso"))

        with self.assertLogs("golden_erp.plugins.signals", level="ERROR") as logs:
            response = api.post(
                f"/api/v1/orders/orders/{order.pk}/transition/",
                {"status": "awaiting_payment"}, format="json",
            )

        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.AWAITING_PAYMENT)
        self.assertEqual(Order.objects.filter(pk=order.pk).count(), 1)
        self.assertFalse(Company.objects.filter(name="receiver-write-order").exists())
        message = logs.records[0].getMessage()
        self.assertIn("order_status_changed", message)
        self.assertIn("exploding_order_receiver", message)
        self.assertIn(str(order.pk), message)
