"""The live adapter contract over every Gmail fixture case (docs/crm/phase6-adapters.md 5.3).

Author or refresh a case's expected files with ``GOLDEN_FIXTURES_WRITE=1``
and review the diff; never in CI.
"""

from __future__ import annotations

from pathlib import Path

from django.test import TestCase, override_settings

from core.plugins.crm_adapter import assert_crm_adapter_contract

from ..adapter import GmailAdapter
from .seed import seed

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "gmail"
PLUGINS = ["golden_erp.addons.gmail.plugin.GmailPlugin"]


def cases() -> list[Path]:
    return sorted(p for p in FIXTURES.iterdir() if (p / "case.json").exists())


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class GmailContractTests(TestCase):
    def test_every_case(self):
        results = assert_crm_adapter_contract(GmailAdapter, cases(), seed_hook=seed)
        self.assertGreaterEqual(len(results), 17)
