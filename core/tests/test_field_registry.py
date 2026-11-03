"""``GOLDEN_FIELD_DEFINITIONS`` loader refusals (phase4-fields.md 4.1) and the
unknown-company system check (``core.W113``)."""

from __future__ import annotations

import uuid

from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings

from core import field_registry, sidecar
from core.field_registry import build_definitions, definitions_for, get_definition
from core.models import Company

GOOD = {"key": "custom.package", "label": "Package", "type": "choice",
        "choices": [["accelerator", "Accelerator"], ["other", "Other"]]}


def build(definitions):
    with override_settings(GOLDEN_FIELD_DEFINITIONS=definitions):
        return build_definitions()


class LoaderTests(SimpleTestCase):
    def assertRefused(self, definitions, fragment):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            build(definitions)
        self.assertIn(fragment, str(ctx.exception))

    def test_a_good_definition_builds_with_defaults(self):
        built = build({"engagement": [GOOD]})
        definition = built["engagement"][0]
        self.assertEqual(definition.key, "custom.package")
        self.assertEqual(definition.choice_values, ("accelerator", "other"))
        self.assertEqual((definition.group, definition.list, definition.export, definition.pii,
                          definition.companies), ("Details", False, True, False, None))
        self.assertEqual(built["party"], ())

    def test_empty_or_unset_is_fine(self):
        self.assertEqual(build({}), {"party": (), "engagement": (), "task": ()})
        self.assertEqual(build(None)["task"], ())

    def test_setting_must_be_a_dict_of_lists(self):
        self.assertRefused([GOOD], "must be a dict")
        self.assertRefused({"party": GOOD}, "must be a list")

    def test_unknown_object(self):
        self.assertRefused({"customer": [GOOD]}, "unknown object 'customer'")
        self.assertRefused({"touchpoint": [GOOD]}, "unknown object 'touchpoint'")

    def test_entry_must_be_a_dict(self):
        self.assertRefused({"party": ["custom.x"]}, "must be a dict")

    def test_unknown_attribute(self):
        self.assertRefused({"party": [{**GOOD, "sortable": True}]}, "unknown attribute(s) 'sortable'")

    def test_missing_required_attributes(self):
        for name in ("key", "label", "type"):
            entry = {k: v for k, v in GOOD.items() if k != name}
            with self.subTest(name=name):
                self.assertRefused({"party": [entry]}, f"missing {name}")

    def test_keys_that_break_the_key_rule(self):
        for key in ("package", "Custom.package", "custom.", "custom.Package", "custom.a-b",
                    "a.b.c", "_private", "provenance.source", "x" * 33 + ".y",
                    "custom." + "y" * 65, 7):
            with self.subTest(key=key):
                self.assertRefused({"party": [{**GOOD, "key": key}]}, "key")

    def test_personal_data_in_a_key(self):
        self.assertRefused({"party": [{**GOOD, "key": "custom.line_5551234567"}]}, "personal data")

    def test_duplicate_key_per_object(self):
        self.assertRefused({"party": [GOOD, {**GOOD, "label": "Again"}]}, "twice")
        # The same key on two objects is two fields.
        build({"party": [GOOD], "engagement": [GOOD]})

    def test_a_promoted_key_cannot_be_defined(self):
        sidecar.PROMOTED_KEYS["engagement"]["custom.package"] = "package"
        try:
            self.assertRefused({"engagement": [GOOD]}, "promoted")
        finally:
            sidecar.PROMOTED_KEYS["engagement"].pop("custom.package")
        sidecar.RETIRED_KEYS["engagement"] = frozenset({"custom.package"})
        try:
            self.assertRefused({"engagement": [GOOD]}, "promoted")
        finally:
            sidecar.RETIRED_KEYS["engagement"] = frozenset()

    def test_unknown_type(self):
        self.assertRefused({"party": [{**GOOD, "type": "money"}]}, "type='money'")

    def test_choices_on_a_non_choice_type(self):
        self.assertRefused({"party": [{**GOOD, "type": "string"}]}, "takes none")

    def test_choice_types_need_choices(self):
        for kind in ("choice", "multi_choice"):
            with self.subTest(kind=kind):
                self.assertRefused({"party": [{"key": "custom.x", "label": "X", "type": kind}]},
                                   "needs a non-empty choices list")

    def test_bad_choices(self):
        for choices, fragment in (
            ([["Accelerator", "A"]], "lower-case key"),
            ([["a b", "A"]], "lower-case key"),
            ([["a", "A"], ["a", "Again"]], "twice"),
            ([["a"]], "pair"),
            ([["a", ""]], "empty label"),
        ):
            with self.subTest(choices=choices):
                self.assertRefused({"party": [{**GOOD, "choices": choices}]}, fragment)

    def test_filterable_needs_an_equality_type(self):
        for kind in ("text", "decimal", "date", "datetime"):
            with self.subTest(kind=kind):
                self.assertRefused({"party": [{"key": "custom.x", "label": "X", "type": kind,
                                               "filterable": True}]}, "equality only")
        for kind in ("string", "integer", "boolean", "url"):
            build({"party": [{"key": "custom.x", "label": "X", "type": kind, "filterable": True}]})
        build({"party": [{**GOOD, "filterable": True}]})

    def test_labels_and_text(self):
        self.assertRefused({"party": [{**GOOD, "label": ""}]}, "empty label")
        self.assertRefused({"party": [{**GOOD, "label": "x" * 61}]}, "longer than 60")
        self.assertRefused({"party": [{**GOOD, "group": 3}]}, "group must be a string")

    def test_flags_must_be_booleans(self):
        self.assertRefused({"party": [{**GOOD, "pii": "yes"}]}, "pii must be true or false")

    def test_list_default_rules(self):
        self.assertRefused({"party": [{**GOOD, "list_default": True}]}, "list_default without list")
        self.assertRefused({"party": [{**GOOD, "list": True, "list_default": True, "pii": True}]},
                           "pii and list_default")

    def test_companies(self):
        self.assertRefused({"party": [{**GOOD, "companies": ["not-a-uuid"]}]}, "not a Company id")
        self.assertRefused({"party": [{**GOOD, "companies": []}]}, "non-empty list")
        self.assertRefused({"party": [{**GOOD, "companies": str(uuid.uuid4())}]}, "non-empty list")
        company = uuid.uuid4()
        built = build({"party": [{**GOOD, "companies": [company.hex]}]})
        self.assertEqual(built["party"][0].companies, frozenset({str(company)}))

    def test_a_provider_key_definition_is_display_only(self):
        definition = build({"party": [{"key": "hubspot.hs_lead_status", "label": "Lead status",
                                       "type": "string", "filterable": True}]})["party"][0]
        self.assertTrue(definition.read_only)
        self.assertFalse(definition.is_custom)


