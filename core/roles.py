"""Roles at runtime: Groups, ``is_staff`` derivation, adoption (D3, §2.3, §3.2).

The registry (``core.role_registry``) says which roles exist; this module
makes the database agree:

- :func:`ensure_roles` creates one ``Group`` per role and adds the role's
  declared permissions. It is idempotent and runs on ``post_migrate`` and from
  ``manage.py sync_roles``. Declared permissions are a floor: permissions an
  admin added to a role's group by hand are left alone.
- ``is_staff`` is **derived**: a non-superuser is staff exactly when it holds
  a ``staff=True`` role. :func:`sync_user_flags` is the only writer, called
  from the ``m2m_changed`` receiver on ``User.groups`` (either side of the
  relation) and from ``sync_roles``. Superusers are never touched: they are
  the break-glass flag, not a role.
- ``is_staff`` is also re-derived when an existing user is saved
  (``post_save``) and when a group is deleted, so a stale in-memory copy, a
  removed superuser flag or a hand-set checkbox cannot outlive the roles.
- First deploy: when ``ensure_roles`` *creates* the ``staff`` group (it did
  not exist before), it first adopts every current ``is_staff`` non-superuser
  into it, so turning derivation on demotes nobody. ``sync_roles
  --adopt-existing`` does the same on demand (e.g. a deployment that already
  had a group called ``staff``). Deleting the ``staff`` group demotes its
  members first, so a later re-creation adopts nobody.
"""

from __future__ import annotations

import logging

from django.contrib.auth.models import Group, Permission, User
from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS
from django.db.models import Q
from django.db.models.signals import m2m_changed, post_delete, post_init, post_save, pre_delete
from django.dispatch import receiver

from .role_registry import ROLES, STAFF_ROLE, staff_role_names

logger = logging.getLogger(__name__)


def _permission(perm: str):
    app_label, codename = perm.split(".", 1)
    return Permission.objects.filter(
        content_type__app_label=app_label, codename=codename
    ).first()


def ensure_role(name: str) -> tuple[Group, bool, list[str]]:
    """Get or create one role's Group. The ONLY way golden creates a role group.

    When this call creates the ``staff`` group, every existing ``is_staff``
    non-superuser is adopted into it first, so whichever code path happens to
    create the group (``migrate``, ``sync_roles``, ``grant_role``,
    ``setup_demo_user``, a test helper) never strands current staff.
    Returns ``(group, created, adopted_usernames)``.
    """
    if name not in ROLES:
        raise ValueError(f"Unknown role {name!r} (not in GOLDEN_ROLES or the built-ins).")
    group, created = Group.objects.get_or_create(name=name)
    adopted = []
    if created and name == STAFF_ROLE:
        adopted = _adopt_into(group)
        if adopted:
            logger.warning(
                "roles: created the %r group and adopted %d existing staff user(s) into it",
                STAFF_ROLE, len(adopted),
            )
    return group, created, adopted


def preexisting_lookalikes():
    """``{role: [username, …]}`` of staff-role group members WITHOUT ``is_staff``.

    Golden never leaves a member of a staff role unflagged (the m2m receiver
    promotes on add), so these are members of a group that existed before
    golden claimed its name (or rows written around the ORM). Treating them as
    staff would be a silent privilege escalation.
    """
    found = {}
    for name in staff_role_names():
        users = list(User.objects.filter(
            groups__name=name, is_staff=False, is_superuser=False
        ).values_list("username", flat=True))
        if users:
            found[name] = users
    return found


def _accept_preexisting_default() -> bool:
    from django.conf import settings

    return bool(getattr(settings, "GOLDEN_ROLES_ADOPT_PREEXISTING", False))


