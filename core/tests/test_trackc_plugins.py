"""Track C: plugin engine — isolation and fail-closed loading.

Covers docs/testing-plan.md §3 "Plugin engine": enable/disable each
addon in isolation (disabled plugin contributes no nav/URLs/hooks) and
malformed plugins failing closed at load while the server still boots.
"""

import logging

from django.test import SimpleTestCase, override_settings

from core.nav_registry import build_nav
from core.plugins import plugin_registry
from core.plugins.registry import PluginRegistry

EXAMPLE = "golden_erp.addons.example_audit_notifier.plugin.ExampleAuditNotifierPlugin"


class TestPluginIsolation(SimpleTestCase):
    def test_disabled_plugin_contributes_nothing(self):
        with override_settings(GOLDEN_PLUGINS=[]):
            plugin_registry.reload()
            self.assertEqual(plugin_registry.get_nav_items(), [])
            self.assertEqual(plugin_registry.get_dashboard_widgets(), [])
            self.assertEqual(plugin_registry.get_payment_gateways(), {})
            self.assertEqual(plugin_registry.get_shipping_carriers(), {})
            self.assertEqual(
                plugin_registry.get_template_extensions("order_detail_actions"), []
            )
            labels = [item.label for item in build_nav()]
            self.assertNotIn("Audit Hub", labels)

    def test_enabled_plugin_contributes_in_isolation(self):
        with override_settings(GOLDEN_PLUGINS=[EXAMPLE]):
            plugin_registry.reload()
            nav_labels = [i["label"] for i in plugin_registry.get_nav_items()]
            self.assertIn("Audit Hub", nav_labels)
            self.assertIn(
                "crypto_transfer", plugin_registry.get_payment_gateways()
            )
            self.assertEqual(len(plugin_registry.get_dashboard_widgets()), 1)

        # Disabling again removes everything (no residue).
        with override_settings(GOLDEN_PLUGINS=[]):
            plugin_registry.reload()
            self.assertEqual(plugin_registry.get_nav_items(), [])
            self.assertEqual(plugin_registry.get_payment_gateways(), {})

    def test_disabled_plugin_hooks_never_fire(self):
        from core.plugins.signals import order_placed

        received = []

        def _probe(sender, **kwargs):
            received.append(sender)

        with override_settings(GOLDEN_PLUGINS=[]):
            plugin_registry.reload()
            plugin_registry.initialize_apps()
            order_placed.connect(_probe, weak=False)
            try:
                order_placed.send(sender="isolation-probe", order=None)
            finally:
                order_placed.disconnect(_probe)
        # Only our probe fired; no plugin listener appended anything.
        self.assertEqual(received, ["isolation-probe"])


class TestMalformedPluginFailsClosed(SimpleTestCase):
    def test_boot_load_skips_bad_entry_with_logged_error(self):
        with override_settings(GOLDEN_PLUGINS=["non.existent.module.Plugin"]):
            with self.assertLogs("golden_erp.plugins", level="ERROR") as logs:
                registry = PluginRegistry()  # boot path: must not raise
        self.assertEqual(registry.plugins, {})
        self.assertTrue(
            any("non.existent.module.Plugin" in line for line in logs.output)
        )

    def test_boot_load_keeps_good_plugins_past_bad_entry(self):
        with override_settings(GOLDEN_PLUGINS=["non.existent.module.Plugin", EXAMPLE]):
            with self.assertLogs("golden_erp.plugins", level="ERROR"):
                registry = PluginRegistry()
        self.assertIn("example_audit_notifier", registry.plugins)
        self.assertIn("crypto_transfer", registry.get_payment_gateways())

    def test_explicit_reload_stays_strict(self):
        # The strict reload() contract (pinned by test_plugins.py) is
        # unchanged: explicit reloads surface config errors loudly.
        from django.core.exceptions import ImproperlyConfigured

        with override_settings(GOLDEN_PLUGINS=["non.existent.module.Plugin"]):
            with self.assertRaises(ImproperlyConfigured):
                plugin_registry.reload()
        # Restore a working registry for other tests.
        plugin_registry.reload()
        logging.getLogger("golden_erp.plugins").info("registry restored")
