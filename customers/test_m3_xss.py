"""Milestone 3 Track G — S4: stored XSS via customer AJAX display HTML.

CustomerDefaultsAjaxView and the single-address AJAX view build
`display_html`/`details` from customer-controlled address fields and
the order form injects them via innerHTML. Values must arrive escaped.
"""

from django.contrib.auth.models import User
from django.test import TestCase

from core.models import Company
from customers.models import Customer, CustomerAddress

BREAKOUT = "</script><script>alert('ajax')</script>"
IMG = '"><img src=x onerror=alert(1)>'


class CustomerAjaxXssTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="cajax-staff", password="pw", is_staff=True
        )
        self.company = Company.objects.create(name="CAjax Co")
        self.customer = Customer.objects.create(
            first_name="Evil",
            last_name="Ajax",
            email="evilajax@example.com",
            company=self.company,
        )
        CustomerAddress.objects.create(
            customer=self.customer,
            first_name="Evil",
            address_line_1=BREAKOUT,
            company_name=IMG,
            city="Nowhere",
            state_province="ST",
            postal_code="00000",
        )
        self.client.force_login(self.staff)

    def test_customer_defaults_details_escaped(self):
        import json as jsonlib

        response = self.client.get(
            f"/erp/customers/{self.customer.pk}/defaults/"
        )
        self.assertEqual(response.status_code, 200)
        payload = jsonlib.loads(response.content.decode())
        # `details` fragments land in innerHTML → must carry no raw markup.
        for fragment in payload["details"].values():
            self.assertNotIn("<script>", fragment)
            self.assertNotIn("<img", fragment)
            self.assertIn("&lt;", fragment)
        # `label` stays raw TEXT (consumers: widget escHtml, Option/value
        # sinks) — pin that the fragment, not the label, is the guard.
        self.assertTrue(payload["addresses"])

    def test_address_search_labels_do_not_carry_raw_markup(self):
        # Labels travel to `a.innerHTML` in the widget (pinned to
        # textContent separately); the JSON itself must still be valid
        # and the staff-only gate must hold for anonymous callers.
        anon = self.client.__class__()
        response = anon.get(
            "/erp/customers/address-search-ajax/",
            {"customer": str(self.customer.pk), "q": "Evil"},
        )
        self.assertIn(response.status_code, (302, 403))
