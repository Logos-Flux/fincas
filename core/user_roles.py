"""The Users & roles screen's model-facing half (``/erp/users/<pk>/roles/``).

Roles are the registry's human roles (``core.role_registry.ROLES``); the
screen grants and revokes through ``core.roles.grant_role`` /
``revoke_role`` only, so ``is_staff`` follows the rules in docs/roles.md
(a staff role grants it, losing the last one revokes it). Service roles are
never offered to a person, and a superuser's flag is never touched here: it
is the break-glass flag, not a role.

Who may change roles: ``auth.change_user`` (Django's own permission; a
superuser holds every permission). Any staff user may read the list.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.contrib.auth.models import User
from django.db import transaction

from .role_registry import ROLES, RoleConfig
from .roles import grant_role, revoke_role

MANAGE_PERMISSION = "auth.change_user"


@dataclass(frozen=True)
class RoleRow:
    role: RoleConfig
    held: bool


def human_roles() -> list[RoleConfig]:
    """Roles a person can hold, built-ins first, then the deployment's in order."""
    return [role for role in ROLES.values() if role.kind == "human"]


def grantable_names() -> list[str]:
    return [role.name for role in human_roles()]


def role_rows(user: User) -> list[RoleRow]:
    held = set(user.groups.values_list("name", flat=True))
    return [RoleRow(role=role, held=role.name in held) for role in human_roles()]


def role_labels(user: User) -> list[str]:
    """Display labels of the human roles ``user`` holds (prefetch ``groups``)."""
    names = {g.name for g in user.groups.all()}
    return [role.label for role in human_roles() if role.name in names]


def can_manage(actor) -> bool:
    return bool(actor and actor.is_authenticated and actor.has_perm(MANAGE_PERMISSION))


def set_roles(user: User, wanted: set[str] | list[str]) -> tuple[list[str], list[str]]:
    """Make ``user`` hold exactly the human roles in ``wanted``.

    Unknown or service role names raise ``ValueError`` before anything is
    written. Returns ``(granted, revoked)`` role names. Every change goes
    through ``grant_role`` / ``revoke_role``, so the ``m2m_changed``
    receiver derives ``is_staff``.
    """
    wanted = set(wanted)
    allowed = set(grantable_names())
    unknown = sorted(wanted - allowed)
    if unknown:
        raise ValueError(f"Not a role a person can hold: {', '.join(unknown)}.")
    held = set(user.groups.values_list("name", flat=True)) & allowed
    granted = sorted(wanted - held)
    revoked = sorted(held - wanted)
    with transaction.atomic():
        for name in granted:
            grant_role(user, name)
        for name in revoked:
            revoke_role(user, name)
    return granted, revoked
