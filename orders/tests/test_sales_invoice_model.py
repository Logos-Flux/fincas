from datetime import date
from decimal import Decimal

from django.test import TestCase

from accounting.tests.test_posting import make_posting_rules
from core.models import Company
from customers.models import Customer
from orders.models import Order, SalesInvoice


class SalesInvoiceModelTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            first_name="James",
            last_name="Tapscott",
            email="jtapscott66@gmail.com",
            company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            subtotal_ex_tax=Decimal("1355.94"),
            shipping_cost_ex_tax=Decimal("222.63"),
            grand_total=Decimal("1578.57"),
        )

    def test_create_invoice_with_bc_number(self):
        inv = SalesInvoice.objects.create(
            invoice_number="INV-BC743",
            order=self.order,
            company=self.company,
            subtotal_ex_tax=Decimal("1355.94"),
            shipping_cost=Decimal("222.63"),
            grand_total=Decimal("1578.57"),
            amount_paid=Decimal("1578.57"),
            balance_due=Decimal("0.00"),
            status=SalesInvoice.Status.PAID,
            paid_date=date(2025, 8, 6),
        )
        self.assertEqual(inv.invoice_number, "INV-BC743")
        self.assertEqual(inv.status, "paid")
        self.assertEqual(inv.balance_due, Decimal("0.00"))
        self.assertIsNotNone(inv.payment_token)

    def test_auto_generate_invoice_number(self):
        inv = SalesInvoice.objects.create(
            order=self.order,
            company=self.company,
            grand_total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
        )
        self.assertTrue(inv.invoice_number.startswith("INV-"))
        self.assertEqual(len(inv.invoice_number), 9)  # INV-00001

    def test_default_status_is_draft(self):
        inv = SalesInvoice.objects.create(
            order=self.order,
            company=self.company,
            grand_total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
        )
        self.assertEqual(inv.status, "draft")

    def test_payment_terms_choices(self):
        inv = SalesInvoice.objects.create(
            order=self.order,
            company=self.company,
            payment_terms=SalesInvoice.PaymentTerms.NET_30,
            grand_total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
        )
        self.assertEqual(inv.payment_terms, "net_30")
