"""Unit tests for Fincas plugin architecture, discovery, and hooks."""

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from core.nav_registry import build_nav
from core.plugins import (
    BasePlugin,
    DashboardWidget,
    order_placed,
    payment_completed,
    plugin_registry,
)


class DummyPlugin(BasePlugin):
    name = "dummy_plugin"
    verbose_name = "Dummy Test Plugin"

    def get_nav_items(self):
        return [
            {
                "type": "link",
                "label": "Plugin Link",
                "section": "plugin_sec",
                "url_name": "erp-dashboard",
                "icon": "ti ti-plug",
                "color": "text-info",
            }
        ]

    def get_dashboard_widgets(self):
        return [
            DashboardWidget(
                id="dummy_widget",
                title="Dummy Widget",
                component_or_template="dummy.html",
                order=5,
            )
        ]


class InvalidDummy:
    pass


class TestPluginRegistry(SimpleTestCase):
    def test_default_empty_plugins(self):
        with override_settings(GOLDEN_PLUGINS=[]):
            plugin_registry.reload()
            assert plugin_registry.get_nav_items() == []
            assert plugin_registry.get_dashboard_widgets() == []

    def test_load_plugin_by_class(self):
        with override_settings(GOLDEN_PLUGINS=[DummyPlugin]):
            plugin_registry.reload()
            plugin = plugin_registry.get_plugin("dummy_plugin")
            assert plugin is not None
            assert plugin.verbose_name == "Dummy Test Plugin"

            widgets = plugin_registry.get_dashboard_widgets()
            assert len(widgets) == 1
            assert widgets[0].id == "dummy_widget"

    def test_load_plugin_by_import_string(self):
        with override_settings(
            GOLDEN_PLUGINS=["golden_erp.addons.example_audit_notifier.plugin.ExampleAuditNotifierPlugin"]
        ):
            plugin_registry.reload()
            plugin = plugin_registry.get_plugin("example_audit_notifier")
            assert plugin is not None
            assert plugin.name == "example_audit_notifier"
            assert "crypto_transfer" in plugin_registry.get_payment_gateways()

    def test_invalid_plugin_target_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(GOLDEN_PLUGINS=["non.existent.module.Class"]):
                plugin_registry.reload()

    def test_non_subclass_plugin_target_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(GOLDEN_PLUGINS=[InvalidDummy]):
                plugin_registry.reload()

    def test_duplicate_plugin_name_raises(self):
        with self.assertRaises(ImproperlyConfigured):
            with override_settings(GOLDEN_PLUGINS=[DummyPlugin, DummyPlugin]):
                plugin_registry.reload()

    def test_nav_registry_incorporates_plugin_items(self):
        with override_settings(GOLDEN_PLUGINS=[DummyPlugin]):
            nav = build_nav()
            labels = [item.label for item in nav]
            assert "Plugin Link" in labels

    def test_lifecycle_signals_reach_plugins(self):
        from golden_erp.addons.example_audit_notifier.plugin import ExampleAuditNotifierPlugin

        with override_settings(
            GOLDEN_PLUGINS=["golden_erp.addons.example_audit_notifier.plugin.ExampleAuditNotifierPlugin"]
        ):
            plugin_registry.reload()
            plugin_registry.initialize_apps()

            plugin = plugin_registry.get_plugin("example_audit_notifier")
            assert isinstance(plugin, ExampleAuditNotifierPlugin)
            initial_count = len(plugin.events_received)

            # Fire canonical signals
            order_placed.send(sender="TestOrderService", order=None)
            payment_completed.send(sender="TestPaymentService", payment=None)

            assert len(plugin.events_received) == initial_count + 2
