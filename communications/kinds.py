"""Communication-kind registry (docs/crm/phase3-engagement.md 2.3, 2.6).

Same loader shape as ``crm/registries.py``: a frozen dataclass per entry and
``ImproperlyConfigured`` on any bad entry (``CommunicationsConfig.ready``
builds the registry, so bad config fails ``manage.py check``). No database
access.

Three layers, merged in this order:

1. **Built-ins** (``BUILTIN_COMMUNICATION_KINDS``): ``email``, ``sms``,
   ``call``, ``meeting``, ``linkedin``, ``note``. Always present; nothing can
   redefine them.
2. **Deployment** (``settings.GOLDEN_COMMUNICATION_KINDS``) appends kinds::

       GOLDEN_COMMUNICATION_KINDS = [
           {"name": "whatsapp", "label": "WhatsApp", "icon": "ti ti-brand-whatsapp"},
       ]

3. **Plugins** append through ``BasePlugin.get_communication_kinds()``. A
   name claimed twice, by two plugins or by a plugin and an earlier layer,
   raises ``ImproperlyConfigured`` naming both owners (the payroll-provider
   rule, docs/plugins.md section 6): a kind never silently changes meaning.

Keys are ``[a-z][a-z0-9_]{0,31}`` and are persisted on
``Communication.kind``: never rename one in place. A kind whose plugin is
uninstalled leaves its rows readable; ``kind_or_unknown`` gives the timeline
a generic entry for them.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from types import MappingProxyType
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

from parties._registry import check_key, check_label, configured_list

INBOUND, OUTBOUND, INTERNAL = "inbound", "outbound", "internal"
DIRECTIONS = (INBOUND, OUTBOUND, INTERNAL)

SETTING = "GOLDEN_COMMUNICATION_KINDS"


@dataclass(frozen=True)
class CommunicationKind:
    name: str
    label: str
    icon: str = ""
    directions: tuple[str, ...] = (INBOUND, OUTBOUND)
    # Calls and meetings: ``duration_seconds`` / ``ended_at`` may be set.
    has_duration: bool = False
    # False for kinds such as a marketing send: never resets the stale clock.
    counts_as_activity: bool = True
    # The adapter's payload version (adapter kinds); ``ingest.record`` refuses
    # a newer envelope.
    raw_schema_version: int | None = None


BUILTIN_COMMUNICATION_KINDS: tuple[CommunicationKind, ...] = (
    CommunicationKind("email", "Email", "ti ti-mail"),
    CommunicationKind("sms", "SMS", "ti ti-message"),
    CommunicationKind("call", "Call", "ti ti-phone", has_duration=True),
    CommunicationKind("meeting", "Meeting", "ti ti-calendar-event",
                      directions=(INBOUND, OUTBOUND, INTERNAL), has_duration=True),
    CommunicationKind("linkedin", "LinkedIn message", "ti ti-brand-linkedin"),
    CommunicationKind("note", "Note", "ti ti-note", directions=(INTERNAL,)),
)

UNKNOWN_KIND = CommunicationKind(
    "unknown", "Communication", "ti ti-message-question", directions=DIRECTIONS,
)

_FIELD_NAMES = frozenset(f.name for f in fields(CommunicationKind))


def _kind(where: str, entry: Any) -> CommunicationKind:
    if isinstance(entry, CommunicationKind):
        raw = {f: getattr(entry, f) for f in _FIELD_NAMES}
    elif isinstance(entry, dict):
        unknown = sorted(set(entry) - _FIELD_NAMES)
        if unknown:
            raise ImproperlyConfigured(f"{where} has unknown field(s) {unknown}.")
        raw = dict(entry)
    else:
        raise ImproperlyConfigured(
            f"{where} must be a CommunicationKind or a dict of its fields, "
            f"got {type(entry).__name__}."
        )
    name = raw.get("name")
    check_key(where, name)
    where = f"{where} ({name})"
    check_label(where, "label", raw.get("label"))
    icon = raw.get("icon", "")
    if not isinstance(icon, str) or (icon and not icon.startswith("ti ti-")):
        raise ImproperlyConfigured(f"{where} has icon={icon!r}; use a 'ti ti-*' class or omit it.")
    directions = raw.get("directions", (INBOUND, OUTBOUND))
    if isinstance(directions, str) or not isinstance(directions, (list, tuple)) or not directions:
        raise ImproperlyConfigured(
            f"{where} directions must be a non-empty list drawn from {', '.join(DIRECTIONS)}."
        )
    bad = [d for d in directions if d not in DIRECTIONS]
    if bad or len(set(directions)) != len(directions):
        raise ImproperlyConfigured(
            f"{where} directions={list(directions)!r}; each once, from {', '.join(DIRECTIONS)}."
        )
    for flag in ("has_duration", "counts_as_activity"):
        if flag in raw and not isinstance(raw[flag], bool):
            raise ImproperlyConfigured(f"{where} {flag} must be True or False.")
    version = raw.get("raw_schema_version")
    if version is not None and (isinstance(version, bool) or not isinstance(version, int) or version < 1):
        raise ImproperlyConfigured(f"{where} raw_schema_version={version!r}; use a positive int or None.")
    return CommunicationKind(
        name=name, label=raw["label"], icon=icon,
        directions=tuple(d for d in DIRECTIONS if d in directions),
        has_duration=raw.get("has_duration", False),
        counts_as_activity=raw.get("counts_as_activity", True),
        raw_schema_version=version,
    )


def build_communication_kinds() -> dict[str, CommunicationKind]:
    """Built-ins, then ``GOLDEN_COMMUNICATION_KINDS``, then plugin kinds."""
    from core.plugins.registry import plugin_registry

    entries = {k.name: k for k in BUILTIN_COMMUNICATION_KINDS}
    owners = {k.name: "core (built-in)" for k in BUILTIN_COMMUNICATION_KINDS}
    for i, raw in enumerate(configured_list(SETTING, getattr(settings, SETTING, []))):
        item = _kind(f"{SETTING}[{i}]", raw)
        if item.name in entries:
            raise ImproperlyConfigured(
                f"{SETTING}[{i}] redefines the kind {item.name!r} already defined by "
                f"{owners[item.name]}. Declare a new key instead."
            )
        entries[item.name] = item
        owners[item.name] = SETTING
    for plugin_name, raw in plugin_registry.get_communication_kinds():
        owner = f"plugin {plugin_name!r}"
        item = _kind(f"Communication kind from {owner}", raw)
        if item.name in entries:
            raise ImproperlyConfigured(
                f"Duplicate communication kind {item.name!r}: {owners[item.name]} and "
                f"{owner} both define it. Rename one — a kind must never silently "
                "change meaning."
            )
        entries[item.name] = item
        owners[item.name] = owner
    return entries


# Built lazily and dropped on a settings change: the plugin registry reloads
# on its own ``setting_changed`` receiver, which may run after this one.
_CACHE: dict[str, CommunicationKind] = {}
_BUILT = False


def communication_kinds() -> MappingProxyType:
    """Every registered kind, by name (read-only)."""
    global _BUILT
    if not _BUILT:
        rebuilt = build_communication_kinds()
        _CACHE.clear()
        _CACHE.update(rebuilt)
        _BUILT = True
    return MappingProxyType(_CACHE)


def reset() -> None:
    """Forget the built registry; the next lookup rebuilds (and revalidates) it."""
    global _BUILT
    _BUILT = False


def get_kind(name: str) -> CommunicationKind | None:
    return communication_kinds().get(name)


def kind_or_unknown(name: str) -> CommunicationKind:
    """The registered kind, or a generic one for a kind whose plugin is gone (2.6)."""
    return communication_kinds().get(name) or UNKNOWN_KIND


@receiver(setting_changed)
def _reload(sender, setting, **kwargs) -> None:
    if setting in (SETTING, "GOLDEN_PLUGINS"):
        reset()


__all__ = [
    "BUILTIN_COMMUNICATION_KINDS",
    "CommunicationKind",
    "DIRECTIONS",
    "INBOUND",
    "INTERNAL",
    "OUTBOUND",
    "UNKNOWN_KIND",
    "build_communication_kinds",
    "communication_kinds",
    "get_kind",
    "kind_or_unknown",
    "reset",
]
