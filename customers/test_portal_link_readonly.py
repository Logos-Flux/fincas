"""``Customer.user`` is the portal-login link and must not be API-writable.

Writing it through the API would let any customer-write client re-point a
customer record at another person's portal login. The field is read-only on
CustomerDetailSerializer; only the portal invite/auth flows set it, via the ORM.
The legacy ``supabase_user_id`` column is gone since identity step 8, so the
API neither shows nor accepts it.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from customers.models import Customer

LIST_URL = "/api/v1/customers/customers/"


class CustomerPortalLinkReadOnlyTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Link Co")
        self.staff = make_staff_user(
            username="link-staff", password="pw"
        )
        UserProfile.objects.create(
            user=self.staff, company=self.company,
        )
        self.api = APIClient()
        self.api.force_authenticate(user=self.staff)
        self.victim_user = User.objects.create_user("victim-portal")
        self.attacker_user = User.objects.create_user("attacker-portal")
        self.customer = Customer.objects.create(
            first_name="Vic", last_name="Tim", email="vic@example.com",
            company=self.company, user=self.victim_user,
        )

    def test_patch_ignores_user(self):
        response = self.api.patch(
            f"{LIST_URL}{self.customer.pk}/",
            {"user": self.attacker_user.pk, "first_name": "Vicky"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.user_id, self.victim_user.pk)
        self.assertEqual(self.customer.first_name, "Vicky")
        self.assertEqual(response.data["user"], self.victim_user.pk)
        self.assertNotIn("supabase_user_id", response.data)

    def test_create_ignores_user(self):
        response = self.api.post(
            LIST_URL,
            {
                "first_name": "New", "last_name": "Person",
                "email": "new@example.com", "company": self.company.pk,
                "user": self.attacker_user.pk,
                "supabase_user_id": "00000000-0000-0000-0000-0000000000aa",
            },
            format="json",
        )
        self.assertIn(response.status_code, (200, 201))
        created = Customer.objects.get(email="new@example.com")
        self.assertIsNone(created.user_id)
        self.assertFalse(Customer.objects.filter(user=self.attacker_user).exists())
