"""Back-office customer screens."""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user

from .models import Customer


class CustomerMoreInfoViewTests(TestCase):
    """``/erp/customers/<pk>/more-info/`` renders the profile template.

    The access matrix found both GET and an invalid POST returning 500: the
    view re-rendered ``erp/customer_detail.html`` with only the form and the
    customer, and the template's ``status_color`` filter raised
    ``VariableDoesNotExist`` on the missing ``status_colors`` (plus the
    addresses, recent orders and relationships the detail view provides).
    """

    def setUp(self):
        self.company = Company.objects.create(name="Example Supply, Inc")
        self.user = make_staff_user("staff", "staff@example.com", "pw")
        UserProfile.objects.create(user=self.user, company=self.company)
        self.client.force_login(self.user)
        self.customer = Customer.objects.create(
            first_name="Ada", last_name="Lovelace", email="ada@example.com",
            company=self.company,
        )
        self.url = reverse("erp-customer-more-info", args=[self.customer.pk])

    def test_get_renders_the_profile(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ada")

    def test_invalid_post_rerenders_with_errors(self):
        # ``company`` is required: blank it and send an unknown price list.
        response = self.client.post(self.url, {
            "company": "",
            "default_price_list": "00000000-0000-0000-0000-000000000000",
        })
        self.assertIn(response.status_code, (200, 400))
        self.assertContains(response, "Ada", status_code=response.status_code)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.company_id, self.company.pk)

    def test_valid_post_saves_and_redirects(self):
        response = self.client.post(self.url, {
            "company": str(self.company.pk),
            "lead_source": "referral",
            "default_currency": "USD",
        })
        self.assertEqual(response.status_code, 302)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.lead_source, "referral")
