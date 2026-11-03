"""CRM Phase 4 step 7: the tenancy remainder (phase4-fields.md 6, items 2-3).

- Back-office customer creates (the quote/order quick-create and the full
  form) land in the requester's tenant and check duplicates there.
- A company-less create through the API (a superuser or the legacy key naming
  no company, an unpinned service account) is refused on a multi-company
  instance instead of filing the row under the portal tenant (customers) or
  under no tenant at all (leads). The portal and storefront channels keep
  their ``GOLDEN_PORTAL_COMPANY`` fallback in ``Customer.save``.
- ``check_tenancy`` reports NULL-company leads (and customers).
"""

from __future__ import annotations

import json
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from core.models import Company, UserProfile
from core.service_accounts import ensure_service_account, issue_api_key
from core.tests.helpers import make_staff_user
from crm.models import Lead
from customers.models import Customer

LEADS = "/api/v1/crm/leads/"
CUSTOMERS = "/api/v1/customers/customers/"
LEAD_BODY = {"lead_number": "LD-77001", "first_name": "Nia", "last_name": "Okafor", "email": "nia@example.com"}
CUSTOMER_BODY = {"first_name": "Nia", "last_name": "Okafor", "email": "nia@example.com"}

LEAD_WRITER_ROLES = [
    {"name": "lead-writer", "label": "Lead writer", "kind": "service",
     "permissions": ["crm.view_lead", "crm.add_lead"]},
    {"name": "manager", "label": "Manager", "staff": True},
]


def _staff(username, company):
    user = make_staff_user(username, password="pw")
    if company is not None:
        UserProfile.objects.create(user=user, company=company)
    return user


def _superuser_api():
    api = APIClient()
    api.force_authenticate(user=User.objects.create_superuser("root", "root@example.com", "pw"))
    return api


class MultiCompanyBase(TestCase):
    def setUp(self):
        self.tenant_a = Company.objects.create(name="Tenant A")
        self.tenant_b = Company.objects.create(name="Tenant B")


class QuickCreateDuplicateScopeTests(MultiCompanyBase):
    """The quick-create duplicate check is the tenant's, case-insensitive."""

    def _post(self, email):
        return self.client.post(
            reverse("erp-customer-create-ajax"),
            data=json.dumps({"first_name": "Nia", "last_name": "Okafor", "email": email}),
            content_type="application/json",
        )

    def test_same_email_in_another_tenant_does_not_block(self):
        Customer.objects.create(company=self.tenant_a, first_name="A", last_name="B",
                                email="nia@example.com")
        self.client.force_login(_staff("bee", self.tenant_b))
        response = self._post("nia@example.com")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Customer.objects.get(pk=response.json()["id"]).company_id, self.tenant_b.pk)

    def test_case_variant_in_own_tenant_is_refused(self):
        Customer.objects.create(company=self.tenant_b, first_name="A", last_name="B",
                                email="nia@example.com")
        self.client.force_login(_staff("bee", self.tenant_b))
        response = self._post("NIA@Example.com")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Customer.objects.count(), 1)


class CustomerFormCreateTests(MultiCompanyBase):
    """The full /erp/ customer form stamps the requester's tenant too."""

    FORM = {"first_name": "Nia", "last_name": "Okafor", "email": "nia@example.com",
            "customer_type": "individual", "pricing_tier": "retail", "default_currency": "USD"}

    def test_tenant_b_staff_creates_in_tenant_b(self):
        self.client.force_login(_staff("bee", self.tenant_b))
        with override_settings(GOLDEN_PORTAL_COMPANY=str(self.tenant_a.pk)):
            response = self.client.post(reverse("erp-customer-create"), self.FORM)
        self.assertEqual(response.status_code, 302, response.content[:2000])
        self.assertEqual(Customer.objects.get().company_id, self.tenant_b.pk)

    def test_staff_without_company_is_refused(self):
        self.client.force_login(_staff("nobody", None))
        with override_settings(GOLDEN_PORTAL_COMPANY=str(self.tenant_a.pk)):
            response = self.client.post(reverse("erp-customer-create"), self.FORM)
        self.assertEqual(response.status_code, 200)  # the form, re-rendered
        self.assertEqual(response.context["form"].errors.as_data()["email"][0].code, "no_tenant")
        self.assertFalse(Customer.objects.exists())

    def test_form_page_renders_for_staff_without_company(self):
        self.client.force_login(_staff("nobody", None))
        self.assertEqual(self.client.get(reverse("erp-customer-create")).status_code, 200)


class CustomerFormSingleCompanyTests(TestCase):
    def test_staff_without_profile_gets_the_only_company(self):
        only = Company.objects.create(name="Only")
        self.client.force_login(_staff("demo", None))
        response = self.client.post(reverse("erp-customer-create"), CustomerFormCreateTests.FORM)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Customer.objects.get().company_id, only.pk)


