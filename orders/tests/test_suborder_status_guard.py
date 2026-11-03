"""Tests for the terminal-status guard in Order.update_status_from_suborders.

A SubOrder status change rolls up to the parent Order via the post_save
signal (orders/signals.py → suborder_status_changed). Once an order has
reached a terminal status (cancelled, declined, disputed, partially
refunded), that roll-up must never resurrect it to an active status
(WP #764). Non-terminal orders must still roll up normally.
"""

from __future__ import annotations

from django.test import TestCase

from core.models import Company
from customers.models import Customer
from orders.models import Order, SubOrder


class SubOrderStatusGuardTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="guard@example.com", company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            status=Order.Status.AWAITING_SHIPMENT,
        )
        self.sub = SubOrder.objects.create(
            sub_order_number="SUB-GUARD-1",
            order=self.order,
            status=SubOrder.Status.PACKED,
        )

    def test_cancelled_order_stays_cancelled_after_suborder_ships(self):
        self.order.status = Order.Status.CANCELLED
        self.order.save(update_fields=["status", "updated_at"])

        # Valid transition packed → shipped; fires the post_save signal.
        self.sub.transition_to(SubOrder.Status.SHIPPED)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.CANCELLED)

    def test_all_terminal_statuses_block_rollup(self):
        for status in (
            Order.Status.CANCELLED,
            Order.Status.DECLINED,
            Order.Status.DISPUTED,
            Order.Status.PARTIALLY_REFUNDED,
        ):
            with self.subTest(status=status):
                Order.objects.filter(pk=self.order.pk).update(status=status)
                self.order.refresh_from_db()

                self.order.update_status_from_suborders()

                self.order.refresh_from_db()
                self.assertEqual(self.order.status, status)

    def test_terminal_guard_leaves_fulfillment_status_untouched(self):
        self.order.status = Order.Status.CANCELLED
        self.order.save(update_fields=["status", "updated_at"])
        original_ff = self.order.fulfillment_status

        self.sub.transition_to(SubOrder.Status.SHIPPED)

        self.order.refresh_from_db()
        self.assertEqual(self.order.fulfillment_status, original_ff)

    def test_non_terminal_order_still_rolls_up(self):
        self.sub.transition_to(SubOrder.Status.SHIPPED)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.SHIPPED)
        self.assertEqual(
            self.order.fulfillment_status, Order.FulfillmentStatus.PARTIAL
        )

    def test_non_terminal_order_completes_on_delivery(self):
        self.sub.transition_to(SubOrder.Status.SHIPPED)
        self.sub.transition_to(SubOrder.Status.DELIVERED)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.COMPLETED)
        self.assertEqual(
            self.order.fulfillment_status, Order.FulfillmentStatus.FULFILLED
        )
