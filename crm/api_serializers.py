"""Staff REST API serializers for CRM Phase 3 (docs/crm/phase3-engagement.md 5.2).

Rules every serializer here follows (the Phase 2 ``parties.serializers`` rules):

- ``company`` is read-only. A tenant user's rows take the requester's
  company; a superuser or service key names it (``crm.api.service_company``).
- Every reference (party, owner, assignee, engagement, stage, pipeline) is
  scoped to the requester's tenant, so another tenant's id is a 400 "does not
  exist", never a cross-tenant link. A superuser is cross-tenant by
  definition; the services and ``clean()`` refuse a link across tenants.
- Stage and status changes go through ``crm.engagements`` (the actions on
  ``EngagementViewSet``); task status through ``crm.tasks``. A PATCH naming
  ``stage`` / ``status`` / ``pipeline`` on an engagement, or ``status`` on a
  task, is a 400 with code ``use_action``.
- A model ``ValidationError`` becomes DRF's 400, never a 500.
- ``metadata`` writes merge and only ``custom.*`` keys in them are written;
  engagements and tasks take defined sidecar fields as ``custom_fields``
  (``core.sidecar_api``, phase4-fields.md 3.3).
"""

from __future__ import annotations

import datetime

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.utils.dateparse import parse_date, parse_datetime
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers
from rest_framework.exceptions import APIException

from core.managers import scope_to_company
from core.models import UserProfile
from core.serializers import ScopedTeamField
from core.sidecar_api import CustomFieldsMixin, MetadataCompatMixin
from parties.models import Party

from .models import (
    Engagement,
    EngagementContact,
    EngagementStageChange,
    Pipeline,
    Stage,
    Task,
    Touchpoint,
)
from .registries import ENGAGEMENT_SOURCES, LOSS_REASONS, TASK_KINDS


def django_errors(exc: DjangoValidationError) -> serializers.ValidationError:
    """A model ``ValidationError`` as DRF's 400 payload."""
    if hasattr(exc, "message_dict"):
        return serializers.ValidationError(exc.message_dict)
    return serializers.ValidationError({"non_field_errors": exc.messages})


class UseAction(APIException):
    """400 ``use_action``: a field only an action (or a service) may change."""

    status_code = 400
    default_code = "use_action"

    def __init__(self, fields, actions):
        super().__init__()
        self.detail = {
            "detail": f"{', '.join(fields)}: use {' / '.join(actions)}; a PATCH does not change it.",
            "code": self.default_code,
            "fields": list(fields),
            "actions": list(actions),
        }


def _request(serializer):
    return serializer.context.get("request")


@extend_schema_field(OpenApiTypes.UUID)
class _Scoped(serializers.PrimaryKeyRelatedField):
    """A row of the requester's tenant (a superuser sees every tenant)."""

    model = None

    def base_queryset(self):
        return self.model._base_manager.all()

    def get_queryset(self):
        request = self.context.get("request")
        if request is None:
            return self.model._base_manager.none()
        return scope_to_company(self.base_queryset(), request)


class ScopedParty(_Scoped):
    model = Party

    def base_queryset(self):
        return Party.objects.filter(merged_into__isnull=True, erased_at__isnull=True)


class ScopedProfile(_Scoped):
    model = UserProfile

    def base_queryset(self):
        return UserProfile.objects.all()


class ScopedPipeline(_Scoped):
    model = Pipeline


class ScopedStage(_Scoped):
    model = Stage

    def base_queryset(self):
        return Stage._base_manager.select_related("pipeline")


class ScopedEngagement(_Scoped):
    model = Engagement


class ScopedCommunication(_Scoped):
    @property
    def model(self):
        from communications.models import Communication

        return Communication


class SafeSaveMixin:
    """A constraint race or a model ``ValidationError`` is a 400, not a 500."""

    def get_unique_together_validators(self):
        # The conditional one-primary / one-default constraints are checked in
        # ``save`` (demoting the old row); DRF would ignore their condition.
        return []

    def save(self, **kwargs):
        try:
            with transaction.atomic():
                return super().save(**kwargs)
        except IntegrityError as exc:
            raise serializers.ValidationError(
                {"non_field_errors": [f"Conflicts with an existing row; retry. ({exc.__class__.__name__})"]}
            ) from exc
        except DjangoValidationError as exc:
            raise django_errors(exc) from exc


