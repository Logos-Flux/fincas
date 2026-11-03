"""Record payment from the invoice page (R1.16), and the invoice landing (R1.14).

Found by the release harness on 2026-10-17 (docs/release-testing.md): no
back-office screen recorded a payment, and "Create Invoice" landed on the
print preview (the PDF's HTML: no chrome, nothing to click, fonts from
``file://`` the browser refuses).

The form posts to ``RecordPaymentView``, which goes through
``payments.services.record_payment``: one settled manual Payment, posted
under the company's payment rule by ``Payment.save()``, then the invoice
and order settled from the ledger. The ``_once`` token is the double-submit
guard (F-033) and the payment's idempotency key.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounting.models import JournalEntry
from accounting.tests.test_posting import make_posting_rules
from core.models import Company
from customers.models import Customer
from orders.models import Order, SalesInvoice
from orders.tests.test_recalculate import company_user

from ..models import Payment

API_URL = "/api/v1/payments/payments/"


def once_token(html):
    match = re.search(r'name="_once" value="([0-9a-f]+)"', html)
    return match.group(1) if match else ""


class RecordPaymentBase(TestCase):
    def setUp(self):
        cache.clear()
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Riley", last_name="Castellano", email="riley@example.com",
            company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer, company=self.company, order_number="SO-R1-16",
            subtotal_ex_tax=Decimal("100"), grand_total=Decimal("100"), balance_due=Decimal("100"),
        )
        self.invoice = SalesInvoice.objects.create(
            order=self.order, company=self.company, invoice_date=date.today(),
            subtotal_ex_tax=Decimal("100"), grand_total=Decimal("100"),
            balance_due=Decimal("100"),
        )
        self.user = company_user(self.company)
        self.client.force_login(self.user)
        self.page = reverse("erp-invoice-detail", args=[self.invoice.pk])
        self.url = reverse("erp-invoice-record-payment", args=[self.invoice.pk])

    def tearDown(self):
        cache.clear()

    def record(self, amount, token=None, **extra):
        if token is None:
            token = once_token(self.client.get(self.page).content.decode())
        data = {
            "_once": token, "amount": str(amount), "payment_date": date.today().isoformat(),
            "payment_method": "check", "reference": f"CHK-{amount}", "notes": "",
        }
        data.update(extra)
        return self.client.post(self.url, data), token


class RecordPaymentTests(RecordPaymentBase):
    def setUp(self):
        super().setUp()
        make_posting_rules(self.company)

    def test_partial_then_full_payment_settles_the_invoice_and_the_order(self):
        response, _ = self.record("40.00")
        self.assertRedirects(response, self.page, fetch_redirect_response=False)
        self.invoice.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.invoice.status, SalesInvoice.Status.PARTIAL)
        self.assertEqual(self.invoice.amount_paid, Decimal("40.00"))
        self.assertEqual(self.invoice.balance_due, Decimal("60.00"))
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PARTIAL)
        self.assertEqual(self.order.amount_paid, Decimal("40.00"))
        page = self.client.get(self.page)
        self.assertContains(page, "still due")
        self.assertContains(page, "CHK-40.00")
        # The default on the form is the remaining balance.
        self.assertEqual(page.context["form"].fields["amount"].initial, Decimal("60.00"))

        response, _ = self.record("60.00")
        self.assertRedirects(response, self.page, fetch_redirect_response=False)
        self.invoice.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(self.invoice.status, SalesInvoice.Status.PAID)
        self.assertEqual(self.invoice.balance_due, Decimal("0.00"))
        self.assertEqual(self.invoice.paid_date, date.today())
        self.assertEqual(self.order.payment_status, Order.PaymentStatus.PAID)
        payments = list(Payment.objects.filter(invoice=self.invoice).order_by("created_at"))
        self.assertEqual([p.amount for p in payments], [Decimal("40.00"), Decimal("60.00")])
        self.assertTrue(all(p.status == Payment.Status.CAPTURED for p in payments))
        self.assertTrue(all(p.payment_provider == Payment.PaymentProvider.MANUAL for p in payments))
        # Posted: one journal entry per payment, cash against AR.
        entries = JournalEntry.objects.filter(
            company=self.company, entry_source=JournalEntry.EntrySource.PAYMENT,
        )
        self.assertEqual(entries.count(), 2)
        page = self.client.get(self.page)
        self.assertContains(page, "Paid in full")
        self.assertNotContains(page, 'id="record-payment-form"')

    def test_a_second_identical_submit_records_nothing(self):
        first, token = self.record("40.00")
        self.assertEqual(first.status_code, 302)
        second, _ = self.record("40.00", token=token)
        self.assertRedirects(second, self.page, fetch_redirect_response=False)
        self.assertEqual(Payment.objects.count(), 1)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.balance_due, Decimal("60.00"))
        texts = [str(m) for m in self.client.get(self.page).context["messages"]]
        self.assertTrue(any("already recorded" in t for t in texts), texts)

    def test_the_same_token_is_also_the_idempotency_key(self):
        # The cache guard is per process; the row carries the key too, so a
        # replay that misses the cache still finds the original payment.
        _, token = self.record("25.00")
        cache.clear()
        self.record("25.00", token=token)
        self.assertEqual(Payment.objects.count(), 1)
        self.assertEqual(Payment.objects.get().idempotency_key, f"erp-once:{token}")

    def test_more_than_the_balance_is_refused(self):
        response, _ = self.record("150.00")
        self.assertRedirects(response, self.page, fetch_redirect_response=False)
        self.assertEqual(Payment.objects.count(), 0)
        texts = [str(m) for m in self.client.get(self.page).context["messages"]]
        self.assertTrue(any("More than the balance due" in t for t in texts), texts)

    def test_a_refused_submit_can_be_retried_with_the_same_form(self):
        _, token = self.record("150.00")
        response, _ = self.record("50.00", token=token)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Payment.objects.count(), 1)

    def test_the_page_needs_the_viewers_company(self):
        other = Company.objects.create(name="Rival, Inc")
        self.client.force_login(company_user(other, username="rival"))
        self.assertEqual(self.client.get(self.page).status_code, 404)
        self.assertEqual(self.client.post(self.url, {"amount": "1"}).status_code, 404)


class NoPostingRuleTests(RecordPaymentBase):
    """With no payment DocumentPostingRule, nothing can post: say so, never 500."""

    def test_the_form_shows_an_error_and_records_nothing(self):
        response, _ = self.record("40.00")
        self.assertRedirects(response, self.page, fetch_redirect_response=False)
        self.assertEqual(Payment.objects.count(), 0)
        page = self.client.get(self.page)
        self.assertEqual(page.status_code, 200)
        texts = [str(m) for m in page.context["messages"]]
        self.assertTrue(any("no posting rule" in t for t in texts), texts)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.balance_due, Decimal("100"))

    def test_the_api_answers_409_no_posting_rule(self):
        api = APIClient()
        api.force_authenticate(user=self.user)
        body = {
            "payment_number": "PAY-API-1", "order": str(self.order.pk), "invoice": str(self.invoice.pk),
            "payment_method": "check", "payment_provider": "manual",
            "amount": "40.00", "payment_type": "payment", "status": "captured",
            "company": str(self.company.pk),
        }
        response = api.post(API_URL, body, format="json")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.data["code"], "no_posting_rule")
        self.assertEqual(Payment.objects.count(), 0)
        # With a key, the same answer (the keyed path wraps the create).
        response = api.post(API_URL, body, format="json", HTTP_IDEMPOTENCY_KEY="k-1")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(Payment.objects.count(), 0)


@patch("orders.posting.post_sales_invoice")  # posting rules are accounting's tests
class InvoiceLandingTests(RecordPaymentBase):
    """R1.14: "Create Invoice" lands on the invoice's page, not the print preview."""

    def test_create_invoice_redirects_to_the_invoice_page(self, _post):
        self.invoice.delete()
        from orders.models import OrderLineItem
        from orders.tests.test_shipment_group_moves import harvest_inputs

        OrderLineItem.objects.create(
            order=self.order, sku="MISC", item_name="Fabrication", quantity=Decimal("1"),
            unit_price_ex_tax=Decimal("100.00"), line_total_ex_tax=Decimal("100.00"),
        )

        edit = reverse("erp-order-edit", args=[self.order.pk])
        data = harvest_inputs(self.client.get(edit).content.decode())
        data["_action"] = "create_invoice"
        response = self.client.post(edit, data)
        self.assertEqual(
            response.status_code, 302,
            [str(m) for m in response.context["messages"]] if response.context else "",
        )
        invoice = SalesInvoice.objects.get(order=self.order)
        self.assertEqual(response.url, reverse("erp-invoice-detail", args=[invoice.pk]))
        page = self.client.get(response.url)
        self.assertEqual(page.status_code, 200)
        self.assertTemplateUsed(page, "erp/invoice_detail.html")
        html = page.content.decode()
        # Back-office chrome and a way out: the sidebar, the order, the preview.
        self.assertIn("nav-link", html)
        self.assertIn(reverse("erp-order-edit", args=[self.order.pk]), html)
        self.assertIn(reverse("erp-invoice-preview", args=[invoice.pk]), html)
        self.assertIn(reverse("erp-invoice-pdf", args=[invoice.pk]), html)
        self.assertIn('id="record-payment-form"', html)
        self.assertNotIn("file://", html)

    def test_the_print_preview_serves_its_fonts_from_static(self, _post):
        response = self.client.get(reverse("erp-invoice-preview", args=[self.invoice.pk]))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn("file://", html)
        self.assertIn("/static/assets/fonts/docs/barlow-400.ttf", html)
        self.assertIn("@font-face", html)

    def test_the_order_editor_links_to_the_invoice_page(self, _post):
        html = self.client.get(reverse("erp-order-edit", args=[self.order.pk])).content.decode()
        self.assertIn(f'href="{self.page}"', html)
        self.assertIn(f'href="{self.page}#record-payment"', html)
        self.assertIn("Record Payment", html)
