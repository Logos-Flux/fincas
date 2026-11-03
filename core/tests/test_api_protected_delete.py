"""API ``DELETE`` of a PROTECT-referenced row is a 409, never a 500.

The access matrix found ``ProtectedError`` escaping every viewset whose
model is the target of an ``on_delete=PROTECT`` FK (Company, Customer,
Order, Product, ProductVariant, Supplier): DRF only translates its own
exceptions and Django's ``Http404`` / ``PermissionDenied``, so the ORM
error reached the 500 handler. ``core.api_exceptions.exception_handler``
(the REST_FRAMEWORK ``EXCEPTION_HANDLER``) maps ``ProtectedError`` and
``RestrictedError`` to 409 with a stable ``code`` and the protecting
relations, for every viewset at once.
"""

from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company
from core.tests.helpers import make_staff_user
from customers.models import Customer
from orders.models import Quote


class ProtectedDeleteTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.user = make_staff_user(
            "root", "root@example.com", "pw", is_staff=True, is_superuser=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_customer_with_a_quote_is_409_protected(self):
        customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace", email="ada@example.com",
            company=self.company,
        )
        Quote.objects.create(customer=customer, company=self.company)

        response = self.client.delete(f"/api/v1/customers/customers/{customer.pk}/")

        self.assertEqual(response.status_code, 409, response.content)
        payload = response.json()
        self.assertEqual(payload["code"], "protected")
        self.assertIn("orders.Quote", payload["protected_by"])
        self.assertIn("orders.Quote", payload["detail"])
        self.assertTrue(Customer.objects.filter(pk=customer.pk).exists())

    def test_company_with_its_seeded_pipeline_is_409_protected(self):
        # Every Company is seeded with a crm.Pipeline that PROTECTs it.
        response = self.client.delete(f"/api/v1/core/companies/{self.company.pk}/")

        self.assertEqual(response.status_code, 409, response.content)
        payload = response.json()
        self.assertEqual(payload["code"], "protected")
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertIn("crm.Pipeline", payload["protected_by"])
        self.assertTrue(Company.objects.filter(pk=self.company.pk).exists())

    def test_unreferenced_row_still_deletes(self):
        customer = Customer.objects.create(
            first_name="Bob", last_name="Free", email="bob@example.com",
            company=self.company,
        )
        response = self.client.delete(f"/api/v1/customers/customers/{customer.pk}/")
        self.assertEqual(response.status_code, 204, response.content)
