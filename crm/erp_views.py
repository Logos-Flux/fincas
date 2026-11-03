"""Server-rendered CRM back office (docs/crm/phase3-engagement.md 6; step 8).

``/erp/crm/``: My day, the engagement list and its queues, the thread view,
the timeline, tasks, leads and the pipelines admin page.

``/erp/`` is staff-only (``StaffOnlyERPMiddleware``). Every queryset starts
from ``for_request``, so staff reach only their own tenant's rows and another
tenant's id is a 404; superusers are cross-tenant, as in the staff API.

**The screens call the services, never the API and never ``save()`` on a
guarded field**: stage moves, wins, losses and reopens go through
``crm.engagements``, task lifecycle through ``crm.tasks``, and a logged
communication through ``communications.ingest.record``. The services write
the history rows, the task events and the next-action recompute.

Times render in the viewer's zone (``core.tz.resolve``: their profile, else
their company, else ``TIME_ZONE``).
"""

from __future__ import annotations

import datetime
import uuid
from collections import OrderedDict
from zoneinfo import ZoneInfo

from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F, Prefetch, Q
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views import View
from django.views.generic import DetailView, TemplateView

from communications import ingest
from core import sidecar_forms, tz
from core.error_views import forbidden
from core.http import safe_next  # noqa: F401  (re-exported for the CRM views and tests)
from core.labels import get_label, labels_for_request
from core.list_columns import Column
from core.models import ExternalRef
from core.staff import archived_q, filter_entry, is_archived, owner_filter_choices
from core.teams import team_of
from core.views.list_view import ERPListView
from core.views.mixins import ERPViewMixin
from parties.models import ConsentRecord, PartyEmail, PartyPhone

from . import engagements as engagement_service
from . import tasks as task_service
from .conf import crm_setting
from .erp_timeline import KIND_CHIPS, page_for
from .forms import (
    BulkForm,
    ContactForm,
    EngagementForm,
    LeadForm,
    LogCommunicationForm,
    LoseForm,
    MoveForm,
    NewTaskForm,
    PipelineForm,
    ReassignForm,
    ReopenForm,
    RescheduleForm,
    StageForm,
    TaskForm,
    profile_label,
    snooze_date,
)
from .models import (
    OPEN_TASK_STATUSES,
    Engagement,
    EngagementContact,
    Lead,
    Pipeline,
    Stage,
    Task,
)
from .registries import ENGAGEMENT_SOURCES, LOSS_REASONS, TASK_KINDS
from .timeline import CursorError

ERP = engagement_service.Source.ERP
OPEN = Engagement.Status.OPEN
OPEN_QUOTE_STATUSES = ("draft", "pending_approval", "approved", "sent", "viewed", "negotiating")
LIST_LIMIT = 100  # rows per My-day section

SECTION_MY_DAY = "crm_my_day"
SECTION_ENGAGEMENTS = "crm_engagements"
SECTION_TASKS = "crm_tasks"
SECTION_LEADS = "crm_leads"
SECTION_PIPELINES = "crm_pipelines"


# --- helpers -----------------------------------------------------------------------


def viewer_profile(request):
    return getattr(request.user, "userprofile", None)


def viewer_zone(request):
    profile = viewer_profile(request)
    return tz.resolve(profile, profile.company if profile is not None and profile.company_id else None)


def viewer_company_id(request):
    """The tenant new rows land in: the user's company; for a superuser
    without one, the only company. ``None``: cannot tell, refuse."""
    from parties.erp_views import request_company_id

    return request_company_id(request)


def local_today(zone, now=None):
    return (now or timezone.now()).astimezone(zone).date()


def thread_url(engagement, anchor=""):
    return reverse("erp-crm-engagement-detail", args=[engagement.pk]) + anchor


def service_errors(request, exc: ValidationError) -> None:
    for text in (exc.messages if hasattr(exc, "messages") else [str(exc)]):
        messages.error(request, text)


def loss_reason_labels(company_id) -> dict[str, str]:
    out = {}
    for key, entry in LOSS_REASONS.items():
        try:
            out[key] = get_label(f"loss_reason.{key}", company=company_id).singular
        except (KeyError, ValueError):
            out[key] = entry.label
    return out


def task_kind_labels(company_id) -> dict[str, str]:
    out = {}
    for key, entry in TASK_KINDS.items():
        try:
            out[key] = get_label(f"task_kind.{key}", company=company_id).singular
        except (KeyError, ValueError):
            out[key] = entry.label
    return out


def source_label(key, company_id=None) -> str:
    if not key:
        return ""
    try:
        return get_label(f"engagement_source.{key}", company=company_id).singular
    except (KeyError, ValueError):
        entry = ENGAGEMENT_SOURCES.get(key)
        return entry.label if entry else key


def stale_days(engagement, now):
    """Days since the last activity (else creation) when that is past the
    stage's ``stale_after_days`` (else the pipeline's), else None (spec 6.1)."""
    if engagement.status != OPEN or engagement.stage_id is None:
        return None
    since = engagement.last_activity_at or engagement.created_at
    if now - since > datetime.timedelta(days=engagement.stage.effective_stale_after_days):
        return (now - since).days
    return None


def attention_rules(engagement, now, grace):
    """Which "Needs next action" rules fire for an open engagement (6.1)."""
    rules = []
    if engagement.next_task_id is None:
        rules.append("No next action")
    elif engagement.next_action_due_at is not None and engagement.next_action_due_at < now - grace:
        rules.append("Next action overdue")
    if engagement.party_id is None:
        rules.append("No counterparty")
    if engagement.owner_id is None:
        rules.append("No owner")
    elif is_archived(engagement.owner):
        rules.append("Owner archived")  # phase4-fields.md 2.5; "Reassign" clears it
    return rules


def attention_q(now, grace) -> Q:
    return Q(status=OPEN) & (
        Q(next_task__isnull=True) | Q(next_action_due_at__lt=now - grace)
        | Q(party__isnull=True) | Q(owner__isnull=True) | archived_q("owner")
    )


def can_change_owner(user, profile, engagement, new_owner) -> bool:
    """Claiming an unowned engagement or handing on your own is free; changing
    someone else's needs ``crm.reassign_engagement`` (spec 5.2)."""
    current = engagement.owner_id
    if current is None or current == getattr(new_owner, "pk", None):
        return True
    if profile is not None and current == profile.pk:
        return True
    return user.has_perm("crm.reassign_engagement")


def set_owner(engagement, owner):
    engagement_service.reassign(engagement, owner)


# --- 6.1 the engagement list and its queues ----------------------------------------


CHIPS = (
    ("mine", "Mine"), ("unassigned", "Unassigned"), ("attention", "Needs next action"),
    ("overdue", "Overdue"), ("stale", "Stale"), ("no_party", "No counterparty"),
    ("open", "All open"), ("closed_month", "Closed this month"),
)
CHIP_KEYS = {key for key, _label in CHIPS}