def ensure_roles(*, accept_preexisting: bool | None = None) -> dict:
    """Create/repair every role's Group and its declared permissions.

    Refuses (``ImproperlyConfigured``) when a staff-role group already holds
    members without ``is_staff`` (:func:`preexisting_lookalikes`), unless
    ``accept_preexisting`` (default: ``settings.GOLDEN_ROLES_ADOPT_PREEXISTING``)
    is true; then those members are promoted and current ``is_staff`` users
    are adopted into ``staff``, i.e. the existing group is taken over.

    Returns a report: ``created`` (group names), ``missing_permissions``
    (``{role: [perm, …]}`` for rows that do not exist yet; a later migrate
    or a system check picks them up), ``adopted`` (users added to ``staff``)
    and ``promoted`` (pre-existing members accepted as staff).
    """
    if accept_preexisting is None:
        accept_preexisting = _accept_preexisting_default()
    lookalikes = preexisting_lookalikes()
    if lookalikes and not accept_preexisting:
        listing = "; ".join(f"{role}: {', '.join(users)}" for role, users in lookalikes.items())
        raise ImproperlyConfigured(
            "Role group(s) already contain members without is_staff, so golden would "
            f"promote them to back-office staff ({listing}). If these groups really mean "
            "back-office staff, set GOLDEN_ROLES_ADOPT_PREEXISTING = True (or run "
            "`manage.py sync_roles --accept-preexisting`); otherwise rename the groups "
            "or remove those members, then migrate again. See docs/roles.md."
        )
    created, missing, adopted = [], {}, []
    for role in ROLES.values():
        group, was_created, newly_adopted = ensure_role(role.name)
        adopted += newly_adopted
        if was_created:
            created.append(role.name)
        for perm in role.permissions:
            permission = _permission(perm)
            if permission is None:
                missing.setdefault(role.name, []).append(perm)
            else:
                group.permissions.add(permission)
    promoted = []
    if lookalikes:
        member_ids = User.objects.filter(
            username__in=[u for users in lookalikes.values() for u in users]
        ).values_list("pk", flat=True)
        promoted = sync_user_flags(list(member_ids))["promoted"]
        adopted += adopt_existing_staff()
        logger.warning("roles: accepted pre-existing role group members as staff: %s", promoted)
    return {"created": created, "missing_permissions": missing, "adopted": adopted,
            "promoted": promoted}


def _staff_role_members():
    return User.objects.filter(groups__name__in=staff_role_names()).values("pk")


def unadopted_staff():
    """Non-superusers with ``is_staff`` but no staff role: derivation would demote them."""
    return User.objects.filter(is_staff=True, is_superuser=False).exclude(
        pk__in=_staff_role_members()
    )


def unflagged_staff():
    """Non-superusers holding a staff role without ``is_staff``: derivation would promote them."""
    return User.objects.filter(is_staff=False, is_superuser=False).filter(
        pk__in=_staff_role_members()
    )


def _adopt_into(group) -> list[str]:
    users = list(unadopted_staff())
    if users:
        group.user_set.add(*users)
    return [user.username for user in users]


def adopt_existing_staff() -> list[str]:
    """Add every ``is_staff`` non-superuser lacking a staff role to ``staff``.

    Returns the adopted usernames. Superusers are not added: they pass every
    gate already (spec §3.2).
    """
    group, _created, adopted = ensure_role(STAFF_ROLE)
    return adopted + _adopt_into(group)


def grant_role(user, role_name: str) -> None:
    """Add ``user`` to a registered role (creating its group if needed).

    ``is_staff`` follows through the m2m receiver; the in-memory ``user``
    is refreshed with it.
    """
    group, _created, _adopted = ensure_role(role_name)
    user.groups.add(group)


def revoke_role(user, role_name: str) -> None:
    """Remove ``user`` from a role's group (a no-op if absent).

    ``is_staff`` follows through the m2m receiver, as for :func:`grant_role`.
    """
    group = Group.objects.filter(name=role_name).first()
    if group is not None:
        user.groups.remove(group)


def has_staff_role(user) -> bool:
    """True when ``user`` belongs to any ``staff=True`` role's group."""
    if getattr(user, "pk", None) is None or not hasattr(user, "groups"):
        return False
    return user.groups.filter(name__in=staff_role_names()).exists()


