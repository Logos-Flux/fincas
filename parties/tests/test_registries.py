"""GOLDEN_PARTY_ROLES and GOLDEN_RELATIONSHIP_KINDS (phase2-party.md 1.2, 1.3)."""

from __future__ import annotations

import dataclasses

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from parties.constants import ORGANISATION, PARTY_KINDS, PERSON
from parties.relationship_registry import (
    BUILTIN_RELATIONSHIP_KIND_NAMES,
    FROM_SIDE,
    RELATIONSHIP_KINDS,
    TO_SIDE,
    RelationshipKindConfig,
    build_relationship_kinds,
    get_relationship_kind,
)
from parties.role_registry import (
    BUILTIN_PARTY_ROLE_NAMES,
    PARTY_ROLES,
    PartyRoleConfig,
    build_party_roles,
    get_party_role,
    roles_for_kind,
    staff_writable_role_names,
)

INSTALLER = {"name": "installer", "label": "Installer", "kinds": ["organisation"]}
INSTALLS_FOR = {"name": "installs_for", "label": "installs for", "reverse_label": "installer",
                "from_kinds": ["organisation"], "to_kinds": ["organisation"]}


class PartyRoleRegistryTests(SimpleTestCase):
    def _build(self, roles):
        with self.settings(GOLDEN_PARTY_ROLES=roles):
            return build_party_roles()

    def test_builtins_match_the_spec_table(self):
        roles = self._build([])
        both = (PERSON, ORGANISATION)
        expected = {
            "customer": (both, "system"),
            "supplier": (both, "staff"),
            "partner": (both, "staff"),
            "lead": ((PERSON,), "system"),
            "referral_source": (both, "staff"),
            "staff": ((PERSON,), "system"),
            "client": (both, "staff"),  # CRM Phase 4 (phase4-fields.md 2.3)
        }
        self.assertEqual(list(roles), list(expected))
        for name, (kinds, managed_by) in expected.items():
            with self.subTest(role=name):
                self.assertEqual(roles[name].kinds, kinds)
                self.assertEqual(roles[name].managed_by, managed_by)
        self.assertEqual(set(expected), BUILTIN_PARTY_ROLE_NAMES)

    def test_appended_role(self):
        roles = self._build([INSTALLER])
        self.assertEqual(list(roles)[-1], "installer")
        self.assertEqual(roles["installer"].kinds, (ORGANISATION,))
        self.assertTrue(roles["installer"].staff_writable)

    def test_kinds_are_canonicalised(self):
        roles = self._build([{"name": "x", "label": "X", "kinds": ["organisation", "person"]}])
        self.assertEqual(roles["x"].kinds, PARTY_KINDS)

    def test_dataclass_entries_accepted(self):
        roles = self._build([PartyRoleConfig("installer", "Installer", kinds=[ORGANISATION])])
        self.assertEqual(roles["installer"].kinds, (ORGANISATION,))

    def test_builtins_are_immutable(self):
        for name in BUILTIN_PARTY_ROLE_NAMES:
            with self.subTest(name=name), self.assertRaisesRegex(ImproperlyConfigured, "built-in"):
                self._build([{"name": name, "label": "Mine", "managed_by": "staff"}])

    def test_registry_is_read_only(self):
        with self.assertRaises(TypeError):
            PARTY_ROLES["customer"] = PartyRoleConfig("customer", "Mine")
        with self.assertRaises(TypeError):
            del PARTY_ROLES["customer"]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            PARTY_ROLES["customer"].managed_by = "staff"

    def test_bad_config(self):
        cases = {
            "not a list": {"name": "x"},
            "string": "installer",
            "entry not a dict": ["installer"],
            "unknown key": [{"name": "x", "label": "X", "colour": "red"}],
            "missing label": [{"name": "x"}],
            "blank label": [{"name": "x", "label": "  "}],
            "upper-case name": [{"name": "Installer", "label": "X"}],
            "hyphen in name": [{"name": "in-staller", "label": "X"}],
            "name too long": [{"name": "x" * 33, "label": "X"}],
            "trailing newline": [{"name": "installer\n", "label": "X"}],
            "name not str": [{"name": 7, "label": "X"}],
            "bad managed_by": [{"name": "x", "label": "X", "managed_by": "robot"}],
            "kinds a string": [{"name": "x", "label": "X", "kinds": "person"}],
            "kinds empty": [{"name": "x", "label": "X", "kinds": []}],
            "unknown kind": [{"name": "x", "label": "X", "kinds": ["company"]}],
            "kind twice": [{"name": "x", "label": "X", "kinds": ["person", "person"]}],
            "duplicate name": [INSTALLER, INSTALLER],
        }
        for label, config in cases.items():
            with self.subTest(case=label), self.assertRaises(ImproperlyConfigured):
                self._build(config)

    def test_override_rebuilds_the_live_registry(self):
        self.assertNotIn("installer", PARTY_ROLES)
        with self.settings(GOLDEN_PARTY_ROLES=[INSTALLER]):
            self.assertIn("installer", PARTY_ROLES)
            self.assertEqual(get_party_role("installer").kinds, (ORGANISATION,))
        self.assertNotIn("installer", PARTY_ROLES)

    def test_bad_override_fails_loudly(self):
        with self.assertRaises(ImproperlyConfigured):
            with self.settings(GOLDEN_PARTY_ROLES=[{"name": "customer", "label": "Mine"}]):
                pass  # pragma: no cover
        self.assertEqual(PARTY_ROLES["customer"].managed_by, "system")

    def test_helpers(self):
        with self.assertRaisesRegex(KeyError, "nope"):
            get_party_role("nope")
        self.assertEqual(
            [r.name for r in roles_for_kind(ORGANISATION)],
            ["customer", "supplier", "partner", "referral_source", "client"],
        )
        self.assertEqual(staff_writable_role_names(), ("supplier", "partner", "referral_source", "client"))
        with self.settings(GOLDEN_PARTY_ROLES=[INSTALLER]):
            self.assertIn("installer", staff_writable_role_names())


