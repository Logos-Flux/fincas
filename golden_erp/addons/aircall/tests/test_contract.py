"""The live adapter contract over every Aircall fixture case (docs/crm/phase6-adapters.md 5.3).

Refresh a case's expected files with ``GOLDEN_FIXTURES_WRITE=1`` and review
the diff; never in CI.
"""

from __future__ import annotations

from django.test import TestCase, override_settings

from core.plugins.crm_adapter import assert_crm_adapter_contract

from ..adapter import AircallAdapter
from .helpers import PLUGINS, cases, seed_hook


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class AircallContractTests(TestCase):
    def test_every_case(self):
        results = assert_crm_adapter_contract(AircallAdapter, cases(), seed_hook=seed_hook)
        self.assertEqual(len(results), 12)

    def test_no_case_requests_a_recording(self):
        """Recordings stay at the provider: every request of every case is the calls listing."""
        results = assert_crm_adapter_contract(AircallAdapter, cases(), seed_hook=seed_hook)
        paths = {r["path"] for res in results for r in res.requests + res.second_requests}
        self.assertEqual(paths, {"/v1/calls"})
        for res in results:
            for request in res.requests + res.second_requests:
                text = repr(request)
                self.assertNotIn("media.example.com", text)
                self.assertNotIn("recording", text)
                self.assertNotIn("voicemail", text)
