"""Serializer enforcement: direct status/totals writes rejected (F-018, S1).

Pins the 2026-09-10 decision that transition maps and server-computed
totals are authoritative at the DRF boundary:

- PATCH of ``status`` on Order / Quote / SubOrder → 400 with the pinned
  field-error envelope — even when the requested move would be legal per
  the transition map. Legal moves go through ``POST .../transition/``
  only (covered by test_status_transitions.py).
- PATCH of server-computed totals (grand_total, subtotal*/tax_total/
  balance_due/amount_paid) → 400 and the stored value is unchanged;
  ``POST .../recalculate/`` recomputes them from the line items.
- Non-status, non-total fields (status_note, sales_stage, ...) and the
  initial status on CREATE remain writable.
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from customers.models import Customer
from orders.models import Order, OrderLineItem, Quote, QuoteLineItem, SubOrder
from orders.views import OrderViewSet, QuoteViewSet, SubOrderViewSet


def company_user(company, *, username, uid):
    user = make_staff_user(username=username, password="pw")
    user.userprofile = UserProfile.objects.create(company=company)
    return user


def patch_detail(viewset, pk, user, payload):
    factory = APIRequestFactory()
    request = factory.patch("/api/v1/patch/", payload, format="json")
    force_authenticate(request, user=user)
    view = viewset.as_view({"patch": "partial_update"})
    return view(request, pk=str(pk))


def post_action(viewset, action_name, pk, user, payload=None):
    factory = APIRequestFactory()
    request = factory.post("/api/v1/action/", payload or {}, format="json")
    force_authenticate(request, user=user)
    view = viewset.as_view({"post": action_name})
    return view(request, pk=str(pk))


class OrderDirectWriteTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="se-order@example.com",
            company=self.company,
        )
        self.user = company_user(self.company, username="se-ot", uid="uid-se-ot")
        self.order = Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.PENDING,
        )

    def test_patch_status_rejected_even_for_legal_move(self):
        # awaiting_payment IS a legal move from pending, but direct PATCH
        # is still rejected — the transition action is the only writer.
        response = patch_detail(
            OrderViewSet, self.order.pk, self.user, {"status": "awaiting_payment"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.data)
        self.assertNotIn("detail", response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)

    def test_patch_status_rejected_for_illegal_move(self):
        response = patch_detail(
            OrderViewSet, self.order.pk, self.user, {"status": "shipped"}
        )
        self.assertEqual(response.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)

    def test_patch_server_totals_rejected(self):
        for field in ("grand_total", "subtotal_ex_tax", "subtotal_inc_tax",
                      "tax_total", "balance_due", "amount_paid"):
            with self.subTest(field=field):
                response = patch_detail(
                    OrderViewSet, self.order.pk, self.user, {field: "9999.99"}
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)
                self.order.refresh_from_db()
                self.assertEqual(getattr(self.order, field), Decimal("0.00"))

    def test_patch_status_note_still_allowed(self):
        response = patch_detail(
            OrderViewSet, self.order.pk, self.user, {"status_note": "Called customer"}
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status_note, "Called customer")

    def test_create_with_initial_status_allowed(self):
        factory = APIRequestFactory()
        request = factory.post(
            "/api/v1/orders/orders/",
            {"customer": str(self.customer.pk), "status": "pending"},
            format="json",
        )
        force_authenticate(request, user=self.user)
        view = OrderViewSet.as_view({"post": "create"})
        response = view(request)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["status"], "pending")


class OrderRecalculateEndpointTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="se-recalc@example.com",
            company=self.company,
        )
        self.user = company_user(self.company, username="se-rt", uid="uid-se-rt")
        self.order = Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.PENDING,
        )
        OrderLineItem.objects.create(
            order=self.order, item_name="Widget", sku="W-1",
            quantity=Decimal("4"), unit_price_ex_tax=Decimal("25.00"),
        )

    def test_totals_patch_rejected_and_recalculate_computes(self):
        # Stale/attacker-set totals cannot be written... (line-item signals
        # have already recalculated the parent to 100.00 at creation).
        self.assertEqual(self.order.grand_total, Decimal("100.00"))
        response = patch_detail(
            OrderViewSet, self.order.pk, self.user, {"grand_total": "9999.99"}
        )
        self.assertEqual(response.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.grand_total, Decimal("100.00"))

        # ...and the recalculate action derives them from the line items.
        response = post_action(OrderViewSet, "recalculate", self.order.pk, self.user)
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.subtotal_ex_tax, Decimal("100.00"))
        self.assertEqual(self.order.grand_total, Decimal("100.00"))
        self.assertEqual(self.order.balance_due, Decimal("100.00"))


class QuoteDirectWriteTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="se-quote@example.com",
            company=self.company,
        )
        self.user = company_user(self.company, username="se-qt", uid="uid-se-qt")
        self.quote = Quote.objects.create(
            customer=self.customer, company=self.company,
            status=Quote.Status.DRAFT,
        )

    def test_patch_status_rejected(self):
        # sent IS legal from draft; PATCH is still rejected.
        response = patch_detail(
            QuoteViewSet, self.quote.pk, self.user, {"status": "sent"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.data)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.DRAFT)

    def test_patch_server_totals_rejected(self):
        for field in ("grand_total", "subtotal_ex_tax", "tax_total"):
            with self.subTest(field=field):
                response = patch_detail(
                    QuoteViewSet, self.quote.pk, self.user, {field: "4242.42"}
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(field, response.data)
                self.quote.refresh_from_db()
                self.assertEqual(getattr(self.quote, field), Decimal("0.00"))

    def test_recalculate_produces_correct_totals(self):
        QuoteLineItem.objects.create(
            quote=self.quote, item_name="Gadget", sku="G-1",
            quantity=Decimal("2"), unit_price_ex_tax=Decimal("100.00"),
            line_tax=Decimal("16.00"),
        )
        response = post_action(QuoteViewSet, "recalculate", self.quote.pk, self.user)
        self.assertEqual(response.status_code, 200)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.subtotal_ex_tax, Decimal("200.00"))
        self.assertEqual(self.quote.tax_total, Decimal("16.00"))
        self.assertEqual(self.quote.grand_total, Decimal("216.00"))


class SubOrderDirectWriteTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="se-sub@example.com",
            company=self.company,
        )
        self.user = company_user(self.company, username="se-st", uid="uid-se-st")
        self.order = Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.AWAITING_SHIPMENT,
        )
        self.sub = SubOrder.objects.create(
            sub_order_number="SUB-SE-1", order=self.order,
            status=SubOrder.Status.PACKED,
        )

    def test_patch_status_rejected(self):
        # shipped IS legal from packed; PATCH is still rejected and the
        # parent-order roll-up does not fire.
        response = patch_detail(
            SubOrderViewSet, self.sub.pk, self.user, {"status": "shipped"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.data)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, SubOrder.Status.PACKED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_SHIPMENT)

    def test_patch_tracking_still_allowed(self):
        response = patch_detail(
            SubOrderViewSet, self.sub.pk, self.user, {"tracking_number": "1Z999"}
        )
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.tracking_number, "1Z999")
