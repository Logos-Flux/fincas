"""Several quotes on one deal; converting one leaves the others alone (R3).

Found by the release harness on 2026-10-17 (docs/release-testing.md,
R3.02): a second quote for a deal always opened its own engagement. The
R1.06 fix ("New quote" on the engagement page, an Engagement field on the
quote editor) covers it; this pins the rest of the journey: the sibling
quote keeps its own status, is neither cancelled nor superseded, stays on
the deal, and the conversion wins the deal (spec 1.6,
``crm.engagements.win_for_conversion``) rather than losing it.
"""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserProfile
from crm.models import Engagement
from customers.models import Customer
from orders.convert import convert_quote_to_order
from orders.models import Order, Quote, QuoteLineItem


class SiblingQuotesTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Grace", last_name="Whitfield", email="grace@example.com", company=self.company,
        )
        self.user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        self.profile = UserProfile.objects.create(company=self.company)
        self.user.userprofile = self.profile
        self.client.force_login(self.user)
        self.quote_a = self.quote("BRK-90-HD", "20", "4.50")
        self.engagement = self.quote_a.engagement
        self.assertIsNotNone(self.engagement)
        # Quote B started from the deal (the engagement page's "New quote").
        self.quote_b = self.quote("SNR-LASER-30", "2", "300.00", engagement=self.engagement)

    def quote(self, sku, qty, price, **kwargs):
        quote = Quote.objects.create(customer=self.customer, company=self.company, **kwargs)
        QuoteLineItem.objects.create(
            quote=quote, variant=None, sku=sku, item_name=sku,
            quantity=Decimal(qty), unit_price_ex_tax=Decimal(price),
            line_total_ex_tax=Decimal(qty) * Decimal(price),
        )
        quote.recalculate_totals()
        quote.refresh_from_db()
        return quote

    def test_two_quotes_share_the_deal(self):
        self.assertEqual(self.quote_b.engagement_id, self.engagement.pk)
        self.assertEqual(Quote.objects.filter(engagement=self.engagement).count(), 2)
        self.assertEqual(Engagement.objects.filter(company=self.company).count(), 1)
        page = self.client.get(reverse("erp-crm-engagement-detail", args=[self.engagement.pk]))
        self.assertContains(page, self.quote_a.quote_number)
        self.assertContains(page, self.quote_b.quote_number)

    def test_converting_one_quote_leaves_the_sibling_with_its_own_status(self):
        order, created = convert_quote_to_order(self.quote_a, user=self.profile)
        self.assertTrue(created)
        self.quote_a.refresh_from_db()
        self.quote_b.refresh_from_db()
        self.engagement.refresh_from_db()
        self.assertEqual(self.quote_a.status, Quote.Status.ACCEPTED)
        self.assertEqual(self.quote_a.converted_to_order_id, order.pk)
        self.assertEqual(order.engagement_id, self.engagement.pk)
        # The sibling: untouched, still on the deal.
        self.assertEqual(self.quote_b.status, Quote.Status.DRAFT)
        self.assertIsNone(self.quote_b.converted_to_order_id)
        self.assertIsNone(self.quote_b.superseded_by_id)
        self.assertEqual(self.quote_b.engagement_id, self.engagement.pk)
        self.assertEqual(Quote.objects.filter(engagement=self.engagement, status=Quote.Status.CANCELLED).count(), 0)
        # The deal: won by the conversion (spec 1.6), never lost.
        self.assertEqual(self.engagement.status, Engagement.Status.WON)
        self.assertEqual(self.engagement.stage_changes.filter(to_status="lost").count(), 0)
        self.assertEqual(self.engagement.stage_changes.filter(to_status="won").count(), 1)
        self.assertEqual(Order.objects.filter(source_quotes=self.quote_a).count(), 1)
        page = self.client.get(reverse("erp-crm-engagement-detail", args=[self.engagement.pk]))
        self.assertContains(page, order.order_number)
        self.assertContains(page, self.quote_b.quote_number)
        self.assertContains(page, "Accepted")
        self.assertContains(page, "Draft")
