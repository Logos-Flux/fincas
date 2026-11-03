"""A quote put on the lead's engagement from the UI (R1.06).

Found by the release harness on 2026-10-17 (docs/release-testing.md): the
quote editor had no engagement field and the engagement page no "New
quote" action, so every quote opened its own engagement
(``crm.engagements.ensure_for_quote``) and the deal that started as a lead
never showed its quote.

Now: "New quote" on the engagement page opens the editor with the deal
and its customer prefilled; the editor's Engagement select offers the
customer's open or won deals (``engagements_for_customer``); a quote saved
with one joins it, with a same-stage history row (``record_quote_added``).
"""

from __future__ import annotations

from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from crm import engagements
from crm.models import Engagement, EngagementStageChange
from customers.models import Customer
from orders.models import Quote
from orders.tests.test_order_create_requires_line import harvest_inputs

PREFIX = "line_items"


class QuoteFromEngagementTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.user = make_staff_user("sales", password="pw")
        self.profile = UserProfile.objects.create(user=self.user, company=self.company, display_name="Sales")
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(
            first_name="Riley", last_name="Castellano", email="riley@example.com", company=self.company,
        )
        self.engagement = engagements.create(
            company=self.company, party=self.customer.party, by=self.profile, title="Riley's line",
        )
        self.new_url = reverse("erp-quote-create")

    def post_new_quote(self, engagement_pk, **overrides):
        """The create editor's POST: the rendered form plus one line."""
        html = self.client.get(f"{self.new_url}?engagement={engagement_pk}").content.decode()
        data = harvest_inputs(html)
        data.update({
            "customer": str(self.customer.pk), "company": str(self.company.pk),
            "engagement": str(engagement_pk), "status": Quote.Status.DRAFT,
            "subtotal_ex_tax": "500.00", "grand_total": "500.00",
            "sg_count": "1",
            f"{PREFIX}-TOTAL_FORMS": "1", f"{PREFIX}-INITIAL_FORMS": "0",
            f"{PREFIX}-MIN_NUM_FORMS": "0", f"{PREFIX}-MAX_NUM_FORMS": "1000",
            f"{PREFIX}-0-variant": "", f"{PREFIX}-0-sku": "MISC", f"{PREFIX}-0-item_name": "Fabrication",
            f"{PREFIX}-0-quantity": "1", f"{PREFIX}-0-unit_price_ex_tax": "500.0000",
            f"{PREFIX}-0-discount_percent": "0", f"{PREFIX}-0-line_total_ex_tax": "500.00",
            f"{PREFIX}-0-shipment_group": "", f"{PREFIX}-0-sg_index": "0",
            "_action": "save_continue",
        })
        data.update(overrides)
        return self.client.post(self.new_url, data)

    def test_the_engagement_page_offers_new_quote(self):
        html = self.client.get(reverse("erp-crm-engagement-detail", args=[self.engagement.pk])).content.decode()
        self.assertIn(f'href="{self.new_url}?engagement={self.engagement.pk}"', html)
        engagements.lose(self.engagement, "price", by=self.profile)
        html = self.client.get(reverse("erp-crm-engagement-detail", args=[self.engagement.pk])).content.decode()
        self.assertNotIn(f"?engagement={self.engagement.pk}", html)

    def test_new_quote_from_the_engagement_prefills_the_deal_and_the_customer(self):
        response = self.client.get(f"{self.new_url}?engagement={self.engagement.pk}")
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(str(form.initial["engagement"]), str(self.engagement.pk))
        self.assertEqual(str(form.initial["customer"]), str(self.customer.pk))
        html = response.content.decode()
        self.assertIn(f'<option value="{self.engagement.pk}" selected>', html)
        self.assertIn("New engagement", html)
        # The customer search widget shows the prefilled customer.
        self.assertIn("Riley Castellano", html)

    def test_the_created_quote_joins_the_engagement_with_a_history_row(self):
        rows_before = EngagementStageChange.objects.filter(engagement=self.engagement).count()
        response = self.post_new_quote(self.engagement.pk)
        self.assertEqual(
            response.status_code, 302,
            [str(m) for m in response.context["messages"]] if response.context else "",
        )
        quote = Quote.objects.get()
        self.assertEqual(quote.engagement_id, self.engagement.pk)
        self.assertEqual(Engagement.objects.count(), 1, "no second engagement was opened")
        row = EngagementStageChange.objects.filter(engagement=self.engagement).order_by("-changed_at", "-pk").first()
        self.assertEqual(EngagementStageChange.objects.filter(engagement=self.engagement).count(), rows_before + 1)
        self.assertEqual(row.note, f"Quote {quote.quote_number} added")
        self.assertEqual(row.from_stage_id, row.to_stage_id)
        self.assertEqual(row.changed_by_id, self.profile.pk)
        self.assertEqual(row.source, "erp")
        # The engagement page lists it.
        html = self.client.get(reverse("erp-crm-engagement-detail", args=[self.engagement.pk])).content.decode()
        self.assertIn(quote.quote_number, html)

    def test_a_won_engagement_still_takes_the_quote(self):
        engagements.win(self.engagement, by=self.profile)
        response = self.post_new_quote(self.engagement.pk)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Quote.objects.get().engagement_id, self.engagement.pk)

    def test_without_a_pick_the_quote_opens_its_own_engagement(self):
        response = self.post_new_quote(self.engagement.pk, engagement="")
        self.assertEqual(response.status_code, 302)
        quote = Quote.objects.get()
        self.assertNotEqual(quote.engagement_id, self.engagement.pk)
        self.assertEqual(quote.engagement.origin, Engagement.Origin.QUOTE)

    def test_a_lost_engagement_or_another_partys_is_refused(self):
        engagements.lose(self.engagement, "price", by=self.profile)
        response = self.post_new_quote(self.engagement.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIn("lost", response.context["form"].errors["engagement"][0])
        self.assertEqual(Quote.objects.count(), 0)

        other = Customer.objects.create(
            first_name="Dana", last_name="Whitfield", email="dana@example.com", company=self.company,
        )
        theirs = engagements.create(company=self.company, party=other.party, by=self.profile, title="Dana")
        response = self.post_new_quote(theirs.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIn("another party", response.context["form"].errors["engagement"][0])
        self.assertEqual(Quote.objects.count(), 0)

    def test_another_companys_engagement_is_not_offered(self):
        rival = Company.objects.create(name="Rival, Inc")
        rival_customer = Customer.objects.create(
            first_name="Riley", last_name="Castellano", email="riley@example.com", company=rival,
        )
        theirs = engagements.create(company=rival, party=rival_customer.party, title="Theirs")
        response = self.client.get(f"{self.new_url}?engagement={theirs.pk}")
        self.assertNotIn("engagement", response.context["form"].initial)
        response = self.post_new_quote(theirs.pk)
        self.assertEqual(response.status_code, 200)
        self.assertIn("engagement", response.context["form"].errors)
        self.assertEqual(Quote.objects.count(), 0)

    def test_the_options_endpoint_lists_the_customers_open_and_won_deals(self):
        url = reverse("erp-quote-engagement-options")
        won = engagements.create(company=self.company, party=self.customer.party, by=self.profile, title="Won one")
        engagements.win(won, by=self.profile)
        lost = engagements.create(company=self.company, party=self.customer.party, by=self.profile, title="Lost one")
        engagements.lose(lost, "price", by=self.profile)
        results = self.client.get(url, {"customer": str(self.customer.pk)}).json()["results"]
        self.assertEqual({r["id"] for r in results}, {str(self.engagement.pk), str(won.pk)})
        self.assertTrue(all(r["label"] for r in results))
        self.assertEqual(self.client.get(url, {"customer": "nope"}).json()["results"], [])
        self.assertEqual(self.client.get(url).json()["results"], [])

    def test_an_existing_quote_has_no_engagement_field(self):
        quote = Quote.objects.create(customer=self.customer, company=self.company, grand_total=Decimal("1"))
        response = self.client.get(reverse("erp-quote-edit", args=[quote.pk]))
        self.assertNotIn("engagement", response.context["form"].fields)