class EngagementListView(ERPListView):
    """``/erp/crm/engagements/``: one query for the rows whatever the page size.

    Default filter: open and mine; if I own no open engagement, open and
    unassigned, with a notice. Default order: the attention order (no next
    action first, oldest first; then the next action due soonest, overdue on
    top). Stale is a badge and a chip, never a sort key.
    """

    model = Engagement
    template_name = "erp/crm/engagement_list.html"
    current_section = SECTION_ENGAGEMENTS
    page_title = "Engagements"
    entity_name = "Engagement"
    entity_label_key = "crm.engagement"
    page_title_format = "{plural}"
    create_url_name = "erp-crm-engagement-create"
    paginate_by = 50
    # Column registry + canonical export (docs/crm/phase4-fields.md 5.1, 5.5).
    # Screen and export share the registry: the 7 Phase 3 defaults, the
    # optional columns, then export-only ones ("All fields"). The attention
    # queue keeps its own column choice (``get_list_id``).
    list_id = "crm.engagement"
    definition_object = "engagement"
    csv_canonical = True
    # Sort allow-list: id -> order paths. Anything else falls back to the
    # attention order (ERPListView ignores an unknown ?sort=). ``value`` and
    # ``last_activity`` are the Phase 3 sort ids, kept for bookmarks.
    sortable_fields = {
        "number": ("number",),
        "stage": ("stage__sort_order",),
        "next_action": ("next_action_due_at",),
        "expected_value": ("expected_value",),
        "last_activity_at": ("last_activity_at",),
        "created_at": ("created_at",),
        "probability": ("probability",),
        "expected_close_date": ("expected_close_date",),
        "closed_at": ("closed_at",),
    }
    # Phase 3 sort ids, kept for bookmarks and saved filters.
    sort_aliases = {"value": "expected_value", "last_activity": "last_activity_at", "created": "created_at"}
    list_columns = [
        Column("number", "Engagement", path="number", priority=1),
        Column("party", "Counterparty", path="party__display_name", priority=2),
        Column("stage", "Stage", path="stage__key", priority=2),
        Column("next_action", "Next action", path="next_action_due_at", priority=1),
        Column("expected_value", "Value", path="expected_value", priority=2),
        Column("owner", "Owner", priority=3),
        Column("last_activity_at", "Last activity", path="last_activity_at", priority=3),
        Column("pipeline", "Pipeline", path="pipeline__key", priority=3, default=False),
        Column("owner_team", "Team", priority=3, default=False),
        Column("probability", "Probability", path="probability", priority=3, default=False),
        Column("weighted_value", "Weighted value", priority=3, default=False),
        Column("expected_close_date", "Expected close", path="expected_close_date", priority=3, default=False,
               format="date"),
        Column("source", "Source", path="source", priority=3, default=False),
        Column("created_at", "Created", path="created_at", priority=3, default=False, format="date"),
        Column("closed_at", "Closed", path="closed_at", priority=3, default=False, format="date"),
        Column("loss_reason", "Loss reason", path="loss_reason", priority=3, default=False),
        # Export only (canonical, never relabelled: Phase 3 3.4).
        Column("id", "id", path="id", export_only=True),
        Column("title", "title", path="title", export_only=True),
        Column("status", "status", path="status", export_only=True),
        Column("party_id", "party_id", path="party_id", export_only=True),
        Column("owner_id", "owner_id", path="owner_id", export_only=True),
        Column("currency", "currency", path="currency", export_only=True),
        Column("next_action_due_at", "next_action_due_at", path="next_action_due_at", export_only=True),
    ]

    def get_list_id(self):
        if self.request.GET.get("queue") == "attention" or self.request.GET.get("chip") == "attention":
            return "crm.engagement.attention"
        return self.list_id

    def export_value(self, obj, col):
        if col.id == "next_action":
            return obj.next_task.title if obj.next_task_id else ""
        if col.id == "owner":
            return profile_label(obj.owner) if obj.owner_id else ""
        if col.id == "owner_team":
            return obj.owner_team.key if obj.owner_team_id else ""
        if col.id == "weighted_value":
            return obj.weighted_value
        return super().export_value(obj, col)

    def get_filter_params(self):
        return {
            "q": None, "chip": tuple(CHIP_KEYS), "queue": ("attention",),
            "status": tuple(Engagement.Status.values), "pipeline": None, "stage": None,
            "owner": None, "source": tuple(ENGAGEMENT_SOURCES),
        }

    def _chip(self):
        params = self.request.GET
        if params.get("queue") == "attention":
            return "attention", ""
        chip = params.get("chip", "")
        if chip in CHIP_KEYS:
            return chip, ""
        if any(params.get(k) for k in ("q", "pipeline", "stage", "owner", "source", "status")):
            return "", ""
        profile = viewer_profile(self.request)
        if profile is None:
            return "open", ""
        mine = Engagement.objects.for_request(self.request).filter(status=OPEN, owner=profile)
        if mine.exists():
            return "mine", ""
        return "unassigned", "You own no open engagements, so this shows the open, unassigned ones."

    def get_queryset(self):
        params = self.request.GET
        now = self.now = timezone.now()
        self.grace = datetime.timedelta(hours=float(crm_setting("overdue_grace_hours")))
        if not hasattr(self, "chip"):
            self.chip, self.notice = self._chip()
        qs = Engagement.objects.for_request(self.request).select_related(
            "stage__pipeline", "pipeline", "party", "owner__user", "owner_team", "next_task",
        )
        profile = viewer_profile(self.request)
        chip = self.chip
        if chip == "mine":
            qs = qs.filter(status=OPEN, owner=profile) if profile else qs.none()
        elif chip == "unassigned":
            qs = qs.filter(status=OPEN, owner__isnull=True)
        elif chip == "attention":
            qs = qs.filter(attention_q(now, self.grace))
        elif chip == "overdue":
            qs = qs.filter(status=OPEN, next_action_due_at__lt=now)
        elif chip == "stale":
            from .api import stale_q

            company = profile.company_id if profile is not None and not self.request.user.is_superuser else None
            qs = qs.filter(stale_q([company] if company else None, now))
        elif chip == "no_party":
            qs = qs.filter(status=OPEN, party__isnull=True)
        elif chip == "open":
            qs = qs.filter(status=OPEN)
        elif chip == "closed_month":
            zone = viewer_zone(self.request)
            start = datetime.datetime.combine(local_today(zone, now).replace(day=1), datetime.time.min,
                                              tzinfo=zone)
            qs = qs.exclude(status=OPEN).filter(closed_at__gte=start)
        status = params.get("status")
        if status in Engagement.Status.values:
            qs = qs.filter(status=status)
        for name in ("pipeline", "stage"):
            value = params.get(name)
            if value:
                try:
                    qs = qs.filter(**{f"{name}_id": value})
                except ValidationError:
                    qs = qs.none()
        owner = params.get("owner")
        if owner == "none":
            qs = qs.filter(owner__isnull=True)
        elif owner:
            try:
                qs = qs.filter(owner_id=owner)
            except ValidationError:
                qs = qs.none()
        source = params.get("source")
        if source in ENGAGEMENT_SOURCES:
            qs = qs.filter(source=source)
        text = params.get("q", "").strip()
        if text:
            qs = qs.filter(Q(number__iexact=text) | Q(title__icontains=text)
                           | Q(party__display_name__istartswith=text))
        qs = qs.order_by(F("next_action_due_at").asc(nulls_first=True), "created_at", "pk")
        return self.apply_sort(qs)

    def get_header_columns(self, sort, direction):
        """Registry headers; the number and stage headers use the tenant's nouns."""
        company_id = labels_for_request(self.request).company_id
        nouns = {"number": get_label("crm.engagement", company=company_id).singular,
                 "stage": get_label("crm.stage", company=company_id).singular}
        columns = super().get_header_columns(sort, direction)
        for col in columns:
            col["label"] = nouns.get(col["id"], col["label"])
        return columns

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = self.now
        company_id = labels_for_request(self.request).company_id
        reasons = loss_reason_labels(company_id)
        for eng in context["object_list"]:
            eng.stale_days = stale_days(eng, now)
            eng.rules = attention_rules(eng, now, self.grace) if eng.status == OPEN else []
            eng.overdue = eng.next_action_due_at is not None and eng.next_action_due_at < now
            eng.in_stage = (now - eng.stage_entered_at) if eng.stage_entered_at else None
            eng.loss_label = reasons.get(eng.loss_reason, eng.loss_reason) if eng.loss_reason else ""
            eng.source_text = source_label(eng.source, company_id)
        params = self.request.GET
        kept = params.copy()
        for name in ("page", "chip", "queue", "export"):
            kept.pop(name, None)
        context.update(
            chips=CHIPS, active_chip=self.chip, notice=self.notice,
            chip_query=kept.urlencode(),
            show_rules=self.chip == "attention",
            search_query=params.get("q", ""), viewer_tz=viewer_zone(self.request).key,
        )
        page_kept = params.copy()
        page_kept.pop("page", None)
        page_kept.pop("export", None)
        context["page_query"] = page_kept.urlencode()
        pipelines = list(Pipeline.objects.for_request(self.request).filter(is_active=True)
                         .prefetch_related(Prefetch("stages", queryset=Stage.objects.filter(is_active=True)
                                                    .order_by("sort_order"), to_attr="active_stages")))
        owner_company = viewer_company_id(self.request)
        owner_filter = filter_entry("owner", "Owner", owner_filter_choices(owner_company))
        context["filters"] = {
            "pipelines": pipelines, "owners": owner_filter["options"], "owner_groups": owner_filter["groups"],
            # Bulk "Assign owner" offers active people only (2.5).
            "bulk_owners": owner_filter["options"],
            "sources": [(k, source_label(k, company_id)) for k in ENGAGEMENT_SOURCES],
            "active": {k: params.get(k, "") for k in ("pipeline", "stage", "owner", "source", "status")},
        }
        stage_noun = get_label("crm.stage", company=company_id).singular
        status_filters = [
            {"field": "chip", "label": "View", "choices": CHIPS},
            {"field": "pipeline", "label": "Pipeline", "choices": [(str(p.pk), p.name) for p in pipelines]},
            {"field": "stage", "label": stage_noun,
             "choices": [(str(s.pk), s.name) for p in pipelines for s in p.active_stages]},
            {"field": "owner", "label": "Owner",
             "choices": [("none", "Unassigned"), *owner_filter["choices"]]},
            {"field": "source", "label": "Source", "choices": context["filters"]["sources"]},
            {"field": "status", "label": "Status", "choices": list(Engagement.Status.choices)},
        ]
        context.update(self.get_list_status(
            context, status_filters, {"chip": self.chip, **context["filters"]["active"]},
            context["current_sort"], context["current_sort_dir"],
            filtered=any(params.get(k) for k in ("q", "chip", "queue", *context["filters"]["active"])),
        ))
        context["active_filter_count"] = sum(1 for v in context["filters"]["active"].values() if v)
        if self.chip == "attention":
            context["bulk_owners"] = context["filters"]["bulk_owners"]
            context["bulk_reasons"] = [(k, v) for k, v in reasons.items() if k != "unknown"]
        return context


