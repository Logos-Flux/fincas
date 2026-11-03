from datetime import date
from decimal import Decimal

from django.test import TestCase

from accounting.tests.test_posting import make_posting_rules
from core.models import Company
from customers.models import Customer
from documents.services.pdf import render_invoice_html, render_invoice_pdf
from orders.models import Order, OrderLineItem, SalesInvoice


class InvoicePDFServiceTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            first_name="James", last_name="Tapscott",
            email="jtapscott66@gmail.com", company=self.company,
        )
        self.order = Order.objects.create(
            order_number="SO-BC743",
            customer=self.customer,
            company=self.company,
            subtotal_ex_tax=Decimal("1355.94"),
            shipping_cost_ex_tax=Decimal("222.63"),
            grand_total=Decimal("1578.57"),
            payment_terms="Prepaid",
            customer_po_number="QN40950",
        )
        OrderLineItem.objects.create(
            order=self.order,
            sku="UNIPGRM-5P2",
            item_name="Tamarack UNI-PGRM Top of Pole Mount — 5 Solar Panels / 38\"-72\"",
            quantity=1,
            unit_price_ex_tax=Decimal("1355.94"),
            line_total_ex_tax=Decimal("1355.94"),
        )
        self.invoice = SalesInvoice.objects.create(
            invoice_number="INV-BC743",
            order=self.order,
            company=self.company,
            invoice_date=date(2025, 8, 6),
            payment_terms=SalesInvoice.PaymentTerms.PREPAID,
            subtotal_ex_tax=Decimal("1355.94"),
            shipping_cost=Decimal("222.63"),
            grand_total=Decimal("1578.57"),
            amount_paid=Decimal("1578.57"),
            balance_due=Decimal("0.00"),
            status=SalesInvoice.Status.PAID,
            paid_date=date(2025, 8, 6),
        )

    def test_render_invoice_html_contains_key_data(self):
        html = render_invoice_html(self.invoice)
        self.assertIn("SALES INVOICE", html)
        self.assertIn("INV-BC743", html)
        self.assertIn("James", html)
        self.assertIn("Tapscott", html)
        self.assertIn("UNIPGRM-5P2", html)
        self.assertIn("1,355.94", html)
        self.assertIn("1,578.57", html)
        self.assertIn("PAID IN FULL", html)

    def test_render_invoice_pdf_returns_bytes(self):
        pdf_bytes = render_invoice_pdf(self.invoice)
        self.assertIsInstance(pdf_bytes, bytes)
        self.assertGreater(len(pdf_bytes), 1000)
        # PDF magic bytes
        self.assertTrue(pdf_bytes.startswith(b"%PDF"))
