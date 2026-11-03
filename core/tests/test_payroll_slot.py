"""Step 5 (XD-1…XD-5): payroll slot, duplicate guard, adapter contract."""

from decimal import Decimal

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, TestCase, override_settings

from core.plugins import BasePlugin, plugin_registry
from core.plugins.payroll import (
    AdapterError,
    AdapterReport,
    BasePayrollProvider,
    PayrollCostLine,
    PayrollRunDraft,
    assert_provider_contract,
)


class StubProvider(BasePayrollProvider):
    provider_id = "csv_stub"
    schema_version = 2

    def pull_runs(self, since: str):
        return [
            PayrollRunDraft(
                provider_run_id="RUN-1",
                period_start="2026-09-01",
                period_end="2026-09-15",
                gross=Decimal("10000"),
                net=Decimal("7500"),
                raw_envelope={
                    "schema_version": 2,
                    "provider": "csv_stub",
                    "payload": {"run": "RUN-1"},
                },
                cost_lines=[
                    PayrollCostLine("wages", Decimal("10000"), "Wages"),
                ],
            )
        ]

    def fetch_raw(self, provider_run_id: str):
        return {
            "schema_version": 2,
            "provider": "csv_stub",
            "payload": {"run": provider_run_id},
        }


class StubPlugin(BasePlugin):
    name = "stub_payroll"

    def get_payroll_providers(self):
        return {"csv_stub": StubProvider}


class ClashingPlugin(BasePlugin):
    name = "clash_payroll"

    def get_payroll_providers(self):
        return {"csv_stub": StubProvider}


class PayrollSlotTests(SimpleTestCase):
    def test_slot_collects_providers(self):
        with override_settings(GOLDEN_PLUGINS=[StubPlugin]):
            plugin_registry.reload()
            providers = plugin_registry.get_payroll_providers()
            self.assertEqual(list(providers), ["csv_stub"])
            self.assertIs(providers["csv_stub"], StubProvider)

    def test_duplicate_id_names_both_plugins(self):
        with override_settings(GOLDEN_PLUGINS=[StubPlugin, ClashingPlugin]):
            plugin_registry.reload()
            with self.assertRaises(ImproperlyConfigured) as ctx:
                plugin_registry.get_payroll_providers()
            message = str(ctx.exception)
            self.assertIn("csv_stub", message)
            self.assertIn("stub_payroll", message)
            self.assertIn("clash_payroll", message)

    def test_empty_by_default(self):
        with override_settings(GOLDEN_PLUGINS=[]):
            plugin_registry.reload()
            self.assertEqual(plugin_registry.get_payroll_providers(), {})


class ProviderContractTests(TestCase):
    def test_stub_passes_contract(self):
        assert_provider_contract(
            StubProvider, [{"provider_run_id": "RUN-1"}]
        )

    def test_missing_provider_id_rejected(self):
        with self.assertRaises(TypeError):
            class Nameless(BasePayrollProvider):
                def pull_runs(self, since):
                    return []

                def fetch_raw(self, provider_run_id):
                    return {}

    def test_envelope_without_version_rejected(self):
        with self.assertRaises(AdapterError):
            PayrollRunDraft(
                provider_run_id="X", period_start="2026-01-01",
                period_end="2026-01-15",
                raw_envelope={"provider": "x", "payload": {}},
            )

    def test_reader_rejects_unknown_newer_version(self):
        from core.plugins.payroll import require_known_version

        envelope = {"schema_version": 99, "provider": "x", "payload": {}}
        with self.assertRaises(AdapterError):
            require_known_version(envelope, known_max=2)
        self.assertEqual(
            require_known_version(
                {"schema_version": 2, "provider": "x", "payload": {}},
                known_max=2,
            ),
            2,
        )


class AdapterReportTests(SimpleTestCase):
    def test_fate_counts(self):
        report = AdapterReport()
        report.record("r1", "created")
        report.record("r2", "updated")
        report.record("r3", "skipped_closed", "period closed")
        report.record("r4", "skipped_unbalanced", "no legs")
        report.record("r5", "error", "boom")
        self.assertEqual(
            (report.created, report.updated, report.skipped_closed,
             report.skipped_unbalanced, report.errors),
            (1, 1, 1, 1, 1),
        )
        self.assertEqual(len(report.rows), 5)
