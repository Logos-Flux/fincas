"""The live adapter contract over every ClickUp fixture case (phase6-adapters.md 5.3).

Author or refresh a case's expected files with ``GOLDEN_FIXTURES_WRITE=1``
and review the diff; never in CI.
"""

from __future__ import annotations

from pathlib import Path

from django.test import TestCase, override_settings

from core.plugins.crm_adapter import assert_crm_adapter_contract

from ..adapter import ClickUpAdapter
from .seed import seed_hook

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PLUGINS = ["golden_erp.addons.clickup.plugin.ClickUpPlugin"]


def cases() -> list[Path]:
    return sorted(p for p in FIXTURES.iterdir() if (p / "case.json").exists())


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class ClickUpContractTests(TestCase):
    def test_every_case(self):
        results = assert_crm_adapter_contract(ClickUpAdapter, cases(), seed_hook=seed_hook)
        names = {r.name for r in results}
        # The 3.1 list, plus the refusals and inbound paths this adapter adds.
        for case in ("push-create", "push-create-timeout", "push-complete", "inbound-create-linked",
                     "inbound-unlinked-ignored", "inbound-status-map", "inbound-stale-ignored", "echo-no-op",
                     "deleted-cancels", "assignee-unknown", "rate-limited", "erase", "newer-schema",
                     "webhook-tampered", "webhook-replayed", "inbound-engagement-refused"):
            self.assertIn(case, names)
