"""Shared bits of the Aircall tests: the plugin setting, the fixture cases and the seed hook."""

from __future__ import annotations

from pathlib import Path

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
PLUGINS = ["golden_erp.addons.aircall.plugin.AircallPlugin"]
#: Aircall's Basic auth pair, as the api_key credential stores it (invented).
API_KEY = "fixture-api-id:fixture-api-token-1"


def cases() -> list[Path]:
    return sorted(p for p in FIXTURES.iterdir() if (p / "case.json").exists())


def seed_hook(tenant, case: dict) -> None:
    """Seeds the generic runner does not know: open engagements, erased numbers, archived staff."""
    from core.integrations import accounts as services
    from core.models import UserProfile
    from crm import engagements
    from parties.consent import address_hash
    from parties.erasure import PHONE_CHANNELS
    from parties.models import Party, Suppression
    from parties.normalize import normalize_phone

    services.set_credential(tenant.account, "api_key", API_KEY)
    seed = case.get("seed") or {}
    by_key = {v: k for k, v in tenant.keys.by_id.items()}
    for spec in seed.get("engagements", []):
        party = Party.objects.get(pk=by_key[spec["party"]])
        eng = engagements.create(company=tenant.company, party=party)
        tenant.keys.add(spec["key"], eng)
    for number in seed.get("erased_numbers", []):
        e164 = normalize_phone(number)[0]
        for channel in PHONE_CHANNELS:
            Suppression.objects.create(company=tenant.company, channel=channel, address_hash=address_hash(e164),
                                       reason=Suppression.Reason.ERASURE, source="erp", source_detail="erasure")
    for key in seed.get("archived_staff", []):
        profile = UserProfile.objects.get(pk=by_key[f"staff:{key}"])
        profile.user.is_active = False
        profile.user.save(update_fields=["is_active"])
