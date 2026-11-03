"""Role registry — ``settings.GOLDEN_ROLES`` (identity-unification D3, §2.3).

Same loader shape as ``core/theme_registry.py``: a frozen dataclass per
entry, ``ImproperlyConfigured`` at import on any bad entry, and a rebuild on
``setting_changed`` so tests can override the setting.

A role is a Django ``Group`` with a declared meaning. Golden ships three
built-ins that are always present and cannot be redefined by a deployment:

- ``staff``      (``staff=True``)  back-office access; ``is_staff`` is derived
  from membership of any ``staff=True`` role (``core.roles``).
- ``customer``   (``portal=True``) may use the customer portal API
  (``core.portal_permissions.IsPortalCustomer``); granted by a portal
  provider's ``roles_on_create`` (``core.identity.providers``).
- ``storefront`` (``kind="service"``) the storefront channel identity's
  group, carrying ``core.storefront.STOREFRONT_PERMISSIONS``.

A deployment *appends* roles in its settings overlay, e.g.::

    GOLDEN_ROLES = [
        {"name": "manager", "label": "Manager", "staff": True,
         "permissions": ["orders.change_quote"]},
        {"name": "vip-customer", "label": "VIP customer", "portal": True},
    ]

Role names are persisted as ``Group.name``: treat them like theme names and
never rename one in place. Whether a declared permission row exists is only
knowable after ``migrate``, so that is a system check (``core/checks.py``),
not import-time validation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.signals import setting_changed
from django.dispatch import receiver

KINDS = ("human", "service")
NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]{0,149}\Z")  # \Z: "$" would accept a trailing newline  # Group.name is 150 chars
PERMISSION_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")  # app_label.codename

STAFF_ROLE = "staff"
CUSTOMER_ROLE = "customer"
STOREFRONT_ROLE = "storefront"  # == core.storefront.STOREFRONT_GROUP (tested)
BUILTIN_ROLE_NAMES = frozenset({STAFF_ROLE, CUSTOMER_ROLE, STOREFRONT_ROLE})


@dataclass(frozen=True)
class RoleConfig:
    name: str                 # Group.name; slug, persisted
    label: str                # human display text
    kind: str = "human"       # "human" | "service"
    staff: bool = False       # membership grants is_staff (back office)
    portal: bool = False      # may use the customer portal API
    permissions: tuple[str, ...] = ()  # "app_label.codename", added to the Group


def _builtin_roles() -> tuple[RoleConfig, ...]:
    # Imported here, not at module top: core.storefront pulls in
    # django.contrib.auth.models, which needs the app registry.
    from .storefront import STOREFRONT_GROUP, STOREFRONT_PERMISSIONS

    return (
        RoleConfig(STAFF_ROLE, "Staff", staff=True, permissions=_staff_permissions()),
        RoleConfig(CUSTOMER_ROLE, "Customer", portal=True),
        RoleConfig(
            STOREFRONT_GROUP,
            "Storefront service",
            kind="service",
            permissions=tuple(f"{app}.{code}" for app, code in STOREFRONT_PERMISSIONS),
        ),
    )


#: Permissions the built-in ``staff`` role carries. ``parties.export_pii``
#: (personal-data columns in CRM CSV exports, docs/crm/phase4-fields.md 5.5)
#: is on by default; a deployment revokes it with
#: ``GOLDEN_STAFF_EXPORT_PII = False`` and by removing it from the ``staff``
#: group (declared permissions are a floor, so an existing grant stays).
def _staff_permissions() -> tuple[str, ...]:
    if getattr(settings, "GOLDEN_STAFF_EXPORT_PII", True):
        return ("parties.export_pii",)
    return ()


def _role_from_config(entry: Any, index: int) -> RoleConfig:
    where = f"GOLDEN_ROLES[{index}]"
    if isinstance(entry, RoleConfig):
        role = entry
    elif isinstance(entry, dict):
        try:
            role = RoleConfig(**entry)
        except TypeError as exc:
            raise ImproperlyConfigured(f"{where} is not a valid RoleConfig: {exc}") from exc
    else:
        raise ImproperlyConfigured(
            f"{where} must be a dict of RoleConfig kwargs, got {type(entry).__name__}."
        )
    if not isinstance(role.name, str) or not NAME_RE.match(role.name):
        raise ImproperlyConfigured(
            f"{where} has name={role.name!r}; use a lower-case slug "
            "([a-z0-9][a-z0-9_-]*, at most 150 characters)."
        )
    if not isinstance(role.label, str) or not role.label.strip():
        raise ImproperlyConfigured(f"{where} ({role.name}) is missing a non-empty label.")
    if role.kind not in KINDS:
        raise ImproperlyConfigured(
            f"{where} ({role.name}) has kind={role.kind!r}; must be one of {', '.join(KINDS)}."
        )
    for flag in ("staff", "portal"):
        if not isinstance(getattr(role, flag), bool):
            raise ImproperlyConfigured(f"{where} ({role.name}) {flag} must be True or False.")
    if role.kind == "service" and (role.staff or role.portal):
        raise ImproperlyConfigured(
            f"{where} ({role.name}) is a service role; it cannot be staff or portal."
        )
    perms = role.permissions
    if isinstance(perms, str) or not isinstance(perms, (list, tuple)):
        raise ImproperlyConfigured(
            f"{where} ({role.name}) permissions must be a list of 'app_label.codename' strings."
        )
    for perm in perms:
        if not isinstance(perm, str) or not PERMISSION_RE.match(perm):
            raise ImproperlyConfigured(
                f"{where} ({role.name}) has permission {perm!r}; expected 'app_label.codename'."
            )
    if len(set(perms)) != len(perms):
        raise ImproperlyConfigured(f"{where} ({role.name}) lists a permission twice.")
    return replace(role, permissions=tuple(perms))


def build_roles() -> dict[str, RoleConfig]:
    """Built-ins plus ``settings.GOLDEN_ROLES``, validated, keyed by name."""
    configured = getattr(settings, "GOLDEN_ROLES", []) or []
    if not isinstance(configured, (list, tuple)):
        raise ImproperlyConfigured(
            f"GOLDEN_ROLES must be a list of RoleConfig kwargs dicts, got {type(configured).__name__}."
        )
    roles: dict[str, RoleConfig] = {role.name: role for role in _builtin_roles()}
    builtin = set(roles)
    for i, entry in enumerate(configured):
        role = _role_from_config(entry, i)
        if role.name in builtin:
            raise ImproperlyConfigured(
                f"GOLDEN_ROLES[{i}] redefines the built-in role {role.name!r}; built-ins "
                f"({', '.join(sorted(builtin))}) are fixed. Declare a new role instead."
            )
        if role.name in roles:
            raise ImproperlyConfigured(f"GOLDEN_ROLES reuses the name {role.name!r}.")
        roles[role.name] = role
    return roles


# Mutated in place on reload so that `from … import ROLES` holds.
ROLES: dict[str, RoleConfig] = build_roles()


@receiver(setting_changed)
def _reload_registry(sender, setting, **kwargs) -> None:
    if setting not in ("GOLDEN_ROLES", "GOLDEN_STAFF_EXPORT_PII"):
        return
    rebuilt = build_roles()
    ROLES.clear()
    ROLES.update(rebuilt)


def staff_role_names() -> tuple[str, ...]:
    """Names of every role whose membership grants back-office access."""
    return tuple(name for name, role in ROLES.items() if role.staff)


def portal_role_names() -> tuple[str, ...]:
    return tuple(name for name, role in ROLES.items() if role.portal)
