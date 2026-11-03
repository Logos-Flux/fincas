"""The live adapter contract over every Klaviyo fixture case (docs/crm/phase6-adapters.md 5.3).

Author or refresh a case's expected files with ``GOLDEN_FIXTURES_WRITE=1``
and review the diff; never in CI.
"""

from __future__ import annotations

from pathlib import Path

from django.test import TestCase, override_settings

from core.plugins.crm_adapter import assert_crm_adapter_contract

from ..adapter import KlaviyoAdapter
from .seed import seed

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PLUGINS = ["golden_erp.addons.klaviyo.plugin.KlaviyoPlugin"]


def cases() -> list[Path]:
    return sorted(p for p in FIXTURES.iterdir() if (p / "case.json").exists())


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class KlaviyoContractTests(TestCase):
    def test_every_case(self):
        results = assert_crm_adapter_contract(KlaviyoAdapter, cases(), seed_hook=seed)
        names = {r.name for r in results}
        # Every case the spec lists (3.2 "Fixtures"), plus 3b and the erased-profile tombstone.
        for expected in (
            "profile-granted", "profile-no-consent-not-sent", "profile-withdrawn-unsubscribes",
            "suppression-pushed", "reconcile-sweep", "events-received-opened", "unsubscribe-pulled",
            "hard-bounce-pulled", "spam-pulled", "subscribed-ignored", "unknown-profile-dropped",
            "newer-schema-refused", "rate-limited", "erase", "erased-profile-skipped", "segment-lists",
            "existing-profile-opt-out-pulled",
        ):
            self.assertIn(expected, names)
