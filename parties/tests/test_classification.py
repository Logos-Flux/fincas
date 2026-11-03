"""CRM Phase 4 step 4: lifecycle stage, contact type, the ``client`` role
(docs/crm/phase4-fields.md 2.2, 2.3).

The two registries follow the ``parties/role_registry.py`` loader shape; the
columns are validated against them on every write surface; the stage
change stamps ``lifecycle_stage_changed_at`` unless the writer names a time.
"""

from __future__ import annotations

import datetime

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from core import label_registry
from core.labels import get_label
from core.models import Company, UserProfile
from core.tests.helpers import make_staff_user
from parties import classification_registry as reg
from parties.models import Party, PartyRole
from parties.role_registry import CLIENT_ROLE, PARTY_ROLES, staff_writable_role_names

BASE = "/api/v1/parties/"
TYPES = [
    {"name": "installer", "label": "Installer"},
    {"name": "reseller", "label": "Reseller", "export_value": "  Reseller Partner"},
]


class LifecycleRegistryTests(TestCase):
    def test_builtins_in_funnel_order(self):
        self.assertEqual(list(reg.LIFECYCLE_STAGES), [
            "subscriber", "lead", "marketing_qualified", "sales_qualified", "opportunity",
            "customer", "evangelist", "other",
        ])

    def test_a_deployment_appends_and_sort_order_places_it(self):
        with override_settings(GOLDEN_LIFECYCLE_STAGES=[
            {"name": "churned", "label": "Churned", "sort_order": 90},
            {"name": "nurture", "label": "Nurture", "sort_order": 25},
        ]):
            names = list(reg.LIFECYCLE_STAGES)
            self.assertEqual(names[-1], "churned")
            self.assertEqual(names.index("nurture"), names.index("lead") + 1)
            self.assertEqual(get_label("lifecycle_stage.churned").singular, "Churned")
        self.assertNotIn("churned", reg.LIFECYCLE_STAGES)
        self.assertNotIn("lifecycle_stage.churned", label_registry.DEFAULTS)

    def test_refusals(self):
        bad = [
            "not a list",
            [{"name": "lead", "label": "Lead again"}],                  # redefines a built-in
            [{"name": "x", "label": "X"}, {"name": "x", "label": "Y"}],  # reused
            [{"name": "Bad-Key", "label": "Bad"}],                       # key rule
            [{"name": "a" * 33, "label": "Long"}],                       # 32 characters
            [{"name": "nolabel", "label": " "}],
            [{"name": "neg", "label": "Neg", "sort_order": -1}],
            [{"name": "flt", "label": "Flt", "sort_order": 1.5}],
            [{"name": "boo", "label": "Boo", "sort_order": True}],
            [{"name": "extra", "label": "Extra", "colour": "red"}],      # unknown field
            ["just a string"],
        ]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ImproperlyConfigured):
                    with override_settings(GOLDEN_LIFECYCLE_STAGES=value):
                        pass


class ContactTypeRegistryTests(TestCase):
    def test_no_builtins(self):
        self.assertEqual(dict(reg.CONTACT_TYPES), {})
        self.assertFalse([k for k in label_registry.DEFAULTS if k.startswith("contact_type.")])

    @override_settings(GOLDEN_CONTACT_TYPES=TYPES)
    def test_export_value_defaults_to_the_label_and_is_kept_byte_for_byte(self):
        self.assertEqual(reg.CONTACT_TYPES["installer"].export_value, "Installer")
        self.assertEqual(reg.CONTACT_TYPES["reseller"].export_value, "  Reseller Partner")
        self.assertEqual(get_label("contact_type.reseller").singular, "Reseller")

    @override_settings(GOLDEN_CONTACT_TYPES=TYPES)
    def test_import_matching_ignores_case_and_refuses_unknowns(self):
        self.assertEqual(reg.match_contact_type("INSTALLER"), "installer")
        self.assertEqual(reg.match_contact_type("  reseller partner "), "reseller")
        self.assertIsNone(reg.match_contact_type("Plumber"))
        self.assertIsNone(reg.match_contact_type(""))

    def test_ambiguous_import_values_are_refused(self):
        with override_settings(GOLDEN_CONTACT_TYPES=[
            {"name": "a", "label": "Partner"}, {"name": "b", "label": "B", "export_value": "partner"},
        ]):
            self.assertIsNone(reg.match_contact_type("Partner"))

    def test_refusals(self):
        bad = [
            {"name": "x"},
            [{"name": "x", "label": "X"}, {"name": "x", "label": "X2"}],
            [{"name": "x", "label": "X", "export_value": 3}],
            [{"name": "x", "label": "X", "export_value": "   "}],
            [{"name": "x", "label": "X", "export_value": "y" * 256}],
            [{"name": "9x", "label": "X"}],
        ]
        for value in bad:
            with self.subTest(value=value):
                with self.assertRaises(ImproperlyConfigured):
                    with override_settings(GOLDEN_CONTACT_TYPES=value):
                        pass


