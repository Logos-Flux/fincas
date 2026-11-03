"""The live adapter contract over every Google Ads fixture case (docs/crm/phase6-adapters.md 5.3).

Each case seeds parties and won engagements (``case.json`` ``seed.deals``),
runs its steps (``drain`` uploads; ``ingest`` replays recorded visits), and
compares the requests and the snapshot with the expected files byte for
byte; a second run must change nothing and send nothing. Author or refresh
a case's expected files with ``GOLDEN_FIXTURES_WRITE=1`` and review the
diff; never in CI.

A deal's ``actions`` run in order at the case's pinned clock: ``win``,
``reopen``, ``uploaded`` (the conversion's ExternalRef exists, as after an
earlier successful upload), ``delivered`` (that deal's pending outbox rows
are done), ``retracted`` (the ref records a retraction), ``erase`` (the
party is erased).
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.utils import timezone

from core.plugins.crm_adapter import assert_crm_adapter_contract

from .. import values
from ..adapter import GoogleAdsAdapter

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PLUGINS = ["golden_erp.addons.google_ads.plugin.GoogleAdsPlugin"]


def cases() -> list[Path]:
    return sorted(p for p in FIXTURES.iterdir() if (p / "case.json").exists())


def seed_deals(tenant, case) -> None:
    """``seed.deals`` -> touchpoints and engagements (see the module docstring)."""
    from core.integrations import accounts
    from core.models import ExternalRef, OutboxEvent
    from core.testing.integration_fixtures import fixture_secret
    from crm import engagements, touchpoints
    from parties.erasure import erase_party
    from parties.models import Party

    # The account-level offline refresh token (the runner seeds per-user grants only).
    accounts.set_credential(tenant.account, "oauth_refresh", fixture_secret("oauth_refresh"))
    by_key = {v: k for k, v in tenant.keys.by_id.items()}
    now = timezone.now()
    for spec in case.get("seed", {}).get("deals", []):
        party = Party.objects.get(pk=by_key[spec["party"]])
        touches = []
        for n, t in enumerate(spec.get("touches", [])):
            touch, _ = touchpoints.record(
                tenant.company, party=party, source="api", occurred_at=now - timedelta(days=t["days"]),
                channel=t.get("channel", ""), click_id_kind=t.get("click_id_kind", ""),
                click_id=t.get("click_id", ""),
            )
            tenant.keys.add(f"touch:{spec['key']}:{n}", touch)
            touches.append(touch)
        eng = engagements.create(
            company=tenant.company, party=party, title=spec["key"],
            expected_value=Decimal(spec["value"]) if spec.get("value") is not None else None,
            currency=spec.get("currency", "USD"),
            first_touch=touches[0] if touches else None, last_touch=touches[-1] if touches else None,
        )
        tenant.keys.add(f"engagement:{spec['key']}", eng)
        for action in spec.get("actions", []):
            eng.refresh_from_db()
            if action == "win":
                engagements.win(eng)
            elif action == "reopen":
                engagements.reopen(eng, source="system")
            elif action == "uploaded":
                ExternalRef.objects.create(
                    company=tenant.company, source=values.PROVIDER_ID, object_type=values.REF_OBJECT_TYPE,
                    source_id=eng.number, content_type=ContentType.objects.get_for_model(eng),
                    object_id=eng.pk, metadata={"pipeline": eng.pipeline.key, "click_id_kind": "gclid"},
                )
            elif action == "delivered":
                OutboxEvent.objects.filter(consumer=values.CONSUMER, object_id=eng.pk, state="pending").update(
                    state="done", completed_at=now)
            elif action == "retracted":
                ref = ExternalRef.objects.get(source=values.PROVIDER_ID, source_id=eng.number,
                                              company=tenant.company)
                ref.metadata = {**ref.metadata, "retracted_at": now.isoformat()}
                ref.save()
            elif action == "erase":
                erase_party(party, reason="fixture")
            else:
                raise AssertionError(f"unknown deal action {action!r}")


@override_settings(GOLDEN_PLUGINS=PLUGINS)
class GoogleAdsContractTests(TestCase):
    def test_every_case(self):
        results = assert_crm_adapter_contract(GoogleAdsAdapter, cases(), seed_hook=seed_deals)
        self.assertGreaterEqual(len(results), 16)
