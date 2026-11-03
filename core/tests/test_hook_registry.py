"""core/hook_registry.py: extension points shared code calls instead of importing
add-ons, and the two products commands without a provider.

Golden ships no provider for either hook; a downstream add-on's registration and
the commands' behaviour through it are tested in that add-on.
"""

import tempfile
from io import StringIO
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings

from core import hook_registry
from products.models import Product, ProductVariant

SPECS = "products.validate_structured_specs"
MPNS = "products.staging_mpns_by_variant"


def _fake(*args, **kwargs):
    return ["fake"]


class BindingValidationTests(SimpleTestCase):
    def test_unknown_hook_name_in_settings_is_rejected(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "unknown hook 'products.nope'"):
            with override_settings(ERP_HOOKS={"products.nope": "x.y"}):
                pass

    def test_binding_must_be_dotted_path_or_none(self):
        for bad in ("nodots", 42, ["a.b"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ImproperlyConfigured):
                    with override_settings(ERP_HOOKS={SPECS: bad}):
                        pass

    def test_erp_hooks_must_be_a_dict(self):
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(ERP_HOOKS=[SPECS]):
                pass

    def test_unimportable_binding_raises_on_get(self):
        with override_settings(ERP_HOOKS={SPECS: "core.no_such_module.func"}):
            with self.assertRaises(ImproperlyConfigured):
                hook_registry.get(SPECS)

    def test_get_of_unknown_hook_is_a_programming_error(self):
        with self.assertRaises(ImproperlyConfigured):
            hook_registry.get("products.nope")


def _other(*args, **kwargs):
    return []


class RegistrationTests(SimpleTestCase):
    """Uses a hook with its provider removed for the test, then restores it."""

    def setUp(self):
        self._saved = dict(hook_registry._registered)
        hook_registry._registered.pop(SPECS, None)

    def tearDown(self):
        hook_registry._registered.clear()
        hook_registry._registered.update(self._saved)

    def test_unprovided_hook_is_none(self):
        self.assertIsNone(hook_registry.get(SPECS))

    def test_registered_implementation_is_returned(self):
        hook_registry.register(SPECS, _fake)
        self.assertIs(hook_registry.get(SPECS), _fake)

    def test_settings_binding_beats_registration(self):
        hook_registry.register(SPECS, _fake)
        with override_settings(ERP_HOOKS={SPECS: "core.tests.test_hook_registry._other"}):
            self.assertIs(hook_registry.get(SPECS), _other)
        self.assertIs(hook_registry.get(SPECS), _fake)

    def test_none_binding_switches_a_hook_off(self):
        hook_registry.register(SPECS, _fake)
        with override_settings(ERP_HOOKS={SPECS: None}):
            self.assertIsNone(hook_registry.get(SPECS))

    def test_re_registering_the_same_callable_is_a_noop(self):
        hook_registry.register(SPECS, _fake)
        hook_registry.register(SPECS, _fake)
        self.assertIs(hook_registry.get(SPECS), _fake)

    def test_a_second_implementation_conflicts(self):
        hook_registry.register(SPECS, _fake)
        with self.assertRaisesMessage(ImproperlyConfigured, "already provided"):
            hook_registry.register(SPECS, _other)

    def test_register_rejects_unknown_names_and_non_callables(self):
        with self.assertRaises(ImproperlyConfigured):
            hook_registry.register("products.nope", _fake)
        with self.assertRaises(ImproperlyConfigured):
            hook_registry.register(MPNS, "not callable")


class CommandsWithoutProviderTests(TestCase):
    """The products commands degrade, not crash, when no add-on provides a hook."""

    def setUp(self):
        self.product = Product.objects.create(title="Panel", slug="panel")
        ProductVariant.objects.create(product=self.product, sku="P-1", mpn="", is_active=True)

    @override_settings(ERP_HOOKS={SPECS: None})
    def test_set_structured_specs_writes_unvalidated_with_a_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "specs.json"
            path.write_text('{"panel": {"not_a_schema_key": {"value": 1}}}')
            out = StringIO()
            call_command("set_structured_specs", "--file", str(path), stdout=out)
        self.assertIn("No structured-spec validator is installed", out.getvalue())
        self.assertNotIn("Unknown spec key", out.getvalue())
        self.product.refresh_from_db()
        self.assertEqual(self.product.structured_specs, {"not_a_schema_key": {"value": 1}})

    def test_set_structured_specs_with_no_provider_registered(self):
        # No ERP_HOOKS binding and nothing registered: golden's own state.
        saved = dict(hook_registry._registered)
        hook_registry._registered.clear()
        self.addCleanup(hook_registry._registered.update, saved)
        with override_settings(ERP_HOOKS={}):
            self.assertIsNone(hook_registry.get(SPECS))
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "specs.json"
                path.write_text('{"panel": {"not_a_schema_key": {"value": 1}}}')
                out = StringIO()
                call_command("set_structured_specs", "--file", str(path), stdout=out)
        self.assertIn("No structured-spec validator is installed", out.getvalue())
        self.product.refresh_from_db()
        self.assertEqual(self.product.structured_specs, {"not_a_schema_key": {"value": 1}})

    @override_settings(ERP_HOOKS={MPNS: None})
    def test_audit_report_runs_and_backfill_refuses(self):
        out = StringIO()
        call_command("audit_mpn_coverage", stdout=out)
        self.assertIn("OVERALL", out.getvalue())
        with self.assertRaisesMessage(CommandError, "products.staging_mpns_by_variant"):
            call_command("audit_mpn_coverage", "--backfill", stdout=StringIO())
