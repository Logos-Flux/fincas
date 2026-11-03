"""The Cancel button must only appear where it does something.

It rendered on all four documents that use erp/document_form.html, but
_action="cancel_order" was handled only by the Sales Order view. On a Quote,
Purchase Order or Pick Ticket the POST fell through to the default branch:
the document was SAVED and the user was redirected, with no status change and
no error. The user believed they had cancelled a purchase order.

Pick Ticket has no CANCELLED status at all, so it opts out entirely; the
other three opt in with can_cancel and handle the action.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserProfile
from customers.models import Customer
from orders.models import Quote
from purchasing.models import PurchaseOrder, Supplier


class CancelButtonTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace",
            email="cancel@example.com", company=self.company,
        )
        user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        UserProfile.objects.create(company=self.company)
        self.client.force_login(user)

    # ── the button renders only where it is handled ───────

    def test_quote_offers_cancel(self):
        quote = Quote.objects.create(customer=self.customer, company=self.company)
        html = self.client.get(reverse("erp-quote-edit", args=[quote.pk])).content.decode()
        self.assertIn('value="cancel_order"', html)

    def test_purchase_order_offers_cancel(self):
        supplier = Supplier.objects.create(company_name="Acme Supply")
        po = PurchaseOrder.objects.create(supplier=supplier, company=self.company)
        html = self.client.get(
            reverse("erp-purchaseorder-edit", args=[po.pk])
        ).content.decode()
        self.assertIn('value="cancel_order"', html)

    # ── and it actually cancels ───────────────────────────

    def test_cancelling_a_quote_changes_its_status(self):
        quote = Quote.objects.create(customer=self.customer, company=self.company)
        self.client.post(
            reverse("erp-quote-edit", args=[quote.pk]),
            self._quote_post(quote, _action="cancel_order"),
        )
        quote.refresh_from_db()
        self.assertEqual(quote.status, Quote.Status.CANCELLED)

    def test_cancelling_a_quote_is_not_a_silent_save(self):
        # The exact failure that shipped: status untouched, no error.
        quote = Quote.objects.create(customer=self.customer, company=self.company)
        self.client.post(
            reverse("erp-quote-edit", args=[quote.pk]),
            self._quote_post(quote, _action="cancel_order"),
        )
        quote.refresh_from_db()
        self.assertNotEqual(quote.status, Quote.Status.DRAFT)

    def _quote_post(self, quote, **extra):
        data = {
            "customer": str(self.customer.pk),
            "company": str(self.company.pk),
            "validity_period_days": "7",
            "status": Quote.Status.DRAFT,
            "currency": "USD",
            "exchange_rate": "1",
            "sales_stage": Quote.SalesStage.PROSPECTING,
            "win_probability": "0",
            "weighted_value": "0",
            "subtotal_ex_tax": "0",
            "discount_amount": "0",
            "tax_total": "0",
            "shipping_cost_ex_tax": "0",
            "grand_total": "0",
            "sg_count": "1",
            "line_items-TOTAL_FORMS": "0",
            "line_items-INITIAL_FORMS": "0",
            "line_items-MIN_NUM_FORMS": "0",
            "line_items-MAX_NUM_FORMS": "1000",
        }
        data.update(extra)
        return data


class PickTicketHasNoCancelTests(TestCase):
    """Pick Ticket has no CANCELLED status, so it must not offer the button."""

    def test_pick_ticket_status_enum_has_no_cancelled_member(self):
        from fulfillment.models import PickTicket

        self.assertNotIn(
            "CANCELLED", [s.name for s in PickTicket.Status],
            "if Pick Ticket gains a cancelled state, set can_cancel on its "
            "view and handle _action=cancel_order — do not just show the "
            "button",
        )
