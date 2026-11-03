"""Quote revisions from the quote editor (R2.06).

Found by the release harness on 2026-10-17 (docs/release-testing.md): the
model and the API carried ``original_quote`` / ``superseded_by`` /
``revision_number`` but the quote editor had no "New revision" action, so
a lost or sent quote could not be revised from the back office.

``orders.revisions.revise_quote`` copies the quote into its chain and keeps
the engagement (a revision inherits its original's, spec 1.6);
``QuoteReviseView`` posts it from the editor behind the ``_once`` guard.
"""

from __future__ import annotations

import re
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserProfile
from customers.models import Customer
from orders.models import Quote, QuoteLineItem
from orders.revisions import revise_quote


def once_token(html):
    match = re.search(r'id="quote-revise-form"[^>]*>.*?name="_once" value="([0-9a-f]+)"', html, re.S)
    return match.group(1) if match else ""


class QuoteRevisionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace", email="ada@example.com", company=self.company,
        )
        self.user = User.objects.create_user(username="staff", password="pw", is_staff=True)
        self.user.userprofile = UserProfile.objects.create(company=self.company)
        self.client.force_login(self.user)
        self.quote = Quote.objects.create(
            customer=self.customer, company=self.company, grand_total=Decimal("500.00"),
        )
        QuoteLineItem.objects.create(
            quote=self.quote, variant=None, sku="MISC", item_name="Fabrication",
            quantity=Decimal("2"), unit_price_ex_tax=Decimal("250.0000"),
            line_total_ex_tax=Decimal("500.00"),
        )

    def tearDown(self):
        cache.clear()

    # -- the service ---------------------------------------------------------

    def test_a_revision_stays_on_the_deal_and_supersedes_its_source(self):
        engagement = self.quote.engagement
        self.assertIsNotNone(engagement)
        history_before = engagement.stage_changes.count()
        revision = revise_quote(self.quote, reason="price_change", by=self.user.userprofile)
        self.quote.refresh_from_db()
        self.assertNotEqual(revision.pk, self.quote.pk)
        self.assertEqual(revision.original_quote_id, self.quote.pk)
        self.assertEqual(revision.revision_number, 2)
        self.assertEqual(revision.revision_reason, "price_change")
        self.assertIsNone(revision.amended_from_id)
        self.assertEqual(revision.status, Quote.Status.DRAFT)
        self.assertEqual(revision.engagement_id, engagement.pk)
        self.assertEqual(self.quote.superseded_by_id, revision.pk)
        self.assertEqual(self.quote.status, Quote.Status.SUPERSEDED)
        self.assertEqual(Quote.objects.filter(engagement=engagement).count(), 2)
        self.assertEqual(revision.line_items.count(), 1)
        self.assertEqual(revision.line_items.get().quantity, Decimal("2"))
        self.assertEqual(engagement.stage_changes.count(), history_before + 1)
        self.assertIn(revision.quote_number, engagement.stage_changes.order_by("-created_at").first().note)

    def test_the_third_revision_points_at_the_root_and_a_superseded_quote_refuses(self):
        second = revise_quote(self.quote)
        third = revise_quote(second)
        self.assertEqual(third.original_quote_id, self.quote.pk)
        self.assertEqual(third.revision_number, 3)
        second.refresh_from_db()
        self.assertEqual(second.superseded_by_id, third.pk)
        with self.assertRaises(ValidationError):
            revise_quote(self.quote)
        self.assertEqual(Quote.objects.count(), 3)

    def test_an_accepted_quote_keeps_its_status_when_revised(self):
        Quote.objects.filter(pk=self.quote.pk).update(status=Quote.Status.ACCEPTED)
        self.quote.refresh_from_db()
        revision = revise_quote(self.quote)
        self.quote.refresh_from_db()
        self.assertEqual(self.quote.status, Quote.Status.ACCEPTED)
        self.assertEqual(self.quote.superseded_by_id, revision.pk)

    # -- the editor ----------------------------------------------------------

    def test_the_editor_offers_new_revision_and_the_post_lands_on_it(self):
        edit = reverse("erp-quote-edit", args=[self.quote.pk])
        page = self.client.get(edit).content.decode()
        self.assertIn("New revision", page)
        self.assertIn(reverse("erp-quote-revise", args=[self.quote.pk]), page)
        token = once_token(page)
        self.assertTrue(token)
        response = self.client.post(reverse("erp-quote-revise", args=[self.quote.pk]), {"_once": token})
        revision = Quote.objects.exclude(pk=self.quote.pk).get()
        self.assertRedirects(response, reverse("erp-quote-edit", args=[revision.pk]), fetch_redirect_response=False)
        self.assertEqual(revision.original_quote_id, self.quote.pk)
        self.assertEqual(revision.engagement_id, self.quote.engagement_id)
        # The superseded quote now links to its revision instead of offering another.
        page = self.client.get(edit).content.decode()
        self.assertIn("View revision", page)
        self.assertNotIn('form="quote-revise-form"', page)
        # The same token again (a double click) creates nothing more.
        response = self.client.post(reverse("erp-quote-revise", args=[self.quote.pk]), {"_once": token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Quote.objects.count(), 2)

    def test_the_double_submit_guard_makes_one_revision(self):
        token = once_token(self.client.get(reverse("erp-quote-edit", args=[self.quote.pk])).content.decode())
        url = reverse("erp-quote-revise", args=[self.quote.pk])
        self.client.post(url, {"_once": token})
        self.client.post(url, {"_once": token})
        self.assertEqual(Quote.objects.count(), 2)
