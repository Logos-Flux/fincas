from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase

from core.models import Company
from customers.models import Customer
from orders.convert import (
    _due_date_for,
    _map_payment_terms,
    convert_order_to_invoice,
)
from orders.models import Order, SalesInvoice


class PaymentTermsMapperTests(TestCase):
    def test_recognises_net_variants(self):
        cases = {
            "Net 30": SalesInvoice.PaymentTerms.NET_30,
            "net 30": SalesInvoice.PaymentTerms.NET_30,
            "NET-30": SalesInvoice.PaymentTerms.NET_30,
            "net30": SalesInvoice.PaymentTerms.NET_30,
            "Net 15 days": SalesInvoice.PaymentTerms.NET_15,
            "Net 45": SalesInvoice.PaymentTerms.NET_45,
            "net 60": SalesInvoice.PaymentTerms.NET_60,
        }
        for input_text, expected in cases.items():
            self.assertEqual(_map_payment_terms(input_text), expected, msg=input_text)

    def test_prefers_longest_match(self):
        # "Net 60" must not be shadowed by a shorter prefix like "Net 6"
        self.assertEqual(_map_payment_terms("Net 60"), SalesInvoice.PaymentTerms.NET_60)
        self.assertEqual(_map_payment_terms("Net 45 days"), SalesInvoice.PaymentTerms.NET_45)

    def test_prepaid_and_advance(self):
        self.assertEqual(_map_payment_terms("Prepaid"), SalesInvoice.PaymentTerms.PREPAID)
        self.assertEqual(_map_payment_terms("payment in advance"), SalesInvoice.PaymentTerms.PREPAID)

    def test_unknown_falls_back_to_due_on_receipt(self):
        self.assertEqual(_map_payment_terms(""), SalesInvoice.PaymentTerms.DUE_ON_RECEIPT)
        self.assertEqual(_map_payment_terms(None), SalesInvoice.PaymentTerms.DUE_ON_RECEIPT)
        self.assertEqual(_map_payment_terms("COD"), SalesInvoice.PaymentTerms.DUE_ON_RECEIPT)
        self.assertEqual(_map_payment_terms("Due on Receipt"), SalesInvoice.PaymentTerms.DUE_ON_RECEIPT)


class DueDateTests(TestCase):
    def test_net_terms_compute_due_date(self):
        d = date(2026, 1, 1)
        self.assertEqual(_due_date_for(d, SalesInvoice.PaymentTerms.NET_30), d + timedelta(days=30))
        self.assertEqual(_due_date_for(d, SalesInvoice.PaymentTerms.NET_60), d + timedelta(days=60))

    def test_prepaid_and_due_on_receipt_yield_none(self):
        d = date(2026, 1, 1)
        self.assertIsNone(_due_date_for(d, SalesInvoice.PaymentTerms.PREPAID))
        self.assertIsNone(_due_date_for(d, SalesInvoice.PaymentTerms.DUE_ON_RECEIPT))


class ConvertOrderToInvoiceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="A", last_name="B", email="conv@example.com", company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            payment_terms="Net 30",
            subtotal_ex_tax=Decimal("1000.00"),
            tax_total=Decimal("80.00"),
            shipping_cost_ex_tax=Decimal("50.00"),
            grand_total=Decimal("1130.00"),
            customer_message="Thanks for your business",
            internal_notes="watch credit limit",
        )

    def test_creates_draft_invoice_with_snapshot_fields(self):
        invoice, created = convert_order_to_invoice(self.order)
        self.assertTrue(created)
        self.assertEqual(invoice.order, self.order)
        self.assertEqual(invoice.company, self.company)
        self.assertEqual(invoice.status, SalesInvoice.Status.DRAFT)
        self.assertEqual(invoice.subtotal_ex_tax, Decimal("1000.00"))
        self.assertEqual(invoice.tax_total, Decimal("80.00"))
        self.assertEqual(invoice.shipping_cost, Decimal("50.00"))
        self.assertEqual(invoice.grand_total, Decimal("1130.00"))
        self.assertEqual(invoice.amount_paid, Decimal("0"))
        self.assertEqual(invoice.balance_due, Decimal("1130.00"))
        self.assertEqual(invoice.notes, "Thanks for your business")
        self.assertEqual(invoice.internal_notes, "watch credit limit")

    def test_payment_terms_and_due_date(self):
        invoice, _ = convert_order_to_invoice(self.order)
        self.assertEqual(invoice.payment_terms, SalesInvoice.PaymentTerms.NET_30)
        self.assertEqual(invoice.due_date, invoice.invoice_date + timedelta(days=30))

    def test_unknown_terms_default_to_due_on_receipt_with_no_due_date(self):
        self.order.payment_terms = "weird custom terms"
        self.order.save(update_fields=["payment_terms", "updated_at"])
        invoice, _ = convert_order_to_invoice(self.order)
        self.assertEqual(invoice.payment_terms, SalesInvoice.PaymentTerms.DUE_ON_RECEIPT)
        self.assertIsNone(invoice.due_date)

    def test_invoice_number_minted_on_save(self):
        invoice, _ = convert_order_to_invoice(self.order)
        self.assertTrue(invoice.invoice_number.startswith("INV-"))

    def test_idempotent_returns_existing_without_duplicating(self):
        first, created_first = convert_order_to_invoice(self.order)
        second, created_second = convert_order_to_invoice(self.order)
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.order.invoices.count(), 1)