# --- pipelines and stages --------------------------------------------------------


class _WriteOnceKey:
    """``key`` is immutable once created (spec E2): a changed key is a 400."""

    def validate_key(self, value):
        if self.instance is not None and value != self.instance.key:
            raise serializers.ValidationError(f"key is write-once ({self.instance.key!r}).")
        return value


class PipelineSerializer(MetadataCompatMixin, _WriteOnceKey, SafeSaveMixin, serializers.ModelSerializer):
    class Meta:
        model = Pipeline
        fields = ["id", "company", "key", "name", "is_default", "is_active", "sort_order",
                  "stale_after_days", "metadata", "created_at", "updated_at"]
        read_only_fields = ["id", "company", "created_at", "updated_at"]

    def validate(self, attrs):
        company = attrs.get("company") or getattr(self.instance, "company", None) \
            or self.context.get("company")
        key = attrs.get("key")
        if self.instance is None and key and company is not None and \
                Pipeline._base_manager.filter(company=company, key=key).exists():
            raise serializers.ValidationError({"key": f"The company already has a pipeline {key!r}."})
        return attrs

    def save(self, **kwargs):
        with transaction.atomic():
            becomes_default = self.validated_data.get("is_default")
            company = kwargs.get("company") or getattr(self.instance, "company", None)
            if becomes_default and company is not None:
                # One default per company (uniq_pipeline_one_default): demote the old one.
                others = Pipeline._base_manager.select_for_update().filter(company=company, is_default=True)
                if self.instance is not None:
                    others = others.exclude(pk=self.instance.pk)
                others.update(is_default=False)
            return super().save(**kwargs)


class StageSerializer(_WriteOnceKey, SafeSaveMixin, serializers.ModelSerializer):
    pipeline = ScopedPipeline()

    class Meta:
        model = Stage
        fields = ["id", "company", "pipeline", "key", "name", "sort_order", "default_probability",
                  "outcome_hint", "stale_after_days", "is_active", "created_at", "updated_at"]
        read_only_fields = ["id", "company", "created_at", "updated_at"]

    def validate_pipeline(self, value):
        if self.instance is not None and value.pk != self.instance.pipeline_id:
            raise serializers.ValidationError("A stage does not move between pipelines.")
        return value

    def validate_default_probability(self, value):
        if value is not None and value > 100:
            raise serializers.ValidationError("0-100.")
        return value

    def validate(self, attrs):
        pipeline = attrs.get("pipeline") or getattr(self.instance, "pipeline", None)
        key = attrs.get("key")
        if self.instance is None and key and pipeline is not None and \
                Stage._base_manager.filter(pipeline=pipeline, key=key).exists():
            raise serializers.ValidationError({"key": f"The pipeline already has a stage {key!r}."})
        return attrs


# --- engagements -----------------------------------------------------------------

#: Changed only through an action (or not at all): a PATCH naming one is a 400.
ENGAGEMENT_ACTION_FIELDS = {
    "stage": ("move",), "pipeline": ("move",), "status": ("win", "lose", "reopen"),
    "closed_at": ("win", "lose", "reopen"), "loss_reason": ("lose",),
    "loss_competitor": ("lose",), "loss_notes": ("lose",),
}


