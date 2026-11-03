"""System checks for the parties app (docs/crm/phase2-party.md 2.4).

``parties.E001``: a portal provider is configured, more than one Company
exists, and ``GOLDEN_PORTAL_COMPANY`` is unset. Portal sign-ups need a
tenant, and the portal never guesses one.

``parties.W001``: ``GOLDEN_PORTAL_COMPANY`` names no Company. A warning, not
an error, so a fresh database can still ``migrate`` before the Company
exists. Until either is fixed the portal answers 503 ``portal_not_configured``
(as it does while no Company exists at all).

The checks read the database; before the tables exist they report nothing.
"""

from __future__ import annotations

from django.core.checks import Error, Warning, register
from django.core.exceptions import ValidationError
from django.db import DatabaseError


def _portal_configured() -> bool:
    from core.identity.providers import PROVIDERS

    return any("portal" in p.config.entry_points for p in PROVIDERS.values())


@register()
def portal_company_check(app_configs, **kwargs):
    from core.models import Company

    from .mirror import configured_portal_company

    if not _portal_configured():
        return []
    configured = configured_portal_company()
    try:
        if configured:
            try:
                exists = Company._base_manager.filter(pk=configured).exists()
            except (ValidationError, ValueError):  # not a pk at all
                exists = False
            if exists:
                return []
            return [Warning(
                f"GOLDEN_PORTAL_COMPANY={configured!r} names no Company.",
                hint="Set it to the pk of the Company portal customers belong to.",
                id="parties.W001",
            )]
        several = Company._base_manager.count() > 1
    except DatabaseError:
        return []
    if several:
        return [Error(
            "Portal sign-in is configured and several Companies exist, but "
            "GOLDEN_PORTAL_COMPANY is unset.",
            hint="Set GOLDEN_PORTAL_COMPANY to the pk of the Company portal customers belong to "
                 "(docs/crm/phase2-party.md 2.4).",
            id="parties.E001",
        )]
    return []
