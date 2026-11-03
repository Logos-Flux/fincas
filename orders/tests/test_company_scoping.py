"""SOL-16 company-scoping contract for child / line-item viewsets.

Regression guard: the line-item viewsets (OrderLineItemViewSet,
QuoteLineItemViewSet, SubOrderViewSet) scope via an FK traversal and used
to read ``request.user.userprofile.company`` directly. AnonymousUser — and
the API service-account superuser — have no ``userprofile``, so that raised
AttributeError -> HTTP 500 instead of a safe, fail-closed empty result.
These tests assert: never 500, fail closed for anonymous, no cross-company
leak, and same-company visibility.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import Company
from core.tests.helpers import make_staff_user
from customers.models import Customer
from orders.models import Order, OrderLineItem
from orders.views import OrderLineItemViewSet

from .test_recalculate import company_user


class OrderLineItemScopingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="ls@example.com", company=self.company,
        )
        self.order = Order.objects.create(customer=self.customer, company=self.company)
        self.line = OrderLineItem.objects.create(
            order=self.order, item_name="A", sku="A-1",
            quantity=Decimal("1"), unit_price_ex_tax=Decimal("10.00"),
            line_total_ex_tax=Decimal("10.00"), line_tax=Decimal("0"),
        )
        self.factory = APIRequestFactory()
        self.list_view = OrderLineItemViewSet.as_view({"get": "list"})

    def _list(self, user):
        request = self.factory.get("/api/v1/orders/order-line-items/")
        force_authenticate(request, user=user)
        return self.list_view(request)

    def test_anonymous_is_fail_closed_not_500(self):
        # The bug this guards: anonymous -> AttributeError -> 500. Under
        # prod permission semantics (explicit IsAuthenticated, F-003)
        # anonymous is rejected at the auth layer: 401/403, never 500,
        # never data.
        response = self._list(user=None)
        self.assertIn(response.status_code, (401, 403))

    def test_same_company_user_sees_own_lines(self):
        response = self._list(user=company_user(self.company))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)

    def test_other_company_user_sees_nothing(self):
        other = Company.objects.create(name="Rival, Inc")
        response = self._list(
            user=company_user(other, username="rival", uid="uid-rival")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_superuser_service_account_sees_all(self):

        su = User.objects.create_user(username="svc", password="pw", is_superuser=True)
        response = self._list(user=su)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)


class OrderCrudMatrixTests(TestCase):
    """Full tenancy matrix on OrderViewSet via APIClient + /erp/ views.

    Read scoping (list/retrieve/``?company=`` tamper) rides
    ``scope_to_company``; write scoping (create-under/move-to-B) rides
    ``CompanyScopedViewSet._enforce_company`` (400, S6 mass-assignment).
    """

    def setUp(self):
        from rest_framework.test import APIClient

        self.company_a = Company.objects.create(name="Example Supply, Inc")
        self.company_b = Company.objects.create(name="Rival, Inc")
        self.customer_a = Customer.objects.create(
            first_name="A", last_name="B", email="mxa@example.com",
            company=self.company_a,
        )
        self.customer_b = Customer.objects.create(
            first_name="X", last_name="Y", email="mxb@example.com",
            company=self.company_b,
        )
        # Explicit number: order_number is globally unique while the
        # per-company sequences both start at 1.
        self.order_b = Order.objects.create(
            customer=self.customer_b, company=self.company_b,
            order_number="SO-B-00001",
        )
        self.user_a = company_user(self.company_a)
        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_list_excludes_other_company(self):
        response = self.client.get("/api/v1/orders/orders/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_company_param_tamper_returns_nothing(self):
        response = self.client.get(
            f"/api/v1/orders/orders/?company={self.company_b.pk}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_retrieve_other_company_404(self):
        response = self.client.get(
            f"/api/v1/orders/orders/{self.order_b.pk}/"
        )
        self.assertEqual(response.status_code, 404)

    def test_update_other_company_404_and_unchanged(self):
        response = self.client.patch(
            f"/api/v1/orders/orders/{self.order_b.pk}/",
            {"external_id": "hijack"}, format="json",
        )
        self.assertEqual(response.status_code, 404)
        self.order_b.refresh_from_db()
        self.assertNotEqual(self.order_b.external_id, "hijack")

    def test_delete_other_company_404_and_preserved(self):
        response = self.client.delete(
            f"/api/v1/orders/orders/{self.order_b.pk}/"
        )
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Order.objects.filter(pk=self.order_b.pk).exists())

    def test_create_under_other_company_is_400(self):
        response = self.client.post(
            "/api/v1/orders/orders/",
            {"customer": str(self.customer_b.pk), "company": str(self.company_b.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.filter(company=self.company_b).count(), 1)

    def test_create_defaults_to_own_company(self):
        response = self.client.post(
            "/api/v1/orders/orders/",
            {"customer": str(self.customer_a.pk)}, format="json",
        )
        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(pk=response.data["id"])
        self.assertEqual(order.company, self.company_a)

    def test_move_to_other_company_is_400(self):
        order_a = Order.objects.create(
            customer=self.customer_a, company=self.company_a,
        )
        response = self.client.patch(
            f"/api/v1/orders/orders/{order_a.pk}/",
            {"company": str(self.company_b.pk)}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        order_a.refresh_from_db()
        self.assertEqual(order_a.company, self.company_a)

    def test_gap_erp_order_list_shows_other_company(self):
        """KNOWN GAP (M1 Track A): /erp/ has no user->company link, so any
        logged-in staff sees every company's orders in the list."""
        staff = make_staff_user("erpstaff", password="pw", is_staff=True)
        self.client.force_login(staff)
        from django.urls import reverse

        response = self.client.get(reverse("erp-order-list"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.order_b.order_number)

    def test_gap_erp_order_edit_guessable_cross_company(self):
        """KNOWN GAP (M1 Track A): /erp/ detail/update views take any PK."""
        staff = make_staff_user("erpstaff2", password="pw", is_staff=True)
        self.client.force_login(staff)
        from django.urls import reverse

        response = self.client.get(
            reverse("erp-order-edit", args=[self.order_b.pk])
        )
        self.assertEqual(response.status_code, 200)
