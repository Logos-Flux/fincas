"""Archived owners (docs/crm/phase4-fields.md 2.5, F6).

An archived owner is an ``auth.User`` with ``is_active=False``, an unusable
password and no roles, plus a ``UserProfile`` (company, frozen
``display_name``, ``team``). It keeps rendering as a name, filtering as an
owner and resolving by email on the records it owned. No new model: one
``auth.User`` per human still holds, so a returning employee is reactivated,
never duplicated.

:func:`archive_profile` is the one writer. It **never deletes**: the user,
the profile, the team and every record the person owns stay. Open
engagements and tasks keep their owner and show up under the "Owner
archived" attention rule until someone reassigns them.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

AUDIT_ACTION = "archive"


def archived_q(path: str = "") -> Q:
    """Profiles that can no longer act: an inactive user, or no user at all.

    ``path`` is the FK to the profile (``"owner"``, ``"assignee"``): rows
    whose profile is NULL never match. Empty: a ``UserProfile`` queryset.
    """
    if not path:
        return Q(user__isnull=True) | Q(user__is_active=False)
    return Q(**{f"{path}__isnull": False}) & (
        Q(**{f"{path}__user__isnull": True}) | Q(**{f"{path}__user__is_active": False})
    )


def is_archived(profile) -> bool:
    """True for a profile with no user or an inactive one."""
    if profile is None:
        return False
    user = profile.user if profile.user_id is not None else None
    return user is None or not user.is_active


ARCHIVED_SUFFIX = " (archived)"


def profile_display(profile, empty: str = "Unassigned") -> str:
    """A profile as staff read it: its display name, else its login, plus
    "(archived)" for an archived one (2.5: the suffix is text, not colour).
    Never reaches through a missing ``user``."""
    if profile is None:
        return empty
    user = profile.user if profile.user_id is not None else None
    name = profile.display_name or (user.get_username() if user is not None else "Unnamed profile")
    return name + ARCHIVED_SUFFIX if is_archived(profile) else name


def picker_profiles(company_id, *, keep=()):
    """Profiles an owner or assignee picker offers (2.5): the tenant's active
    people, plus the ids in ``keep`` (the value a record already holds, so
    editing a record owned by an archived person does not fail validation)."""
    from core.models import UserProfile

    keep = [pk for pk in keep if pk]
    qs = UserProfile.objects.filter(company_id=company_id).select_related("user")
    active = Q(user__isnull=False, user__is_active=True)
    qs = qs.filter(active | Q(pk__in=keep)) if keep else qs.filter(active)
    return qs.order_by("display_name", "user__username")


def owner_filter_choices(company_id, *, limit: int = 200):
    """Choices for an owner filter: active people, then an "Archived" group
    (Django's optgroup shape: ``("Archived", [(id, label), ...])``)."""
    from core.models import UserProfile

    if company_id is None:
        return []
    qs = UserProfile.objects.filter(company_id=company_id).select_related("user") \
        .order_by("display_name", "user__username")
    active, archived = [], []
    for p in qs[:limit * 2]:  # one query; split in Python
        (archived if is_archived(p) else active).append((str(p.pk), profile_display(p)))
    active, archived = active[:limit], archived[:limit]
    return active + ([("Archived", archived)] if archived else [])


def filter_entry(field: str, label: str, choices) -> dict:
    """A list-filter dict whose ``choices`` may hold Django-style optgroups:
    ``choices`` comes back flat (the caption and saved-filter checks read it),
    ``options`` holds the ungrouped ones and ``groups`` the ``(label, [...])``
    groups, for templates to render as ``<optgroup>``."""
    options, groups = [], []
    for value, text in choices:
        if isinstance(text, (list, tuple)):
            groups.append((value, list(text)))
        else:
            options.append((value, text))
    flat = options + [pair for _label, pairs in groups for pair in pairs]
    return {"field": field, "label": label, "choices": flat, "options": options, "groups": groups}


def frozen_display_name(profile) -> str:
    """The name an archived profile keeps: its display name, else the user's
    full name, else the username."""
    if profile.display_name:
        return profile.display_name
    user = profile.user if profile.user_id is not None else None
    if user is None:
        return str(profile)
    return (user.get_full_name() or "").strip() or user.get_username()


def archive_profile(profile, by=None):
    """Archive ``profile``'s person; returns the profile. Never deletes.

    Freezes ``display_name`` (filled from the user's full name, else the
    username, when blank), deactivates the user, makes its password
    unusable, removes every role (``GOLDEN_ROLES`` group) it holds, and
    writes one ``AuditLog`` row (``action="archive"``). ``by`` is the acting
    ``UserProfile`` (or None for a system caller).

    Refused (``ValidationError``): a service account, a superuser,
    archiving yourself, and an actor of another company. A profile with
    nothing left to do (inactive, no roles, unusable password, a name) is a
    no-op with no second audit row; a user merely deactivated elsewhere
    (roles and password intact) is archived properly.
    """
    from django.contrib.auth import get_user_model

    from core.models import AuditLog, UserProfile
    from core.role_registry import ROLES
    from core.service_accounts import is_service_account

    with transaction.atomic():
        profile = UserProfile.objects.select_for_update().get(pk=profile.pk)
        user = None
        if profile.user_id is not None:
            # Lock the login too: a concurrent role grant must not slip in.
            user = get_user_model()._default_manager.select_for_update().get(pk=profile.user_id)
        if by is not None and getattr(by, "pk", None) == profile.pk:
            raise ValidationError("You cannot archive your own profile.")
        if by is not None and by.company_id and profile.company_id and by.company_id != profile.company_id:
            raise ValidationError("The profile belongs to another company.")
        if user is not None and user.is_superuser:
            raise ValidationError("A superuser cannot be archived; remove superuser status first.")
        if user is not None and is_service_account(user):
            raise ValidationError("A service account is not a person; revoke its keys instead.")
        held = sorted(user.groups.filter(name__in=list(ROLES)).values_list("name", flat=True)) if user else []
        done = profile.display_name and (
            user is None or (not user.is_active and not held and not user.has_usable_password())
        )
        if done:  # already archived, nothing left to do: no second audit row
            return profile
        changes = {}
        name = frozen_display_name(profile)
        if name != profile.display_name:
            changes["display_name"] = [profile.display_name, name]
            profile.display_name = name
            profile.save(update_fields=["display_name", "updated_at"])
        if user is not None:
            if held:
                user.groups.remove(*user.groups.filter(name__in=held))
                changes["roles_removed"] = held
            if user.is_active:
                changes["is_active"] = [True, False]
            user.is_active = False
            user.set_unusable_password()
            user.save(update_fields=["is_active", "password"])
        AuditLog.objects.create(
            user=by if getattr(by, "pk", None) else None, action=AUDIT_ACTION,
            model_name="core.UserProfile", object_id=profile.pk, changes=changes,
        )
    return profile