class ReloadAndScopeTests(SimpleTestCase):
    def test_rebuilt_on_setting_changed_and_scoped_by_company(self):
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        with override_settings(GOLDEN_FIELD_DEFINITIONS={"party": [
            {**GOOD, "key": "custom.shared"},
            {**GOOD, "key": "custom.only_a", "companies": [a]},
        ]}):
            self.assertEqual([d.key for d in definitions_for("party", a)], ["custom.shared", "custom.only_a"])
            self.assertEqual([d.key for d in definitions_for("party", b)], ["custom.shared"])
            self.assertEqual([d.key for d in definitions_for("party", None)], ["custom.shared"])
            self.assertIsNone(get_definition("party", "custom.only_a", b))
            self.assertIsNotNone(get_definition("party", "custom.only_a", b, any_company=True))
        self.assertEqual(field_registry.DEFINITIONS["party"], ())


class UnknownCompanyCheckTests(TestCase):
    databases = {"default"}

    def test_w113_names_an_unknown_company(self):
        known = Company.objects.create(name="Known")
        missing = str(uuid.uuid4())
        with override_settings(GOLDEN_FIELD_DEFINITIONS={"party": [
            {**GOOD, "companies": [str(known.pk), missing]},
        ]}):
            messages = [m for m in run_checks(databases=["default"]) if m.id == "core.W113"]
        self.assertEqual(len(messages), 1)
        self.assertIn(missing, messages[0].msg)
        self.assertNotIn(str(known.pk), messages[0].msg)

    def test_quiet_when_every_company_exists(self):
        known = Company.objects.create(name="Known")
        with override_settings(GOLDEN_FIELD_DEFINITIONS={"party": [{**GOOD, "companies": [str(known.pk)]}]}):
            self.assertFalse([m for m in run_checks(databases=["default"]) if m.id == "core.W113"])

    def test_w110_is_still_the_signing_key_warning(self):
        # The spec named core.W110 for this check; that id was taken.
        with override_settings(API_JWT_SIGNING_KEY=""):
            messages = [m for m in run_checks() if m.id == "core.W110"]
        self.assertTrue(messages and "API_JWT_SIGNING_KEY" in messages[0].msg)
