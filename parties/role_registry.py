"""Party role registry: ``settings.GOLDEN_PARTY_ROLES`` (phase2-party.md 1.2).

Same loader shape as ``core/role_registry.py``: a frozen dataclass per entry,
``ImproperlyConfigured`` at import on any bad entry, and a rebuild on
``setting_changed`` so tests can override the setting.

*Party roles* say what a party is to the business; *auth roles*
(``GOLDEN_ROLES``) say what a login may do. They never share a registry.

Built-ins, which a deployment can neither remove nor redefine:

- ``customer``         both kinds     system (the Customer sync; the API and UI refuse it)
- ``supplier``         both kinds     staff
- ``partner``          both kinds     staff
- ``lead``             person         system (the Lead hook)
- ``referral_source``  both kinds     staff
- ``staff``            person         system (written from Party.user's auth roles)
- ``client``           both kinds     staff (a party the tenant delivers services to;
                                       phase4-fields.md 2.3, independent of ``customer``)

A deployment appends roles in its settings overlay::

    GOLDEN_PARTY_ROLES = [
        {"name": "installer", "label": "Installer", "kinds": ["organisation"]},
    ]

Role names are persisted in ``PartyRole.role``: never rename one in place.
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
from .constants import PARTY_KINDS, PERSON

MANAGED_BY = ("staff", "system")

CUSTOMER_ROLE = "customer"
SUPPLIER_ROLE = "supplier"
PARTNER_ROLE = "partner"
LEAD_ROLE = "lead"
REFERRAL_SOURCE_ROLE = "referral_source"
STAFF_ROLE = "staff"
CLIENT_ROLE = "client"


@dataclass(frozen=True)
class PartyRoleConfig:
    name: str                                  # PartyRole.role; persisted, <= 32 chars
    label: str                                 # display text ("Roles" in the UI)
    kinds: tuple[str, ...] = PARTY_KINDS       # party kinds that may hold the role
    managed_by: str = "staff"                  # "staff" | "system" (API/UI refuse system roles)

    @property
    def staff_writable(self) -> bool:
        return self.managed_by == "staff"


BUILTIN_PARTY_ROLES: tuple[PartyRoleConfig, ...] = (
    PartyRoleConfig(CUSTOMER_ROLE, "Customer", managed_by="system"),
    PartyRoleConfig(SUPPLIER_ROLE, "Supplier"),
    PartyRoleConfig(PARTNER_ROLE, "Partner"),
    PartyRoleConfig(LEAD_ROLE, "Lead", kinds=(PERSON,), managed_by="system"),
    PartyRoleConfig(REFERRAL_SOURCE_ROLE, "Referral source"),
    PartyRoleConfig(STAFF_ROLE, "Staff", kinds=(PERSON,), managed_by="system"),
    # Active client = an open PartyRole(client); former = a closed one (F5).
    PartyRoleConfig(CLIENT_ROLE, "Client"),
)
BUILTIN_PARTY_ROLE_NAMES = frozenset(role.name for role in BUILTIN_PARTY_ROLES)


def _role_from_config(entry: Any, index: int) -> PartyRoleConfig:
    where = f"GOLDEN_PARTY_ROLES[{index}]"
    if isinstance(entry, PartyRoleConfig):
        role = entry
    elif isinstance(entry, dict):
        try:
            role = PartyRoleConfig(**entry)
        except TypeError as exc:
            raise ImproperlyConfigured(f"{where} is not a valid PartyRoleConfig: {exc}") from exc
    else:
        raise ImproperlyConfigured(
            f"{where} must be a dict of PartyRoleConfig kwargs, got {type(entry).__name__}."
        )
    check_key(where, role.name)
    where = f"{where} ({role.name})"
    check_label(where, "label", role.label)
    if role.managed_by not in MANAGED_BY:
        raise ImproperlyConfigured(
            f"{where} has managed_by={role.managed_by!r}; must be one of {', '.join(MANAGED_BY)}."
        )
    return replace(role, kinds=party_kinds(where, "kinds", role.kinds))


def build_party_roles() -> dict[str, PartyRoleConfig]:
    """Built-ins plus ``settings.GOLDEN_PARTY_ROLES``, validated, keyed by name."""
    configured = configured_list(
        "GOLDEN_PARTY_ROLES", getattr(settings, "GOLDEN_PARTY_ROLES", [])
    )
    roles = {role.name: role for role in BUILTIN_PARTY_ROLES}
    for i, entry in enumerate(configured):
        role = _role_from_config(entry, i)
        if role.name in BUILTIN_PARTY_ROLE_NAMES:
            raise ImproperlyConfigured(
                f"GOLDEN_PARTY_ROLES[{i}] redefines the built-in party role {role.name!r}; "
                f"built-ins ({', '.join(sorted(BUILTIN_PARTY_ROLE_NAMES))}) are fixed. "
                "Declare a new role instead."
            )
        if role.name in roles:
            raise ImproperlyConfigured(f"GOLDEN_PARTY_ROLES reuses the name {role.name!r}.")
        roles[role.name] = role
    return roles


_ROLES: dict[str, PartyRoleConfig] = build_party_roles()
# Read-only view; the backing dict is replaced in place on reload, so
# `from parties.role_registry import PARTY_ROLES` stays current.
PARTY_ROLES = MappingProxyType(_ROLES)


@receiver(setting_changed)
def _reload_registry(sender, setting, **kwargs) -> None:
    if setting != "GOLDEN_PARTY_ROLES":
        return
    rebuilt = build_party_roles()
    _ROLES.clear()
    _ROLES.update(rebuilt)


def get_party_role(name: str) -> PartyRoleConfig:
    """The registered role, or ``KeyError`` naming the unknown key."""
    try:
        return PARTY_ROLES[name]
    except KeyError:
        raise KeyError(f"unknown party role {name!r}") from None


def roles_for_kind(kind: str) -> tuple[PartyRoleConfig, ...]:
    """Roles a party of ``kind`` may hold, in registry order."""
    return tuple(role for role in PARTY_ROLES.values() if kind in role.kinds)


def staff_writable_role_names() -> tuple[str, ...]:
    """Roles the API and UI may open or close (not ``managed_by="system"``)."""
    return tuple(name for name, role in PARTY_ROLES.items() if role.staff_writable)


__all__ = [
    "BUILTIN_PARTY_ROLES",
    "BUILTIN_PARTY_ROLE_NAMES",
    "CLIENT_ROLE",
    "CUSTOMER_ROLE",
    "LEAD_ROLE",
    "MANAGED_BY",
    "PARTNER_ROLE",
    "PARTY_ROLES",
    "PartyRoleConfig",
    "REFERRAL_SOURCE_ROLE",
    "STAFF_ROLE",
    "SUPPLIER_ROLE",
    "build_party_roles",
    "get_party_role",
    "roles_for_kind",
    "staff_writable_role_names",
]