class EngagementSerializer(CustomFieldsMixin, SafeSaveMixin, serializers.ModelSerializer):
    """An engagement. ``custom_fields`` holds the deployment's defined sidecar fields."""

    pipeline = ScopedPipeline(required=False)
    stage = ScopedStage(required=False)
    owner = ScopedProfile(required=False, allow_null=True)
    # Stamped from the owner's team when the owner changes (phase4-fields.md
    # 2.1); naming it directly needs crm.reassign_engagement (the viewset).
    owner_team = ScopedTeamField(required=False, allow_null=True)
    party = ScopedParty(required=False, allow_null=True)
    stage_key = serializers.CharField(source="stage.key", read_only=True, default=None)
    pipeline_key = serializers.CharField(source="pipeline.key", read_only=True)
    party_name = serializers.CharField(source="party.display_name", read_only=True, default=None)
    effective_probability = serializers.IntegerField(read_only=True)
    weighted_value = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    note = serializers.CharField(write_only=True, required=False, allow_blank=True, max_length=255)

    class Meta:
        model = Engagement
        fields = [
            "id", "company", "number", "title", "pipeline", "pipeline_key", "stage", "stage_key",
            "stage_entered_at", "status", "closed_at", "loss_reason", "loss_competitor",
            "loss_notes", "owner", "owner_team", "party", "party_name", "expected_value", "currency",
            "probability", "effective_probability", "weighted_value", "expected_close_date",
            "source", "first_touch", "last_touch", "next_task", "next_action_due_at",
            "last_activity_at", "origin", "metadata", "custom_fields", "tags", "note", "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id", "company", "number", "stage_entered_at", "status", "closed_at", "loss_reason",
            "loss_competitor", "loss_notes", "first_touch", "last_touch", "next_task",
            "next_action_due_at", "last_activity_at", "origin", "created_at", "updated_at",
        ]

    def validate_source(self, value):
        if value and value not in ENGAGEMENT_SOURCES:
            raise serializers.ValidationError(f"Unknown source {value!r} (GOLDEN_ENGAGEMENT_SOURCES).")
        return value

    def validate_probability(self, value):
        if value is not None and value > 100:
            raise serializers.ValidationError("0-100.")
        return value

    def to_internal_value(self, data):
        if self.instance is not None and hasattr(data, "keys"):
            named = sorted(set(data.keys()) & set(ENGAGEMENT_ACTION_FIELDS))
            if named:
                actions = []
                for name in named:
                    actions += [a for a in ENGAGEMENT_ACTION_FIELDS[name] if a not in actions]
                raise UseAction(named, actions)
        return super().to_internal_value(data)

    def create(self, validated_data):
        from . import engagements

        note = validated_data.pop("note", "")
        company = validated_data.pop("company")
        title = validated_data.pop("title", "")
        source = validated_data.pop("source", "")
        try:
            return engagements.create(
                company=company, by=self.context.get("profile"), source="api",
                origin=Engagement.Origin.API, title=title, engagement_source=source, note=note,
                **validated_data,
            )
        except DjangoValidationError as exc:
            raise django_errors(exc) from exc

    def update(self, instance, validated_data):
        validated_data.pop("note", None)
        for name, value in validated_data.items():
            setattr(instance, name, value)
        try:
            instance.full_clean(exclude=["number", "title"])
        except DjangoValidationError as exc:
            raise django_errors(exc) from exc
        instance.save()
        return instance


class MoveSerializer(serializers.Serializer):
    stage = ScopedStage()
    note = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")


class NoteSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")


class LoseSerializer(NoteSerializer):
    loss_reason = serializers.CharField(max_length=32)
    loss_competitor = serializers.CharField(required=False, allow_blank=True, max_length=255, default="")
    loss_notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_loss_reason(self, value):
        if value not in LOSS_REASONS:
            raise serializers.ValidationError(f"Unknown loss reason {value!r} (GOLDEN_LOSS_REASONS).")
        return value


class ReopenSerializer(NoteSerializer):
    stage = ScopedStage(required=False, allow_null=True, default=None)


class EngagementContactSerializer(SafeSaveMixin, serializers.ModelSerializer):
    engagement = ScopedEngagement()
    party = ScopedParty()
    party_name = serializers.CharField(source="party.display_name", read_only=True)

    class Meta:
        model = EngagementContact
        fields = ["id", "company", "engagement", "party", "party_name", "role", "is_primary",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "company", "created_at", "updated_at"]

    def validate_engagement(self, value):
        if self.instance is not None and value.pk != self.instance.engagement_id:
            raise serializers.ValidationError("A contact does not move between engagements.")
        return value

    def validate(self, attrs):
        engagement = attrs.get("engagement") or self.instance.engagement
        party = attrs.get("party") or self.instance.party
        row = EngagementContact(engagement=engagement, party=party)
        if self.instance is not None:
            row.pk = self.instance.pk
        try:
            row.clean()
        except DjangoValidationError as exc:
            raise django_errors(exc) from exc
        clash = EngagementContact._base_manager.filter(engagement=engagement, party=party)
        if self.instance is not None:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError({"party": "Already a contact on this engagement."})
        return attrs

    def save(self, **kwargs):
        with transaction.atomic():
            if self.validated_data.get("is_primary"):
                engagement = self.validated_data.get("engagement") or self.instance.engagement
                others = EngagementContact._base_manager.select_for_update().filter(
                    engagement=engagement, is_primary=True)
                if self.instance is not None:
                    others = others.exclude(pk=self.instance.pk)
                others.update(is_primary=False)
            return super().save(**kwargs)