class EngagementBulkView(ERPViewMixin, View):
    """POST: the exception queue's bulk actions, each row through the services."""

    def post(self, request):
        back = safe_next(request, reverse("erp-crm-engagement-list") + "?queue=attention")
        ids = request.POST.getlist("ids")
        rows = list(Engagement.objects.for_request(request).filter(pk__in=_uuids(ids), status=OPEN)
                    .select_related("owner", "party"))
        company_id = viewer_company_id(request)
        form = BulkForm(request.POST, company_id=company_id, id_choices=[r.pk for r in rows])
        if not rows or not form.is_valid():
            messages.error(request, "Nothing changed: pick engagements and complete the action.")
            return redirect(back)
        profile = viewer_profile(request)
        action, done, refused = form.cleaned_data["action"], 0, 0
        tomorrow = local_today(viewer_zone(request)) + datetime.timedelta(days=1)
        for eng in rows:
            try:
                if action == "assign":
                    owner = form.cleaned_data["owner"]
                    if owner.company_id != eng.company_id or not can_change_owner(request.user, profile, eng, owner):
                        refused += 1
                        continue
                    set_owner(eng, owner)
                elif action == "follow_up":
                    by = profile if profile is not None and profile.company_id == eng.company_id else None
                    task_service.create(engagement=eng, title="Follow up", kind="follow_up",
                                        assignee=eng.owner or by, by=by, due=tomorrow, source="erp")
                else:
                    by = profile if profile is not None and profile.company_id == eng.company_id else None
                    engagement_service.lose(eng, form.cleaned_data["loss_reason"], by=by, source=ERP,
                                            note="Bulk close from the exception queue")
                done += 1
            except ValidationError:
                refused += 1
        messages.success(request, f"Updated {done}." + (f" Skipped {refused}." if refused else ""))
        return redirect(back)


def _uuids(values):
    out = []
    for v in values:
        try:
            out.append(uuid.UUID(str(v)))
        except ValueError:
            continue
    return out


# --- 6.2 the thread view -----------------------------------------------------------


def _scoped_engagement(request, pk):
    return get_object_or_404(
        Engagement.objects.for_request(request).select_related(
            "pipeline", "stage__pipeline", "party", "owner__user", "owner_team", "next_task__assignee__user",
            "first_touch", "last_touch",
        ),
        pk=pk,
    )


class EngagementDetailView(ERPViewMixin, DetailView):
    """Attribute ranking: what do I do next, then what is this deal, then what
    happened. A constant number of queries whatever the thread's size."""

    model = Engagement
    template_name = "erp/crm/engagement_detail.html"
    context_object_name = "engagement"
    current_section = SECTION_ENGAGEMENTS
    entity_label_key = "crm.engagement"

    def get_object(self, queryset=None):
        return _scoped_engagement(self.request, self.kwargs["pk"])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        eng = self.object
        request = self.request
        now = timezone.now()
        zone = viewer_zone(request)
        today = local_today(zone, now)
        company_id = eng.company_id
        profile = viewer_profile(request)
        context.update(now=now, viewer_tz=zone.key, today=today, page_title=f"{eng.number} {eng.title}")

        # 1. Next action.
        open_tasks = list(
            eng.tasks.filter(status__in=OPEN_TASK_STATUSES).select_related("assignee__user")
            .order_by(F("due_at").asc(nulls_last=True), "created_at", "pk")
        )
        next_task = next((t for t in open_tasks if t.pk == eng.next_task_id), None)
        kind_labels = task_kind_labels(company_id)
        for task in open_tasks:
            task.kind_label = kind_labels.get(task.kind, task.kind)
            task.overdue = task.due_at is not None and task.due_at < now
            task.set_in = task.due_tz if task.due_tz and task.due_tz != zone.key else ""
        context["next_task"] = next_task
        context["other_open_tasks"] = [t for t in open_tasks if t.pk != eng.next_task_id]
        context["open_tasks"] = open_tasks
        tomorrow = today + datetime.timedelta(days=1)
        context["task_form"] = TaskForm(company_id=company_id, kind_labels=kind_labels, user=request.user, initial={
            "title": "", "kind": "follow_up", "due_date": tomorrow,
            "assignee": profile.pk if profile is not None and profile.company_id == company_id else None,
        })
        context["whats_next"] = request.GET.get("whats_next") == "1"
        context["reassign_form"] = ReassignForm(company_id=company_id, keep=next_task.assignee_id if next_task else None,
                                                initial={
            "assignee": next_task.assignee_id if next_task else None})

        # 2. Deal facts.
        stages = list(
            Stage.objects.filter(company_id=company_id, is_active=True, pipeline__is_active=True)
            .select_related("pipeline").order_by("pipeline__sort_order", "pipeline__name", "sort_order")
        )
        context["stepper"] = [s for s in stages if s.pipeline_id == eng.pipeline_id]
        other_groups = OrderedDict()
        for stage in stages:
            if stage.pipeline_id != eng.pipeline_id:
                other_groups.setdefault(stage.pipeline.name, []).append(stage)
        context["other_pipeline_stages"] = list(other_groups.items())
        context["time_in_stage"] = (now - eng.stage_entered_at) if eng.stage_entered_at else None
        context["probability"] = eng.effective_probability
        context["probability_inherited"] = eng.probability is None and eng.effective_probability is not None
        context["weighted_value"] = eng.weighted_value
        context["close_date_past"] = bool(eng.status == OPEN and eng.expected_close_date
                                          and eng.expected_close_date < today)
        context["source_label"] = source_label(eng.source, company_id)
        reasons = loss_reason_labels(company_id)
        context["loss_label"] = reasons.get(eng.loss_reason, eng.loss_reason)
        context["lose_form"] = LoseForm(reason_labels=reasons, initial={
            "loss_reason": request.GET.get("reason", "")})
        context["confirm_close"] = request.GET.get("confirm") if request.GET.get("confirm") in ("won", "lost") else ""
        context["reopen_form"] = ReopenForm(engagement=eng)
        context["can_reopen"] = request.user.has_perm("crm.reopen_engagement") and profile is not None
        context["stale_days"] = stale_days(eng, now)
        # Defined fields inside Deal facts; provider / undefined keys in Other data (5.2).
        context["definition_groups"] = sidecar_forms.definition_groups(eng, request.user)
        context["other_data"] = sidecar_forms.other_data(eng, request.user)

        # 3. Timeline: the first page; "Load older" goes to the timeline page.
        context.update(self._timeline(eng, zone, today))

        # Side column.
        context.update(self._contacts(eng))
        context.update(self._documents(eng))
        context["stage_history"] = list(
            eng.stage_changes.select_related("from_stage", "to_stage", "changed_by")
            .order_by("-changed_at", "-created_at")[:50]
        )
        context["leads"] = list(eng.leads.order_by("-created_at")[:20])
        context["identifiers"] = list(ExternalRef.objects.filter(
            company_id=company_id, content_type=ContentType.objects.get_for_model(Engagement),
            object_id=eng.pk,
        ).order_by("source", "object_type")[:20])
        context["contact_form"] = ContactForm(engagement=eng)
        context["log_form"] = self.log_form(eng, zone, now)
        return context

    def _timeline(self, eng, zone, today):
        kinds = [k for k in (self.request.GET.get("kinds") or "").split(",") if k]
        page, days, upcoming = page_for(
            eng, zone=zone, today=today, kinds=kinds,
            can_view_raw=self.request.user.has_perm("communications.view_raw_communication"),
        )
        return {"timeline_days": days, "timeline_upcoming": upcoming, "timeline_next": page.next_before,
                "kind_chips": KIND_CHIPS, "active_kinds": ",".join(kinds)}

    @staticmethod
    def _contacts(eng):
        rows = list(eng.contacts.select_related("party").order_by("-is_primary", "created_at"))
        people = []
        if eng.party_id and eng.party.kind == "person":
            people.append({"party": eng.party, "role": "Counterparty", "is_primary": not any(
                r.is_primary for r in rows)})
        for row in rows:
            people.append({"party": row.party, "role": row.get_role_display(), "is_primary": row.is_primary,
                           "contact_id": row.pk})
        party_ids = [p["party"].pk for p in people]
        consent = {}
        if party_ids:
            for record in (ConsentRecord.objects.filter(party_id__in=party_ids, channel="email",
                                                        purpose="marketing")
                           .order_by("party_id", "-occurred_at", "-recorded_at")
                           .only("party_id", "state")):
                consent.setdefault(record.party_id, record.state)
        for person in people:
            person["consent"] = consent.get(person["party"].pk, "unknown")
        return {"contacts": people}

    @staticmethod
    def _documents(eng):
        from orders.models import Order, Quote, SalesInvoice

        quotes = list(Quote._base_manager.filter(engagement=eng).order_by("created_at", "revision_number"))
        chains = OrderedDict()
        for quote in quotes:
            root = quote.original_quote_id or quote.pk
            chains.setdefault(root, []).append(quote)
        quote_chains = []
        for revisions in chains.values():
            live = [q for q in revisions if q.status != "superseded" and q.superseded_by_id is None]
            head = max(live or revisions, key=lambda q: q.revision_number)
            quote_chains.append({"head": head, "revisions": len(revisions)})
        orders = list(Order._base_manager.filter(engagement=eng).order_by("-order_date", "-created_at"))
        invoices = list(SalesInvoice._base_manager.filter(order__engagement=eng)
                        .select_related("order").order_by("-created_at"))
        open_quotes = [q for q in quotes if q.status in OPEN_QUOTE_STATUSES and not q.converted_to_order_id
                       and q.superseded_by_id is None]
        return {"quote_chains": quote_chains, "orders": orders, "invoices": invoices,
                "open_quotes_banner": eng.status == Engagement.Status.WON and bool(open_quotes),
                "open_quotes": open_quotes}

    @staticmethod
    def contact_points(eng):
        """``(value, party, address, label)`` for the log form's participants."""
        party_ids = [eng.party_id] if eng.party_id else []
        party_ids += list(eng.contacts.values_list("party_id", flat=True))
        if not party_ids:
            return []
        points = []
        for row in PartyEmail.objects.filter(party_id__in=party_ids).select_related("party") \
                .order_by("-is_primary", "email_normalized")[:20]:
            points.append((f"e:{row.pk}", row.party, row.email, f"{row.party.display_name} · {row.email}"))
        for row in PartyPhone.objects.filter(party_id__in=party_ids).select_related("party") \
                .order_by("-is_primary", "number_raw")[:20]:
            if row.number_e164 or row.number_raw:
                address = row.number_e164 or row.number_raw
                points.append((f"p:{row.pk}", row.party, address, f"{row.party.display_name} · {row.number_raw}"))
        return points

    def log_form(self, eng, zone, now, data=None):
        profile = viewer_profile(self.request)
        return LogCommunicationForm(
            data, company_id=eng.company_id, contact_points=self.contact_points(eng), zone=zone,
            initial={"kind": "call", "direction": "outbound",
                     "occurred_at": now.astimezone(zone).strftime("%Y-%m-%dT%H:%M"),
                     "next_assignee": profile.pk if profile is not None and profile.company_id == eng.company_id
                     else None},
        )


