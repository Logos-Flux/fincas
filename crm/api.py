"""Staff REST API for engagements, tasks and their history (CRM Phase 3 step 7).

docs/crm/phase3-engagement.md 5.2. Mounted at ``/api/v1/crm/`` beside the
lead and activity endpoints. Every viewset pins ``PrincipalPermission``
explicitly: staff roles and superusers pass, the storefront identity gets 403
(its group holds no ``crm`` permission), a customer-only account gets 403.
Rows are company-scoped through ``CompanyScopedViewSet``.

Inside a tenant every staff user sees every engagement, task and
communication (no private records in Phase 3). Extra permissions:
``crm.manage_pipelines`` for pipeline and stage writes,
``crm.reopen_engagement`` for ``reopen``, ``crm.reassign_engagement`` to
change the owner of an engagement someone else owns.

``me``: ``owner=me`` / ``assignee=me`` and ``/crm/me/`` resolve to the
caller's UserProfile. A service principal (API key, service bearer, or an
account with no profile) has none and gets 400 ``me_needs_human``, never an
empty list that reads as "nothing overdue".
"""

from __future__ import annotations

import datetime
import uuid

from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import F, ProtectedError, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied, ValidationError
from rest_framework.response import Response

from core import tz
from core.models import Company
from core.permissions import PrincipalPermission
from core.staff import archived_q
from core.views import CompanyScopedViewSet, request_company

from . import engagements as engagement_service
from . import tasks as task_service
from .api_serializers import (
    EngagementContactSerializer,
    EngagementSerializer,
    LoseSerializer,
    MoveSerializer,
    NoteSerializer,
    PipelineSerializer,
    ReopenSerializer,
    StageChangeSerializer,
    StageSerializer,
    TaskReopenSerializer,
    TaskSerializer,
    TouchpointCreateSerializer,
    TouchpointSerializer,
    django_errors,
)
from .models import (
    OPEN_TASK_STATUSES,
    Engagement,
    EngagementContact,
    EngagementStageChange,
    Pipeline,
    Stage,
    Task,
    Touchpoint,
)

TAG = "crm"
TRUE = ("1", "true", "yes")
API_SOURCE = EngagementStageChange.Source.API


class MeNeedsHuman(APIException):
    """400 ``me_needs_human``: ``me`` asked by a principal with no UserProfile."""

    status_code = 400
    default_code = "me_needs_human"

    def __init__(self, param="assignee"):
        super().__init__()
        self.detail = {
            "detail": f"'me' needs a signed-in staff user; a service principal has none. "
                      f"Pass {param}=<profile id>.",
            "code": self.default_code,
        }


def parse_instant(raw):
    """An ISO 8601 instant from a query string, else None (never a 500).

    ``+`` in a query string arrives as a space; an impossible date (month 13)
    raises ValueError in ``parse_datetime`` and is None here.
    """
    try:
        return parse_datetime((raw or "").replace(" ", "+"))
    except ValueError:
        return None


def _flag(request, name) -> bool:
    return (request.query_params.get(name) or "").lower() in TRUE


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


def caller_profile(request):
    """The caller's UserProfile, or None for a service principal / no profile.

    A pinned ``svc-*`` account has a UserProfile (its company), but it is not
    a person: ``me`` never resolves to it.
    """
    from core.service_accounts import is_service_account
    from core.throttling import is_service_request

    if is_service_request(request) or is_service_account(request.user):
        return None
    return getattr(request.user, "userprofile", None)


def require_me(request, param="assignee"):
    profile = caller_profile(request)
    if profile is None:
        raise MeNeedsHuman(param)
    return profile


def service_company(request):
    """The tenant a superuser / service write lands in: ``company`` in the body,
    else the only company. Several companies and none named is a 400."""
    from parties.mirror import default_company_id

    raw = request.data.get("company") if hasattr(request.data, "get") else None
    if raw:
        company = Company.objects.filter(pk=raw).first() if _is_uuid(raw) else None
        if company is None:
            raise ValidationError({"company": "No such company."})
        return company
    company_id = default_company_id(Company)
    if company_id is None:
        raise ValidationError({"company": "Several companies exist: name the tenant with company=<uuid>."})
    return Company.objects.get(pk=company_id)


