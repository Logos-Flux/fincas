"""Guarded status transitions: quote, order, sub-order (shipment).

``SubOrder.transition_to`` validated moves but was never wired to the API —
any status could be PUT/PATCHed directly — and Quote/Order had no
transition map at all. These tests pin the guarded path
(testing-plan §3 state machines, extending the test_suborder_status_guard
pattern to the API):

- legal moves succeed (model + ``POST .../transition/`` → 200),
- illegal moves fail: model raises, API returns 400 with the pinned DRF
  error envelope (``{"status": [...]}`` — §4, never HTML, never ``detail``),
- terminal states (cancelled/returned/accepted/completed/...) are immutable.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from customers.models import Customer
from orders.models import Order, Quote, SubOrder
from orders.views import OrderViewSet, QuoteViewSet, SubOrderViewSet


def company_user(company, *, username, uid):
    user = make_staff_user(username=username, password="pw")
    user.userprofile = UserProfile.objects.create(company=company)
    return user


def post_transition(viewset, pk, user, target):
    factory = APIRequestFactory()
    request = factory.post(
        "/api/v1/transition/", {"status": target}, format="json"
    )
    force_authenticate(request, user=user)
    view = viewset.as_view({"post": "transition"})
    return view(request, pk=str(pk))


class QuoteTransitionTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="qt@example.com", company=self.company,
        )
        self.user = company_user(self.company, username="qt-staff", uid="uid-qt")
        self.quote = Quote.objects.create(
            customer=self.customer, company=self.company,
            status=Quote.Status.DRAFT,
        )

    def test_legal_move_succeeds(self):
        self.quote.transition_to(Quote.Status.SENT)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.SENT)

    def test_illegal_move_raises(self):
        with self.assertRaises(ValidationError):
            self.quote.transition_to(Quote.Status.ACCEPTED)

    def test_terminal_accepted_is_immutable(self):
        self.quote.status = Quote.Status.ACCEPTED
        self.quote.save(update_fields=["status", "updated_at"])
        with self.assertRaises(ValidationError):
            self.quote.transition_to(Quote.Status.SENT)

    def test_api_legal_move_returns_200(self):
        response = post_transition(QuoteViewSet, self.quote.pk, self.user, "sent")
        self.assertEqual(response.status_code, 200)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.SENT)

    def test_api_illegal_move_returns_400_field_envelope(self):
        response = post_transition(QuoteViewSet, self.quote.pk, self.user, "accepted")
        self.assertEqual(response.status_code, 400)
        payload = response.data
        self.assertIsInstance(payload, dict)
        self.assertIn("status", payload)
        self.assertNotIn("detail", payload)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.DRAFT)

    def test_api_terminal_state_is_immutable(self):
        self.quote.status = Quote.Status.CANCELLED
        self.quote.save(update_fields=["status", "updated_at"])
        response = post_transition(QuoteViewSet, self.quote.pk, self.user, "sent")
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.data)


class OrderTransitionTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="ot@example.com", company=self.company,
        )
        self.user = company_user(self.company, username="ot-staff", uid="uid-ot")
        self.order = Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.PENDING,
        )

    def test_legal_move_succeeds(self):
        self.order.transition_to(Order.Status.AWAITING_PAYMENT)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)

    def test_illegal_move_raises(self):
        with self.assertRaises(ValidationError):
            self.order.transition_to(Order.Status.SHIPPED)

    def test_terminal_completed_is_immutable(self):
        self.order.status = Order.Status.COMPLETED
        self.order.save(update_fields=["status", "updated_at"])
        with self.assertRaises(ValidationError):
            self.order.transition_to(Order.Status.DISPUTED)

    def test_api_legal_move_returns_200(self):
        response = post_transition(
            OrderViewSet, self.order.pk, self.user, "awaiting_payment"
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.AWAITING_PAYMENT)

    def test_api_illegal_move_returns_400_field_envelope(self):
        response = post_transition(OrderViewSet, self.order.pk, self.user, "shipped")
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.data)
        self.assertNotIn("detail", response.data)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)

    def test_api_terminal_state_is_immutable(self):
        self.order.status = Order.Status.CANCELLED
        self.order.save(update_fields=["status", "updated_at"])
        response = post_transition(
            OrderViewSet, self.order.pk, self.user, "awaiting_payment"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.data)


class SubOrderTransitionApiTests(TestCase):
    """The transition_to map on SubOrder, now enforced at the API boundary."""

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="st@example.com", company=self.company,
        )
        self.user = company_user(self.company, username="st-staff", uid="uid-st")
        self.order = Order.objects.create(
            customer=self.customer, company=self.company,
            status=Order.Status.AWAITING_SHIPMENT,
        )
        self.sub = SubOrder.objects.create(
            sub_order_number="SUB-TRANS-1",
            order=self.order,
            status=SubOrder.Status.PACKED,
        )

    def test_api_legal_move_succeeds_and_rolls_up(self):
        response = post_transition(SubOrderViewSet, self.sub.pk, self.user, "shipped")
        self.assertEqual(response.status_code, 200)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.status, SubOrder.Status.SHIPPED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.SHIPPED)

    def test_api_illegal_move_returns_400_field_envelope(self):
        pending = SubOrder.objects.create(
            sub_order_number="SUB-TRANS-2",
            order=self.order,
            status=SubOrder.Status.PENDING,
        )
        response = post_transition(SubOrderViewSet, pending.pk, self.user, "shipped")
        self.assertEqual(response.status_code, 400)
        payload = response.data
        self.assertIsInstance(payload, dict)
        self.assertIn("status", payload)
        self.assertNotIn("detail", payload)
        pending.refresh_from_db()
        self.assertEqual(pending.status, SubOrder.Status.PENDING)

    def test_api_terminal_cancelled_is_immutable(self):
        self.sub.status = SubOrder.Status.CANCELLED
        self.sub.save(update_fields=["status", "updated_at"])
        response = post_transition(SubOrderViewSet, self.sub.pk, self.user, "pending")
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.data)

    def test_api_terminal_returned_is_immutable(self):
        self.sub.status = SubOrder.Status.RETURNED
        self.sub.save(update_fields=["status", "updated_at"])
        response = post_transition(SubOrderViewSet, self.sub.pk, self.user, "pending")
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.data)
