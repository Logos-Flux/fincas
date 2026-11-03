"""Owner-team stamping (docs/crm/phase4-fields.md 2.1).

``Party.owner_team`` and ``Engagement.owner_team`` are **stamped** from the
owner's ``UserProfile.team`` whenever the owner changes, on every path that
saves the row (forms, the API, ``crm.engagements.reassign``, imports through
the services). They are *not* recomputed when the owner later moves team:
"deals won by team X last quarter" must not change when a rep transfers.

The stamp lives in ``save()`` (:class:`OwnerTeamStamped`), so no caller can
forget it. A write that changes the owner *and* names a different
``owner_team`` keeps the named team: that is the direct edit staff with
``crm.reassign_engagement`` may make (the API and forms check the
permission; the model does not). Clearing the owner clears the stamp.

Writes around ``save()`` (``bulk_create``, ``update()``) stamp through
:func:`backfill_owner_teams`, a plain function over any model with
``owner`` / ``owner_team`` / ``company``, historical or live.
"""

from __future__ import annotations

from django import forms
from django.db.models import F, OuterRef, Q, Subquery

_UNLOADED = object()


def team_of(owner, company_id):
    """The team id ``owner`` stamps onto a row of ``company_id``: its
    profile's team, or None (no owner, no team, an owner or a team of another
    company, or an archived team: it stays on old records, never new ones)."""
    if owner is None or owner.team_id is None or owner.company_id != company_id:
        return None
    team = owner.team
    if team.company_id != company_id or not team.is_active:
        return None
    return team.pk


class OwnerTeamStamped:
    """Mixin for a model with ``company``, ``owner`` (UserProfile) and ``owner_team``."""

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._snapshot_owner()
        return instance

    def refresh_from_db(self, using=None, fields=None, from_queryset=None):
        super().refresh_from_db(using=using, fields=fields, from_queryset=from_queryset)
        self._snapshot_owner(fields)

    def _snapshot_owner(self, fields=None):
        loaded = getattr(self, "_loaded_owner", {})
        for attname in ("owner_id", "owner_team_id"):
            if fields is None or attname in fields or attname.removesuffix("_id") in fields:
                loaded[attname] = self.__dict__.get(attname, _UNLOADED)
        self._loaded_owner = loaded

    def _stamp_owner_team(self, kwargs) -> None:
        """Called by ``save()`` before writing; extends ``update_fields``."""
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and not {"owner", "owner_id"} & set(update_fields):
            return
        adding = self._state.adding or bool(kwargs.get("force_insert"))
        loaded = getattr(self, "_loaded_owner", {})
        if adding:
            owner_changed = self.owner_id is not None
            team_named = self.owner_team_id is not None
        else:
            if "owner_id" not in self.__dict__:
                return  # deferred and never assigned: not part of this write
            was = loaded.get("owner_id", _UNLOADED)
            owner_changed = was is _UNLOADED or self.owner_id != was
            was_team = loaded.get("owner_team_id", _UNLOADED)
            # Deferred at load but assigned since: the caller named it.
            team_named = (
                "owner_team_id" in self.__dict__
                and (was_team is _UNLOADED or self.owner_team_id != was_team)
                and (update_fields is None or {"owner_team", "owner_team_id"} & set(update_fields))
            )
        if not owner_changed or team_named:
            return
        self.owner_team_id = team_of(self.owner if self.owner_id else None, self.company_id)
        if update_fields is not None:
            kwargs["update_fields"] = {*update_fields, "owner_team"}

    def _after_owner_save(self, kwargs) -> None:
        # Re-snapshot only what was written, like Engagement's guarded fields.
        update_fields = kwargs.get("update_fields")
        self._snapshot_owner(None if update_fields is None else set(update_fields))


def backfill_owner_teams(model, profile_model, *, ids=None) -> int:
    """Stamp ``owner_team`` on rows written around ``save()``; the row count.

    Only rows with an owner and no team yet, whose owner and the owner's
    (active) team belong to the row's company. Idempotent: a second run finds nothing to do.
    Plain function: ``model`` / ``profile_model`` may be historical.
    """
    qs = model._base_manager.filter(
        owner__isnull=False, owner_team__isnull=True,
        owner__team__isnull=False, owner__team__is_active=True,
        owner__company=F("company"), owner__team__company=F("company"),
    )
    if ids is not None:
        qs = qs.filter(pk__in=list(ids))
    team = profile_model._base_manager.filter(pk=OuterRef("owner_id")).values("team_id")[:1]
    return qs.update(owner_team=Subquery(team))


# --- pickers (phase4-fields.md 2.7: staff with crm.reassign_engagement edit it) ----


def team_choices(company_id, *, keep=None):
    """Active teams of a tenant, plus ``keep`` (the team a record already
    holds, which may since have been archived)."""
    from core.models import Team

    qs = Team.objects.filter(company_id=company_id)
    qs = qs.filter(Q(is_active=True) | Q(pk=keep)) if keep else qs.filter(is_active=True)
    return qs.order_by("sort_order", "name")


def team_display(team) -> str:
    if team is None:
        return ""
    return team.name if team.is_active else f"{team.name} (archived)"


class TeamChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return team_display(obj)