class ClientRoleTests(TestCase):
    def test_client_is_a_staff_managed_builtin_for_both_kinds(self):
        role = PARTY_ROLES[CLIENT_ROLE]
        self.assertEqual((role.label, role.managed_by, set(role.kinds)), ("Client", "staff",
                                                                          {"person", "organisation"}))
        self.assertIn(CLIENT_ROLE, staff_writable_role_names())
        self.assertEqual(get_label("party_role.client").singular, "Client")

    def test_a_deployment_cannot_redefine_it(self):
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(GOLDEN_PARTY_ROLES=[{"name": "client", "label": "Client"}]):
                pass

    def test_active_and_former_clients(self):
        company = Company.objects.create(name="Tenant A")
        org = Party.objects.create(company=company, kind="organisation", display_name="Acme",
                                   origin="erp")
        role = PartyRole.objects.create(party=org, role=CLIENT_ROLE, valid_from=datetime.date(2026, 1, 1))
        self.assertTrue(PartyRole.objects.filter(role=CLIENT_ROLE, valid_to__isnull=True).exists())
        role.valid_to = datetime.date(2026, 6, 1)
        role.save()
        self.assertFalse(PartyRole.objects.filter(role=CLIENT_ROLE, valid_to__isnull=True).exists())


@override_settings(GOLDEN_CONTACT_TYPES=TYPES)
class PartyColumnTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.company = Company.objects.create(name="Tenant A")
        cls.other_company = Company.objects.create(name="Tenant B")

    def org(self, **fields):
        return Party.objects.create(company=self.company, kind="organisation", display_name="Acme",
                                    origin="erp", **fields)

    def test_defaults_are_blank(self):
        party = self.org()
        self.assertEqual((party.lifecycle_stage, party.contact_type), ("", ""))
        self.assertIsNone(party.lifecycle_stage_changed_at)

    def test_clean_refuses_unknown_keys(self):
        party = self.org()
        party.lifecycle_stage, party.contact_type = "zombie", "plumber"
        with self.assertRaises(ValidationError) as ctx:
            party.full_clean()
        self.assertEqual(set(ctx.exception.message_dict) & {"lifecycle_stage", "contact_type"},
                         {"lifecycle_stage", "contact_type"})

    def test_a_stage_change_stamps_changed_at(self):
        before = timezone.now()
        party = self.org(lifecycle_stage="lead")
        self.assertGreaterEqual(party.lifecycle_stage_changed_at, before)
        first = party.lifecycle_stage_changed_at
        party = Party.objects.get(pk=party.pk)
        party.display_name = "Acme 2"
        party.save()
        self.assertEqual(Party.objects.get(pk=party.pk).lifecycle_stage_changed_at, first)
        party.lifecycle_stage = "customer"
        party.save(update_fields=["lifecycle_stage"])
        self.assertGreater(Party.objects.get(pk=party.pk).lifecycle_stage_changed_at, first)

    def test_a_named_time_is_kept(self):
        source_time = timezone.now() - datetime.timedelta(days=400)
        party = self.org(lifecycle_stage="opportunity", lifecycle_stage_changed_at=source_time)
        self.assertEqual(Party.objects.get(pk=party.pk).lifecycle_stage_changed_at, source_time)
        party = Party.objects.get(pk=party.pk)
        later = source_time + datetime.timedelta(days=30)
        party.lifecycle_stage, party.lifecycle_stage_changed_at = "customer", later
        party.save()
        self.assertEqual(Party.objects.get(pk=party.pk).lifecycle_stage_changed_at, later)

    def test_api_validates_and_filters(self):
        user = make_staff_user("staffer", password="pw")
        UserProfile.objects.create(user=user, company=self.company, display_name="Staffer")
        api = APIClient()
        api.force_authenticate(user=user)
        url = f"{BASE}parties/"
        response = api.post(url, {"kind": "organisation", "display_name": "Bad", "lifecycle_stage": "zombie",
                                  "contact_type": "plumber"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.json()) & {"lifecycle_stage", "contact_type"},
                         {"lifecycle_stage", "contact_type"})
        response = api.post(url, {"kind": "organisation", "display_name": "Good", "lifecycle_stage": "lead",
                                  "contact_type": "installer"}, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual((body["lifecycle_stage"], body["contact_type"]), ("lead", "installer"))
        self.assertIsNotNone(body["lifecycle_stage_changed_at"])
        blank = self.org()
        ids = lambda r: {row["id"] for row in r.json()["results"]}  # noqa: E731
        self.assertEqual(ids(api.get(url, {"lifecycle_stage": "lead"})), {body["id"]})
        self.assertEqual(ids(api.get(url, {"contact_type": "installer"})), {body["id"]})
        self.assertEqual(ids(api.get(url, {"contact_type": "none"})), {str(blank.pk)})
        listed = api.get(url).json()["results"]
        self.assertIn("lifecycle_stage", listed[0])
        self.assertIn("owner_team", listed[0])
