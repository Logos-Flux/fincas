"""Company-scoping contract for fulfillment viewsets.

PickTicket scopes via ``order__company``; PickTicketLine via
``pick_ticket__order__company``. Anonymous / cross-company callers must
never see another company's tickets (fail closed, never 500).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIRequestFactory, force_authenticate

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from customers.models import Customer
from fulfillment.models import PickTicket, PickTicketLine
from fulfillment.views import PickTicketLineViewSet, PickTicketViewSet
from orders.models import Order


def company_user(company, *, username="staff", uid="uid-staff"):
    user = make_staff_user(username=username, password="pw")
    user.userprofile = UserProfile.objects.create(company=company)
    return user


class PickTicketScopingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        customer = Customer.objects.create(
            first_name="A", last_name="B", email="ft@example.com", company=self.company,
        )
        self.order = Order.objects.create(customer=customer, company=self.company)
        self.ticket = PickTicket.objects.create(order=self.order)
        self.factory = APIRequestFactory()
        self.list_view = PickTicketViewSet.as_view({"get": "list"})
        self.detail_view = PickTicketViewSet.as_view({"get": "retrieve"})

    def _list(self, user):
        request = self.factory.get("/api/v1/fulfillment/pick-tickets/")
        force_authenticate(request, user=user)
        return self.list_view(request)

    def _retrieve(self, user, pk):
        request = self.factory.get(f"/api/v1/fulfillment/pick-tickets/{pk}/")
        force_authenticate(request, user=user)
        return self.detail_view(request, pk=pk)

    def test_anonymous_is_fail_closed_not_500(self):
        # Prod permission semantics (explicit IsAuthenticated, F-003):
        # rejected at the auth layer — never 500, never data.
        response = self._list(user=None)
        self.assertIn(response.status_code, (401, 403))

    def test_same_company_user_sees_own_tickets(self):
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

    def test_other_company_user_cannot_guess_pk(self):
        other = Company.objects.create(name="Rival2, Inc")
        response = self._retrieve(
            user=company_user(other, username="rival2", uid="uid-rival2"),
            pk=self.ticket.pk,
        )
        self.assertEqual(response.status_code, 404)

    def test_superuser_service_account_sees_all(self):
        su = User.objects.create_user(username="svc", password="pw", is_superuser=True)
        response = self._list(user=su)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)


class PickTicketLineScopingTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        customer = Customer.objects.create(
            first_name="A", last_name="B", email="fl@example.com", company=self.company,
        )
        order = Order.objects.create(customer=customer, company=self.company)
        self.ticket = PickTicket.objects.create(order=order)
        self.factory = APIRequestFactory()
        self.list_view = PickTicketLineViewSet.as_view({"get": "list"})

    def _list(self, user):
        request = self.factory.get("/api/v1/fulfillment/pick-ticket-lines/")
        force_authenticate(request, user=user)
        return self.list_view(request)

    def test_other_company_user_sees_nothing(self):
        from decimal import Decimal

        from products.models import Product, ProductVariant

        product = Product.objects.create(title="Panel", part_number="P-9", slug="p-9")
        variant = ProductVariant.objects.create(product=product, sku="P-9-A")
        PickTicketLine.objects.create(
            pick_ticket=self.ticket, variant=variant, quantity=Decimal("1"),
        )
        other = Company.objects.create(name="Rival, Inc")
        response = self._list(
            user=company_user(other, username="rival", uid="uid-rival")
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 0)

    def test_anonymous_line_list_is_fail_closed_not_500(self):
        response = self._list(user=None)
        self.assertIn(response.status_code, (401, 403))