def sync_user_flags(user_ids=None) -> dict:
    """Make ``is_staff`` match staff-role membership. The only ``is_staff`` writer.

    ``user_ids=None`` syncs everyone. Superusers are skipped. Uses
    ``QuerySet.update`` so no ``post_save`` fires; returns the usernames
    ``promoted`` and ``demoted``.
    """
    scope = User.objects.filter(is_superuser=False)
    if user_ids is not None:
        scope = scope.filter(pk__in=list(user_ids))
    members = _staff_role_members()
    promote = scope.filter(Q(is_staff=False) & Q(pk__in=members))
    demote = scope.filter(Q(is_staff=True) & ~Q(pk__in=members))
    promoted = dict(promote.values_list("pk", "username"))
    demoted = dict(demote.values_list("pk", "username"))
    if promoted:
        User.objects.filter(pk__in=promoted).update(is_staff=True)
    if demoted:
        User.objects.filter(pk__in=demoted).update(is_staff=False)
        logger.warning(
            "roles: is_staff cleared for %s (no staff role)", ", ".join(demoted.values())
        )
    return {"promoted": list(promoted.values()), "demoted": list(demoted.values())}


_PRE_CLEAR_ATTR = "_golden_roles_pre_clear_user_ids"


@receiver(m2m_changed, sender=User.groups.through)
def derive_is_staff_on_group_change(sender, instance, action, reverse, pk_set, **kwargs):
    """Re-derive ``is_staff`` for every user whose groups just changed."""
    if action not in ("pre_clear", "post_add", "post_remove", "post_clear"):
        return
    if not reverse:
        # user.groups.add/remove/clear/set: the instance is the User.
        if action == "pre_clear":
            return
        sync_user_flags([instance.pk])
        # Keep the caller's in-memory object truthful (update() bypassed it).
        instance.is_staff = User.objects.filter(pk=instance.pk).values_list(
            "is_staff", flat=True
        ).first() or False
        setattr(instance, _LOADED_ATTR, instance.is_staff)
        return
    # group.user_set.add/remove/clear: the instance is the Group.
    if action == "pre_clear":
        setattr(instance, _PRE_CLEAR_ATTR, list(instance.user_set.values_list("pk", flat=True)))
        return
    if action == "post_clear":
        user_ids = getattr(instance, _PRE_CLEAR_ATTR, [])
        if hasattr(instance, _PRE_CLEAR_ATTR):
            delattr(instance, _PRE_CLEAR_ATTR)
    else:
        user_ids = pk_set or []
    if user_ids:
        sync_user_flags(user_ids)


_LOADED_ATTR = "_golden_roles_loaded_is_staff"


@receiver(post_init, sender=User)
def _remember_loaded_is_staff(sender, instance, **kwargs):
    # What this in-memory copy said when it was built. Read from __dict__ so a
    # deferred field is never fetched here.
    setattr(instance, _LOADED_ATTR, instance.__dict__.get("is_staff"))


def _patch_refresh_from_db() -> None:
    """Keep the loaded-flag record true across ``refresh_from_db``.

    Django refreshes by building a new instance (``post_init`` fires on that
    one) and copying its values onto ``self``, so ``self``'s record would stay
    stale: a copy that loaded False, refreshed to True after a grant and saved
    after a revoke would look like it "turned the flag on" and re-grant staff.
    """
    original = User.refresh_from_db
    if getattr(original, "_golden_roles_patched", False):
        return

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        original(self, using=using, fields=fields, from_queryset=from_queryset)
        if fields is None or "is_staff" in fields:
            setattr(self, _LOADED_ATTR, self.__dict__.get("is_staff"))

    refresh_from_db._golden_roles_patched = True
    User.refresh_from_db = refresh_from_db


_patch_refresh_from_db()


def _revoke_staff_roles(user) -> None:
    groups = Group.objects.filter(name__in=staff_role_names())
    user.groups.remove(*groups)


