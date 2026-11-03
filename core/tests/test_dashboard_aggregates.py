"""Dashboard KPIs must come from DB aggregates, not Python loops.

QA #18/#19: the dashboard computed revenue with Python sum() over every
captured payment of the month and low stock by iterating every
WarehouseStock row. Both read the whole table into Python; fine at
launch, unbounded at scale. They are now a SUM() and a COUNT() with an
F() expression. The revenue query also filtered status="CAPTURED" while
every writer stores the enum value "captured", so the KPI always read
$0.00; it now filters via Payment.Status.CAPTURED. These tests pin the
KPI semantics so a future rewrite keeps the same numbers:
captured-and-this-month payments only, and "quantity <= reorder_level"
(boundary included) among monitored lines (reorder_level > 0).
"""

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Company, UserProfile
from customers.models import Customer
from inventory.models import Warehouse, WarehouseStock
from orders.models import Order, Quote
from payments.models import Payment
from products.models import Product, ProductVariant


class DashboardKpiTests(TestCase):
    def setUp(self):
        from accounting.tests.test_posting import make_posting_rules

        self.company = Company.objects.create(name="Example Supply, Inc")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="dash@example.com", company=self.company,
        )
        user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        UserProfile.objects.create(company=self.company)
        self.client.force_login(user)

        self.warehouse = Warehouse.objects.create(
            warehouse_code="MAIN", name="Main Warehouse",
        )
        self.product = Product.objects.create(
            title="Dash Product", company=self.company,
        )
        self.variant = ProductVariant.objects.create(
            product=self.product, sku="DASH-001",
        )
        self.order = Order.objects.create(
            customer=self.customer, company=self.company, grand_total=Decimal("100.00"),
        )

    # ── revenue this month ──────────────────────────────────

    def test_revenue_sums_only_captured_payments_this_month(self):
        now = timezone.now()
        last_month = (now.replace(day=1) - timedelta(days=1)).replace(
            hour=12, minute=0, second=0, microsecond=0,
        )
        for i, (amount, status, when) in enumerate([
            (Decimal("100.50"), Payment.Status.CAPTURED, now),        # counted
            (Decimal("20.25"), Payment.Status.CAPTURED, now),         # counted
            (Decimal("500.00"), Payment.Status.PENDING, now),         # not CAPTURED
            (Decimal("1000.00"), Payment.Status.CAPTURED, last_month),  # previous month
        ]):
            Payment.objects.create(
                order=self.order,
                payment_number=f"PAY-{i:05d}",
                payment_method=Payment.PaymentMethod.CREDIT_CARD,
                payment_provider=Payment.PaymentProvider.STRIPE,
                amount=amount,
                status=status,
                payment_date=when,
                company=self.company,
            )

        response = self.client.get(reverse("erp-dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["revenue_total"], Decimal("120.75"))

    def test_revenue_total_is_zero_when_no_captured_payments(self):
        response = self.client.get(reverse("erp-dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["revenue_total"], 0)

    # ── low stock ───────────────────────────────────────────

    def test_low_stock_counts_at_or_below_reorder_level(self):
        variants = [
            ProductVariant.objects.create(
                product=self.product, sku=f"DASH-{i + 11:03d}",
            )
            for i in range(5)
        ]
        # quantity, reorder_level, expected
        stock_rows = [
            (Decimal("3"), Decimal("5"), True),    # below the level
            (Decimal("5"), Decimal("5"), True),    # boundary: at the level
            (Decimal("10"), Decimal("5"), False),  # healthy
            (Decimal("0"), Decimal("0"), False),   # reorder_level 0 = not monitored
            (Decimal("2"), Decimal("0"), False),   # reorder_level 0 = not monitored
        ]
        for variant, (qty, reorder, _) in zip(variants, stock_rows):
            WarehouseStock.objects.create(
                variant=variant, warehouse=self.warehouse,
                quantity=qty, reorder_level=reorder,
            )

        response = self.client.get(reverse("erp-dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["low_stock_count"], 2)

    # ── KPI wiring ──────────────────────────────────────────

    def test_open_orders_and_pending_quotes_counts(self):
        Order.objects.create(
            customer=self.customer, company=self.company,
            status="completed", grand_total=Decimal("10.00"),
        )
        # Stored values are the lowercase enum values; the view used to filter
        # on "SENT"/"DRAFT" literals and count nothing.
        Quote.objects.create(customer=self.customer, company=self.company, status=Quote.Status.SENT)
        Quote.objects.create(customer=self.customer, company=self.company, status="draft")
        Quote.objects.create(customer=self.customer, company=self.company, status=Quote.Status.ACCEPTED)

        response = self.client.get(reverse("erp-dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["open_order_count"], 1)
        self.assertEqual(response.context["pending_quote_count"], 2)

    def test_open_orders_means_not_shipped_yet(self):
        # F-089: a shipped (paid or not), declined, disputed or abandoned
        # order is not "open"; only orders that still need work before they
        # ship are. setUp's order is pending, so it counts.
        for status in (
            Order.Status.AWAITING_FULFILLMENT, Order.Status.PARTIALLY_SHIPPED,
            Order.Status.BACKORDERED, Order.Status.ON_HOLD,
        ):
            Order.objects.create(
                customer=self.customer, company=self.company, status=status,
                grand_total=Decimal("10.00"),
            )
        for status in (
            Order.Status.SHIPPED, Order.Status.COMPLETED, Order.Status.CANCELLED,
            Order.Status.DECLINED, Order.Status.DISPUTED,
            Order.Status.PARTIALLY_REFUNDED, Order.Status.INCOMPLETE,
        ):
            Order.objects.create(
                customer=self.customer, company=self.company, status=status,
                payment_status=Order.PaymentStatus.PAID, grand_total=Decimal("10.00"),
            )

        response = self.client.get(reverse("erp-dashboard"))

        self.assertEqual(response.context["open_order_count"], 5)

    def test_alerts_use_enum_values(self):
        Quote.objects.create(
            customer=self.customer, company=self.company, status=Quote.Status.SENT,
            quote_expiration_date=timezone.now().date() - timedelta(days=1),
        )
        Order.objects.create(
            customer=self.customer, company=self.company, grand_total=Decimal("10.00"),
            status=Order.Status.AWAITING_FULFILLMENT, payment_status=Order.PaymentStatus.UNPAID,
        )
        response = self.client.get(reverse("erp-dashboard"))
        self.assertEqual(response.context["overdue_quotes"], 1)
        self.assertEqual(response.context["unpaid_invoices"], 1)
        self.assertEqual(response.context["low_stock_count"], 0)


class DashboardQuickActionTests(TestCase):
    def setUp(self):
        from accounting.tests.test_posting import make_posting_rules

        self.company = Company.objects.create(name="QA Co")
        make_posting_rules(self.company)
        UserProfile.objects.create(company=self.company)
        self.user = User.objects.create_user(username="qa", password="pw", is_staff=True)
        self.client.force_login(self.user)

    def _actions(self):
        response = self.client.get(reverse("erp-dashboard"))
        self.assertEqual(response.status_code, 200)
        return response, {a["key"]: a for a in response.context["quick_actions"]}

    def test_all_five_show_by_default(self):
        response, actions = self._actions()
        self.assertEqual(
            list(actions),
            ["new-quote", "new-order", "new-customer", "new-product", "new-purchase-order"],
        )
        self.assertTrue(all(a["visible"] for a in actions.values()))
        self.assertEqual(response.context["quick_actions_visible"], 5)
        self.assertContains(response, reverse("erp-product-create"))
        self.assertContains(response, reverse("erp-purchaseorder-create"))
        self.assertContains(response, "New Purchase Order")

    def test_hide_is_per_user_and_ignores_unknown_keys(self):
        response = self.client.post(
            reverse("erp-dashboard-quick-actions"),
            {"show": ["new-quote", "new-order", "bogus-key"]},
        )
        self.assertRedirects(response, reverse("erp-dashboard"))
        self.user.refresh_from_db()
        self.assertEqual(
            self.user.erp_settings.hidden_quick_actions,
            ["new-customer", "new-product", "new-purchase-order"],
        )
        response, actions = self._actions()
        self.assertTrue(actions["new-quote"]["visible"])
        self.assertFalse(actions["new-product"]["visible"])
        self.assertEqual(response.context["quick_actions_visible"], 2)
        self.assertNotContains(response, 'href="%s" class="btn' % reverse("erp-product-create"))

        other = User.objects.create_user(username="qb", password="pw", is_staff=True)
        self.client.force_login(other)
        _, actions = self._actions()
        self.assertTrue(all(a["visible"] for a in actions.values()))

    def test_hide_everything_shows_hint_and_stale_keys_are_dropped(self):
        self.client.post(reverse("erp-dashboard-quick-actions"), {})
        response, actions = self._actions()
        self.assertEqual(response.context["quick_actions_visible"], 0)
        self.assertContains(response, "All quick actions are hidden")

        settings_obj = self.user.erp_settings
        settings_obj.hidden_quick_actions = ["retired-action", "new-order", 42]
        settings_obj.save()
        _, actions = self._actions()
        self.assertFalse(actions["new-order"]["visible"])
        self.assertTrue(actions["new-quote"]["visible"])

        # A scalar stored by hand must not take the dashboard down.
        settings_obj.hidden_quick_actions = 42
        settings_obj.save()
        _, actions = self._actions()
        self.assertTrue(all(a["visible"] for a in actions.values()))

    def test_get_is_not_allowed(self):
        response = self.client.get(reverse("erp-dashboard-quick-actions"))
        self.assertEqual(response.status_code, 405)
