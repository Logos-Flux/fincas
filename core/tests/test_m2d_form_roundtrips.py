"""Milestone 2 Track D — form round-trips for document_form/form.html consumers.

Covers testing-plan §5 "Form round-trips": valid submit persists, invalid
submit re-renders with field errors (no data loss on re-render), CSRF
token present.

- CustomerCreateView (erp/form.html via ERPCreateView)
- DiscountCodeCreateView (erp/form.html)
- QuoteCreateView (erp/document_form.html with line-item formset)
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Company
from customers.models import Customer
from discounts.models import DiscountCode
from orders.models import Quote


class FormRoundTripMixin:
    def setUp(self):
        self.company = Company.objects.create(name="Form Corp")
        user = User.objects.create_user(
            username="form-staff", password="pw", is_staff=True
        )
        self.client.force_login(user)


class CustomerFormTests(FormRoundTripMixin, TestCase):
    url = staticmethod(lambda: reverse("erp-customer-create"))

    def test_get_contains_csrf_token(self):
        html = self.client.get(self.url()).content.decode()
        self.assertIn("csrfmiddlewaretoken", html)

    def test_valid_submit_persists_and_redirects(self):
        response = self.client.post(self.url(), {
            "first_name": "Round",
            "last_name": "Trip",
            "email": "round.trip@example.com",
            "customer_type": Customer.CustomerType.INDIVIDUAL,
            "pricing_tier": Customer.PricingTier.RETAIL,
        })
        self.assertEqual(response.status_code, 302)
        customer = Customer.objects.get(email="round.trip@example.com")
        self.assertEqual(customer.first_name, "Round")

    def test_invalid_submit_rerenders_with_field_errors_and_no_data_loss(self):
        response = self.client.post(self.url(), {
            "first_name": "KeepMe",
            "last_name": "",  # required
            "email": "not-an-email",  # invalid
        })
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn("last_name", form.errors)
        self.assertIn("email", form.errors)
        # Submitted values survive the re-render.
        html = response.content.decode()
        self.assertIn("KeepMe", html)
        self.assertIn("not-an-email", html)
        self.assertFalse(
            Customer.objects.filter(first_name="KeepMe").exists()
        )


class DiscountFormTests(FormRoundTripMixin, TestCase):
    url = staticmethod(lambda: reverse("erp-discount-create"))

    def test_get_contains_csrf_token(self):
        html = self.client.get(self.url()).content.decode()
        self.assertIn("csrfmiddlewaretoken", html)

    def test_valid_submit_persists_and_redirects(self):
        response = self.client.post(self.url(), {
            "code": "ROUNDTRIP20",
            "description": "round trip test",
            "is_active": "on",
            "discount_type": DiscountCode.DiscountType.PERCENT,
            "value": "20",
            "max_uses": "1",
            "minimum_order": "0",
            "applies_to": DiscountCode.AppliesTo.ALL,
            "allowed_skus": "",
        })
        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            DiscountCode.objects.filter(code="ROUNDTRIP20").exists()
        )

    def test_invalid_submit_rerenders_with_field_errors_and_no_data_loss(self):
        response = self.client.post(self.url(), {
            "code": "",  # required
            "description": "KeepThisDescription",
            "discount_type": DiscountCode.DiscountType.PERCENT,
            "value": "not-a-number",  # invalid
        })
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn("code", form.errors)
        self.assertIn("value", form.errors)
        html = response.content.decode()
        self.assertIn("KeepThisDescription", html)
        self.assertFalse(
            DiscountCode.objects.filter(
                description="KeepThisDescription"
            ).exists()
        )


class QuoteDocumentFormTests(FormRoundTripMixin, TestCase):
    """document_form.html consumer with an inline line-item formset."""

    def setUp(self):
        super().setUp()
        self.customer = Customer.objects.create(
            first_name="Quentin",
            last_name="Quote",
            email="quentin@example.com",
            company=self.company,
        )

    def _post(self, **overrides):
        data = {
            "customer": str(self.customer.pk),
            "company": str(self.company.pk),
            "status": Quote.Status.DRAFT,
            "validity_period_days": "7",
            "currency": "USD",
            "exchange_rate": "1",
            "subtotal_ex_tax": "0",
            "discount_amount": "0",
            "tax_total": "0",
            "shipping_cost_ex_tax": "0",
            "grand_total": "0",
            "sales_stage": Quote.SalesStage.PROSPECTING,
            "win_probability": "0",
            "weighted_value": "0",
            "line_items-TOTAL_FORMS": "0",
            "line_items-INITIAL_FORMS": "0",
            "line_items-MIN_NUM_FORMS": "0",
            "line_items-MAX_NUM_FORMS": "1000",
        }
        data.update(overrides)
        return self.client.post(reverse("erp-quote-create"), data)

    def test_get_contains_csrf_token(self):
        html = self.client.get(reverse("erp-quote-create")).content.decode()
        self.assertIn("csrfmiddlewaretoken", html)

    def test_valid_submit_persists_and_redirects(self):
        # A valid create carries at least one entered line — a lineless
        # submit is rejected by the empty-document guard (ISSUE-001, see
        # orders/tests/test_order_create_requires_line.py).
        response = self._post(**{
            "line_items-TOTAL_FORMS": "1",
            "line_items-0-sku": "MISC",
            "line_items-0-item_name": "Consulting, on-site",
            "line_items-0-quantity": "1",
            "line_items-0-unit_price_ex_tax": "100.00",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            Quote.objects.filter(customer=self.customer).count(), 1
        )

    def test_invalid_submit_rerenders_with_field_errors_and_no_quote(self):
        response = self._post(customer="")
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertIn("customer", form.errors)
        self.assertEqual(Quote.objects.count(), 0)