class RelationshipKindRegistryTests(SimpleTestCase):
    def _build(self, kinds):
        with self.settings(GOLDEN_RELATIONSHIP_KINDS=kinds):
            return build_relationship_kinds()

    def test_builtins_match_the_spec_table(self):
        kinds = self._build([])
        both = (PERSON, ORGANISATION)
        # name: (symmetric, from_kinds, to_kinds, label, reverse_label)
        expected = {
            "employment": (False, (PERSON,), (ORGANISATION,), "works at", "employs"),
            "contact_for": (False, (PERSON,), both, "contact for", "has contact"),
            "partnership": (True, both, both, "partner of", "partner of"),
            "spouse": (True, (PERSON,), (PERSON,), "spouse of", "spouse of"),
            "referral": (False, both, both, "referred", "referred by"),
            "parent_organisation": (False, (ORGANISATION,), (ORGANISATION,), "subsidiary of", "parent of"),
        }
        self.assertEqual(list(kinds), list(expected))
        for name, (symmetric, from_kinds, to_kinds, label, reverse) in expected.items():
            with self.subTest(kind=name):
                k = kinds[name]
                self.assertEqual(
                    (k.symmetric, k.from_kinds, k.to_kinds, k.label, k.reverse_label),
                    (symmetric, from_kinds, to_kinds, label, reverse),
                )
        self.assertEqual(set(expected), BUILTIN_RELATIONSHIP_KIND_NAMES)

    def test_labels_by_side(self):
        employment = get_relationship_kind("employment")
        self.assertEqual(employment.label_for(FROM_SIDE), "works at")
        self.assertEqual(employment.label_for(TO_SIDE), "employs")
        spouse = get_relationship_kind("spouse")
        self.assertEqual(spouse.label_for(FROM_SIDE), spouse.label_for(TO_SIDE))
        with self.assertRaises(ValueError):
            employment.label_for("sideways")

    def test_allows(self):
        employment = get_relationship_kind("employment")
        self.assertTrue(employment.allows(PERSON, ORGANISATION))
        self.assertFalse(employment.allows(ORGANISATION, PERSON))
        self.assertFalse(employment.allows(PERSON, PERSON))
        self.assertTrue(get_relationship_kind("contact_for").allows(PERSON, PERSON))
        self.assertFalse(get_relationship_kind("spouse").allows(PERSON, ORGANISATION))
        self.assertTrue(get_relationship_kind("partnership").allows(ORGANISATION, PERSON))

    def test_appended_kind(self):
        kinds = self._build([INSTALLS_FOR, {"name": "sibling", "label": "sibling of", "symmetric": True,
                                           "from_kinds": ["person"], "to_kinds": ["person"]}])
        self.assertEqual(kinds["installs_for"].reverse_label, "installer")
        self.assertEqual(kinds["sibling"].reverse_label, "sibling of")  # symmetric: copied

    def test_builtins_are_immutable(self):
        for name in BUILTIN_RELATIONSHIP_KIND_NAMES:
            with self.subTest(name=name), self.assertRaisesRegex(ImproperlyConfigured, "built-in"):
                self._build([{"name": name, "label": "mine", "reverse_label": "theirs"}])

    def test_registry_is_read_only(self):
        with self.assertRaises(TypeError):
            RELATIONSHIP_KINDS["spouse"] = RelationshipKindConfig("spouse", "x", "y")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            RELATIONSHIP_KINDS["spouse"].symmetric = False

    def test_bad_config(self):
        ok = {"label": "x", "reverse_label": "y"}
        cases = {
            "not a list": {"name": "x"},
            "entry not a dict": ["x"],
            "unknown key": [{"name": "x", **ok, "weight": 1}],
            "missing label": [{"name": "x"}],
            "upper-case name": [{"name": "X", **ok}],
            "name too long": [{"name": "k" * 33, **ok}],
            "trailing newline": [{"name": "kind\n", **ok}],
            "directed without reverse_label": [{"name": "x", "label": "x"}],
            "blank reverse_label": [{"name": "x", "label": "x", "reverse_label": " "}],
            "reverse_label not str": [{"name": "x", "label": "x", "reverse_label": 3}],
            "symmetric not bool": [{"name": "x", **ok, "symmetric": "yes"}],
            "symmetric with a different reverse": [{"name": "x", **ok, "symmetric": True}],
            "symmetric with unequal kinds": [{"name": "x", "label": "x", "symmetric": True,
                                              "from_kinds": ["person"], "to_kinds": ["organisation"]}],
            "from_kinds empty": [{"name": "x", **ok, "from_kinds": []}],
            "to_kinds unknown": [{"name": "x", **ok, "to_kinds": ["robot"]}],
            "to_kinds a string": [{"name": "x", **ok, "to_kinds": "person"}],
            "duplicate name": [INSTALLS_FOR, INSTALLS_FOR],
        }
        for label, config in cases.items():
            with self.subTest(case=label), self.assertRaises(ImproperlyConfigured):
                self._build(config)

    def test_override_rebuilds_the_live_registry(self):
        self.assertNotIn("installs_for", RELATIONSHIP_KINDS)
        with self.settings(GOLDEN_RELATIONSHIP_KINDS=[INSTALLS_FOR]):
            self.assertEqual(get_relationship_kind("installs_for").label, "installs for")
        self.assertNotIn("installs_for", RELATIONSHIP_KINDS)
        with self.assertRaisesRegex(KeyError, "installs_for"):
            get_relationship_kind("installs_for")


class AppWiringTests(SimpleTestCase):
    def test_app_is_installed(self):
        self.assertEqual(apps.get_app_config("parties").name, "parties")

    def test_ready_validates_config(self):
        config = apps.get_app_config("parties")
        config.ready()  # the shipped defaults pass
        with override_settings(GOLDEN_DEFAULT_PHONE_REGION="XX"):
            with self.assertRaises(ImproperlyConfigured):
                config.ready()
