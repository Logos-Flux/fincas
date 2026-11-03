"""F-033: idempotent order creation via Idempotency-Key / external_id.

Mirrors payments.charge_with_idempotency: the header carries the dedup key
(falling back to body external_id), the lookup is company-scoped under
select_for_update, retried POSTs get the original order with 200, and a
blank key always creates.
"""

from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company
from customers.models import Customer
from orders.models import Order

from .test_recalculate import company_user

URL = "/api/v1/orders/orders/"


class OrderIdempotencyTests(TestCase):
    def setUp(self):
        self.company_a = Company.objects.create(name="Example Supply, Inc")
        self.company_b = Company.objects.create(name="Rival, Inc")
        self.customer_a = Customer.objects.create(
            first_name="A", last_name="B", email="idem-a@example.com",
            company=self.company_a,
        )
        self.customer_b = Customer.objects.create(
            first_name="X", last_name="Y", email="idem-b@example.com",
            company=self.company_b,
        )
        self.client_a = APIClient()
        self.client_a.force_authenticate(user=company_user(self.company_a))
        self.client_b = APIClient()
        self.client_b.force_authenticate(
            user=company_user(self.company_b, username="rival", uid="uid-rival")
        )

    def test_double_post_same_body_returns_original(self):
        first = self.client_a.post(
            URL,
            {"customer": str(self.customer_a.pk), "external_id": "webhook-1"},
            format="json",
        )
        self.assertEqual(first.status_code, 201)
        second = self.client_a.post(
            URL,
            {"customer": str(self.customer_a.pk), "external_id": "webhook-1"},
            format="json",
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(
            Order.objects.filter(external_id="webhook-1").count(), 1
        )

    def test_header_key_falls_back_and_persists(self):
        first = self.client_a.post(
            URL,
            {"customer": str(self.customer_a.pk)},
            format="json",
            HTTP_IDEMPOTENCY_KEY="hdr-abc-123",
        )
        self.assertEqual(first.status_code, 201)
        order = Order.objects.get(pk=first.data["id"])
        self.assertEqual(order.external_id, "hdr-abc-123")
        second = self.client_a.post(
            URL,
            {"customer": str(self.customer_a.pk)},
            format="json",
            HTTP_IDEMPOTENCY_KEY="hdr-abc-123",
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(Order.objects.filter(company=self.company_a).count(), 1)

    def test_blank_key_always_creates(self):
        for _ in range(2):
            response = self.client_a.post(
                URL, {"customer": str(self.customer_a.pk)}, format="json"
            )
            self.assertEqual(response.status_code, 201)
        self.assertEqual(Order.objects.filter(company=self.company_a).count(), 2)

    def test_same_key_in_other_company_creates_fresh(self):
        # Explicit numbers: order_number is globally unique while the
        # per-company sequences both start at 1.
        mine = self.client_a.post(
            URL,
            {
                "customer": str(self.customer_a.pk),
                "external_id": "shared-key",
                "order_number": "SO-A-00001",
            },
            format="json",
        )
        self.assertEqual(mine.status_code, 201)
        theirs = self.client_b.post(
            URL,
            {
                "customer": str(self.customer_b.pk),
                "external_id": "shared-key",
                "order_number": "SO-B-00001",
            },
            format="json",
        )
        self.assertEqual(theirs.status_code, 201)
        self.assertNotEqual(theirs.data["id"], mine.data["id"])
        self.assertEqual(Order.objects.filter(external_id="shared-key").count(), 2)
