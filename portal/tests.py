"""Milestone 2 Track D — portal B2B isolation.

Covers testing-plan §5 "Portal (B2B)": guests cannot reach customer
routes; customer A cannot see customer B orders/invoices/quotes; the
token-gated invoice view rejects enumerated and expired tokens.
"""

from __future__ import annotations

import secrets
import time
from datetime import date
from decimal import Decimal

import jwt
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from core.models import Company
from core.tests.helpers import make_portal_customer
from orders.models import Order, Quote, SalesInvoice

SECRET = "test-portal-isolation-" + secrets.token_hex(16)


def make_token(sub, email="a@example.com", exp_delta_s=3600):
    payload = {
        "sub": sub,
        "aud": "authenticated",
        "iat": int(time.time()),
        "exp": int(time.time()) + exp_delta_s,
        "role": "authenticated",
        "email": email,
        "user_metadata": {"email_verified": True},
    }
    return jwt.encode(payload, SECRET, algorithm="HS256")


@override_settings(SUPABASE_JWT_SECRET=SECRET)
class PortalIsolationTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Portal Corp")
        # Portal logins as identity step 4 stores them: User + UserIdentity.
        self.customer_a, self.sub_a = make_portal_customer(
            "alice@example.com", first_name="Alice", last_name="A", company=self.company,
        )
        self.customer_b, self.sub_b = make_portal_customer(
            "bob@example.com", first_name="Bob", last_name="B", company=self.company,
        )
        self.order_a = Order.objects.create(
            customer=self.customer_a,
            company=self.company,
            grand_total=Decimal("100.00"),
            balance_due=Decimal("100.00"),
        )
        self.order_b = Order.objects.create(
            customer=self.customer_b,
            company=self.company,
            grand_total=Decimal("200.00"),
            balance_due=Decimal("200.00"),
        )
        self.invoice_b = SalesInvoice.objects.create(
            order=self.order_b,
            company=self.company,
            invoice_date=date.today(),
            grand_total=Decimal("200.00"),
            balance_due=Decimal("200.00"),
        )
        self.quote_b = Quote.objects.create(
            customer=self.customer_b, company=self.company
        )

    def _client_for(self, sub, email="x@example.com"):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {make_token(sub, email=email)}"
        )
        return client

    # ── guests ──────────────────────────────────────────────

    def test_guest_cannot_reach_customer_routes(self):
        anon = APIClient()
        for name in ("portal-order-list", "portal-invoice-list",
                     "portal-quote-list", "portal-profile"):
            with self.subTest(route=name):
                response = anon.get(reverse(name))
                self.assertIn(response.status_code, (401, 403), name)

    def test_staff_session_cannot_reach_customer_routes(self):
        """Portal endpoints reject staff session auth and vice versa."""
        from core.tests.helpers import make_staff_user

        staff = make_staff_user("staffer", password="pw")
        client = APIClient()
        client.force_authenticate(user=staff)
        response = client.get(reverse("portal-order-list"))
        self.assertIn(response.status_code, (401, 403))

    # ── customer A vs customer B ────────────────────────────

    def test_customer_a_order_list_excludes_b(self):
        client = self._client_for(self.sub_a, "alice@example.com")
        data = client.get(reverse("portal-order-list")).json()
        numbers = [o["order_number"] for o in data["results"]]
        self.assertIn(self.order_a.order_number, numbers)
        self.assertNotIn(self.order_b.order_number, numbers)

    def test_customer_a_cannot_retrieve_b_order_detail(self):
        client = self._client_for(self.sub_a, "alice@example.com")
        response = client.get(
            reverse("portal-order-detail", kwargs={"pk": self.order_b.pk})
        )
        self.assertEqual(response.status_code, 404)

    def test_owner_can_retrieve_own_order_detail(self):
        """Guards the <uuid:pk> converter: owner detail must be 200."""
        client = self._client_for(self.sub_a, "alice@example.com")
        response = client.get(
            reverse("portal-order-detail", kwargs={"pk": self.order_a.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["order_number"], self.order_a.order_number
        )

    def test_customer_a_cannot_list_b_invoices(self):
        client = self._client_for(self.sub_a, "alice@example.com")
        data = client.get(reverse("portal-invoice-list")).json()
        numbers = [i["invoice_number"] for i in data["results"]]
        self.assertNotIn(self.invoice_b.invoice_number, numbers)

    def test_customer_a_cannot_retrieve_b_quote(self):
        client = self._client_for(self.sub_a, "alice@example.com")
        response = client.get(
            reverse("portal-quote-detail", kwargs={"pk": self.quote_b.pk})
        )
        self.assertEqual(response.status_code, 404)

    def test_quote_payloads_hide_internal_sales_stage(self):
        """sales_stage is the internal pipeline state; customers never see it,
        nor the engagement that holds it now (CRM Phase 3, spec 1.6)."""
        client = self._client_for(self.sub_b, "bob@example.com")
        listed = client.get(reverse("portal-quote-list")).json()["results"]
        self.assertEqual(len(listed), 1)
        detail = client.get(
            reverse("portal-quote-detail", kwargs={"pk": self.quote_b.pk})
        )
        self.assertEqual(detail.status_code, 200)
        for payload in (listed[0], detail.json()):
            self.assertIn("status", payload)
            for internal in ("sales_stage", "engagement", "win_probability", "loss_reason",
                             "follow_up_date", "estimated_close_date"):
                self.assertNotIn(internal, payload)
            self.assertFalse([k for k in payload if k.startswith("legacy_")])

    def test_portal_user_session_cannot_reach_customer_routes(self):
        """The customer's own User with a session is not portal auth."""
        client = APIClient()
        client.force_login(self.customer_a.user)
        self.assertIn(client.get(reverse("portal-order-list")).status_code, (401, 403))

    def test_expired_portal_jwt_rejected(self):  # beyond the 30 s skew leeway
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {make_token(self.sub_a, exp_delta_s=-120)}")
        response = client.get(reverse("portal-order-list"))
        self.assertIn(response.status_code, (401, 403))