class TimelineView(ERPViewMixin, TemplateView):
    """``/erp/crm/engagements/<uuid>/timeline/``: the timeline alone, paged
    with ``?before=<time>|<kind>|<id>`` and filtered with ``?kinds=``."""

    template_name = "erp/crm/timeline.html"
    current_section = SECTION_ENGAGEMENTS
    entity_label_key = "crm.engagement"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        eng = _scoped_engagement(self.request, self.kwargs["pk"])
        zone = viewer_zone(self.request)
        now = timezone.now()
        today = local_today(zone, now)
        kinds = [k for k in (self.request.GET.get("kinds") or "").split(",") if k]
        before = self.request.GET.get("before") or None
        can_raw = self.request.user.has_perm("communications.view_raw_communication")
        try:
            page, days, upcoming = page_for(eng, zone=zone, today=today, before=before, kinds=kinds,
                                            can_view_raw=can_raw)
        except CursorError:
            messages.warning(self.request, "That page link is not valid; showing the newest items.")
            before = None
            page, days, upcoming = page_for(eng, zone=zone, today=today, kinds=kinds, can_view_raw=can_raw)
        context.update(
            engagement=eng, viewer_tz=zone.key, page_title=f"Timeline · {eng.number}",
            timeline_days=days, timeline_upcoming=upcoming if before is None else None,
            timeline_next=page.next_before, kind_chips=KIND_CHIPS, active_kinds=",".join(kinds),
            is_older_page=before is not None,
        )
        return context


# --- thread actions (POST only, through the services) ------------------------------


class _EngagementAction(ERPViewMixin, View):
    anchor = ""

    def post(self, request, pk):
        eng = _scoped_engagement(request, pk)
        self.profile = viewer_profile(request)
        if self.profile is not None and self.profile.company_id != eng.company_id:
            self.profile = None  # a superuser acting in another tenant: no cross-tenant "by"
        try:
            response = self.act(request, eng)
        except ValidationError as exc:
            service_errors(request, exc)
            response = None
        except PermissionDenied as exc:
            # An authorization refusal (a role without the permission) is
            # the styled 403, as the hidden action's direct URL must be
            # (release-hardening-plan decision 10). A business-rule refusal
            # ("needs a staff profile") is a ValidationError: a message.
            return forbidden(request, str(exc))
        return response or redirect(safe_next(request, thread_url(eng, self.anchor)))

    def act(self, request, eng):
        raise NotImplementedError


class EngagementMoveView(_EngagementAction):
    anchor = "#deal-facts"

    def act(self, request, eng):
        form = MoveForm(request.POST, company_id=eng.company_id)
        if not form.is_valid():
            messages.error(request, "Pick a stage.")
            return None
        stage, note = form.cleaned_data["stage"], form.cleaned_data["note"]
        if stage.pipeline_id != eng.pipeline_id:
            engagement_service.change_pipeline(eng, stage.pipeline, stage, by=self.profile, source=ERP, note=note)
            hint = stage.outcome_hint
        else:
            hint = engagement_service.move(eng, stage, by=self.profile, note=note, source=ERP).needs_close
        if hint:
            # Never close silently (spec 1.4): ask, with the close form open.
            messages.info(request, f"{stage.name} proposes closing this as {hint}. Confirm below, or leave it open.")
            return redirect(thread_url(eng, "") + f"?confirm={hint}#close")
        messages.success(request, f"Moved to {stage.name}.")
        return None


class EngagementWinView(_EngagementAction):
    def act(self, request, eng):
        engagement_service.win(eng, by=self.profile, source=ERP, note=request.POST.get("note", "")[:255])
        messages.success(request, "Marked as won." + (" It still has open tasks: complete, cancel or keep them."
                                                      if eng.next_task_id else ""))


class EngagementLoseView(_EngagementAction):
    def act(self, request, eng):
        form = LoseForm(request.POST, reason_labels=loss_reason_labels(eng.company_id))
        if not form.is_valid():
            messages.error(request, "Not closed: pick a loss reason.")
            return redirect(thread_url(eng) + "?confirm=lost#close")
        data = form.cleaned_data
        engagement_service.lose(eng, data["loss_reason"], by=self.profile, competitor=data["loss_competitor"],
                                notes=data["loss_notes"], source=ERP)
        messages.success(request, "Marked as lost.")


class EngagementReopenView(_EngagementAction):
    def act(self, request, eng):
        if not request.user.has_perm("crm.reopen_engagement"):
            raise PermissionDenied("Reopening an engagement needs the crm.reopen_engagement permission.")
        if self.profile is None:
            raise ValidationError("Not reopened: your account has no staff profile in this company.")
        form = ReopenForm(request.POST, engagement=eng)
        if not form.is_valid():
            raise ValidationError("Not reopened: pick an active stage of this pipeline.")
        stage = form.cleaned_data["stage"]
        engagement_service.reopen(eng, by=self.profile, stage=stage, source=ERP)
        messages.success(request, "Reopened.")


class EngagementCloseRemainingView(_EngagementAction):
    """On a closed engagement: cancel every open task ("close remaining")."""

    anchor = "#next-action"

    def act(self, request, eng):
        if eng.status == OPEN:
            raise ValidationError("Only a closed engagement closes its remaining tasks here.")
        tasks = list(eng.tasks.filter(status__in=OPEN_TASK_STATUSES))
        with transaction.atomic():
            for task in tasks:
                task_service.cancel(task, by=self.profile)
        messages.success(request, f"Canceled {len(tasks)} open task{'s' if len(tasks) != 1 else ''}.")


