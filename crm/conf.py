"""``settings.GOLDEN_CRM``: CRM knobs (docs/crm/phase3-engagement.md 1.7, 1.8).

A dict; every key is optional::

    GOLDEN_CRM = {
        "lead_response_hours": 24,        # the intake task "Respond to inquiry" is due this long after the lead
        "lead_default_owner": None,       # UserProfile id owning engagements of unassigned leads
        "attribution_lookback_days": 90,  # first touch = earliest touchpoint this far back
        "overdue_grace_hours": 0,         # "Needs next action": overdue by more than this (spec 6.1)
    }

Read at call time (``crm_setting``), so ``override_settings`` works. A bad
value raises ``ImproperlyConfigured`` naming the key.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

DEFAULTS = {
    "lead_response_hours": 24,
    "lead_default_owner": None,
    "attribution_lookback_days": 90,
    "overdue_grace_hours": 0,
}


def crm_setting(name: str):
    if name not in DEFAULTS:
        raise KeyError(name)
    raw = getattr(settings, "GOLDEN_CRM", None) or {}
    if not isinstance(raw, dict):
        raise ImproperlyConfigured("GOLDEN_CRM must be a dict.")
    unknown = sorted(set(raw) - set(DEFAULTS))
    if unknown:
        raise ImproperlyConfigured(f"GOLDEN_CRM has unknown key(s) {unknown}; known: {sorted(DEFAULTS)}.")
    value = raw.get(name, DEFAULTS[name])
    if name in ("lead_response_hours", "attribution_lookback_days", "overdue_grace_hours"):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ImproperlyConfigured(f"GOLDEN_CRM[{name!r}] must be a non-negative number, not {value!r}.")
    elif name == "lead_default_owner" and value is not None and not isinstance(value, str):
        value = str(value)
    return value


def intake_strict() -> bool:
    """Tests set ``GOLDEN_CRM_INTAKE_STRICT``: an intake error fails the save."""
    return bool(getattr(settings, "GOLDEN_CRM_INTAKE_STRICT", False))