def write_company(request):
    """The tenant a create lands in (fails closed for staff without one)."""
    if request.user.is_superuser:
        return service_company(request)
    company = request_company(request)
    if company is None:
        raise PermissionDenied("Your account has no company: it cannot create CRM records.")
    return company


def _service_call(fn, *args, **kwargs):
    """Run a service; its ValidationError is a 400, its PermissionDenied a 403."""
    try:
        return fn(*args, **kwargs)
    except DjangoValidationError as exc:
        raise django_errors(exc) from exc
    except DjangoPermissionDenied as exc:
        raise PermissionDenied(str(exc) or None) from exc


class _CrmViewSet(CompanyScopedViewSet):
    permission_classes = [PrincipalPermission]

    def get_queryset(self):
        """``?company=<uuid>`` narrows any read to one tenant (step 10).

        A tenant user is already scoped to its own company, so naming another
        one yields nothing; a superuser / service key, which reads every
        tenant, uses it to stay inside one (the assistant passes its tenant
        context on every call). Not a uuid: no rows, never every tenant.
        """
        qs = super().get_queryset()
        company = self.request.query_params.get("company") if getattr(self, "request", None) else None
        if company:
            if not _is_uuid(company):
                return qs.none()
            qs = qs.filter(company_id=company)
        return qs

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if getattr(self, "request", None) is not None:
            context["profile"] = caller_profile(self.request)
        return context

    def perform_create(self, serializer):
        serializer.save(company=write_company(self.request))

    def perform_update(self, serializer):
        serializer.save()


def _require_perm(request, perm):
    if not request.user.has_perm(perm):
        raise PermissionDenied(f"This needs the {perm} permission.")


# --- pipelines and stages ----------------------------------------------------------


class _PipelineAdminMixin:
    """Reads for every staff user; writes need ``crm.manage_pipelines``."""

    def check_permissions(self, request):
        super().check_permissions(request)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            _require_perm(request, "crm.manage_pipelines")

    def perform_destroy(self, instance):
        try:
            instance.delete()
        except ProtectedError as exc:
            raise ValidationError({"detail": f"{type(instance).__name__} is referenced; archive it "
                                   "(is_active=false) instead.", "code": "referenced"}) from exc


@extend_schema(tags=[TAG])
class PipelineViewSet(_PipelineAdminMixin, _CrmViewSet):
    """``pipelines/``: GET; POST, PATCH, DELETE with ``crm.manage_pipelines``.
    ``key`` is write-once; DELETE is refused while stages or engagements use it."""

    permission_classes = [PrincipalPermission]
    queryset = Pipeline.objects.all()
    serializer_class = PipelineSerializer
    filterset_fields = ["is_active", "is_default"]
    ordering_fields = ["sort_order", "name", "created_at"]
    ordering = ["sort_order", "name"]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if getattr(self, "request", None) is not None and self.request.method == "POST":
            context["company"] = request_company(self.request)
        return context


@extend_schema(tags=[TAG])
class StageViewSet(_PipelineAdminMixin, _CrmViewSet):
    """``stages/``: GET (``pipeline=``); POST, PATCH, DELETE with ``crm.manage_pipelines``."""

    permission_classes = [PrincipalPermission]
    queryset = Stage.objects.select_related("pipeline")
    serializer_class = StageSerializer
    parent_links = [("pipeline", "company")]
    filterset_fields = ["pipeline", "is_active", "outcome_hint"]
    ordering_fields = ["sort_order", "created_at"]
    ordering = ["pipeline", "sort_order"]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def perform_create(self, serializer):
        # The company comes from the pipeline (Stage.save copies it).
        pipeline = serializer.validated_data["pipeline"]
        serializer.save(company=pipeline.company)


# --- engagements -------------------------------------------------------------------


def stale_q(company_ids=None, now=None) -> Q:
    """Open engagements whose last activity (else creation) is older than the
    stage's ``stale_after_days``, else the pipeline's (spec 6.1)."""
    now = now or timezone.now()
    stages = Stage._base_manager.select_related("pipeline")
    if company_ids is not None:
        stages = stages.filter(company_id__in=company_ids)
    cond = Q(pk__in=[])
    for stage in stages:
        cutoff = now - datetime.timedelta(days=stage.effective_stale_after_days)
        cond |= Q(stage_id=stage.pk) & (
            Q(last_activity_at__lt=cutoff) | Q(last_activity_at__isnull=True, created_at__lt=cutoff)
        )
    return Q(status=Engagement.Status.OPEN) & cond