class StageChangeSerializer(serializers.ModelSerializer):
    from_stage_key = serializers.CharField(source="from_stage.key", read_only=True, default=None)
    to_stage_key = serializers.CharField(source="to_stage.key", read_only=True, default=None)

    class Meta:
        model = EngagementStageChange
        fields = ["id", "company", "engagement", "from_pipeline", "to_pipeline", "from_stage",
                  "from_stage_key", "to_stage", "to_stage_key", "from_status", "to_status",
                  "changed_at", "changed_by", "source", "note", "created_at"]
        read_only_fields = fields


class TouchpointSerializer(serializers.ModelSerializer):
    class Meta:
        model = Touchpoint
        fields = ["id", "company", "party", "engagement", "occurred_at", "channel", "utm_source",
                  "utm_medium", "utm_campaign", "utm_term", "utm_content", "referrer_url",
                  "landing_url", "click_id_kind", "click_id", "source", "metadata", "created_at"]
        read_only_fields = fields


class TouchpointCreateSerializer(serializers.Serializer):
    """``POST touchpoints/``: one visit from a site or a tag manager (Phase 6 3.5).

    Name the person with ``party`` or ``email`` (exact match only, never a
    guess), or neither: the visit is stored party-less under ``visitor_id``
    and linked when a later post or lead names the visitor. ``event_id``
    makes a retry a no-op. A click id not given is read from ``landing_url``.
    """

    company = serializers.UUIDField(required=False)
    party = serializers.UUIDField(required=False)
    email = serializers.EmailField(required=False, allow_blank=True)
    visitor_id = serializers.CharField(max_length=255, required=False, allow_blank=True)
    event_id = serializers.CharField(max_length=255, required=False, allow_blank=True)
    occurred_at = serializers.DateTimeField(required=False)
    channel = serializers.CharField(max_length=32, required=False, allow_blank=True)
    utm_source = serializers.CharField(max_length=255, required=False, allow_blank=True)
    utm_medium = serializers.CharField(max_length=255, required=False, allow_blank=True)
    utm_campaign = serializers.CharField(max_length=255, required=False, allow_blank=True)
    utm_term = serializers.CharField(max_length=255, required=False, allow_blank=True)
    utm_content = serializers.CharField(max_length=255, required=False, allow_blank=True)
    referrer_url = serializers.CharField(max_length=2048, required=False, allow_blank=True)
    landing_url = serializers.CharField(max_length=2048, required=False, allow_blank=True)
    click_id_kind = serializers.CharField(max_length=16, required=False, allow_blank=True)
    click_id = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate(self, attrs):
        if attrs.get("party") and attrs.get("email"):
            raise serializers.ValidationError({"email": "Name the person with party or email, not both."})
        return attrs


# --- tasks -----------------------------------------------------------------------


@extend_schema_field({"type": "string", "description": "YYYY-MM-DD (all-day) or an ISO 8601 datetime"})
class DueField(serializers.Field):
    """A ``date`` (all-day: due at the end of that local day) or a ``datetime``
    (naive = wall time in ``due_tz``); null clears the due time."""

    def to_internal_value(self, data):
        if data in (None, ""):
            return None
        if not isinstance(data, str):
            raise serializers.ValidationError("A date (YYYY-MM-DD) or an ISO 8601 datetime.")
        if len(data) == 10:
            value = parse_date(data)
        else:
            try:
                value = parse_datetime(data)
            except ValueError:
                value = None
        if value is None:
            raise serializers.ValidationError("A date (YYYY-MM-DD) or an ISO 8601 datetime.")
        return value

    def to_representation(self, value):
        return value.isoformat() if isinstance(value, (datetime.date, datetime.datetime)) else value


