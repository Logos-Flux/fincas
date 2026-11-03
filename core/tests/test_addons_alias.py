"""The deprecated top-level ``addons`` alias of ``golden_erp.addons`` (#120).

REMOVE IN v0.6.0 together with ``addons/__init__.py`` and
``core.plugin_apps.canonical_plugin_path``.

The alias must hand out the module objects registered under the new name,
never a second copy: a duplicate module means a duplicate plugin class, and
two registrations of one adapter.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import warnings
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from core.plugin_apps import canonical_plugin_path, compose_installed_apps, is_base_plugin_path
from core.plugins import plugin_registry
from core.plugins.registry import check_plugin_classification

OLD = "addons.example_audit_notifier.plugin.ExampleAuditNotifierPlugin"
NEW = "golden_erp.addons.example_audit_notifier.plugin.ExampleAuditNotifierPlugin"


def _forget_alias():
    """Drop the alias from this process, as if it was never imported."""
    for name in [n for n in sys.modules if n == "addons" or n.startswith("addons.")]:
        del sys.modules[name]
    sys.meta_path[:] = [f for f in sys.meta_path if type(f).__name__ != "_AliasFinder"]


class AliasModuleIdentityTests(SimpleTestCase):
    def setUp(self):
        self.addCleanup(_forget_alias)
        _forget_alias()

    def test_old_path_is_the_same_module_object(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            old = importlib.import_module("addons.gmail.plugin")
        new = importlib.import_module("golden_erp.addons.gmail.plugin")
        self.assertIs(old, new)
        self.assertIs(sys.modules["addons.gmail"], sys.modules["golden_erp.addons.gmail"])
        self.assertIs(sys.modules["addons.gmail.plugin"], new)
        self.assertEqual(old.__name__, "golden_erp.addons.gmail.plugin")
        self.assertIs(old.GmailPlugin, new.GmailPlugin)

    def test_from_import_and_attribute_access(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import addons.klaviyo.values  # noqa: F401,PLC0415
            from addons.klaviyo import values as old_values  # noqa: PLC0415

            import addons  # noqa: PLC0415
        from golden_erp.addons.klaviyo import values as new_values

        self.assertIs(old_values, new_values)
        self.assertIs(addons.klaviyo.values, new_values)

    def test_import_warns_once(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            importlib.import_module("addons.clickup.plugin")
            importlib.import_module("addons.klaviyo.plugin")
            importlib.import_module("addons.gmail")
        deprecations = [w for w in caught if issubclass(w.category, DeprecationWarning)
                        and "golden_erp.addons" in str(w.message)]
        self.assertEqual(len(deprecations), 1, [str(w.message) for w in caught])
        self.assertIn("v0.6.0", str(deprecations[0].message))

    def test_missing_module_still_raises(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            with self.assertRaises(ModuleNotFoundError):
                importlib.import_module("addons.no_such_addon")
            with self.assertRaises(ModuleNotFoundError):
                importlib.import_module("addons.gmail.no_such_module")

    def test_alias_first_in_a_fresh_process(self):
        """Old spelling imported before the new one: still one module object,
        and the plugin file executed once."""
        code = (
            "import sys, warnings\n"
            "warnings.simplefilter('always')\n"
            "import django; django.setup()\n"
            "with warnings.catch_warnings(record=True) as caught:\n"
            "    warnings.simplefilter('always')\n"
            "    assert 'golden_erp.addons.clickup.adapter' not in sys.modules\n"
            "    import addons.clickup.adapter as old\n"
            "    import golden_erp.addons.clickup.adapter as new\n"
            "assert old is new, (old, new)\n"
            "copies = [m for m in list(sys.modules.values())\n"
            "          if getattr(m, '__file__', None) == new.__file__]\n"
            "assert len(set(map(id, copies))) == 1, copies\n"
            "n = sum(issubclass(w.category, DeprecationWarning) and 'golden_erp.addons' in str(w.message)\n"
            "        for w in caught)\n"
            "assert n == 1, [str(w.message) for w in caught]\n"
            "print('ALIAS-OK')\n"
        )
        env = {**os.environ, "DJANGO_SETTINGS_MODULE": "config.settings.test"}
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=Path(settings.BASE_DIR), env=env,
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        self.assertIn("ALIAS-OK", result.stdout)


class LegacyPluginEntryTests(SimpleTestCase):
    def tearDown(self):
        plugin_registry.reload(raise_on_error=False)

    def test_canonical_plugin_path(self):
        self.assertEqual(canonical_plugin_path(OLD), NEW)
        self.assertEqual(canonical_plugin_path(NEW), NEW)
        self.assertEqual(canonical_plugin_path("addons"), "golden_erp.addons")
        self.assertEqual(canonical_plugin_path("addons_extra.x"), "addons_extra.x")
        self.assertEqual(canonical_plugin_path("core.plugins"), "core.plugins")

    def test_old_spelling_loads_the_same_plugin(self):
        with override_settings(GOLDEN_PLUGINS=[NEW]):
            new_cls = type(plugin_registry.get_plugin("example_audit_notifier"))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with self.assertLogs("golden_erp.plugins", "WARNING"):
                with override_settings(GOLDEN_PLUGINS=[OLD]):
                    plugin = plugin_registry.get_plugin("example_audit_notifier")
                    self.assertIsNotNone(plugin)
                    self.assertIs(type(plugin), new_cls)
                    self.assertEqual(list(plugin_registry.plugins), ["example_audit_notifier"])
                    check_plugin_classification()
                    plugin_registry.get_urls()
        self.assertTrue(any(issubclass(w.category, DeprecationWarning) and NEW in str(w.message)
                            for w in caught))

    def test_both_spellings_refused_clearly(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            with self.assertLogs("golden_erp.plugins", "WARNING"):
                with override_settings(GOLDEN_PLUGINS=[NEW, OLD]):
                    # Boot path: logged and skipped, one registration kept.
                    self.assertEqual(list(plugin_registry.plugins), ["example_audit_notifier"])
                    with self.assertRaisesMessage(ImproperlyConfigured, "deprecated spelling"):
                        plugin_registry.reload(raise_on_error=True)

    def test_plugin_apps_compare_canonical_paths(self):
        self.assertTrue(is_base_plugin_path(OLD))
        with self.assertRaisesMessage(ImproperlyConfigured, "GOLDEN_PLUGINS"):
            compose_installed_apps([], [OLD])
        with self.assertRaisesMessage(ImproperlyConfigured, "repeats"):
            compose_installed_apps([], ["some_app.x", "some_app.x"])
