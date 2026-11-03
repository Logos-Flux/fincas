"""EmailLog -> Communication at the four send sites (docs/crm/phase3-engagement.md 2.5)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from communications import services
from communications.email_link import link_email_log
from communications.models import Communication, CommunicationParticipant, EmailLog
from core.models import Company, UserProfile
from customers.models import Customer
from orders.models import Order, Quote, SalesInvoice, SubOrder


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
@patch("orders.posting.post_sales_invoice")  # posting rules are accounting's tests
@patch("documents.services.pdf.render_quote_pdf", return_value=b"%PDF")
@patch("documents.services.pdf.render_invoice_pdf", return_value=b"%PDF")
class SendSiteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace", email="ada@example.com", company=cls.company,
        )
        cls.order = Order.objects.create(
            customer=cls.customer, company=cls.company, subtotal_ex_tax=100, grand_total=100,
        )
        cls.invoice = SalesInvoice.objects.create(
            order=cls.order, company=cls.company, invoice_date=date.today(),
            subtotal_ex_tax=Decimal("100"), grand_total=Decimal("100"), balance_due=Decimal("100"),
        )
        cls.quote = Quote.objects.create(customer=cls.customer, company=cls.company)
        cls.sub_order = SubOrder.objects.create(
            order=cls.order, sub_order_number="SO-1-1", status="shipped", carrier="ups",
            tracking_number="1Z",
        )
        cls.user = get_user_model().objects.create_user("clerk", password="x", is_staff=True)
        cls.profile = UserProfile.objects.create(user=cls.user, company=cls.company, display_name="Clerk")

    def _sends(self):
        return {
            "sales_order": lambda: services.send_order_confirmation(
                self.order, "ada@example.com", "manual", self.user),
            "invoice": lambda: services.send_invoice_email(
                self.invoice, "ada@example.com", "manual", self.user),
            "shipping_notification": lambda: services.send_shipping_notification(
                self.sub_order, "manual", self.user),
            "quote": lambda: services.send_quote_email(
                self.quote, "ada@example.com", "manual", self.user),
        }

    def test_each_send_names_its_email_log_in_a_header(self, *_):
        """X-Golden-Email-Log lets a mailbox adapter skip golden's own sends (phase6 3.3)."""
        from django.core import mail

        for document_type, send in self._sends().items():
            with self.subTest(document_type=document_type):
                mail.outbox.clear()
                log = send()
                self.assertEqual(len(mail.outbox), 1)
                self.assertEqual(mail.outbox[0].extra_headers.get("X-Golden-Email-Log"), str(log.pk))
                self.assertIn(f"X-Golden-Email-Log: {log.pk}", mail.outbox[0].message().as_string())

    def test_each_send_heads_the_email_with_the_configured_company(self, *_):
        """The templates read `brand`; a context without it showed the fallback name."""
        from django.core import mail

        with patch.dict(services.BRAND, {"company_name": "Probe Trading Co"}):
            for document_type, send in self._sends().items():
                with self.subTest(document_type=document_type):
                    mail.outbox.clear()
                    send()
                    message = mail.outbox[0]
                    html = next(
                        (body for body, mime in getattr(message, "alternatives", []) if mime == "text/html"),
                        message.body,
                    )
                    self.assertIn("Probe Trading Co", html)

    def test_a_failed_send_still_logs_under_the_id_it_announced(self, *_):
        with patch("communications.services.EmailMultiAlternatives.send", side_effect=Exception("SMTP")):
            log = services.send_order_confirmation(self.order, "ada@example.com", "manual", self.user)
        self.assertEqual(log.status, EmailLog.Status.FAILED)
        self.assertTrue(EmailLog.objects.filter(pk=log.pk).exists())

    def test_each_send_site_creates_exactly_one_communication(self, *_):
        party = self.customer.party
        self.assertIsNotNone(party)
        for document_type, send in self._sends().items():
            with self.subTest(document_type=document_type):
                before = Communication.objects.count()
                log = send()
                self.assertEqual(Communication.objects.count(), before + 1)
                comm = Communication.objects.get(email_log=log)
                self.assertEqual(
                    (comm.kind, comm.direction, comm.provider, comm.status, comm.is_editable),
                    ("email", "outbound", "golden", "completed", False),
                )
                self.assertEqual(comm.company, self.company)
                self.assertEqual(comm.party, party)
                self.assertEqual(comm.subject, log.subject)
                self.assertEqual(comm.body_text, "")
                self.assertEqual(comm.thread_key, f"golden:{document_type}:{log.document_id}")
                self.assertEqual(comm.occurred_at, log.created_at)
                self.assertEqual(comm.performed_by, self.profile)
                # The document's thread (CRM Phase 3 step 5): every quote has
                # one; this order was sold without a quote, so it has none.
                expected = self.quote.engagement_id if document_type == "quote" else None
                self.assertIsNotNone(self.quote.engagement_id)
                self.assertEqual(comm.engagement_id, expected)
                part = comm.participants.get()
                self.assertEqual(part.role, CommunicationParticipant.Role.TO)
                self.assertEqual(part.party, party)
                self.assertEqual(part.party_email.email_normalized, "ada@example.com")

    def test_a_failed_send_is_a_failed_communication(self, *_):
        with patch("communications.services.EmailMultiAlternatives.send", side_effect=Exception("SMTP")):
            for document_type, send in self._sends().items():
                with self.subTest(document_type=document_type):
                    log = send()
                    self.assertEqual(log.status, EmailLog.Status.FAILED)
                    self.assertEqual(log.communication.status, Communication.Status.FAILED)

    def test_a_bounce_flips_the_communication_to_failed(self, *_):
        log = services.send_order_confirmation(self.order, "ada@example.com", "manual", self.user)
        log.status = EmailLog.Status.BOUNCED
        log.save()
        self.assertEqual(Communication.objects.get(email_log=log).status, Communication.Status.FAILED)
        log.status = EmailLog.Status.DELIVERED
        log.save()
        self.assertEqual(Communication.objects.get(email_log=log).status, Communication.Status.COMPLETED)

    def test_link_is_idempotent_on_the_email_log(self, *_):
        log = services.send_quote_email(self.quote, "ada@example.com", "manual", self.user)
        first = log.communication
        self.assertEqual(link_email_log(log, self.quote), first)
        self.assertEqual(Communication.objects.filter(email_log=log).count(), 1)
        self.assertEqual(CommunicationParticipant.objects.filter(communication=first).count(), 1)

    def test_an_unknown_recipient_is_an_unlinked_participant(self, *_):
        log = services.send_quote_email(self.quote, "accounts@elsewhere.test", "manual", self.user)
        part = log.communication.participants.get()
        self.assertIsNone(part.party_id)
        self.assertEqual(part.address_normalized, "accounts@elsewhere.test")
        self.assertEqual(log.communication.party, self.customer.party)

    def test_a_link_failure_never_loses_the_delivery_log(self, *_):
        # The mail has gone out: the EmailLog commits, the follow-up writes
        # run, and the step-6 backfill links the log later.
        with patch("communications.email_link.build", side_effect=RuntimeError("boom")), \
                self.assertLogs("communications.email_link", "ERROR"):
            log = services.send_quote_email(self.quote, "ada@example.com", "manual", self.user)
        self.assertTrue(EmailLog.objects.filter(pk=log.pk).exists())
        self.assertFalse(Communication.objects.exists())
        self.quote.refresh_from_db()
        self.assertIsNotNone(self.quote.sent_at)
        self.assertIsNotNone(link_email_log(log, self.quote))

    def test_purchase_order_emails_get_no_communication(self, *_):
        log = EmailLog.objects.create(
            document_type=EmailLog.DocumentType.PURCHASE_ORDER, document_id=self.order.pk,
            document_number="PO-1", recipient_email="supplier@example.com", subject="PO",
            template_name="po", status="sent", triggered_by="manual",
        )
        self.assertIsNone(link_email_log(log, object()))
        self.assertFalse(Communication.objects.exists())

    def test_historical_auto_send_writes_nothing(self, *_):
        Order.objects.filter(pk=self.order.pk).update(order_date=date(2020, 1, 1))
        self.order.refresh_from_db()
        self.assertIsNone(services.send_order_confirmation(self.order, "ada@example.com", "auto", None))
        self.assertFalse(Communication.objects.exists())