class TaskSerializer(CustomFieldsMixin, SafeSaveMixin, serializers.ModelSerializer):
    """A task. ``custom_fields`` holds the deployment's defined sidecar fields."""

    assignee = ScopedProfile(required=False, allow_null=True)
    engagement = ScopedEngagement(required=False, allow_null=True)
    party = ScopedParty(required=False, allow_null=True)
    communication = ScopedCommunication(required=False, allow_null=True)
    due = DueField(write_only=True, required=False, allow_null=True)
    due_tz = serializers.CharField(required=False, allow_blank=True, max_length=64)
    is_overdue = serializers.BooleanField(read_only=True)

    class Meta:
        model = Task
        fields = [
            "id", "company", "title", "description", "kind", "status", "priority", "assignee",
            "created_by", "engagement", "party", "communication", "due", "due_at", "all_day",
            "due_tz", "is_overdue", "completed_at", "completed_by", "due_notified_at", "source",
            "metadata", "custom_fields", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "company", "status", "created_by", "due_at", "all_day", "completed_at",
            "completed_by", "due_notified_at", "source", "created_at", "updated_at",
        ]

    def validate_kind(self, value):
        if value not in TASK_KINDS:
            raise serializers.ValidationError(f"Unknown task kind {value!r} (GOLDEN_TASK_KINDS).")
        return value

    def validate_due_tz(self, value):
        from core.tz import is_valid_timezone

        if value and not is_valid_timezone(value):
            raise serializers.ValidationError(f"{value!r} is not an IANA time zone.")
        return value

    def to_internal_value(self, data):
        if self.instance is not None and hasattr(data, "keys"):
            named = sorted(set(data.keys()) & {"status", "completed_at", "due_at"})
            if named:
                raise UseAction(named, ["complete", "reopen", "cancel"] if "due_at" not in named
                                else ["due"])
        return super().to_internal_value(data)

    def _check_tenant(self, company, attrs):
        for name in ("assignee", "engagement", "party", "communication"):
            target = attrs.get(name)
            if target is not None and target.company_id != company.pk:
                raise serializers.ValidationError({name: f"The {name} belongs to another company."})

    def create(self, validated_data):
        from . import tasks

        company = validated_data.pop("company")
        engagement = validated_data.get("engagement")
        if engagement is not None:
            company = engagement.company
        self._check_tenant(company, validated_data)
        try:
            task = tasks.create(
                company=company, title=validated_data["title"], engagement=engagement,
                party=validated_data.get("party"), assignee=validated_data.get("assignee"),
                by=self.context.get("profile"), due=validated_data.get("due"),
                due_tz=validated_data.get("due_tz") or None,
                kind=validated_data.get("kind", "todo"),
                priority=validated_data.get("priority", Task.Priority.NORMAL),
                description=validated_data.get("description", ""), source="api",
                metadata=validated_data.get("metadata"),
            )
        except DjangoValidationError as exc:
            raise django_errors(exc) from exc
        if validated_data.get("communication") is not None:
            task.communication = validated_data["communication"]
            task.save(update_fields=["communication", "updated_at"])
        return task

    def update(self, instance, validated_data):
        from . import tasks

        by = self.context.get("profile")
        self._check_tenant(instance.company, validated_data)
        try:
            with transaction.atomic():
                if "assignee" in validated_data:
                    tasks.assign(instance, validated_data.pop("assignee"), by=by)
                if "due" in validated_data or "due_tz" in validated_data:
                    due = validated_data.pop("due", _current_due(instance))
                    tasks.reschedule(instance, due, by=by, due_tz=validated_data.pop("due_tz", None) or None)
                validated_data.pop("due_tz", None)
                if validated_data:
                    for name, value in validated_data.items():
                        setattr(instance, name, value)
                    instance.full_clean()
                    instance.save(update_fields={*validated_data, "updated_at"})
        except DjangoValidationError as exc:
            raise django_errors(exc) from exc
        return instance


def _current_due(task):
    """The task's due as ``reschedule`` takes it: a date when all-day, else the instant."""
    if task.due_at is None:
        return None
    if task.all_day:
        from .tasks import local_due_date

        return local_due_date(task)
    return task.due_at


class TaskReopenSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["open", "in_progress", "waiting"], default="open")