class TaskCreateView(_EngagementAction):
    """The next-action form ("what, due, who"): ``crm.tasks.create``."""

    anchor = "#next-action"

    def act(self, request, eng):
        form = TaskForm(request.POST, company_id=eng.company_id, user=request.user)
        if not form.is_valid():
            for field, errors in form.errors.items():
                messages.error(request, f"{form.fields[field].label if field in form.fields else ''}: {errors[0]}"
                               .lstrip(": "))
            return None
        data = form.cleaned_data
        with transaction.atomic():
            task = task_service.create(engagement=eng, title=data["title"], kind=data["kind"],
                                       priority=data["priority"], assignee=data["assignee"], by=self.profile,
                                       due=form.due(), source="erp")
            form.save_definition_fields(task, by=request.user)
        messages.success(request, "Next action set." if eng.next_task_id is None else "Task added.")


class ContactCreateView(_EngagementAction):
    anchor = "#contacts"

    def act(self, request, eng):
        form = ContactForm(request.POST, engagement=eng)
        if not form.is_valid():
            for errors in form.errors.values():
                messages.error(request, errors[0])
            return None
        data = form.cleaned_data
        with transaction.atomic():
            if data["is_primary"]:
                eng.contacts.filter(is_primary=True).update(is_primary=False)
            contact = EngagementContact(engagement=eng, party=data["party"], role=data["role"],
                                        is_primary=data["is_primary"])
            contact.full_clean(exclude=["company"])
            contact.save()
        messages.success(request, f"Added {data['party'].display_name}.")


class ContactRemoveView(ERPViewMixin, View):
    def post(self, request, pk):
        contact = get_object_or_404(EngagementContact.objects.for_request(request).select_related("engagement"),
                                    pk=pk)
        contact.delete()
        messages.success(request, "Removed.")
        return redirect(thread_url(contact.engagement, "#contacts"))


class LogCommunicationView(ERPViewMixin, View):
    """``ingest.record`` with ``provider=manual``, then (optionally) the next
    action in the same step. GET shows the full-page form; the thread view
    embeds the same form."""

    template_name = "erp/crm/log_form.html"

    def _render(self, request, eng, form, status=200):
        return render(request, self.template_name, {
            "engagement": eng, "form": form, "current_section": SECTION_ENGAGEMENTS,
            "page_title": f"Log a communication · {eng.number}", "viewer_tz": viewer_zone(request).key,
        }, status=status)

    def get(self, request, pk):
        eng = _scoped_engagement(request, pk)
        view = EngagementDetailView()
        view.request = request
        return self._render(request, eng, view.log_form(eng, viewer_zone(request), timezone.now()))

    def post(self, request, pk):
        eng = _scoped_engagement(request, pk)
        view = EngagementDetailView()
        view.request = request
        zone = viewer_zone(request)
        form = view.log_form(eng, zone, timezone.now(), data=request.POST)
        if not form.is_valid():
            return self._render(request, eng, form, status=400)
        data = form.cleaned_data
        profile = viewer_profile(request)
        performer = profile if profile is not None and profile.company_id == eng.company_id else None
        minutes = data.get("duration_minutes")
        try:
            with transaction.atomic():
                ingest.record(
                    eng.company, data["kind"], direction=data["direction"], occurred_at=data["occurred_at"],
                    participants=form.participant_specs(), provider=ingest.MANUAL,
                    subject=data["subject"], body_text=data["body_text"],
                    duration_seconds=minutes * 60 if minutes else None,
                    engagement=eng, party=eng.party, performed_by=performer,
                    metadata={"logged_from": "erp"},
                )
                # ingest.record moved the engagement's last_activity_at.
                if data.get("next_title"):
                    task_service.create(engagement=eng, title=data["next_title"], kind="follow_up",
                                        assignee=data.get("next_assignee"), by=performer,
                                        due=data.get("next_due"), source="erp")
        except (ingest.IngestError, ValidationError) as exc:
            form.add_error(None, "; ".join(getattr(exc, "messages", [str(exc)])))
            return self._render(request, eng, form, status=400)
        messages.success(request, "Logged." + (" Task added." if data.get("next_title") else ""))
        return redirect(thread_url(eng, "#timeline"))


# --- engagement create / edit ------------------------------------------------------


class _EngagementFormView(ERPViewMixin, View):
    template_name = "erp/crm/engagement_form.html"
    current_section = SECTION_ENGAGEMENTS

    def _render(self, request, form, title, engagement=None, status=200):
        return render(request, self.template_name, {
            "form": form, "engagement": engagement, "page_title": title,
            "current_section": self.current_section,
        }, status=status)


class EngagementCreateView(_EngagementFormView):
    def _company(self, request):
        company_id = viewer_company_id(request)
        if company_id is None:
            raise PermissionDenied("Your account has no company, so an engagement cannot be created.")
        return company_id

    def _title(self, request):
        return "New " + get_label("crm.engagement", company=labels_for_request(request).company_id).singular.lower()

    def get(self, request):
        company_id = self._company(request)
        profile = viewer_profile(request)
        initial = {"currency": "USD", "party": request.GET.get("party", ""),
                   "owner": profile.pk if profile is not None and profile.company_id == company_id else None}
        return self._render(request, EngagementForm(company_id=company_id, initial=initial, user=request.user),
                            self._title(request))

    def post(self, request):
        from core.models import Company

        company_id = self._company(request)
        form = EngagementForm(request.POST, company_id=company_id, user=request.user)
        if not form.is_valid():
            return self._render(request, form, self._title(request), status=400)
        data = form.cleaned_data
        try:
            with transaction.atomic():
                eng = engagement_service.create(
                    company=Company.objects.get(pk=company_id), party=data["party"], pipeline=data.get("pipeline"),
                    owner=data["owner"], by=viewer_profile(request), source=ERP, origin=Engagement.Origin.ERP,
                    title=data["title"], engagement_source=data["source"], expected_value=data["expected_value"],
                    currency=data["currency"], probability=data["probability"],
                    expected_close_date=data["expected_close_date"], tags=data["tags_text"],
                )
                team = data.get("owner_team")
                if "owner_team" in form.fields and team is not None and team.pk != eng.owner_team_id:
                    engagement_service.reassign(eng, eng.owner, owner_team=team)
                form.save_definition_fields(eng, by=request.user)
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
            return self._render(request, form, self._title(request), status=400)
        messages.success(request, f"Created {eng.number}. Set its next action.")
        return redirect(thread_url(eng, "#next-action"))


class EngagementUpdateView(_EngagementFormView):
    """The deal's facts. Stage and outcome are actions, not fields here."""

    def get(self, request, pk):
        eng = _scoped_engagement(request, pk)
        return self._render(request, EngagementForm(company_id=eng.company_id, instance=eng, user=request.user),
                            f"Edit {eng.number}", eng)

    def post(self, request, pk):
        eng = _scoped_engagement(request, pk)
        form = EngagementForm(request.POST, company_id=eng.company_id, instance=eng, user=request.user)
        if not form.is_valid():
            return self._render(request, form, f"Edit {eng.number}", eng, status=400)
        data = form.cleaned_data
        if data["owner"] != eng.owner and not can_change_owner(request.user, viewer_profile(request), eng,
                                                                data["owner"]):
            form.add_error("owner", "Changing another user's engagement owner needs crm.reassign_engagement.")
            return self._render(request, form, f"Edit {eng.number}", eng, status=403)
        eng.title = data["title"] or ""
        eng.party = data["party"]
        eng.owner = data["owner"]
        eng.expected_value = data["expected_value"]
        eng.currency = data["currency"]
        eng.probability = data["probability"]
        eng.expected_close_date = data["expected_close_date"]
        eng.source = data["source"]
        eng.tags = data["tags_text"]
        fields = ["title", "party", "owner", "expected_value", "currency", "probability",
                  "expected_close_date", "source", "tags", "updated_at"]
        if "owner_team" in form.fields and "owner_team" in request.POST and "owner_team" in form.changed_data:
            # A named team is a direct edit (2.1), kept over the stamp; blank
            # ("From the owner") takes the owner's team now.
            team = data["owner_team"]
            eng.owner_team_id = team.pk if team is not None else team_of(eng.owner, eng.company_id)
            fields.append("owner_team")
        try:
            eng.full_clean(exclude=["number", "title"])
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
            return self._render(request, form, f"Edit {eng.number}", eng, status=400)
        try:
            with transaction.atomic():
                eng.save(update_fields=fields)
                form.save_definition_fields(eng, by=request.user)
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
            return self._render(request, form, f"Edit {eng.number}", eng, status=400)
        messages.success(request, "Saved.")
        return redirect(thread_url(eng))


# --- tasks -------------------------------------------------------------------------


def _scoped_task(request, pk):
    return get_object_or_404(Task.objects.for_request(request).select_related("engagement", "assignee"), pk=pk)


def _task_back(request, task, whats_next=False):
    if task.engagement_id:
        default = thread_url(task.engagement, "") + ("?whats_next=1" if whats_next else "") + "#next-action"
    else:
        default = reverse("erp-crm-task-list")
    return safe_next(request, default)


