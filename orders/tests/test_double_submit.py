"""F-033: a double submit on /erp/ sends one email and moves money once.

Client side, every form that saves a document or sends mail carries
``data-submit-once`` (``includes/submit_guard.html`` disables its submit
controls after the first submission). Server side, the send forms carry a
``_once`` token (``{% once_field %}``) that ``core.once.claim`` accepts
once: the same token posted again is answered with "already sent" and a
redirect, creating no EmailLog, Communication or message. Converting a
quote to an order is idempotent already
(``test_quote_to_order.test_a_second_convert_does_not_mint_a_second_order``);
recording a payment through the API dedups on ``Idempotency-Key``
(``payments/tests/test_payment_idempotency.py``).
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from communications.models import Communication, EmailLog
from core.models import Company, UserProfile
from customers.models import Customer
from orders.models import Order, Quote, QuoteLineItem, SalesInvoice


@patch("orders.posting.post_sales_invoice")  # posting rules are accounting's tests
@patch("documents.services.pdf.render_quote_pdf", return_value=b"%PDF")
@patch("documents.services.pdf.render_invoice_pdf", return_value=b"%PDF")
class DoubleSubmitServerGuardTests(TestCase):
    def setUp(self):
        cache.clear()
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="ada@example.com", company=self.company,
        )
        self.user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        self.user.userprofile = UserProfile.objects.create(company=self.company)
        self.client.force_login(self.user)
        self.quote = Quote.objects.create(
            customer=self.customer, company=self.company, grand_total=Decimal("500.00"),
        )
        QuoteLineItem.objects.create(
            quote=self.quote, variant=None, sku="MISC", item_name="Fabrication",
            quantity=Decimal("1"), unit_price_ex_tax=Decimal("500.0000"),
            line_total_ex_tax=Decimal("500.00"),
        )
        self.order = Order.objects.create(
            customer=self.customer, company=self.company,
            subtotal_ex_tax=Decimal("100"), grand_total=Decimal("100"),
        )
        self.invoice = SalesInvoice.objects.create(
            order=self.order, company=self.company, invoice_date=date.today(),
            subtotal_ex_tax=Decimal("100"), grand_total=Decimal("100"),
            balance_due=Decimal("100"),
        )

    def tearDown(self):
        cache.clear()

    # -- quote -----------------------------------------------------------

    def test_quote_sent_twice_with_the_same_token_sends_once(self, *_):
        url = reverse("erp-quote-send", args=[self.quote.pk])
        first = self.client.post(url, {"_once": "tok-1"})
        second = self.client.post(url, {"_once": "tok-1"})

        self.assertEqual(first.status_code, 302)
        self.assertEqual(second.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(EmailLog.objects.count(), 1)
        self.assertEqual(Communication.objects.count(), 1)
        edit = self.client.get(reverse("erp-quote-edit", args=[self.quote.pk]))
        self.assertContains(edit, "already sent")

    def test_quote_sent_with_fresh_tokens_sends_each_time(self, *_):
        url = reverse("erp-quote-send", args=[self.quote.pk])
        self.client.post(url, {"_once": "tok-1"})
        self.client.post(url, {"_once": "tok-2"})
        self.assertEqual(EmailLog.objects.count(), 2)

    def test_quote_sent_without_a_token_is_not_guarded(self, *_):
        # API-style callers keep their own idempotency; the form always has one.
        url = reverse("erp-quote-send", args=[self.quote.pk])
        self.client.post(url)
        self.client.post(url)
        self.assertEqual(EmailLog.objects.count(), 2)

    def test_a_failed_quote_send_can_be_retried_with_the_same_token(self, *_):
        url = reverse("erp-quote-send", args=[self.quote.pk])
        with patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            self.client.post(url, {"_once": "tok-1"})
        self.assertEqual(EmailLog.objects.filter(status=EmailLog.Status.FAILED).count(), 1)
        self.client.post(url, {"_once": "tok-1"})
        self.assertEqual(EmailLog.objects.filter(status=EmailLog.Status.SENT).count(), 1)

    # -- invoice ---------------------------------------------------------

    def test_invoice_sent_twice_with_the_same_token_sends_once(self, *_):
        url = reverse("erp-invoice-send", args=[self.invoice.pk])
        self.client.post(url, {"_once": "tok-1"})
        second = self.client.post(url, {"_once": "tok-1"})

        self.assertEqual(second.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(EmailLog.objects.count(), 1)
        self.assertEqual(Communication.objects.count(), 1)

    # -- order confirmation (JSON modal) ----------------------------------

    def test_order_confirmation_sent_twice_with_the_same_token_sends_once(self, *_):
        url = reverse("erp-order-send-email", args=[self.order.pk])
        body = {"recipient_email": "ada@example.com", "confirmed_old_date": True, "_once": "tok-1"}
        first = self.client.post(url, json.dumps(body), content_type="application/json")
        second = self.client.post(url, json.dumps(body), content_type="application/json")

        self.assertEqual(first.status_code, 200, first.content)
        self.assertTrue(first.json()["success"])
        self.assertTrue(second.json()["success"])
        self.assertTrue(second.json()["already_sent"])
        self.assertIn("already sent", second.json()["message"])
        self.assertEqual(EmailLog.objects.count(), 1)
        self.assertEqual(Communication.objects.count(), 1)


class DoubleSubmitMarkupTests(TestCase):
    """The client-side guard is wired on the forms that need it."""

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="ada@example.com", company=self.company,
        )
        self.user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        self.user.userprofile = UserProfile.objects.create(company=self.company)
        self.client.force_login(self.user)

    def test_document_editor_forms_are_guarded(self):
        quote = Quote.objects.create(customer=self.customer, company=self.company)
        html = self.client.get(reverse("erp-quote-edit", args=[quote.pk])).content.decode()

        self.assertRegex(html, r'<form[^>]*id="document-form"[^>]*data-submit-once')
        send_form = re.search(r'<form[^>]*id="quote-send-form"[^>]*>(.*?)</form>', html, re.S)
        self.assertIsNotNone(send_form)
        self.assertIn("data-submit-once", send_form.group(0))
        self.assertRegex(send_form.group(1), r'name="_once" value="[0-9a-f]{32}"')
        # the shared guard script is on the page
        self.assertIn("data-submit-pending", html)

    def test_generic_form_is_guarded(self):
        html = self.client.get(reverse("erp-customer-create")).content.decode()
        self.assertRegex(html, r'<form method="post"[^>]*data-submit-once')
        self.assertIn("data-submit-pending", html)

    def test_once_field_mints_a_fresh_token_per_render(self):
        quote = Quote.objects.create(customer=self.customer, company=self.company)
        url = reverse("erp-quote-edit", args=[quote.pk])
        tokens = {
            re.search(r'name="_once" value="([0-9a-f]{32})"', self.client.get(url).content.decode()).group(1)
            for _ in range(2)
        }
        self.assertEqual(len(tokens), 2)