@receiver(post_save, sender=User)
def derive_is_staff_on_user_save(sender, instance, created, raw=False, **kwargs):
    """Keep ``is_staff`` and staff roles in step on every save of a non-superuser.

    - Created with ``is_staff=True`` (``create_user(..., is_staff=True)``,
      ``createsuperuser`` aside) → **adopted** into the ``staff`` role, so the
      account is whole staff rather than half (flag without role).
    - This copy turned the flag on (``user.is_staff = True; user.save()``) →
      adopted the same way.
    - This copy turned the flag off while the user holds a staff role →
      the staff roles are **revoked** (an explicit removal wins).
    - Otherwise the flag is re-derived from the roles, with a warning when that
      reverts it: a stale copy saved after its role was removed, a superuser
      flag removed from an account that never held a role, a flag written
      around the ORM.

    Saves whose ``update_fields`` name neither flag (login's ``last_login``)
    are skipped. Superusers are never touched. A copy loaded with ``is_staff``
    deferred has no record of what it loaded, so it can never adopt by save
    (it is re-derived instead: fails closed).
    """
    if raw or instance.pk is None or instance.is_superuser:
        setattr(instance, _LOADED_ATTR, instance.__dict__.get("is_staff"))
        return
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and not {"is_staff", "is_superuser"} & set(update_fields):
        return
    loaded = getattr(instance, _LOADED_ATTR, None)
    flag = bool(instance.is_staff)
    turned_on = flag and (created or loaded is False)
    turned_off = (not flag) and loaded is True and not created
    if turned_on:
        if not has_staff_role(instance):
            grant_role(instance, STAFF_ROLE)  # m2m receiver keeps is_staff True
            logger.info("roles: %s was saved with is_staff; adopted into %r",
                        instance.username, STAFF_ROLE)
    elif turned_off:
        if has_staff_role(instance):
            _revoke_staff_roles(instance)  # m2m receiver keeps is_staff False
            logger.warning("roles: %s was saved without is_staff; staff roles revoked",
                           instance.username)
    elif not created:
        desired = has_staff_role(instance)
        if flag != desired:
            User.objects.filter(pk=instance.pk).update(is_staff=desired)
            instance.is_staff = desired
            logger.warning(
                "roles: is_staff for %s reverted to %s on save (staff role %s)",
                instance.username, desired, "held" if desired else "not held",
            )
    setattr(instance, _LOADED_ATTR, instance.is_staff)


_PRE_DELETE_ATTR = "_golden_roles_pre_delete_user_ids"


@receiver(pre_delete, sender=Group)
def _remember_members_before_group_delete(sender, instance, **kwargs):
    # Deleting a Group drops its membership rows without m2m_changed.
    setattr(instance, _PRE_DELETE_ATTR, list(instance.user_set.values_list("pk", flat=True)))


@receiver(post_delete, sender=Group)
def derive_is_staff_after_group_delete(sender, instance, **kwargs):
    user_ids = getattr(instance, _PRE_DELETE_ATTR, None)
    if user_ids:
        sync_user_flags(user_ids)


def ensure_roles_after_migrate(sender, app_config=None, verbosity=1, using=DEFAULT_DB_ALIAS,
                               **kwargs):
    """``post_migrate`` receiver, run for every app's emission.

    Idempotent and cheap, so it simply runs each time: Django emits
    ``post_migrate`` only for apps that have a models module, so "the last
    app" is not a reliable trigger (a model-less plugin app appended last
    would silence it), and permission rows for later apps appear on later
    emissions and are picked up then.
    """
    if using != DEFAULT_DB_ALIAS:
        return
    report = ensure_roles()
    stranded = list(unadopted_staff().values_list("username", flat=True)[:20])
    if stranded and app_config is not None and app_config.label == "core":
        # The staff group already existed, so nobody was adopted; these
        # accounts are refused by /erp/ and the staff API until adopted.
        print(  # noqa: T201
            f"  Roles: WARNING {len(stranded)} is_staff user(s) hold no staff role and "
            f"are locked out until `manage.py sync_roles --adopt-existing`: {stranded}"
        )
    if verbosity >= 1 and (report["created"] or report["adopted"] or report["promoted"]):
        print(  # noqa: T201 — migrate output, like Django's own post_migrate prints
            f"  Roles: created {report['created'] or 'none'}; "
            f"adopted {len(report['adopted'])} existing staff user(s) into {STAFF_ROLE!r}."
        )
