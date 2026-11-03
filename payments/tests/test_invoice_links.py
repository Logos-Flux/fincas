"""Invoice documents (the PDF, the print preview) open in a new tab, never
in place of the page that links to them (bug batch 2026-10-15). The invoice
page itself is a back-office screen since 2026-10-17 (R1.14) and opens in
place, like every other list row."""

import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from accounting.tests.test_posting import make_posting_rules
from core.models import Company
from customers.models import Customer
from orders.models import Order, SalesInvoice


class InvoiceLinksOpenNewTabTests(TestCase):
    def setUp(self):
        company = Company.objects.create(name="Link Test Co")
        make_posting_rules(company)
        customer = Customer.objects.create(
            first_name="Ada", last_name="Link", email="ada@example.com", company=company,
        )
        self.order = Order.objects.create(customer=customer, company=company, grand_total=Decimal("100.00"))
        self.invoice = SalesInvoice.objects.create(
            invoice_number="INV-LINK1", order=self.order, company=company,
            grand_total=Decimal("100.00"), balance_due=Decimal("100.00"),
        )
        self.client.force_login(User.objects.create_superuser("boss", "boss@example.com", "pw"))

    def _anchors_to(self, html, url):
        return re.findall(r'<a href="%s"[^>]*>' % re.escape(url), html)

    def test_invoice_list_link_and_row_open_the_invoice_page_in_place(self):
        html = self.client.get(reverse("erp-invoice-list")).content.decode()
        detail = reverse("erp-invoice-detail", args=[self.invoice.pk])
        anchors = self._anchors_to(html, detail)
        self.assertTrue(anchors)
        for anchor in anchors:
            self.assertNotIn('target="_blank"', anchor)
        self.assertIn(f'data-href="{detail}"', html)
        self.assertNotIn('data-new-tab="1"', html)

    def test_order_page_invoice_page_opens_in_place_and_documents_in_a_new_tab(self):
        html = self.client.get(reverse("erp-order-edit", args=[self.order.pk])).content.decode()
        anchors = self._anchors_to(html, reverse("erp-invoice-detail", args=[self.invoice.pk]))
        self.assertTrue(anchors)
        for anchor in anchors:
            self.assertNotIn('target="_blank"', anchor)
        anchors = self._anchors_to(html, reverse("erp-invoice-pdf", args=[self.invoice.pk]))
        self.assertTrue(anchors)
        for anchor in anchors:
            self.assertIn('target="_blank"', anchor)

    def test_invoice_page_documents_open_in_a_new_tab(self):
        html = self.client.get(reverse("erp-invoice-detail", args=[self.invoice.pk])).content.decode()
        for name in ("erp-invoice-preview", "erp-invoice-pdf"):
            anchors = self._anchors_to(html, reverse(name, args=[self.invoice.pk]))
            self.assertTrue(anchors, name)
            for anchor in anchors:
                self.assertIn('target="_blank"', anchor, name)
                self.assertIn('rel="noopener"', anchor, name)

    def test_other_lists_still_open_in_place(self):
        html = self.client.get(reverse("erp-order-list")).content.decode()
        self.assertNotIn('data-new-tab="1"', html)