@extend_schema(tags=[TAG])
@extend_schema_view(
    list=extend_schema(parameters=[
        OpenApiParameter("status", str, description="open / won / lost."),
        OpenApiParameter("owner", str, description="A profile id, `me`, `none` or `archived`."),
        OpenApiParameter("owner_team", str, description="A team id, or `none`."),
        OpenApiParameter("pipeline", str), OpenApiParameter("stage", str),
        OpenApiParameter("party", str), OpenApiParameter("source", str),
        OpenApiParameter("needs_next_action", bool, description="Open, with no open task."),
        OpenApiParameter("overdue", bool, description="Open, next action due before now."),
        OpenApiParameter("stale", bool, description="Open, no activity for the stage's stale_after_days."),
        OpenApiParameter("no_party", bool, description="No counterparty (the exception queue)."),
        OpenApiParameter("search", str, description="Number, title, or party name prefix."),
    ]),
    partial_update=extend_schema(description="stage / status / pipeline / loss fields are "
                                             "refused (400 use_action): use the actions."),
)
class EngagementViewSet(_CrmViewSet):
    """``engagements/``: GET, POST, PATCH; ``move``, ``win``, ``lose``, ``reopen``
    and ``timeline`` on one engagement. No DELETE: close it instead.

    Default order is the attention order (spec 6.1): no next action first
    (oldest first), then the next action due soonest (overdue on top).
    """

    permission_classes = [PrincipalPermission]
    queryset = Engagement.objects.select_related("pipeline", "stage", "party", "owner")
    serializer_class = EngagementSerializer
    ordering_fields = ["created_at", "updated_at", "next_action_due_at", "last_activity_at",
                       "expected_value", "expected_close_date", "number"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action != "list":
            return qs
        params = self.request.query_params
        for name in ("status", "source"):
            if params.get(name):
                qs = qs.filter(**{name: params[name]})
        for name in ("pipeline", "stage", "party"):
            value = params.get(name)
            if value:
                if not _is_uuid(value):
                    return qs.none()
                qs = qs.filter(**{f"{name}_id": value})
        owner = params.get("owner")
        if owner == "me":
            qs = qs.filter(owner=require_me(self.request, "owner"))
        elif owner == "none":
            qs = qs.filter(owner__isnull=True)
        elif owner == "archived":
            qs = qs.filter(archived_q("owner"))
        elif owner:
            if not _is_uuid(owner):
                return qs.none()
            qs = qs.filter(owner_id=owner)
        team = params.get("owner_team")
        if team == "none":
            qs = qs.filter(owner_team__isnull=True)
        elif team:
            if not _is_uuid(team):
                return qs.none()
            qs = qs.filter(owner_team_id=team)
        now = timezone.now()
        if _flag(self.request, "needs_next_action"):
            qs = qs.filter(status=Engagement.Status.OPEN, next_task__isnull=True)
        if _flag(self.request, "overdue"):
            qs = qs.filter(status=Engagement.Status.OPEN, next_action_due_at__lt=now)
        if _flag(self.request, "no_party"):
            qs = qs.filter(party__isnull=True)
        if _flag(self.request, "stale"):
            company = request_company(self.request)
            named = params.get("company")
            ids = [company.pk] if company is not None else [named] if named and _is_uuid(named) else None
            qs = qs.filter(stale_q(ids, now))
        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(number__iexact=search) | Q(title__icontains=search)
                           | Q(party__display_name__istartswith=search))
        if not params.get("ordering"):
            qs = qs.order_by(F("next_action_due_at").asc(nulls_first=True), "created_at", "pk")
        return qs

    def perform_create(self, serializer):
        if serializer.validated_data.get("owner_team") is not None:
            _require_perm(self.request, "crm.reassign_engagement")
        super().perform_create(serializer)

    def perform_update(self, serializer):
        instance = serializer.instance
        if "owner" in serializer.validated_data:
            new_owner = serializer.validated_data["owner"]
            me = caller_profile(self.request)
            reassigning = instance.owner_id is not None and instance.owner_id != getattr(new_owner, "pk", None) \
                and (me is None or instance.owner_id != me.pk)
            if reassigning:
                _require_perm(self.request, "crm.reassign_engagement")
        if "owner_team" in serializer.validated_data and \
                serializer.validated_data["owner_team"] != instance.owner_team:
            _require_perm(self.request, "crm.reassign_engagement")
        serializer.save()

    def _respond(self, engagement, change=None, **extra):
        engagement = self.get_queryset().get(pk=engagement.pk)
        body = {"engagement": EngagementSerializer(engagement, context=self.get_serializer_context()).data,
                "change": str(change.pk) if change is not None else None, **extra}
        return Response(body)

    @extend_schema(request=MoveSerializer, description="Move to a stage. A stage of another "
                   "pipeline changes the pipeline. `needs_close` is the stage's outcome hint: "
                   "confirm with win / lose; a move never closes.")
    @action(detail=True, methods=["post"])
    def move(self, request, pk=None):
        engagement = self.get_object()
        data = MoveSerializer(data=request.data, context=self.get_serializer_context())
        data.is_valid(raise_exception=True)
        stage, note = data.validated_data["stage"], data.validated_data["note"]
        by = caller_profile(request)
        if stage.pipeline_id != engagement.pipeline_id:
            change = _service_call(engagement_service.change_pipeline, engagement, stage.pipeline, stage,
                                   by=by, source=API_SOURCE, note=note)
            return self._respond(engagement, change, needs_close=stage.outcome_hint)
        result = _service_call(engagement_service.move, engagement, stage, by=by, note=note,
                               source=API_SOURCE)
        return self._respond(engagement, result.change, needs_close=result.needs_close)

    @extend_schema(request=NoteSerializer)
    @action(detail=True, methods=["post"])
    def win(self, request, pk=None):
        engagement = self.get_object()
        data = NoteSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        change = _service_call(engagement_service.win, engagement, by=caller_profile(request),
                               source=API_SOURCE, note=data.validated_data["note"])
        return self._respond(engagement, change)

    @extend_schema(request=LoseSerializer)
    @action(detail=True, methods=["post"])
    def lose(self, request, pk=None):
        engagement = self.get_object()
        data = LoseSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        change = _service_call(engagement_service.lose, engagement, v["loss_reason"],
                               by=caller_profile(request), competitor=v["loss_competitor"],
                               notes=v["loss_notes"], source=API_SOURCE, note=v["note"])
        return self._respond(engagement, change)

    @extend_schema(request=ReopenSerializer, description="Needs crm.reopen_engagement and a "
                   "signed-in user (a service principal cannot reopen).")
    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        engagement = self.get_object()
        data = ReopenSerializer(data=request.data, context=self.get_serializer_context())
        data.is_valid(raise_exception=True)
        _require_perm(request, "crm.reopen_engagement")
        by = caller_profile(request)
        if by is None:
            raise PermissionDenied("Reopening needs a signed-in user with crm.reopen_engagement.")
        change = _service_call(engagement_service.reopen, engagement, by=by,
                               stage=data.validated_data["stage"], source=API_SOURCE,
                               note=data.validated_data["note"])
        return self._respond(engagement, change)

    @extend_schema(parameters=[
        OpenApiParameter("before", str, description="Cursor: the previous page's next_before."),
        OpenApiParameter("kinds", str, description="Comma list: stage, task, document, or "
                         "communication kinds (email, call, meeting, note, ...)."),
        OpenApiParameter("limit", int, description="Items per page (default 50, max 200)."),
    ])
    @action(detail=True, methods=["get"])
    def timeline(self, request, pk=None):
        from .timeline import CursorError, build

        engagement = self.get_object()
        kinds = [k for k in (request.query_params.get("kinds") or "").split(",") if k.strip()]
        try:
            limit = int(request.query_params.get("limit") or 50)
        except ValueError as exc:
            raise ValidationError({"limit": "An integer."}) from exc
        try:
            page = build(engagement, before=request.query_params.get("before") or None,
                         kinds=[k.strip() for k in kinds], limit=limit)
        except CursorError as exc:
            raise ValidationError({"before": str(exc)}) from exc
        return Response(page.as_dict())

    @extend_schema(parameters=[
        OpenApiParameter("channel", str, description="email (default) or sms."),
    ], responses={200: dict}, description="Read-only facts for a follow-up proposal (spec 5.3): "
       "the recipient and contact point, the can_contact decision, the contacts (erased parties "
       "left out), the last communication and the open tasks. `refusal` is `suppressed` (the "
       "address is withheld), `no_recipient` or `no_address`. Writes nothing.")
    @action(detail=True, methods=["get"], url_path="follow-up-facts")
    def follow_up_facts(self, request, pk=None):
        from .assist import FOLLOW_UP_CHANNELS, follow_up_facts

        engagement = self.get_object()
        channel = request.query_params.get("channel") or "email"
        if channel not in FOLLOW_UP_CHANNELS:
            raise ValidationError({"channel": f"One of {', '.join(FOLLOW_UP_CHANNELS)}."})
        return Response(follow_up_facts(engagement, channel))


