"""The live adapter contract over every fixture case (docs/crm/phase6-adapters.md 5.3).

This is the test every real adapter copies: one call per adapter class over
its fixture directories. Author or refresh a case's expected files with
``GOLDEN_FIXTURES_WRITE=1`` and review the diff; never in CI.
"""

from __future__ import annotations

from pathlib import Path

from django.test import TestCase, override_settings

from core.plugins.crm_adapter import assert_crm_adapter_contract

from ..adapter import FixtureCrmAdapter, FixtureOAuthAdapter

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PLUGINS = ["golden_erp.addons.fixture_crm.plugin.FixtureCrmPlugin"]


def cases(provider: str) -> list[Path]:
    return sorted(p for p in (FIXTURES / provider).iterdir() if (p / "case.json").exists())


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class FixtureAdapterContractTests(TestCase):
    def test_fixture_adapter(self):
        results = assert_crm_adapter_contract(FixtureCrmAdapter, cases("fixture"))
        self.assertGreaterEqual(len(results), 13)

    def test_fixture_oauth_adapter(self):
        assert_crm_adapter_contract(FixtureOAuthAdapter, cases("fixture_oauth"))
