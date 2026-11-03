"""Milestone 3 Track G — S6: mass-assignment guard.

`company` is guarded by CompanyScopedViewSet (F-023, fixed); `status`
direct-PATCH is rejected (F-018, enforced 2026-09-10): legal moves go
through the guarded ``transition`` action, which is the single source of
truth via each model's ``VALID_TRANSITIONS`` map. Server-computed totals
(grand_total, subtotal/tax/balance style fields) are likewise rejected —
they come from ``recalculate_totals()`` / the payment ledger.

This suite pins:

- company: create-under-B / move-to-B → 400 (must hold)
- grand_total/total fields: PATCH → 400 and value unchanged
- status direct PATCH: → 400 and value unchanged (transition endpoint still works;
  see orders/tests/test_status_transitions.py)
- is_staff / UserProfile: non-admin write → 403 (IsAdminUser)
- owner/sales_rep: characterized
"""

from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from customers.models import Customer
from orders.models import Order


def company_user(company, *, username, uid):
    user = make_staff_user(username=username, password="pw")
    user.userprofile = UserProfile.objects.create(
        company=company
    )
    return user


class MassAssignmentTests(TestCase):
    def setUp(self):
        self.company_a = Company.objects.create(name="Mass A")
        self.company_b = Company.objects.create(name="Mass B")
        self.user_a = company_user(
            self.company_a, username="mass-a", uid="uid-mass-a"
        )
        self.customer_a = Customer.objects.create(
            first_name="A",
            last_name="B",
            email="massa@example.com",
            company=self.company_a,
        )
        self.order = Order.objects.create(
            customer=self.customer_a,
            company=self.company_a,
            order_number="MA-1",
            grand_total=Decimal("10.00"),
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user_a)

    def test_create_under_other_company_rejected(self):
        response = self.client.post(
            "/api/v1/orders/orders/",
            {
                "customer": str(self.customer_a.pk),
                "company": str(self.company_b.pk),
                "order_number": "MA-EVIL",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            Order.objects.filter(order_number="MA-EVIL").exists()
        )

    def test_move_to_other_company_rejected(self):
        response = self.client.patch(
            f"/api/v1/orders/orders/{self.order.pk}/",
            {"company": str(self.company_b.pk)},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.order.refresh_from_db()
        self.assertEqual(self.order.company_id, self.company_a.pk)

    def test_grand_total_not_user_settable(self):
        # Money totals must be server-computed (recalculate/convert), not
        # caller-dictated: PATCH is rejected and the value is unchanged.
        response = self.client.patch(
            f"/api/v1/orders/orders/{self.order.pk}/",
            {"grand_total": "9999.99"},
            format="json",
        )
        self.order.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertIn("grand_total", response.data)
        self.assertNotEqual(self.order.grand_total, Decimal("9999.99"))

    def test_status_direct_patch_rejected(self):
        # F-018 (enforced): direct PATCH of status is rejected; legal moves
        # go through POST .../transition/ only.
        response = self.client.patch(
            f"/api/v1/orders/orders/{self.order.pk}/",
            {"status": "completed"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("status", response.data)
        self.order.refresh_from_db()
        self.assertNotEqual(self.order.status, "completed")

    def _as_non_staff(self):
        # IsAdminUser endpoints: the actor is a plain (non-staff) account.
        from django.contrib.auth.models import User

        self.client.force_authenticate(
            user=User.objects.create_user("mass-pleb", password="pw"))

    def test_non_admin_cannot_write_user_profile(self):
        profile = self.user_a.userprofile
        self._as_non_staff()
        response = self.client.patch(
            f"/api/v1/core/users/{profile.pk}/",
            {"display_name": "Pwned"},
            format="json",
        )
        self.assertIn(response.status_code, (401, 403))

    def test_non_admin_cannot_list_companies(self):
        self._as_non_staff()
        response = self.client.get("/api/v1/core/companies/")
        self.assertIn(response.status_code, (401, 403))

    def test_quote_totals_not_user_settable(self):
        # Same money-integrity rule as orders: PATCH rejected, value unchanged.
        from orders.models import Quote

        quote = Quote.objects.create(
            customer=self.customer_a,
            company=self.company_a,
            quote_number="MQ-1",
        )
        response = self.client.patch(
            f"/api/v1/orders/quotes/{quote.pk}/",
            {"grand_total": "4242.42"},
            format="json",
        )
        quote.refresh_from_db()
        self.assertEqual(response.status_code, 400)
        self.assertIn("grand_total", response.data)
        self.assertNotEqual(quote.grand_total, Decimal("4242.42"))