@extend_schema(tags=[TAG])
class EngagementContactViewSet(_CrmViewSet):
    """``engagement-contacts/``: GET (``engagement=``), POST, PATCH, DELETE."""

    permission_classes = [PrincipalPermission]
    queryset = EngagementContact.objects.select_related("party", "engagement")
    serializer_class = EngagementContactSerializer
    parent_links = [("engagement", "company"), ("party", "company")]
    filterset_fields = ["engagement", "party", "role", "is_primary"]
    ordering = ["-is_primary", "created_at"]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def perform_create(self, serializer):
        serializer.save(company=serializer.validated_data["engagement"].company)


@extend_schema(tags=[TAG])
class StageChangeViewSet(_CrmViewSet):
    """``stage-changes/``: read-only engagement history (``engagement=``)."""

    permission_classes = [PrincipalPermission]
    queryset = EngagementStageChange.objects.select_related("from_stage", "to_stage")
    serializer_class = StageChangeSerializer
    filterset_fields = ["engagement", "to_stage", "source"]
    ordering_fields = ["changed_at"]
    ordering = ["-changed_at"]
    http_method_names = ["get", "head", "options"]


@extend_schema(tags=[TAG])
class TouchpointViewSet(_CrmViewSet):
    """``touchpoints/``: reads for staff; ``POST`` records one visit
    (``crm.touchpoints.record``) for a caller holding ``crm.add_touchpoint``:
    a service account pinned to its company, typically through an
    ``attribution``-scoped key (the Google Ads add-on registers the scope,
    bound to this path). A caller that cannot read touchpoints always gets
    202 ``{"status": "accepted"}``; one with ``crm.view_touchpoint`` gets 201
    with the row, 200 when ``event_id`` was seen before, 202 ``skipped`` when
    the address belongs to an erased person.
    Never updated or deleted through the API."""

    permission_classes = [PrincipalPermission]
    queryset = Touchpoint.objects.all()
    serializer_class = TouchpointSerializer
    filterset_fields = ["party", "engagement", "channel", "source"]
    ordering_fields = ["occurred_at"]
    ordering = ["-occurred_at"]
    http_method_names = ["get", "post", "head", "options"]

    def get_serializer_class(self):
        return TouchpointCreateSerializer if self.action == "create" else TouchpointSerializer

    @extend_schema(request=TouchpointCreateSerializer, responses={201: TouchpointSerializer})
    def create(self, request, *args, **kwargs):
        from . import touchpoints

        _require_perm(request, "crm.add_touchpoint")
        serializer = TouchpointCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        company = write_company(request)
        requested = data.pop("company", None)
        if requested and requested != company.pk:
            raise ValidationError({"company": "Cannot create records for another company."})
        with transaction.atomic():
            touch, created = _service_call(touchpoints.record_visit, company, data)
        if not request.user.has_perm("crm.view_touchpoint"):
            # A write-only caller (a tag manager's key) learns nothing: not
            # whether the email is a customer's, nor whether that person was
            # erased, nor another visit's row by replaying its event id.
            return Response({"status": "accepted"}, status=202)
        if touch is None:
            return Response({"status": "skipped"}, status=202)
        return Response(TouchpointSerializer(touch).data, status=201 if created else 200)


