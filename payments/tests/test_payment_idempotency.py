"""F-033: recording the same payment twice through the API creates one row.

``PaymentViewSet.create`` dedups on the ``Idempotency-Key`` header (or the
body's ``idempotency_key``), mirroring ``OrderViewSet.create``: a retried
POST with the same key gets the original Payment with 200. A blank key
keeps the old always-create behaviour.
"""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company
from customers.models import Customer
from orders.models import Order
from orders.tests.test_recalculate import company_user

from ..models import Payment

URL = "/api/v1/payments/payments/"


@patch("payments.posting.post_payment")  # posting rules are accounting's tests
class PaymentIdempotencyTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace", email="ada@example.com",
            company=self.company,
        )
        self.order = Order.objects.create(customer=self.customer, company=self.company)
        self.client = APIClient()
        self.client.force_authenticate(user=company_user(self.company))

    def _body(self, number):
        return {
            "payment_number": number, "order": str(self.order.pk),
            "payment_method": "credit_card", "payment_provider": "stripe",
            "amount": "10.00", "payment_type": "payment", "status": "captured",
        }

    def test_same_header_key_twice_records_one_payment(self, _post):
        first = self.client.post(URL, self._body("PAY-T-1"), format="json", HTTP_IDEMPOTENCY_KEY="click-1")
        second = self.client.post(URL, self._body("PAY-T-1"), format="json", HTTP_IDEMPOTENCY_KEY="click-1")

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Payment.objects.get().idempotency_key, "click-1")

    def test_body_key_dedups_too(self, _post):
        body = {**self._body("PAY-T-2"), "idempotency_key": "click-2"}
        self.client.post(URL, body, format="json")
        second = self.client.post(URL, body, format="json")
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(Payment.objects.count(), 1)

    def test_blank_key_always_creates(self, _post):
        self.assertEqual(self.client.post(URL, self._body("PAY-T-3"), format="json").status_code, 201)
        self.assertEqual(self.client.post(URL, self._body("PAY-T-4"), format="json").status_code, 201)
        self.assertEqual(Payment.objects.count(), 2)