@override_settings(API_KEY="")
class CompanyLessApiCreateTests(MultiCompanyBase):
    """A company-less API create is refused, never filed under the portal tenant."""

    def test_superuser_customer_without_company_is_refused_despite_portal_tenant(self):
        with override_settings(GOLDEN_PORTAL_COMPANY=str(self.tenant_a.pk)):
            response = _superuser_api().post(CUSTOMERS, CUSTOMER_BODY, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.data["company"][0].code, "no_tenant")
        self.assertFalse(Customer.objects.exists())

    def test_superuser_customer_naming_a_company_lands_there(self):
        body = {**CUSTOMER_BODY, "company": str(self.tenant_b.pk)}
        with override_settings(GOLDEN_PORTAL_COMPANY=str(self.tenant_a.pk)):
            response = _superuser_api().post(CUSTOMERS, body, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Customer.objects.get().company_id, self.tenant_b.pk)

    def test_superuser_lead_without_company_is_refused(self):
        response = _superuser_api().post(LEADS, LEAD_BODY, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.data["company"][0].code, "no_tenant")
        self.assertFalse(Lead.objects.exists())

    def test_superuser_lead_naming_a_company_lands_there(self):
        body = {**LEAD_BODY, "company": str(self.tenant_b.pk)}
        response = _superuser_api().post(LEADS, body, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Lead.objects.get().company_id, self.tenant_b.pk)

    def test_superuser_cannot_clear_a_lead_company(self):
        api = _superuser_api()
        body = {**LEAD_BODY, "company": str(self.tenant_b.pk)}
        lead_id = api.post(LEADS, body, format="json").data["id"]
        response = api.patch(f"{LEADS}{lead_id}/", {"company": None}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.data["company"][0].code, "no_tenant")
        self.assertEqual(Lead.objects.get().company_id, self.tenant_b.pk)

    def test_tenant_b_staff_lead_lands_in_tenant_b(self):
        api = APIClient()
        api.force_authenticate(user=_staff("bee", self.tenant_b))
        response = api.post(LEADS, LEAD_BODY, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Lead.objects.get().company_id, self.tenant_b.pk)

    def test_unpinned_service_lead_create_is_refused(self):
        with override_settings(GOLDEN_ROLES=LEAD_WRITER_ROLES):
            svc = ensure_service_account("leads", ["lead-writer"])
            _, raw = issue_api_key(svc, name="leads")
            api = APIClient(HTTP_AUTHORIZATION=f"Api-Key {raw}")
            response = api.post(LEADS, {**LEAD_BODY, "company": str(self.tenant_b.pk)}, format="json")
            self.assertEqual(response.status_code, 403, response.content)
            self.assertFalse(Lead.objects.exists())
            # Pinned to B, the same create lands in B.
            ensure_service_account("leads", company=self.tenant_b)
            response = api.post(LEADS, LEAD_BODY, format="json")
            self.assertEqual(response.status_code, 201, response.content)
            self.assertEqual(Lead.objects.get().company_id, self.tenant_b.pk)


@override_settings(API_KEY="")
class SingleCompanyApiCreateTests(TestCase):
    """With one Company there is nothing to choose: the create lands in it."""

    def setUp(self):
        self.only = Company.objects.create(name="Only")

    def test_superuser_lead_gets_the_only_company(self):
        response = _superuser_api().post(LEADS, LEAD_BODY, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Lead.objects.get().company_id, self.only.pk)

    def test_superuser_customer_gets_the_only_company(self):
        response = _superuser_api().post(CUSTOMERS, CUSTOMER_BODY, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Customer.objects.get().company_id, self.only.pk)


class CheckTenancyNullCompanyTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(name="Acme")

    def run_check(self, *args):
        out = StringIO()
        call_command("check_tenancy", *args, stdout=out)
        return out.getvalue()

    def test_clean_database_passes(self):
        Customer.objects.create(company=self.company, first_name="A", last_name="B",
                                email="a@example.com")
        self.assertIn("0 company-less row(s)", self.run_check("--app", "crm", "--app", "customers"))

    def test_reports_company_less_leads(self):
        # Written around save() (as legacy rows and pre-step-7 API writes are).
        (lead,) = Lead.objects.bulk_create([Lead(
            lead_number="LD-90001", first_name="Nia", last_name="Okafor",
            email="nia@example.com", company=None)])
        out = StringIO()
        with self.assertRaises(CommandError) as raised:
            call_command("check_tenancy", "--app", "crm", stdout=out)
        self.assertEqual(raised.exception.returncode, 1)
        self.assertIn("crm.Lead: 1 row(s) with no company", out.getvalue())
        self.assertIn(str(lead.pk), out.getvalue())
        # Another app's check does not count crm's rows.
        self.assertIn("0 company-less row(s)", self.run_check("--app", "parties"))