# --- tasks -------------------------------------------------------------------------


def due_window(request, which: str, now=None):
    """``(start, end)`` of ``?due=today|week`` in the caller's zone (the
    company's for a service principal)."""
    now = now or timezone.now()
    profile = caller_profile(request)
    company = request_company(request) or (profile.company if profile is not None else None)
    zone = tz.resolve(profile, company)
    start = datetime.datetime.combine(now.astimezone(zone).date(), datetime.time.min, tzinfo=zone)
    days = 1 if which == "today" else 7
    return start, start + datetime.timedelta(days=days)


@extend_schema(tags=[TAG])
@extend_schema_view(
    list=extend_schema(parameters=[
        OpenApiParameter("assignee", str, description="A profile id, `me`, `none` or `archived`."),
        OpenApiParameter("engagement", str), OpenApiParameter("party", str),
        OpenApiParameter("status", str, description="A status, or `open` for every open status "
                         "(open / in_progress / waiting)."),
        OpenApiParameter("due_before", str, description="ISO 8601 instant."),
        OpenApiParameter("overdue", bool, description="Open and due before now."),
        OpenApiParameter("due", str, description="overdue / today / week (the caller's local "
                         "day, or the 7 days from today's start); open tasks only."),
    ]),
    partial_update=extend_schema(description="status is refused (400 use_action): use complete "
                                             "/ reopen / cancel. `due` reschedules, `assignee` "
                                             "reassigns; both write a TaskEvent."),
)
class TaskViewSet(_CrmViewSet):
    """``tasks/``: GET, POST, PATCH; ``complete``, ``reopen``, ``cancel``. No
    DELETE: cancel keeps the history."""

    permission_classes = [PrincipalPermission]
    queryset = Task.objects.select_related("engagement", "assignee")
    serializer_class = TaskSerializer
    ordering_fields = ["due_at", "created_at", "completed_at", "priority"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):  # noqa: C901 - one branch per documented filter
        qs = super().get_queryset()
        if self.action != "list":
            return qs
        params = self.request.query_params
        assignee = params.get("assignee")
        if assignee == "me":
            qs = qs.filter(assignee=require_me(self.request, "assignee"))
        elif assignee == "none":
            qs = qs.filter(assignee__isnull=True)
        elif assignee == "archived":
            qs = qs.filter(archived_q("assignee"))
        elif assignee:
            if not _is_uuid(assignee):
                return qs.none()
            qs = qs.filter(assignee_id=assignee)
        for name in ("engagement", "party"):
            value = params.get(name)
            if value:
                if not _is_uuid(value):
                    return qs.none()
                qs = qs.filter(**{f"{name}_id": value})
        state = params.get("status")
        if state == "open":
            qs = qs.filter(status__in=OPEN_TASK_STATUSES)
        elif state:
            if state not in Task.Status.values:
                raise ValidationError({"status": f"One of {', '.join(Task.Status.values)}."})
            qs = qs.filter(status=state)
        now = timezone.now()
        if params.get("due_before"):
            before = parse_instant(params["due_before"])
            if before is None:
                raise ValidationError({"due_before": "An ISO 8601 instant."})
            qs = qs.filter(due_at__lt=before)
        due = params.get("due")
        if _flag(self.request, "overdue") or due == "overdue":
            qs = qs.filter(status__in=OPEN_TASK_STATUSES, due_at__lt=now)
        elif due in ("today", "week"):
            start, end = due_window(self.request, due, now)
            qs = qs.filter(status__in=OPEN_TASK_STATUSES, due_at__gte=start, due_at__lt=end)
        elif due:
            raise ValidationError({"due": "One of overdue, today, week."})
        if not params.get("ordering"):
            qs = qs.order_by(F("due_at").asc(nulls_last=True), "created_at", "pk")
        return qs

    def _done(self, task):
        return Response(TaskSerializer(self.get_queryset().get(pk=task.pk),
                                       context=self.get_serializer_context()).data)

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def complete(self, request, pk=None):
        task = self.get_object()
        _service_call(task_service.complete, task, by=caller_profile(request))
        return self._done(task)

    @extend_schema(request=TaskReopenSerializer)
    @action(detail=True, methods=["post"])
    def reopen(self, request, pk=None):
        task = self.get_object()
        data = TaskReopenSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        _service_call(task_service.reopen, task, by=caller_profile(request),
                      status=data.validated_data["status"])
        return self._done(task)

    @extend_schema(request=None)
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        task = self.get_object()
        _service_call(task_service.cancel, task, by=caller_profile(request))
        return self._done(task)


