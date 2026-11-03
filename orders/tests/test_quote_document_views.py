"""Quote preview / PDF / email routes.

Mirrors the invoice trio in payments/erp_views.py: login-gated, not
company-scoped. See QuoteDocumentScopingTests below for why scoping was
tried and reverted.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from communications.models import EmailLog
from core.models import Company, UserProfile
from customers.models import Customer
from orders.models import Quote, QuoteLineItem


class QuoteDocumentViewTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="ada@example.com", company=self.company,
        )
        self.user = User.objects.create_user(
            username="staff", password="pw", is_staff=True,
        )
        self.user.userprofile = UserProfile.objects.create(
            company=self.company,
        )
        self.client.force_login(self.user)
        self.quote = Quote.objects.create(
            customer=self.customer, company=self.company,
            grand_total=Decimal("500.00"),
        )
        QuoteLineItem.objects.create(
            quote=self.quote, variant=None, sku="MISC", item_name="Fabrication",
            quantity=Decimal("1"), unit_price_ex_tax=Decimal("500.0000"),
            line_total_ex_tax=Decimal("500.00"),
        )

    # ── PDF ───────────────────────────────────────────────

    def test_pdf_route_returns_a_pdf_inline(self):
        response = self.client.get(reverse("erp-quote-pdf", args=[self.quote.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(
            f'filename="{self.quote.quote_number}.pdf"',
            response["Content-Disposition"],
        )
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_downloading_the_pdf_sends_no_email(self):
        # The download path must never contact a customer.
        self.client.get(reverse("erp-quote-pdf", args=[self.quote.pk]))
        self.assertEqual(len(mail.outbox), 0)
        self.quote.refresh_from_db()
        self.assertIsNone(self.quote.sent_at)
        self.assertEqual(self.quote.status, Quote.Status.DRAFT)

    def test_preview_route_returns_html(self):
        response = self.client.get(reverse("erp-quote-detail", args=[self.quote.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn("QUOTE", response.content.decode())

    def test_routes_require_login(self):
        self.client.logout()
        for name in ("erp-quote-pdf", "erp-quote-detail"):
            with self.subTest(route=name):
                response = self.client.get(reverse(name, args=[self.quote.pk]))
                self.assertEqual(response.status_code, 302)
                self.assertIn("/erp/login/", response["Location"])

    # ── email ─────────────────────────────────────────────

    def test_send_emails_the_pdf_and_logs_it(self):
        response = self.client.post(reverse("erp-quote-send", args=[self.quote.pk]))
        self.assertRedirects(
            response,
            reverse("erp-quote-edit", args=[self.quote.pk]),
            fetch_redirect_response=False,
        )
        self.assertEqual(len(mail.outbox), 1)
        sent = mail.outbox[0]
        self.assertEqual(sent.to, ["ada@example.com"])
        self.assertIn(self.quote.quote_number, sent.subject)
        # PDF actually attached, not just referenced.
        names = [a[0] for a in sent.attachments]
        self.assertIn(f"{self.quote.quote_number}.pdf", names)
        payload = dict((a[0], a[1]) for a in sent.attachments)[
            f"{self.quote.quote_number}.pdf"
        ]
        self.assertTrue(payload.startswith(b"%PDF"))

        log = EmailLog.objects.get()
        self.assertEqual(log.document_type, EmailLog.DocumentType.QUOTE)
        self.assertEqual(log.document_number, self.quote.quote_number)
        self.assertEqual(log.status, EmailLog.Status.SENT)

    def test_sending_stamps_the_quote_and_promotes_draft_to_sent(self):
        self.client.post(reverse("erp-quote-send", args=[self.quote.pk]))
        self.quote.refresh_from_db()
        self.assertIsNotNone(self.quote.sent_at)
        self.assertEqual(self.quote.sent_to_email, "ada@example.com")
        self.assertEqual(self.quote.sent_via, "email")
        self.assertEqual(self.quote.status, Quote.Status.SENT)

    def test_resending_does_not_rewind_a_later_status(self):
        self.quote.status = Quote.Status.ACCEPTED
        self.quote.save(update_fields=["status"])
        self.client.post(reverse("erp-quote-send", args=[self.quote.pk]))
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.ACCEPTED)

    def test_missing_email_is_reported_not_crashed(self):
        self.customer.email = ""
        self.customer.save(update_fields=["email"])
        response = self.client.post(reverse("erp-quote-send", args=[self.quote.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(EmailLog.objects.count(), 0)


class QuoteDocumentScopingTests(TestCase):
    """Document the CURRENT boundary, so a future change is a deliberate one.

    These routes are login-gated but not company-scoped, matching the
    invoice views. Scoping them with Quote.objects.for_request() was tried
    and reverted: scope_to_company fails closed without a UserProfile, and
    the ERP staff UI authenticates by Django session where request.user is
    a plain auth.User with no userprofile attached -- so every staff user
    got a 404. Tightening this belongs in one pass across all of /erp/.
    """

    def setUp(self):
        self.mine = Company.objects.create(name="Example Supply, Inc")
        self.theirs = Company.objects.create(name="Someone Else Ltd")
        other_customer = Customer.objects.create(
            first_name="Not", last_name="Mine",
            email="other@example.com", company=self.theirs,
        )
        self.foreign_quote = Quote.objects.create(
            customer=other_customer, company=self.theirs,
        )
        user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        UserProfile.objects.create(company=self.mine)
        self.client.force_login(user)

    def test_any_logged_in_staff_user_can_read_any_quote(self):
        # Not an endorsement -- a pin. If someone adds real scoping, this
        # test fails and they are told why, rather than silently 404ing
        # every staff user the way the first attempt did.
        response = self.client.get(
            reverse("erp-quote-pdf", args=[self.foreign_quote.pk])
        )
        self.assertEqual(response.status_code, 200)

    def test_anonymous_users_are_still_shut_out(self):
        self.client.logout()
        response = self.client.get(
            reverse("erp-quote-pdf", args=[self.foreign_quote.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/erp/login/", response["Location"])


class EmailButtonIsWiredTests(TestCase):
    """The Email button must actually reach the send route.

    Regression test for a bug that shipped: the send <form> was nested inside
    #document-form. A nested <form> is a parse error -- the HTML5 parser drops
    the inner tag entirely, its submit button silently becomes a submit
    control for the OUTER form, and clicking "Email Quote" POSTed to the edit
    URL and just saved the quote. Confirmed in Chrome: 1 form in the DOM, 0
    nested, button owned by #document-form.

    The original view tests could not see this: they POST directly to
    erp-quote-send and never render the page. This one renders the real page
    and inspects the markup a browser would receive.
    """

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="ada@example.com", company=self.company,
        )
        user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        UserProfile.objects.create(company=self.company)
        self.client.force_login(user)
        self.quote = Quote.objects.create(
            customer=self.customer, company=self.company,
        )

    def _html(self):
        response = self.client.get(reverse("erp-quote-edit", args=[self.quote.pk]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_send_form_is_not_nested_inside_the_document_form(self):
        html = self._html()
        outer = html.index('id="document-form"')
        close = html.index("</form>", outer)
        send = html.index('id="quote-send-form"')
        self.assertGreater(
            send, close,
            "quote-send-form is inside #document-form. A nested <form> is "
            "dropped by the HTML5 parser and the Email button silently "
            "becomes a Save.",
        )

    def test_email_button_targets_the_send_form(self):
        html = self._html()
        self.assertIn('form="quote-send-form"', html)
        self.assertIn(
            f'id="quote-send-form" action="{reverse("erp-quote-send", args=[self.quote.pk])}"',
            html,
        )

    def test_send_form_carries_a_csrf_token(self):
        # A form outside #document-form does not inherit its token.
        html = self._html()
        tail = html[html.index('id="quote-send-form"'):]
        self.assertIn("csrfmiddlewaretoken", tail[: tail.index("</form>")])
