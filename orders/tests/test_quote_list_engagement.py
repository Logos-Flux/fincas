"""The quote list names each quote's engagement and links to it (bug batch
2026-10-15)."""

from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Company
from customers.models import Customer
from orders.models import Quote


class QuoteListEngagementColumnTests(TestCase):
    def setUp(self):
        company = Company.objects.create(name="Quote List Co")
        customer = Customer.objects.create(
            first_name="Bea", last_name="Quote", email="bea@example.com", company=company,
        )
        self.quote = Quote.objects.create(company=company, customer=customer, grand_total=Decimal("80.00"))
        self.client.force_login(User.objects.create_superuser("boss", "boss@example.com", "pw"))

    def test_engagement_column_links_to_the_engagement(self):
        engagement = self.quote.engagement
        self.assertIsNotNone(engagement)
        html = self.client.get(reverse("erp-quote-list")).content.decode()
        detail = reverse("erp-crm-engagement-detail", args=[engagement.pk])
        self.assertIn(f'<a href="{detail}"', html)
        self.assertIn(str(engagement), html)
        # The row click still opens the quote, not the engagement.
        self.assertIn(f'data-href="{reverse("erp-quote-edit", args=[self.quote.pk])}"', html)
        self.assertNotIn(f'data-href="{detail}"', html)

    def test_quote_without_an_engagement_shows_a_dash(self):
        Quote.objects.filter(pk=self.quote.pk).update(engagement=None)
        response = self.client.get(reverse("erp-quote-list"))
        self.assertEqual(response.status_code, 200)
        self.assertNotRegex(response.content.decode(), r"/crm/engagements/[0-9a-f-]{36}/")
