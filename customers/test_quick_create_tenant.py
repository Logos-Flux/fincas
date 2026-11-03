"""The quote/order customer quick-create stamps the requester's company.

It created customers with no company; the party sync then filed them under
the portal company, so on a multi-company instance a tenant-B staff user's
new customer landed in another tenant (phase4-fields.md 6, item 2).
"""

from __future__ import annotations

import json

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserProfile
from customers.models import Customer

URL = "erp-customer-create-ajax"


class QuickCreateTenantTests(TestCase):
    def setUp(self):
        self.tenant_a = Company.objects.create(name="Tenant A")
        self.tenant_b = Company.objects.create(name="Tenant B")

    def _staff(self, username, company, *, profile=True, superuser=False):
        user = User.objects.create_user(username=username, password="pw", is_staff=True,
                                        is_superuser=superuser)
        if profile:
            UserProfile.objects.create(user=user, company=company)
        self.client.force_login(user)
        return user

    def _post(self, email="new@example.com"):
        return self.client.post(
            reverse(URL),
            data=json.dumps({"first_name": "Nia", "last_name": "Okafor", "email": email}),
            content_type="application/json",
        )

    def test_tenant_b_staff_creates_in_tenant_b(self):
        self._staff("bee", self.tenant_b)
        response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        customer = Customer.objects.get(pk=response.json()["id"])
        self.assertEqual(customer.company_id, self.tenant_b.pk)
        if customer.party_id:  # the party sync follows the customer's tenant
            self.assertEqual(customer.party.company_id, self.tenant_b.pk)

    def test_superuser_uses_their_profile_company(self):
        self._staff("root", self.tenant_a, superuser=True)
        response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Customer.objects.get().company_id, self.tenant_a.pk)

    def test_staff_without_company_is_refused_on_a_multi_company_instance(self):
        self._staff("nobody", None)
        response = self._post()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "no_tenant")
        self.assertFalse(Customer.objects.exists())

    def test_staff_without_profile_is_refused(self):
        self._staff("noprofile", None, profile=False)
        response = self._post()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "no_tenant")
        self.assertFalse(Customer.objects.exists())


class QuickCreateSingleCompanyTests(TestCase):
    """A login with no profile company (the staging demo user, a fresh
    createsuperuser) is unambiguous on a single-company instance."""

    def setUp(self):
        self.only = Company.objects.create(name="Only Tenant")

    _staff = QuickCreateTenantTests._staff
    _post = QuickCreateTenantTests._post

    def test_falls_back_to_the_only_company(self):
        self._staff("demo", None, profile=False)
        response = self._post()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Customer.objects.get().company_id, self.only.pk)
