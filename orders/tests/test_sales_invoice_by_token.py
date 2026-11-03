"""Public pay-by-link endpoint tests.

Guards the no-auth GET /api/v1/orders/sales-invoices/by-token/<uuid>/
endpoint used by the storefront's /pay/<token> page. Verifies the happy
path payload, the 404/410 error branches, and that the serializer does
not leak internal-only fields.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounting.tests.test_posting import make_posting_rules
from core.models import Company
from customers.models import Customer
from orders.models import Order, OrderLineItem, SalesInvoice


class SalesInvoiceByTokenTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply Inc")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            first_name="Test",
            last_name="Payer",
            email="payer@example.com",
            company_name="Test Payer LLC",
            company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            status=Order.Status.AWAITING_PAYMENT,
            payment_status=Order.PaymentStatus.UNPAID,
            subtotal_ex_tax=Decimal("1000.00"),
            grand_total=Decimal("1000.00"),
            balance_due=Decimal("1000.00"),
        )
        OrderLineItem.objects.create(
            order=self.order,
            item_name="Consulting hours",
            sku="MISC-CONSULT",
            quantity=Decimal("4"),
            unit_price_ex_tax=Decimal("250.00"),
            line_total_ex_tax=Decimal("1000.00"),
        )
        self.invoice = SalesInvoice.objects.create(
            order=self.order,
            company=self.company,
            status=SalesInvoice.Status.SENT,
            invoice_date=date.today(),
            payment_instructions="Pay by ACH or card.",
            subtotal_ex_tax=Decimal("1000.00"),
            grand_total=Decimal("1000.00"),
            balance_due=Decimal("1000.00"),
            internal_notes="DO NOT LEAK — internal",
        )
        self.client = Client()
        self.url = reverse(
            "sales-invoice-by-token", kwargs={"token": self.invoice.payment_token}
        )

    def test_returns_200_with_public_shape(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["invoice_number"], self.invoice.invoice_number)
        self.assertEqual(data["grand_total"], "1000.00")
        self.assertEqual(data["payment_instructions"], "Pay by ACH or card.")
        self.assertEqual(data["customer_name"], "Test Payer")
        self.assertEqual(data["customer_company_name"], "Test Payer LLC")
        self.assertEqual(data["company_name"], "Example Supply Inc")
        self.assertEqual(data["order_number"], self.order.order_number)
        self.assertEqual(len(data["line_items"]), 1)
        self.assertEqual(data["line_items"][0]["item_name"], "Consulting hours")

    def test_does_not_leak_internal_notes(self):
        response = self.client.get(self.url)
        self.assertNotIn("internal_notes", response.json())
        # And certainly not the string itself in any field.
        self.assertNotIn("DO NOT LEAK", response.content.decode())

    def test_404_on_unknown_token(self):
        bad_url = reverse(
            "sales-invoice-by-token",
            kwargs={"token": "00000000-0000-0000-0000-000000000000"},
        )
        response = self.client.get(bad_url)
        self.assertEqual(response.status_code, 404)

    def test_410_on_expired_token(self):
        self.invoice.payment_token_expires_at = timezone.now() - timedelta(days=1)
        self.invoice.save(update_fields=["payment_token_expires_at"])
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()["error"], "link_expired")

    def test_requires_no_authentication(self):
        """Anonymous client should succeed — this is a public payer link."""
        # Client() starts anonymous; just confirm no 401/403.
        response = self.client.get(self.url)
        self.assertNotIn(response.status_code, (401, 403))
