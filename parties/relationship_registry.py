"""Relationship-kind registry: ``settings.GOLDEN_RELATIONSHIP_KINDS`` (phase2-party.md 1.3).

Same loader shape as ``core/role_registry.py``: a frozen dataclass per entry,
``ImproperlyConfigured`` at import on any bad entry, and a rebuild on
``setting_changed`` so tests can override the setting.

Symmetric versus directed is a property of the kind, not of the row: a
symmetric relationship is stored once, in canonical order, and reads the same
label from both ends. A directed one reads ``label`` from the ``from_party``
side and ``reverse_label`` from the ``to_party`` side.

Built-ins, which a deployment can neither remove nor redefine:

- ``employment``           directed   person -> organisation        works at / employs
- ``contact_for``          directed   person -> any                 contact for / has contact
- ``partnership``          symmetric  any <-> any                   partner of
- ``spouse``               symmetric  person <-> person             spouse of
- ``referral``             directed   any -> any                    referred / referred by
- ``parent_organisation``  directed   organisation -> organisation  subsidiary of / parent of

A deployment appends kinds in its settings overlay::

    GOLDEN_RELATIONSHIP_KINDS = [
        {"name": "installs_for", "label": "installs for", "reverse_label": "installer",
         "from_kinds": ["organisation"], "to_kinds": ["organisation"]},
    ]

Kind names are persisted in ``PartyRelationship.kind``: never rename one in
place. Kind-to-party validation (``allows``) runs in ``clean()`` and the
relationship service; it cannot be a database constraint.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

from ._registry import check_key, check_label, configured_list, party_kinds
from .constants import ORGANISATION, PARTY_KINDS, PERSON

EMPLOYMENT = "employment"
CONTACT_FOR = "contact_for"
PARTNERSHIP = "partnership"
SPOUSE = "spouse"
REFERRAL = "referral"
PARENT_ORGANISATION = "parent_organisation"

FROM_SIDE = "from"
TO_SIDE = "to"


@dataclass(frozen=True)
class RelationshipKindConfig:
    name: str                                      # PartyRelationship.kind; persisted, <= 32 chars
    label: str                                     # read from the from_party side
    reverse_label: str = ""                        # read from the to_party side; symmetric: = label
    symmetric: bool = False
    from_kinds: tuple[str, ...] = PARTY_KINDS
    to_kinds: tuple[str, ...] = PARTY_KINDS

    def label_for(self, side: str) -> str:
        """The label as read from ``side`` (``"from"`` or ``"to"``)."""
        if side == FROM_SIDE:
            return self.label
        if side == TO_SIDE:
            return self.reverse_label
        raise ValueError(f"side must be {FROM_SIDE!r} or {TO_SIDE!r}, got {side!r}")

    def allows(self, from_kind: str, to_kind: str) -> bool:
        """Whether a ``from_kind`` party may relate to a ``to_kind`` party.

        For a symmetric kind the two kind lists are equal (enforced at load),
        so the answer does not depend on which end is stored first.
        """
        return from_kind in self.from_kinds and to_kind in self.to_kinds


BUILTIN_RELATIONSHIP_KINDS: tuple[RelationshipKindConfig, ...] = (
    RelationshipKindConfig(EMPLOYMENT, "works at", "employs",
                           from_kinds=(PERSON,), to_kinds=(ORGANISATION,)),
    RelationshipKindConfig(CONTACT_FOR, "contact for", "has contact", from_kinds=(PERSON,)),
    RelationshipKindConfig(PARTNERSHIP, "partner of", "partner of", symmetric=True),
    RelationshipKindConfig(SPOUSE, "spouse of", "spouse of", symmetric=True,
                           from_kinds=(PERSON,), to_kinds=(PERSON,)),
    RelationshipKindConfig(REFERRAL, "referred", "referred by"),
    RelationshipKindConfig(PARENT_ORGANISATION, "subsidiary of", "parent of",
                           from_kinds=(ORGANISATION,), to_kinds=(ORGANISATION,)),
)
BUILTIN_RELATIONSHIP_KIND_NAMES = frozenset(kind.name for kind in BUILTIN_RELATIONSHIP_KINDS)


def _kind_from_config(entry: Any, index: int) -> RelationshipKindConfig:
    where = f"GOLDEN_RELATIONSHIP_KINDS[{index}]"
    if isinstance(entry, RelationshipKindConfig):
        kind = entry
    elif isinstance(entry, dict):
        try:
            kind = RelationshipKindConfig(**entry)
        except TypeError as exc:
            raise ImproperlyConfigured(
                f"{where} is not a valid RelationshipKindConfig: {exc}"
            ) from exc
    else:
        raise ImproperlyConfigured(
            f"{where} must be a dict of RelationshipKindConfig kwargs, got {type(entry).__name__}."
        )
    check_key(where, kind.name)
    where = f"{where} ({kind.name})"
    check_label(where, "label", kind.label)
    if not isinstance(kind.symmetric, bool):
        raise ImproperlyConfigured(f"{where} symmetric must be True or False.")
    if not isinstance(kind.reverse_label, str):
        raise ImproperlyConfigured(f"{where} reverse_label must be a string.")
    from_kinds = party_kinds(where, "from_kinds", kind.from_kinds)
    to_kinds = party_kinds(where, "to_kinds", kind.to_kinds)
    if kind.symmetric:
        if kind.reverse_label and kind.reverse_label != kind.label:
            raise ImproperlyConfigured(
                f"{where} is symmetric, so it reads the same from both ends; drop "
                "reverse_label or make the kind directed."
            )
        if from_kinds != to_kinds:
            raise ImproperlyConfigured(
                f"{where} is symmetric, so from_kinds and to_kinds must be equal "
                "(a symmetric row is stored in either order)."
            )
        reverse_label = kind.label
    else:
        check_label(where, "reverse_label (a directed kind needs one)", kind.reverse_label)
        reverse_label = kind.reverse_label
    return replace(kind, reverse_label=reverse_label, from_kinds=from_kinds, to_kinds=to_kinds)


def build_relationship_kinds() -> dict[str, RelationshipKindConfig]:
    """Built-ins plus ``settings.GOLDEN_RELATIONSHIP_KINDS``, validated, keyed by name."""
    configured = configured_list(
        "GOLDEN_RELATIONSHIP_KINDS", getattr(settings, "GOLDEN_RELATIONSHIP_KINDS", [])
    )
    kinds = {kind.name: kind for kind in BUILTIN_RELATIONSHIP_KINDS}
    for i, entry in enumerate(configured):
        kind = _kind_from_config(entry, i)
        if kind.name in BUILTIN_RELATIONSHIP_KIND_NAMES:
            raise ImproperlyConfigured(
                f"GOLDEN_RELATIONSHIP_KINDS[{i}] redefines the built-in kind {kind.name!r}; "
                f"built-ins ({', '.join(sorted(BUILTIN_RELATIONSHIP_KIND_NAMES))}) are fixed. "
                "Declare a new kind instead."
            )
        if kind.name in kinds:
            raise ImproperlyConfigured(
                f"GOLDEN_RELATIONSHIP_KINDS reuses the name {kind.name!r}."
            )
        kinds[kind.name] = kind
    return kinds


_KINDS: dict[str, RelationshipKindConfig] = build_relationship_kinds()
# Read-only view; the backing dict is replaced in place on reload, so
# `from parties.relationship_registry import RELATIONSHIP_KINDS` stays current.
RELATIONSHIP_KINDS = MappingProxyType(_KINDS)


@receiver(setting_changed)
def _reload_registry(sender, setting, **kwargs) -> None:
    if setting != "GOLDEN_RELATIONSHIP_KINDS":
        return
    rebuilt = build_relationship_kinds()
    _KINDS.clear()
    _KINDS.update(rebuilt)


def get_relationship_kind(name: str) -> RelationshipKindConfig:
    """The registered kind, or ``KeyError`` naming the unknown key."""
    try:
        return RELATIONSHIP_KINDS[name]
    except KeyError:
        raise KeyError(f"unknown relationship kind {name!r}") from None


__all__ = [
    "BUILTIN_RELATIONSHIP_KINDS",
    "BUILTIN_RELATIONSHIP_KIND_NAMES",
    "CONTACT_FOR",
    "EMPLOYMENT",
    "FROM_SIDE",
    "PARENT_ORGANISATION",
    "PARTNERSHIP",
    "REFERRAL",
    "RELATIONSHIP_KINDS",
    "RelationshipKindConfig",
    "SPOUSE",
    "TO_SIDE",
    "build_relationship_kinds",
    "get_relationship_kind",
]
