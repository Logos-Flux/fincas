"""One instance per company (release-hardening-plan.md 9.6, decision 14).

``core.E070`` fires on ``migrate`` / ``check --database default`` when more
than one Company exists; the Django admin and ``CompanyViewSet`` refuse to
create a second one. ``Company.save`` stays permissive on purpose (tests and
``seed_test_edge`` build two companies to prove the per-company scoping).
"""

from __future__ import annotations

from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.checks import Error
from django.core.checks.registry import registry
from django.core.management import call_command
from django.core.management.base import SystemCheckError
from django.db.utils import OperationalError
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from core.checks import ONE_COMPANY_CODE, one_company_check, one_company_message
from core.models import Company


def e070(databases=("default",)):
    return [m for m in one_company_check(None, databases=list(databases)) if m.id == "core.E070"]


class OneCompanyCheckTests(TestCase):
    def test_registered_with_the_database_tag(self):
        self.assertIn(one_company_check, registry.registered_checks)
        self.assertIn("database", one_company_check.tags)

    def test_no_database_requested_is_silent(self):
        Company.objects.create(name="A")
        Company.objects.create(name="B")
        self.assertEqual(one_company_check(None, databases=None), [])
        self.assertEqual(one_company_check(None), [])

    def test_empty_table_passes(self):
        self.assertEqual(e070(), [])

    def test_one_company_passes(self):
        Company.objects.create(name="Only")
        self.assertEqual(e070(), [])

    def test_two_companies_fail_and_the_message_names_both(self):
        a = Company.objects.create(name="Alpha Corp")
        b = Company.objects.create(name="Beta Corp")
        [error] = e070()
        self.assertIsInstance(error, Error)
        self.assertEqual(error.id, "core.E070")
        self.assertTrue(error.msg.startswith("2 companies exist ("), error.msg)
        self.assertIn(f"{a.pk}: Alpha Corp", error.msg)
        self.assertIn(f"{b.pk}: Beta Corp", error.msg)
        self.assertIn(
            "Fincas runs one company per instance; keep one Company and run a separate "
            "instance for another company. See docs/deployment.md#one-instance-per-company.",
            error.msg,
        )

    def test_missing_table_passes(self):
        # The first migrate on an empty database: core_company does not exist yet.
        with mock.patch.object(Company.objects, "order_by", side_effect=OperationalError("no such table")):
            self.assertEqual(e070(), [])

    def test_check_command_fails_only_with_the_database(self):
        a = Company.objects.create(name="A")
        Company.objects.create(name="B")
        # parties.E001 (portal company unset with several Companies) is not
        # database-tagged and would fail the plain check on its own; pin it.
        with override_settings(GOLDEN_PORTAL_COMPANY=str(a.pk)):
            # A plain `check` (runserver's) never runs E070.
            call_command("check", stdout=StringIO(), stderr=StringIO())
            with self.assertRaises(SystemCheckError) as caught:
                call_command("check", databases=["default"], stdout=StringIO(), stderr=StringIO())
        self.assertIn("core.E070", str(caught.exception))


class AdminRefusesSecondCompanyTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("root", "root@example.com", "pw")
        self.client.force_login(self.admin)

    def test_first_company_can_be_added(self):
        response = self.client.get(reverse("admin:core_company_add"))
        self.assertEqual(response.status_code, 200)
        response = self.client.post(reverse("admin:core_company_add"), {
            "name": "Only Co", "website_url": "", "phone": "", "notes": "",
            "bc_company_name": "", "erpnext_name": "", "timezone": "",
        })
        self.assertEqual(response.status_code, 302, response.content[:300])
        self.assertEqual(Company.objects.count(), 1)

    def test_second_company_is_refused(self):
        only = Company.objects.create(name="Only Co")
        changelist = self.client.get(reverse("admin:core_company_changelist"))
        self.assertEqual(changelist.status_code, 200)
        self.assertNotContains(changelist, reverse("admin:core_company_add"))
        self.assertContains(changelist, "Fincas runs one company per instance")
        self.assertContains(changelist, f"{only.pk}: Only Co")
        response = self.client.get(reverse("admin:core_company_add"))
        self.assertEqual(response.status_code, 403)
        response = self.client.post(reverse("admin:core_company_add"), {"name": "Second"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Company.objects.count(), 1)


class ApiRefusesSecondCompanyTests(TestCase):
    URL = "/api/v1/core/companies/"

    def setUp(self):
        self.admin = User.objects.create_superuser("root", "root@example.com", "pw")
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def test_first_company_is_created(self):
        response = self.client.post(self.URL, {"name": "Only Co"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Company.objects.count(), 1)

    def test_second_company_is_refused_with_a_stable_code(self):
        only = Company.objects.create(name="Only Co")
        response = self.client.post(self.URL, {"name": "Second"}, format="json")
        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["code"], ONE_COMPANY_CODE)
        self.assertEqual(response.json()["detail"], one_company_message([(only.pk, "Only Co")]))
        self.assertEqual(Company.objects.count(), 1)

    def test_save_stays_permissive(self):
        Company.objects.create(name="A")
        Company.objects.create(name="B")  # seeds and scoping tests rely on this
        self.assertEqual(Company.objects.count(), 2)
