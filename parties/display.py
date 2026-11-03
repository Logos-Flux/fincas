"""Staff-UI wording for party classifications (docs/crm/phase4-fields.md 2.2, 3.5).

Lifecycle stages and contact types are registry *values*: each contributes a
``lifecycle_stage.<name>`` / ``contact_type.<name>`` label key, so a tenant
can reword them. Display only; the API, CSV and webhooks use the keys.
"""

from __future__ import annotations

from core.labels import get_label

from .classification_registry import CONTACT_TYPES, LIFECYCLE_STAGES


def _label(prefix, registry, name, company_id) -> str:
    if not name:
        return ""
    try:
        return get_label(f"{prefix}.{name}", company=company_id).singular
    except (KeyError, ValueError):
        entry = registry.get(name)
        return entry.label if entry else name


def lifecycle_label(name, company_id=None) -> str:
    return _label("lifecycle_stage", LIFECYCLE_STAGES, name, company_id)


def contact_type_label(name, company_id=None) -> str:
    return _label("contact_type", CONTACT_TYPES, name, company_id)


def lifecycle_choices(company_id=None, *, empty="Unknown"):
    """Funnel order (the registry's), with a blank "unknown" first."""
    return [("", empty)] + [(name, lifecycle_label(name, company_id)) for name in LIFECYCLE_STAGES]


def contact_type_choices(company_id=None, *, empty="Not set"):
    return [("", empty)] + [(name, contact_type_label(name, company_id)) for name in CONTACT_TYPES]
