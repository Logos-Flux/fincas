"""Posting into a closed fiscal period is refused before anything happens (R9.05, R9.06).

Found by the release harness on 2026-10-17 (docs/release-testing.md): with
the current period closed, "Email Invoice" on a draft invoice sent the
email and wrote the EmailLog, then ``invoice.save()`` (draft -> sent)
posted and ``JournalEntry._reject_closed_period`` raised a
``ValidationError`` the view did not catch: a 500, the customer had the
email, the invoice stayed draft.

Now ``send_invoice_email`` asks ``accounting.posting.assert_period_open``
before building the email, the send and record-payment views answer any
``ValidationError`` with a message, and the invoice API answers 400.
"""

from __future__ import annotations

import datetime
from datetime import date
from decimal import Decimal

from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from accounting.models import FiscalPeriod, JournalEntry
from accounting.posting import assert_period_open, closed_period_for
from accounting.tests.test_posting import make_posting_rules
from communications.models import EmailLog
from communications.services import send_invoice_email
from core.models import Company
from customers.models import Customer
from orders.models import Order, SalesInvoice
from orders.tests.test_recalculate import company_user
from payments.tests.test_record_payment import once_token

from ..models import Payment


class ClosedPeriodBase(TestCase):
    def setUp(self):
        cache.clear()
        self.company = Company.objects.create(name="Example Supply, Inc")
        make_posting_rules(self.company)
        self.customer = Customer.objects.create(
            first_name="Riley", last_name="Castellano", email="riley@example.com",
            company=self.company,
        )
        self.order = Order.objects.create(
            customer=self.customer, company=self.company, order_number="SO-R9-05",
            subtotal_ex_tax=Decimal("100"), grand_total=Decimal("100"), balance_due=Decimal("100"),
        )
        self.invoice = SalesInvoice.objects.create(
            order=self.order, company=self.company, invoice_date=date.today(),
            subtotal_ex_tax=Decimal("100"), grand_total=Decimal("100"),
            balance_due=Decimal("100"),
        )
        today = date.today()
        self.period = FiscalPeriod.objects.create(
            company=self.company, name=f"FY{today.year}",
            start_date=datetime.date(today.year, 1, 1), end_date=datetime.date(today.year, 12, 31),
            status=FiscalPeriod.Status.CLOSED,
        )
        self.user = company_user(self.company)
        self.client.force_login(self.user)
        self.page = reverse("erp-invoice-detail", args=[self.invoice.pk])

    def tearDown(self):
        cache.clear()

    def reopen(self):
        self.period.status = FiscalPeriod.Status.OPEN
        self.period.save(update_fields=["status"])


class PeriodHelperTests(ClosedPeriodBase):
    def test_closed_period_for_names_the_period_and_open_is_none(self):
        self.assertEqual(closed_period_for(self.company, date.today()), self.period)
        with self.assertRaises(ValidationError) as caught:
            assert_period_open(self.company, date.today(), what="Invoice INV-1")
        message = " ".join(caught.exception.messages)
        self.assertIn(self.period.name, message)
        self.assertIn("closed", message)
        self.assertIn("Invoice INV-1", message)
        self.reopen()
        self.assertIsNone(closed_period_for(self.company, date.today()))
        assert_period_open(self.company, date.today())  # no raise
        # A date outside every period is unguarded (period-less entries post).
        self.assertIsNone(closed_period_for(self.company, datetime.date(1999, 1, 1)))


class SendInvoiceClosedPeriodTests(ClosedPeriodBase):
    def send(self):
        token = once_token(self.client.get(self.page).content.decode())
        return self.client.post(
            reverse("erp-invoice-send", args=[self.invoice.pk]), {"_once": token, "next": self.page}
        )

    def test_the_service_refuses_before_sending_anything(self):
        with self.assertRaises(ValidationError):
            send_invoice_email(
                invoice=self.invoice, recipient_email="riley@example.com",
                triggered_by="manual", user=self.user,
            )
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(EmailLog.objects.count(), 0)
        self.assertEqual(JournalEntry.objects.count(), 0)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, SalesInvoice.Status.DRAFT)
        self.assertIsNone(self.invoice.sent_at)

    def test_email_invoice_into_a_closed_period_is_a_message_not_a_500(self):
        response = self.send()
        self.assertRedirects(response, self.page, fetch_redirect_response=False)
        shown = [str(m) for m in response.wsgi_request._messages]
        self.assertEqual(len(shown), 1)
        self.assertIn(self.period.name, shown[0])
        self.assertIn("closed", shown[0])
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(EmailLog.objects.count(), 0)
        self.assertEqual(JournalEntry.objects.count(), 0)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, SalesInvoice.Status.DRAFT)
        # The refusal released the double-submit claim: the same form sends
        # once the period is reopened.
        self.reopen()
        self.send()
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, SalesInvoice.Status.SENT)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(EmailLog.objects.filter(status=EmailLog.Status.SENT).count(), 1)
        self.assertEqual(JournalEntry.objects.count(), 1)

    def test_an_already_sent_invoice_is_not_checked(self):
        # Re-sending a posted invoice posts nothing, so the period does not matter.
        self.reopen()
        self.send()
        self.period.status = FiscalPeriod.Status.CLOSED
        self.period.save(update_fields=["status"])
        self.send()
        self.assertEqual(len(mail.outbox), 2)


class RecordPaymentClosedPeriodTests(ClosedPeriodBase):
    def test_a_payment_dated_in_the_closed_period_is_refused_with_a_message(self):
        token = once_token(self.client.get(self.page).content.decode())
        response = self.client.post(
            reverse("erp-invoice-record-payment", args=[self.invoice.pk]),
            {"_once": token, "amount": "100.00", "payment_date": date.today().isoformat(),
             "payment_method": "check", "reference": "CHK-1", "notes": ""},
        )
        self.assertRedirects(response, self.page, fetch_redirect_response=False)
        shown = " ".join(str(m) for m in response.wsgi_request._messages)
        self.assertIn("Not recorded", shown)
        self.assertIn(self.period.name, shown)
        self.assertEqual(Payment.objects.count(), 0)
        self.assertEqual(JournalEntry.objects.count(), 0)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, SalesInvoice.Status.DRAFT)
        self.assertEqual(self.invoice.balance_due, Decimal("100"))


class InvoiceApiClosedPeriodTests(ClosedPeriodBase):
    def test_patching_status_sent_answers_400_naming_the_period(self):
        api = APIClient()
        api.force_authenticate(user=self.user)
        response = api.patch(
            f"/api/v1/orders/sales-invoices/{self.invoice.pk}/", {"status": "sent"}, format="json"
        )
        self.assertEqual(response.status_code, 400, response.content)
        self.assertIn(self.period.name, " ".join(response.json()["status"]))
        self.assertEqual(JournalEntry.objects.count(), 0)
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, SalesInvoice.Status.DRAFT)
        self.reopen()
        response = api.patch(
            f"/api/v1/orders/sales-invoices/{self.invoice.pk}/", {"status": "sent"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(JournalEntry.objects.count(), 1)
