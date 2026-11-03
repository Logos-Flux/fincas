"""Milestone 2 Track D — token-gated invoice isolation.

Extends orders/tests/test_sales_invoice_by_token.py per testing-plan §5:
enumerated tokens must 404 (no oracle distinguishing "no such invoice"
from "not yours"), expired tokens 410 with a machine-readable error.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounting.tests.test_posting import make_posting_rules
from core.models import Company
from customers.models import Customer
from orders.models import Order, SalesInvoice


class TokenInvoiceIsolationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Token Corp")
        make_posting_rules(self.company)
        customer = Customer.objects.create(
            first_name="Tess",
            last_name="Token",
            email="tess@example.com",
            company=self.company,
        )
        order = Order.objects.create(
            customer=customer,
            company=self.company,
            status=Order.Status.AWAITING_PAYMENT,
            payment_status=Order.PaymentStatus.UNPAID,
            subtotal_ex_tax=Decimal("500.00"),
            grand_total=Decimal("500.00"),
            balance_due=Decimal("500.00"),
        )
        self.invoice = SalesInvoice.objects.create(
            order=order,
            company=self.company,
            status=SalesInvoice.Status.SENT,
            invoice_date=date.today(),
            subtotal_ex_tax=Decimal("500.00"),
            grand_total=Decimal("500.00"),
            balance_due=Decimal("500.00"),
        )
        self.client = Client()

    def _url(self, token):
        return reverse("sales-invoice-by-token", kwargs={"token": token})

    def test_enumerated_tokens_404_without_oracle(self):
        """Sequential/random UUIDs give the same 404 as any unknown token —
        no status/body difference leaks which tokens exist."""
        responses = [
            self.client.get(self._url(uuid.uuid4())) for _ in range(3)
        ] + [self.client.get(self._url("00000000-0000-0000-0000-000000000000"))]
        for response in responses:
            with self.subTest(url=response.wsgi_request.path):
                self.assertEqual(response.status_code, 404)
        bodies = {r.content for r in responses}
        self.assertEqual(len(bodies), 1)

    def test_expired_token_410_with_machine_readable_error(self):
        self.invoice.payment_token_expires_at = (
            timezone.now() - timedelta(hours=1)
        )
        self.invoice.save(update_fields=["payment_token_expires_at"])
        response = self.client.get(self._url(self.invoice.payment_token))
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["error"], "link_expired")

    def test_valid_token_still_200_after_other_invoice_expires(self):
        other_order = Order.objects.create(
            customer=self.invoice.order.customer,
            company=self.company,
            subtotal_ex_tax=Decimal("10.00"),
            grand_total=Decimal("10.00"),
            balance_due=Decimal("10.00"),
        )
        other = SalesInvoice.objects.create(
            order=other_order,
            company=self.company,
            invoice_date=date.today(),
            subtotal_ex_tax=Decimal("10.00"),
            grand_total=Decimal("10.00"),
            balance_due=Decimal("10.00"),
            payment_token_expires_at=timezone.now() - timedelta(hours=1),
        )
        self.assertEqual(
            self.client.get(self._url(other.payment_token)).status_code, 410
        )
        self.assertEqual(
            self.client.get(self._url(self.invoice.payment_token)).status_code,
            200,
        )