# --- the signed-in user's queue ----------------------------------------------------


@extend_schema(tags=[TAG])
class MyQueueViewSet(viewsets.ViewSet):
    """``me/``: the caller's open tasks and next actions (spec 6.4, "My day").

    ``overdue``: my open tasks due before now. ``today``: my open tasks due
    from now to the end of my local day. ``needs_next_action``: my open
    engagements with no open task. A service principal gets 400
    ``me_needs_human``.
    """

    permission_classes = [PrincipalPermission]
    # Names the model for the service contract (view_task), so a per-key
    # service account with that permission reaches the 400, not a 403.
    queryset = Task.objects.none()

    @extend_schema(responses={200: dict})
    def list(self, request):
        return Response(queue_payload(request, require_me(request)))


def queue_payload(request, profile) -> dict:
    """``me/`` and ``queues/<id>/``: the three sections for ``profile``."""
    from .assist import queue_for

    queue = queue_for(profile)
    context = {"request": request, "profile": profile}
    return {
        "profile": str(profile.pk),
        "timezone": queue["timezone"],
        "overdue": TaskSerializer(queue["overdue"], many=True, context=context).data,
        "today": TaskSerializer(queue["today"], many=True, context=context).data,
        "needs_next_action": EngagementSerializer(queue["needs_next_action"], many=True,
                                                  context=context).data,
    }


