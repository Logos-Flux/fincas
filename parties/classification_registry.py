"""Party classification registries (docs/crm/phase4-fields.md 2.2).

Same loader shape as ``parties/role_registry.py``: a frozen dataclass per
entry, ``ImproperlyConfigured`` at import on any bad entry (``PartiesConfig``
imports this module), and a rebuild on ``setting_changed``.

- ``GOLDEN_LIFECYCLE_STAGES``  ``Party.lifecycle_stage``. **Ordered** (a
  funnel). Built-ins: ``subscriber``, ``lead``, ``marketing_qualified``,
  ``sales_qualified``, ``opportunity``, ``customer``, ``evangelist``,
  ``other``; a deployment appends ``{name, label, sort_order}``. Golden never
  derives a stage from other data: ``lifecycle_stage = customer`` and an open
  ``PartyRole(customer)`` are different facts.
- ``GOLDEN_CONTACT_TYPES``     ``Party.contact_type``. **No built-ins**: what
  kinds of contacts a business deals with is its own vocabulary. Each entry
  has ``name``, ``label`` and ``export_value`` (default: ``label``), the
  string a segment publisher emits byte for byte.

Blank (``""``) on the column means unknown / unset and is never an entry.
Keys are ``[a-z][a-z0-9_]{0,31}`` and are persisted: never rename one in
place. Each entry contributes the label key ``lifecycle_stage.<name>`` /
``contact_type.<name>`` (``core.label_registry``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

from ._registry import check_key, check_label, configured_list

LIFECYCLE_SETTING = "GOLDEN_LIFECYCLE_STAGES"
CONTACT_TYPE_SETTING = "GOLDEN_CONTACT_TYPES"
MAX_EXPORT_VALUE = 255


@dataclass(frozen=True)
class LifecycleStage:
    name: str
    label: str
    sort_order: int = 0


@dataclass(frozen=True)
class ContactType:
    name: str
    label: str
    export_value: str = ""  # "" = the label, filled in by the loader


BUILTIN_LIFECYCLE_STAGES: tuple[LifecycleStage, ...] = (
    LifecycleStage("subscriber", "Subscriber", 10),
    LifecycleStage("lead", "Lead", 20),
    LifecycleStage("marketing_qualified", "Marketing qualified lead", 30),
    LifecycleStage("sales_qualified", "Sales qualified lead", 40),
    LifecycleStage("opportunity", "Opportunity", 50),
    LifecycleStage("customer", "Customer", 60),
    LifecycleStage("evangelist", "Evangelist", 70),
    LifecycleStage("other", "Other", 80),
)
BUILTIN_LIFECYCLE_STAGE_NAMES = frozenset(s.name for s in BUILTIN_LIFECYCLE_STAGES)


def _entry(setting: str, cls, entry: Any, index: int):
    where = f"{setting}[{index}]"
    if isinstance(entry, cls):
        item = entry
    elif isinstance(entry, dict):
        try:
            item = cls(**entry)
        except TypeError as exc:
            raise ImproperlyConfigured(f"{where} is not a valid {cls.__name__}: {exc}") from exc
    else:
        raise ImproperlyConfigured(
            f"{where} must be a dict of {cls.__name__} kwargs, got {type(entry).__name__}."
        )
    check_key(where, item.name)
    where = f"{where} ({item.name})"
    check_label(where, "label", item.label)
    return where, item


def _lifecycle_entry(entry: Any, index: int) -> LifecycleStage:
    where, item = _entry(LIFECYCLE_SETTING, LifecycleStage, entry, index)
    order = item.sort_order
    if isinstance(order, bool) or not isinstance(order, int) or not 0 <= order <= 32767:
        raise ImproperlyConfigured(f"{where} sort_order must be an integer 0-32767, got {order!r}.")
    return item


def _contact_type_entry(entry: Any, index: int) -> ContactType:
    where, item = _entry(CONTACT_TYPE_SETTING, ContactType, entry, index)
    value = item.export_value
    if not isinstance(value, str):
        raise ImproperlyConfigured(f"{where} export_value must be a string, got {type(value).__name__}.")
    if value == "":
        value = item.label
    elif not value.strip():
        raise ImproperlyConfigured(f"{where} export_value is blank; omit it to use the label.")
    if len(value) > MAX_EXPORT_VALUE:
        raise ImproperlyConfigured(f"{where} export_value is longer than {MAX_EXPORT_VALUE} characters.")
    # Never stripped or case-folded: consumers match it byte for byte.
    return replace(item, export_value=value)


def build_lifecycle_stages() -> dict[str, LifecycleStage]:
    """Built-ins plus the setting, validated, in funnel order (sort_order, then
    declaration order)."""
    configured = configured_list(LIFECYCLE_SETTING, getattr(settings, LIFECYCLE_SETTING, []))
    stages = {s.name: s for s in BUILTIN_LIFECYCLE_STAGES}
    for i, entry in enumerate(configured):
        stage = _lifecycle_entry(entry, i)
        if stage.name in BUILTIN_LIFECYCLE_STAGE_NAMES:
            raise ImproperlyConfigured(
                f"{LIFECYCLE_SETTING}[{i}] redefines the built-in stage {stage.name!r}; built-ins "
                f"({', '.join(s.name for s in BUILTIN_LIFECYCLE_STAGES)}) are fixed. "
                "Declare a new stage instead."
            )
        if stage.name in stages:
            raise ImproperlyConfigured(f"{LIFECYCLE_SETTING} reuses the name {stage.name!r}.")
        stages[stage.name] = stage
    ordered = sorted(enumerate(stages.values()), key=lambda pair: (pair[1].sort_order, pair[0]))
    return {stage.name: stage for _i, stage in ordered}


def build_contact_types() -> dict[str, ContactType]:
    """The setting, validated, in declaration order (no built-ins)."""
    configured = configured_list(CONTACT_TYPE_SETTING, getattr(settings, CONTACT_TYPE_SETTING, []))
    types: dict[str, ContactType] = {}
    for i, entry in enumerate(configured):
        item = _contact_type_entry(entry, i)
        if item.name in types:
            raise ImproperlyConfigured(f"{CONTACT_TYPE_SETTING} reuses the name {item.name!r}.")
        types[item.name] = item
    return types


_STAGES: dict[str, LifecycleStage] = build_lifecycle_stages()
_TYPES: dict[str, ContactType] = build_contact_types()
# Read-only views; the backing dicts are replaced in place on reload.
LIFECYCLE_STAGES = MappingProxyType(_STAGES)
CONTACT_TYPES = MappingProxyType(_TYPES)


@receiver(setting_changed)
def _reload_registry(sender, setting, **kwargs) -> None:
    if setting == LIFECYCLE_SETTING:
        rebuilt = build_lifecycle_stages()
        _STAGES.clear()
        _STAGES.update(rebuilt)
    elif setting == CONTACT_TYPE_SETTING:
        rebuilt = build_contact_types()
        _TYPES.clear()
        _TYPES.update(rebuilt)


def match_contact_type(value: str) -> str | None:
    """The key whose name, label or export value equals ``value`` ignoring
    case (the import rule), or None. Unknown and ambiguous values are
    refused, never guessed."""
    folded = (value or "").strip().casefold()
    if not folded:
        return None
    hits = {
        item.name for item in CONTACT_TYPES.values()
        if folded in {v.strip().casefold() for v in (item.name, item.label, item.export_value)}
    }
    return hits.pop() if len(hits) == 1 else None  # ambiguous is refused too


__all__ = [
    "BUILTIN_LIFECYCLE_STAGES",
    "BUILTIN_LIFECYCLE_STAGE_NAMES",
    "CONTACT_TYPES",
    "ContactType",
    "LIFECYCLE_STAGES",
    "LifecycleStage",
    "build_contact_types",
    "build_lifecycle_stages",
    "match_contact_type",
]