class _TaskAction(ERPViewMixin, View):
    def post(self, request, pk):
        task = _scoped_task(request, pk)
        self.profile = viewer_profile(request)
        if self.profile is not None and self.profile.company_id != task.company_id:
            self.profile = None  # a superuser acting in another tenant: no cross-tenant "by"
        whats_next = False
        try:
            whats_next = bool(self.act(request, task))
        except ValidationError as exc:
            service_errors(request, exc)
        return HttpResponseRedirect(_task_back(request, task, whats_next))


class TaskCompleteView(_TaskAction):
    def act(self, request, task):
        task_service.complete(task, by=self.profile)
        messages.success(request, f"Completed: {task.title}. What's next?" if task.engagement_id
                         else f"Completed: {task.title}.")
        return task.engagement_id is not None


class TaskReopenView(_TaskAction):
    def act(self, request, task):
        task_service.reopen(task, by=self.profile)
        messages.success(request, f"Reopened: {task.title}.")


class TaskCancelView(_TaskAction):
    def act(self, request, task):
        task_service.cancel(task, by=self.profile)
        messages.success(request, f"Canceled: {task.title}.")


class TaskRescheduleView(_TaskAction):
    """Snooze: tomorrow, next week (Monday) or a picked date, all-day in the
    zone the task was set in."""

    def act(self, request, task):
        form = RescheduleForm(request.POST)
        if not form.is_valid():
            raise ValidationError("Pick when.")
        zone = ZoneInfo(task.due_tz) if tz.is_valid_timezone(task.due_tz) else viewer_zone(request)
        day = snooze_date(form.cleaned_data["when"], local_today(zone), form.cleaned_data.get("date"))
        task_service.reschedule(task, day, by=self.profile)
        messages.success(request, f"Moved to {day:%a %b} {day.day}: {task.title}.")


class TaskReassignView(_TaskAction):
    def act(self, request, task):
        form = ReassignForm(request.POST, company_id=task.company_id, keep=task.assignee_id)
        if not form.is_valid():
            raise ValidationError("Pick someone in this company.")
        task_service.assign(task, form.cleaned_data["assignee"], by=self.profile)
        messages.success(request, "Reassigned.")


def _create_company(request, noun):
    """The tenant a new row lands in (``viewer_company_id``), else 403."""
    company_id = viewer_company_id(request)
    if company_id is None:
        raise PermissionDenied(f"Your account has no company, so a {noun} cannot be created.")
    return company_id


def _same_tenant_profile(request, company_id):
    """The viewer's profile when it belongs to ``company_id`` (no cross-tenant "by")."""
    profile = viewer_profile(request)
    return profile if profile is not None and profile.company_id == company_id else None


class _CreatePage(ERPViewMixin, View):
    """A create screen on ``erp/crm/create_form.html``: ``form_sections`` lists
    the fact fields by section; definition fieldsets (if any) follow.
    ``intro`` picks the template's explanatory line (it names label-layer
    nouns, so it lives in the template)."""

    template_name = "erp/crm/create_form.html"
    label_key = ""
    form_sections: list = []
    intro = ""

    def title(self, request, company_id):
        label_company = labels_for_request(request).company_id or company_id
        return "New " + get_label(self.label_key, company=label_company).singular.lower()

    def cancel_url(self, request):
        raise NotImplementedError

    def render_form(self, request, form, company_id, status=200):
        return render(request, self.template_name, {
            "form": form, "page_title": self.title(request, company_id), "current_section": self.current_section,
            "form_sections": [(title, [form[name] for name in names if name in form.fields])
                              for title, names in self.form_sections],
            "intro": self.intro, "cancel_url": self.cancel_url(request),
            "next": safe_next(request, ""),
        }, status=status)


class TaskNewView(_CreatePage):
    """``/erp/crm/tasks/new/``: a task from the task list, on an engagement,
    a party, or neither (``crm.tasks.create``). Back to the list after."""

    current_section = SECTION_TASKS
    label_key = "crm.task"
    intro = "task"
    form_sections = [("Task", ["title", "kind", "due_date", "due_time", "assignee", "priority"]),
                     ("Related to", ["engagement", "party"])]

    def cancel_url(self, request):
        return safe_next(request, reverse("erp-crm-task-list"))

    def form(self, request, company_id, data=None, initial=None, keep=None):
        label_company = labels_for_request(request).company_id or company_id
        return NewTaskForm(data, company_id=company_id, user=request.user, initial=initial, keep=keep,
                           kind_labels=task_kind_labels(label_company),
                           engagement_label=get_label("crm.engagement", company=label_company).singular)

    def get(self, request):
        company_id = _create_company(request, "task")
        profile = _same_tenant_profile(request, company_id)
        keep = _uuids([request.GET.get("engagement", "")])
        keep = keep[0] if keep and Engagement.objects.filter(company_id=company_id, pk=keep[0]).exists() else None
        initial = {"assignee": profile.pk if profile is not None else None, "engagement": keep,
                   "party": request.GET.get("party", "")}
        return self.render_form(request, self.form(request, company_id, initial=initial, keep=keep), company_id)

    def post(self, request):
        from core.models import Company

        company_id = _create_company(request, "task")
        keep = _uuids([request.POST.get("engagement", "")])
        form = self.form(request, company_id, data=request.POST, keep=keep[0] if keep else None)
        if not form.is_valid():
            return self.render_form(request, form, company_id, status=400)
        data = form.cleaned_data
        engagement = data.get("engagement")
        try:
            with transaction.atomic():
                task = task_service.create(
                    company=Company.objects.get(pk=company_id), engagement=engagement, party=data.get("party"),
                    title=data["title"], kind=data["kind"], priority=data["priority"], assignee=data["assignee"],
                    by=_same_tenant_profile(request, company_id), due=form.due(), source="erp",
                )
                form.save_definition_fields(task, by=request.user)
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
            return self.render_form(request, form, company_id, status=400)
        where = f" on {engagement.number}" if engagement is not None else ""
        messages.success(request, f"Task added{where}: {task.title}.")
        return redirect(safe_next(request, reverse("erp-crm-task-list")))


TASK_STATUS_FILTERS = (("open", "Open"), ("done", "Done"), ("cancelled", "Canceled"), ("all", "All"))
TASK_DUE_FILTERS = (("", "Any time"), ("overdue", "Overdue"), ("today", "Due today"), ("week", "Due this week"))