# --- the assistant's staff directory and named queues (step 10, spec 5.3) -------------


def _directory_companies(request):
    """Tenants a directory read may cover: a tenant user its own (``company``
    naming another is 400); a superuser / service key every tenant, or the one
    it names."""
    raw = request.query_params.get("company")
    if raw and not _is_uuid(raw):
        raise ValidationError({"company": "A company id."})
    if request.user.is_superuser:
        return [raw] if raw else None
    company = request_company(request)
    if raw and (company is None or str(company.pk) != str(raw)):
        raise ValidationError({"company": "Cannot read another company's staff."})
    return [company.pk] if company is not None else []


@extend_schema(tags=[TAG])
class StaffDirectoryViewSet(viewsets.ViewSet):
    """``staff/?q=``: the staff directory as the 2.7 match envelope.

    For a principal that acts for someone else (the assistant's shared key):
    ``q`` is a staff email (``exact`` when one profile has it), a profile id,
    or a name (never better than ``fuzzy``). Two tenants' staff with the same
    email are ``ambiguous`` unless ``company`` names one. Never picks.
    """

    permission_classes = [PrincipalPermission]
    queryset = Task.objects.none()  # the service contract: view_task, as me/

    @extend_schema(parameters=[OpenApiParameter("q", str), OpenApiParameter("company", str)],
                   responses={200: dict})
    def list(self, request):
        from .assist import staff_envelope

        return Response(staff_envelope(_directory_companies(request), request.query_params.get("q", "")))


@extend_schema(tags=[TAG])
class StaffQueueViewSet(viewsets.ViewSet):
    """``queues/<profile id>/``: ``me/``'s three sections for a named staff
    person, in that person's time zone.

    The record-level rule is the API's: a tenant user reads only its own
    company's people (another tenant's profile is 404), a superuser / service
    key any tenant's, and ``company`` (when given) must be the person's
    company or it is 404. A service account's profile is not a person: 404.
    """

    permission_classes = [PrincipalPermission]
    queryset = Task.objects.none()

    @extend_schema(parameters=[OpenApiParameter("company", str)], responses={200: dict})
    def retrieve(self, request, pk=None):
        from rest_framework.exceptions import NotFound

        from core.managers import scope_to_company
        from core.models import UserProfile

        from .assist import is_staff_person

        companies = _directory_companies(request)
        profile = None
        if _is_uuid(pk):
            qs = scope_to_company(UserProfile.objects.select_related("user", "company"), request)
            if companies is not None:
                qs = qs.filter(company_id__in=companies)
            profile = qs.filter(pk=pk, company__isnull=False).first()
        if profile is None or not is_staff_person(profile):
            raise NotFound("No such staff person in this tenant.")
        return Response(queue_payload(request, profile))
