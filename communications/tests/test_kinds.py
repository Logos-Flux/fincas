"""The communication-kind registry (docs/crm/phase3-engagement.md 2.3, 2.6)."""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from communications import kinds
from communications.kinds import CommunicationKind, build_communication_kinds, communication_kinds
from core.plugins.base import BasePlugin


class CallsAdapter(BasePlugin):
    name = "calls_adapter"

    def get_communication_kinds(self):
        return [CommunicationKind(name="voicemail", label="Voicemail", icon="ti ti-voicemail",
                                  directions=("inbound",), has_duration=True,
                                  raw_schema_version=1)]


class OtherCallsAdapter(BasePlugin):
    name = "other_calls_adapter"

    def get_communication_kinds(self):
        return [{"name": "voicemail", "label": "Voice mail"}]


class EmailHijacker(BasePlugin):
    name = "email_hijacker"

    def get_communication_kinds(self):
        return [{"name": "email", "label": "Email (ours)"}]


class MarketingAdapter(BasePlugin):
    name = "marketing_adapter"

    def get_communication_kinds(self):
        return [{"name": "marketing_email", "label": "Marketing email",
                 "directions": ["outbound"], "counts_as_activity": False}]


class BuiltinTests(SimpleTestCase):
    def test_builtins(self):
        registry = build_communication_kinds()
        self.assertEqual(list(registry)[:6], ["email", "sms", "call", "meeting", "linkedin", "note"])
        self.assertEqual(registry["note"].directions, ("internal",))
        self.assertEqual(registry["meeting"].directions, ("inbound", "outbound", "internal"))
        self.assertTrue(registry["call"].has_duration)
        self.assertFalse(registry["email"].has_duration)
        self.assertTrue(all(k.counts_as_activity for k in registry.values()))

    def test_kind_or_unknown(self):
        self.assertEqual(kinds.kind_or_unknown("email").label, "Email")
        self.assertEqual(kinds.kind_or_unknown("gone").name, "unknown")


class SettingsTests(SimpleTestCase):
    @override_settings(GOLDEN_COMMUNICATION_KINDS=[
        {"name": "whatsapp", "label": "WhatsApp", "icon": "ti ti-brand-whatsapp"}])
    def test_a_deployment_appends_a_kind(self):
        self.assertEqual(communication_kinds()["whatsapp"].directions, ("inbound", "outbound"))

    def test_registry_follows_the_setting(self):
        self.assertNotIn("whatsapp", communication_kinds())
        with override_settings(GOLDEN_COMMUNICATION_KINDS=[{"name": "whatsapp", "label": "WhatsApp"}]):
            self.assertIn("whatsapp", communication_kinds())
        self.assertNotIn("whatsapp", communication_kinds())

    def assertRefused(self, configured, *fragments):
        with override_settings(GOLDEN_COMMUNICATION_KINDS=configured):
            with self.assertRaises(ImproperlyConfigured) as ctx:
                build_communication_kinds()
        for fragment in fragments:
            self.assertIn(fragment, str(ctx.exception))

    def test_redefining_a_builtin_is_refused(self):
        self.assertRefused([{"name": "email", "label": "Mail"}], "'email'", "core (built-in)")

    def test_a_name_twice_is_refused(self):
        self.assertRefused([{"name": "fax", "label": "Fax"}, {"name": "fax", "label": "Fax 2"}],
                           "'fax'", "GOLDEN_COMMUNICATION_KINDS")

    def test_bad_entries_are_refused(self):
        cases = [
            ("not a list", "must be a list"),
            (["fax"], "must be a CommunicationKind or a dict"),
            ([{"name": "Fax", "label": "Fax"}], "lower-case key"),
            ([{"name": "fax"}], "label"),
            ([{"name": "fax", "label": "Fax", "colour": "red"}], "unknown field"),
            ([{"name": "fax", "label": "Fax", "icon": "fa-fax"}], "ti ti-"),
            ([{"name": "fax", "label": "Fax", "directions": []}], "directions"),
            ([{"name": "fax", "label": "Fax", "directions": "inbound"}], "directions"),
            ([{"name": "fax", "label": "Fax", "directions": ["sideways"]}], "directions"),
            ([{"name": "fax", "label": "Fax", "directions": ["inbound", "inbound"]}], "each once"),
            ([{"name": "fax", "label": "Fax", "has_duration": "yes"}], "has_duration"),
            ([{"name": "fax", "label": "Fax", "counts_as_activity": 0}], "counts_as_activity"),
            ([{"name": "fax", "label": "Fax", "raw_schema_version": 0}], "raw_schema_version"),
            ([{"name": "fax", "label": "Fax", "raw_schema_version": True}], "raw_schema_version"),
        ]
        for configured, fragment in cases:
            with self.subTest(configured=configured):
                self.assertRefused(configured, fragment)


class PluginTests(SimpleTestCase):
    @override_settings(GOLDEN_PLUGINS=[CallsAdapter, MarketingAdapter])
    def test_a_plugin_kind_registers(self):
        registry = communication_kinds()
        self.assertEqual(registry["voicemail"].raw_schema_version, 1)
        self.assertEqual(registry["voicemail"].directions, ("inbound",))
        self.assertFalse(registry["marketing_email"].counts_as_activity)

    def test_plugin_kinds_leave_with_the_plugin(self):
        with override_settings(GOLDEN_PLUGINS=[CallsAdapter]):
            self.assertIn("voicemail", communication_kinds())
        self.assertNotIn("voicemail", communication_kinds())

    @override_settings(GOLDEN_PLUGINS=[CallsAdapter, OtherCallsAdapter])
    def test_a_duplicate_across_plugins_fails_naming_both(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            build_communication_kinds()
        self.assertIn("'calls_adapter'", str(ctx.exception))
        self.assertIn("'other_calls_adapter'", str(ctx.exception))
        self.assertIn("'voicemail'", str(ctx.exception))

    @override_settings(GOLDEN_PLUGINS=[EmailHijacker])
    def test_a_plugin_redefining_a_builtin_fails_naming_both(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            build_communication_kinds()
        self.assertIn("core (built-in)", str(ctx.exception))
        self.assertIn("'email_hijacker'", str(ctx.exception))

    @override_settings(GOLDEN_PLUGINS=[CallsAdapter],
                       GOLDEN_COMMUNICATION_KINDS=[{"name": "voicemail", "label": "VM"}])
    def test_a_plugin_redefining_a_deployment_kind_fails_naming_both(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            build_communication_kinds()
        self.assertIn("GOLDEN_COMMUNICATION_KINDS", str(ctx.exception))
        self.assertIn("'calls_adapter'", str(ctx.exception))

    def test_base_plugin_contributes_nothing(self):
        class Bare(BasePlugin):
            name = "bare"

        self.assertEqual(Bare().get_communication_kinds(), [])