class TaskListView(ERPListView):
    """``/erp/crm/tasks/``: default everyone's open tasks, by due ("Mine" is a
    filter). Defaulting to mine hid unassigned and colleagues' tasks, so a
    new list looked empty (bug batch 2026-10-15)."""

    model = Task
    template_name = "erp/crm/task_list.html"
    current_section = SECTION_TASKS
    page_title = "Tasks"
    entity_name = "Task"
    entity_label_key = "crm.task"
    page_title_format = "{plural}"
    paginate_by = 50
    list_id = "crm.task"
    definition_object = "task"
    csv_canonical = True
    # ``due`` / ``created`` / ``completed``: the Phase 3 sort ids, kept.
    sortable_fields = {"due_at": ("due_at",), "created_at": ("created_at",), "completed_at": ("completed_at",),
                       "priority": ("priority",)}
    sort_aliases = {"due": "due_at", "created": "created_at", "completed": "completed_at"}
    # 5.1: Task 1 · Related 2 · Due 1 · Assignee 2 · Priority 3 · Status 3.
    list_columns = [
        Column("title", "Task", path="title", priority=1),
        Column("related", "Related", priority=2),
        Column("due_at", "Due", path="due_at", priority=1),
        Column("assignee", "Assignee", priority=2),
        Column("priority", "Priority", path="priority", priority=3),
        Column("status", "Status", path="status", priority=3),
        Column("created_at", "Created", path="created_at", priority=3, default=False, format="date"),
        Column("completed_at", "Completed", path="completed_at", priority=3, default=False, format="date"),
        Column("created_by", "Created by", priority=3, default=False),
        # Export only.
        Column("id", "id", path="id", export_only=True),
        Column("kind", "kind", path="kind", export_only=True),
        Column("due_tz", "due_tz", path="due_tz", export_only=True),
        Column("assignee_id", "assignee_id", path="assignee_id", export_only=True),
        Column("engagement", "engagement", path="engagement__number", export_only=True),
        Column("party_id", "party_id", path="party_id", export_only=True),
    ]

    def export_value(self, obj, col):
        if col.id == "related":
            return obj.engagement.number if obj.engagement_id else (obj.party.display_name if obj.party_id else "")
        if col.id == "assignee":
            return profile_label(obj.assignee) if obj.assignee_id else ""
        if col.id == "created_by":
            return profile_label(obj.created_by) if obj.created_by_id else ""
        return super().export_value(obj, col)

    def get_filter_params(self):
        return {"q": None, "assignee": None, "status": tuple(dict(TASK_STATUS_FILTERS)),
                "due": tuple(k for k in dict(TASK_DUE_FILTERS) if k)}

    def get_queryset(self):
        params = self.request.GET
        now = self.now = timezone.now()
        qs = Task.objects.for_request(self.request).select_related(
            "engagement", "party", "assignee__user", "created_by__user")
        profile = viewer_profile(self.request)
        assignee = params.get("assignee", "all")
        self.assignee = assignee
        if assignee == "me":
            qs = qs.filter(assignee=profile) if profile else qs.none()
        elif assignee == "none":
            qs = qs.filter(assignee__isnull=True)
        elif assignee != "all":
            try:
                qs = qs.filter(assignee_id=assignee)
            except ValidationError:
                qs = qs.none()
        status = params.get("status", "open")
        self.status = status if status in dict(TASK_STATUS_FILTERS) else "open"
        if self.status == "open":
            qs = qs.filter(status__in=OPEN_TASK_STATUSES)
        elif self.status != "all":
            qs = qs.filter(status=self.status)
        due = params.get("due", "")
        self.due = due if due in dict(TASK_DUE_FILTERS) else ""
        if self.due == "overdue":
            qs = qs.filter(status__in=OPEN_TASK_STATUSES, due_at__lt=now)
        elif self.due in ("today", "week"):
            zone = viewer_zone(self.request)
            start = datetime.datetime.combine(local_today(zone, now), datetime.time.min, tzinfo=zone)
            end = start + datetime.timedelta(days=1 if self.due == "today" else 7)
            qs = qs.filter(status__in=OPEN_TASK_STATUSES, due_at__gte=start, due_at__lt=end)
        text = params.get("q", "").strip()
        if text:
            qs = qs.filter(title__icontains=text)
        qs = qs.order_by(F("due_at").asc(nulls_last=True), "created_at", "pk")
        return self.apply_sort(qs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        now = self.now
        labels = task_kind_labels(labels_for_request(self.request).company_id)
        for task in context["object_list"]:
            task.overdue = task.is_open and task.due_at is not None and task.due_at < now
            task.kind_label = labels.get(task.kind, task.kind)
        company_id = viewer_company_id(self.request)
        people = filter_entry("assignee", "Assignee", owner_filter_choices(company_id))
        context.update(
            assignee_filter=self.assignee, status_filter=self.status, due_filter=self.due,
            assignee_choices=[("me", "Mine"), ("none", "Unassigned"), ("all", "Everyone")] + people["choices"],
            assignee_options=[("me", "Mine"), ("none", "Unassigned"), ("all", "Everyone")] + people["options"],
            assignee_groups=people["groups"],
            status_choices=TASK_STATUS_FILTERS, due_choices=TASK_DUE_FILTERS,
            search_query=self.request.GET.get("q", ""), viewer_tz=viewer_zone(self.request).key,
        )
        params = self.request.GET
        context.update(self.get_list_status(
            context,
            [{"field": "assignee", "label": "Assignee", "choices": context["assignee_choices"]},
             {"field": "status", "label": "Status", "choices": TASK_STATUS_FILTERS},
             {"field": "due", "label": "Due", "choices": TASK_DUE_FILTERS}],
            {"assignee": self.assignee, "status": self.status, "due": self.due},
            context["current_sort"], context["current_sort_dir"],
            filtered=any(params.get(k) for k in ("q", "assignee", "status", "due")),
        ))
        kept = params.copy()
        kept.pop("page", None)
        kept.pop("export", None)
        context["page_query"] = kept.urlencode()
        return context


# --- 6.4 My day --------------------------------------------------------------------


class MyDayView(ERPViewMixin, TemplateView):
    """Overdue, Today (before the end of my local day), Needs a next action."""

    template_name = "erp/crm/my_day.html"
    current_section = SECTION_MY_DAY
    page_title = "My day"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        profile = viewer_profile(self.request)
        context["profile"] = profile
        if profile is None or profile.company_id is None:
            return context
        zone = viewer_zone(self.request)
        now = timezone.now()
        today = local_today(zone, now)
        end = datetime.datetime.combine(today, datetime.time.min, tzinfo=zone) + datetime.timedelta(days=1)
        tasks = Task.objects.filter(company_id=profile.company_id, assignee=profile,
                                    status__in=OPEN_TASK_STATUSES).select_related("engagement__party")
        overdue = list(tasks.filter(due_at__lt=now).order_by("due_at", "pk")[:LIST_LIMIT])
        due_today = list(tasks.filter(due_at__gte=now, due_at__lt=end).order_by("due_at", "pk")[:LIST_LIMIT])
        idle = list(
            Engagement.objects.filter(company_id=profile.company_id, owner=profile, status=OPEN,
                                      next_task__isnull=True)
            .select_related("stage", "party").order_by("created_at", "pk")[:LIST_LIMIT]
        )
        labels = task_kind_labels(profile.company_id)
        for task in overdue + due_today:
            task.kind_label = labels.get(task.kind, task.kind)
            task.set_in = task.due_tz if task.due_tz and task.due_tz != zone.key else ""
        context.update(overdue=overdue, due_today=due_today, idle=idle, viewer_tz=zone.key, today=today,
                       now=now, all_clear=not (overdue or due_today or idle))
        return context


# --- leads (6.5, read-mostly) ------------------------------------------------------


def lead_status(lead):
    """``(key, text)``: the lead's state, derived from its engagement (5.1).
    Lead's own workflow columns are frozen (Phase 3 1.7)."""
    eng = lead.engagement if lead.engagement_id else None
    if eng is None:
        return ("support", "Support") if lead.request_type == "support" else ("unthreaded", "No engagement")
    if eng.status == Engagement.Status.OPEN:
        return "open", "Open" + (f" · {eng.stage.name}" if eng.stage_id else "")
    return eng.status, eng.get_status_display()


class LeadListView(ERPListView):
    """``/erp/crm/leads/``: the intake log; each row links to its engagement."""

    model = Lead
    template_name = "erp/crm/lead_list.html"
    current_section = SECTION_LEADS
    page_title = "Leads"
    entity_name = "Lead"
    entity_label_key = "crm.lead"
    page_title_format = "{plural}"
    paginate_by = 50
    list_id = "crm.lead"
    csv_canonical = True
    ordering = ["-created_at"]
    sortable_fields = {"created_at": ("created_at",), "lead_number": ("lead_number",)}
    sort_aliases = {"created": "created_at", "number": "lead_number"}
    # 5.1: Received 1 · Name 1 · Email 2 · Source 2 · Request type 3 · Engagement 2 · Status 3.
    list_columns = [
        Column("created_at", "Received", path="created_at", priority=1),
        Column("name", "Name", priority=1),
        Column("email", "Email", path="email", priority=2, pii=True),
        Column("source", "Source", path="source", priority=2),
        Column("request_type", "Request type", path="request_type", priority=3),
        Column("engagement", "Engagement", path="engagement__number", priority=2),
        Column("status", "Status", priority=3),
        Column("lead_number", "Lead", path="lead_number", priority=3, default=False),
        Column("company_name", "Company", path="company_name", priority=3, default=False),
        # Export only.
        Column("id", "id", path="id", export_only=True),
        Column("first_name", "first_name", path="first_name", export_only=True),
        Column("last_name", "last_name", path="last_name", export_only=True),
    ]

    def export_value(self, obj, col):
        if col.id == "name":
            return f"{obj.first_name} {obj.last_name}".strip()
        if col.id == "status":
            return lead_status(obj)[0]
        return super().export_value(obj, col)

    def get_filter_params(self):
        return {"q": None, "source": tuple(Lead.Source.values), "request_type": tuple(Lead.RequestType.values)}

    def get_queryset(self):
        params = self.request.GET
        qs = Lead.objects.for_request(self.request).select_related("engagement__stage")
        source = params.get("source")
        if source in Lead.Source.values:
            qs = qs.filter(source=source)
        kind = params.get("request_type")
        if kind in Lead.RequestType.values:
            qs = qs.filter(request_type=kind)
        text = params.get("q", "").strip()
        if text:
            qs = qs.filter(Q(lead_number__iexact=text) | Q(email__iexact=text) | Q(last_name__istartswith=text)
                           | Q(company_name__istartswith=text))
        return self.apply_sort(qs.order_by("-created_at", "pk"))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        params = self.request.GET
        for lead in context["object_list"]:
            lead.status_key, lead.status_text = lead_status(lead)
        context.update(
            source_choices=Lead.Source.choices, request_choices=Lead.RequestType.choices,
            active={"source": params.get("source", ""), "request_type": params.get("request_type", "")},
            search_query=params.get("q", ""),
        )
        context.update(self.get_list_status(
            context,
            [{"field": "source", "label": "Source", "choices": Lead.Source.choices},
             {"field": "request_type", "label": "Request", "choices": Lead.RequestType.choices}],
            context["active"], context["current_sort"], context["current_sort_dir"],
        ))
        kept = params.copy()
        kept.pop("page", None)
        kept.pop("export", None)
        context["page_query"] = kept.urlencode()
        return context


class LeadCreateView(_CreatePage):
    """``/erp/crm/leads/new/``: a lead typed in by staff. ``form.save()`` is
    ``Lead.save()``, so intake runs as for a web form (``crm.intake``); off
    to the engagement it produced, else back to the list."""

    current_section = SECTION_LEADS
    label_key = "crm.lead"
    intro = "lead"
    form_sections = [
        ("Who", ["first_name", "last_name", "email", "phone", "company_name", "job_title"]),
        ("Request", ["source", "source_detail", "request_type", "estimated_value", "project_timeline",
                     "assigned_to", "message"]),
        ("Location", ["city", "state_province", "postal_code", "country"]),
    ]

    def cancel_url(self, request):
        return reverse("erp-crm-lead-list")

    def get(self, request):
        company_id = _create_company(request, "lead")
        return self.render_form(request, LeadForm(company_id=company_id), company_id)

    def post(self, request):
        company_id = _create_company(request, "lead")
        form = LeadForm(request.POST, company_id=company_id)
        if not form.is_valid():
            return self.render_form(request, form, company_id, status=400)
        try:
            lead = form.save()
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
            return self.render_form(request, form, company_id, status=400)
        if lead.engagement_id:
            messages.success(request, f"Added {lead.lead_number}, on {lead.engagement.number}.")
            return redirect(thread_url(lead.engagement, "#next-action"))
        messages.success(request, f"Added {lead.lead_number}.")
        return redirect("erp-crm-lead-list")


# --- pipelines admin (6.5, crm.manage_pipelines) -----------------------------------


class _PipelineAdmin(ERPViewMixin):
    current_section = SECTION_PIPELINES

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated and not request.user.has_perm("crm.manage_pipelines"):
            raise PermissionDenied("Editing pipelines needs the crm.manage_pipelines permission.")
        return super().dispatch(request, *args, **kwargs)


class PipelineListView(ERPViewMixin, TemplateView):
    """Every staff user reads the pipelines (as in the API); the edit
    controls show, and the write routes answer, only with
    ``crm.manage_pipelines``."""

    template_name = "erp/crm/pipeline_list.html"
    current_section = SECTION_PIPELINES
    entity_label_key = "crm.pipeline"
    page_title = "Pipelines"
    page_title_format = "{plural}"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["pipelines"] = list(
            Pipeline.objects.for_request(self.request).order_by("sort_order", "name")
            .prefetch_related(Prefetch("stages", queryset=Stage.objects.order_by("sort_order", "key"),
                                       to_attr="ordered_stages"))
        )
        for pipeline in context["pipelines"]:
            for stage in pipeline.ordered_stages:
                stage.effective_stale = stage.stale_after_days if stage.stale_after_days is not None \
                    else pipeline.stale_after_days
        context["can_manage"] = self.request.user.has_perm("crm.manage_pipelines")
        context["can_create"] = context["can_manage"] and viewer_company_id(self.request) is not None
        return context


class _ModelFormPage(_PipelineAdmin, View):
    template_name = "erp/crm/admin_form.html"

    def _render(self, request, form, title, back, status=200):
        return render(request, self.template_name, {
            "form": form, "page_title": title, "back_url": back, "current_section": self.current_section,
        }, status=status)


class PipelineCreateView(_ModelFormPage):
    def _company(self, request):
        company_id = viewer_company_id(request)
        if company_id is None:
            raise PermissionDenied("Your account has no company.")
        return company_id

    def get(self, request):
        form = PipelineForm(company_id=self._company(request), instance=Pipeline())
        return self._render(request, form, "New pipeline", reverse("erp-crm-pipeline-list"))

    def post(self, request):
        company_id = self._company(request)
        form = PipelineForm(request.POST, company_id=company_id, instance=Pipeline())
        if not form.is_valid():
            return self._render(request, form, "New pipeline", reverse("erp-crm-pipeline-list"), status=400)
        with transaction.atomic():
            pipeline = form.save()
        messages.success(request, f"Created {pipeline.name}. Add its stages.")
        return redirect(reverse("erp-crm-pipeline-list") + f"#pipeline-{pipeline.pk}")


class PipelineUpdateView(_ModelFormPage):
    def get(self, request, pk):
        pipeline = get_object_or_404(Pipeline.objects.for_request(request), pk=pk)
        return self._render(request, PipelineForm(company_id=pipeline.company_id, instance=pipeline),
                            f"Edit {pipeline.name}", reverse("erp-crm-pipeline-list"))

    def post(self, request, pk):
        pipeline = get_object_or_404(Pipeline.objects.for_request(request), pk=pk)
        form = PipelineForm(request.POST, company_id=pipeline.company_id, instance=pipeline)
        if not form.is_valid():
            return self._render(request, form, f"Edit {pipeline.name}", reverse("erp-crm-pipeline-list"),
                                status=400)
        with transaction.atomic():
            if form.cleaned_data.get("is_default"):
                # Lock the tenant's pipelines so two concurrent switches serialise
                # instead of tripping uniq_pipeline_one_default.
                list(Pipeline.objects.select_for_update().filter(company_id=pipeline.company_id))
                Pipeline.objects.filter(company_id=pipeline.company_id, is_default=True).exclude(
                    pk=pipeline.pk).update(is_default=False)
            form.save()
        messages.success(request, "Saved.")
        return redirect(reverse("erp-crm-pipeline-list") + f"#pipeline-{pipeline.pk}")


class StageCreateView(_ModelFormPage):
    def get(self, request, pipeline_pk):
        pipeline = get_object_or_404(Pipeline.objects.for_request(request), pk=pipeline_pk)
        return self._render(request, StageForm(pipeline=pipeline, instance=Stage()),
                            f"New stage · {pipeline.name}", reverse("erp-crm-pipeline-list"))

    def post(self, request, pipeline_pk):
        pipeline = get_object_or_404(Pipeline.objects.for_request(request), pk=pipeline_pk)
        form = StageForm(request.POST, pipeline=pipeline, instance=Stage())
        if not form.is_valid():
            return self._render(request, form, f"New stage · {pipeline.name}",
                                reverse("erp-crm-pipeline-list"), status=400)
        stage = form.save()
        messages.success(request, f"Added {stage.name}.")
        return redirect(reverse("erp-crm-pipeline-list") + f"#pipeline-{pipeline.pk}")


class StageUpdateView(_ModelFormPage):
    def get(self, request, pk):
        stage = get_object_or_404(Stage.objects.for_request(request).select_related("pipeline"), pk=pk)
        return self._render(request, StageForm(pipeline=stage.pipeline, instance=stage),
                            f"Edit {stage.name} · {stage.pipeline.name}", reverse("erp-crm-pipeline-list"))

    def post(self, request, pk):
        stage = get_object_or_404(Stage.objects.for_request(request).select_related("pipeline"), pk=pk)
        form = StageForm(request.POST, pipeline=stage.pipeline, instance=stage)
        if not form.is_valid():
            return self._render(request, form, f"Edit {stage.name} · {stage.pipeline.name}",
                                reverse("erp-crm-pipeline-list"), status=400)
        form.save()
        messages.success(request, "Saved.")
        return redirect(reverse("erp-crm-pipeline-list") + f"#pipeline-{stage.pipeline_id}")


class StageReorderView(_PipelineAdmin, View):
    """POST ``direction=up|down``: swap with the neighbour (keyboard-accessible
    buttons instead of drag and drop)."""

    def post(self, request, pk):
        stage = get_object_or_404(Stage.objects.for_request(request), pk=pk)
        direction = request.POST.get("direction")
        with transaction.atomic():
            siblings = list(Stage.objects.select_for_update().filter(pipeline_id=stage.pipeline_id)
                            .order_by("sort_order", "key"))
            index = next(i for i, s in enumerate(siblings) if s.pk == stage.pk)
            other = index - 1 if direction == "up" else index + 1
            if direction in ("up", "down") and 0 <= other < len(siblings):
                siblings[index], siblings[other] = siblings[other], siblings[index]
                for position, row in enumerate(siblings):
                    wanted = (position + 1) * 10
                    if row.sort_order != wanted:
                        row.sort_order = wanted
                        row.save(update_fields=["sort_order", "updated_at"])
        return redirect(reverse("erp-crm-pipeline-list") + f"#stage-{stage.pk}")
