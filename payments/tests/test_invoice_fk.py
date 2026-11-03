from decimal import Decimal

from django.test import TestCase

from accounting.tests.test_posting import make_posting_rules
from core.models import Company
from customers.models import Customer
from orders.models import Order, SalesInvoice
from payments.models import Payment


class PaymentInvoiceFKTest(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            first_name="Test", last_name="User", email="test@test.com", company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer, company=self.company, grand_total=Decimal("100.00"),
        )
        self.invoice = SalesInvoice.objects.create(
            invoice_number="INV-BC999",
            order=self.order,
            company=self.company,
            grand_total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
        )

    def test_payment_can_link_to_invoice(self):
        payment = Payment.objects.create(
            order=self.order,
            invoice=self.invoice,
            amount=Decimal("100.00"),
            payment_method=Payment.PaymentMethod.CREDIT_CARD,
            status=Payment.Status.CAPTURED,
            company=self.company,
        )
        self.assertEqual(payment.invoice, self.invoice)
        self.assertIn(payment, self.invoice.payments.all())

    def test_payment_invoice_is_optional(self):
        payment = Payment.objects.create(
            order=self.order,
            amount=Decimal("50.00"),
            payment_method=Payment.PaymentMethod.CHECK,
            status=Payment.Status.CAPTURED,
            company=self.company,
        )
        self.assertIsNone(payment.invoice)
