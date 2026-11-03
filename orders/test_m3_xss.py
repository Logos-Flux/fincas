"""Milestone 3 Track G — S4: stored XSS via address display HTML.

Order/quote edit pages embed per-address "display HTML" (built from
customer-controlled address fields) into a <script> block and later
inject it via innerHTML. Both layers must be neutralized:

- HTML layer: field values escaped (intended <br> separators kept)
- script layer: no literal </script> sequence in the embedded JSON
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Company
from customers.models import Customer, CustomerAddress
from orders.models import Order

BREAKOUT = "</script><script>alert('addr')</script>"
IMG = '"><img src=x onerror=alert(1)>'


class AddressDisplayXssTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="addr-staff", password="pw", is_staff=True
        )
        self.company = Company.objects.create(name="Addr Co")
        self.customer = Customer.objects.create(
            first_name="Evil",
            last_name="Customer",
            email="evil@example.com",
            company=self.company,
        )
        self.address = CustomerAddress.objects.create(
            customer=self.customer,
            first_name="Evil",
            last_name="Customer",
            address_line_1=BREAKOUT,
            company_name=IMG,
            city="Nowhere",
            state_province="ST",
            postal_code="00000",
        )
        self.order = Order.objects.create(
            customer=self.customer,
            company=self.company,
            order_number="AX-1",
            billing_address=self.address,
        )
        self.client.force_login(self.staff)

    def test_order_edit_embeds_no_raw_script_breakout(self):
        response = self.client.get(
            reverse("erp-order-edit", args=[self.order.pk])
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn("<script>alert('addr')</script>", content)
        self.assertNotIn("</script><script>", content)

    def test_order_edit_embeds_no_raw_img_onerror(self):
        response = self.client.get(
            reverse("erp-order-edit", args=[self.order.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<img src=x onerror=alert(1)>", response.content.decode())

    def test_quote_edit_embeds_no_raw_script_breakout(self):
        # Quote pages embed ajax_search_fields (customer labels) though
        # not address_details_json — poison the customer name so the
        # assertion is non-vacuous.
        from orders.models import Quote

        self.customer.first_name = BREAKOUT
        self.customer.save(update_fields=["first_name"])
        quote = Quote.objects.create(
            customer=self.customer,
            company=self.company,
            quote_number="AQX-1",
        )
        response = self.client.get(
            reverse("erp-quote-edit", args=[quote.pk])
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertNotIn(
            "<script>alert('addr')</script>", content
        )
        self.assertIn("addrDetails", content)  # guard: page embeds the JSON

    def test_ajax_label_widget_escapes_labels(self):
        # Regression pin: search-result labels are user-controlled; the
        # widget must escape them before innerHTML insertion.
        from golden_erp.paths import TEMPLATES_DIR

        template = (TEMPLATES_DIR / "erp" / "document_form.html").read_text()
        self.assertIn("escHtml(item.label)", template)
        self.assertIn("escHtml(item.company)", template)
