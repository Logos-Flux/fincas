"""``/api/v1/crm/activities/`` for one release after ActivityLog froze (spec 2.4, E8).

Phase 3 step 6 copied every ``crm.ActivityLog`` row, **with the same id**, to
a ``communications.Communication`` (call / email / meeting / note) or a done
``crm.Task`` (task), marking each with ``metadata.from_activitylog`` and
keeping the lead, customer and raw duration under ``metadata.activitylog``.
This module serves those rows in ActivityLog's JSON shape, so the endpoint's
URL, ids and payloads are unchanged (a lookup, not a mapping table):

- reads list both tables, filtered (``lead``, ``customer``, ``activity_type``,
  ``performed_by``), searched (``subject``) and ordered (``activity_date``)
  like the old viewset, scoped to the requester's company;
- writes create, change or delete a Communication (``provider="manual"``,
  through ``communications.ingest.record``) or a done Task, linked as the
  backfill links them; changing a row between a task and a communication is
  a 400 (they are different tables now);
- every response carries ``Deprecation: true``.

Only rows that came from, or through, this endpoint are served: a golden
document email or a provider import is a communication, never an
"activity". Release N+1 (step 12) removes the endpoint.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.db import transaction
from django.db.models import Q
from rest_framework import serializers

from core.models import Company, UserProfile
from customers.models import Customer

from .backfill import ACTIVITY_COMM_KINDS, ACTIVITY_LINKS, ACTIVITY_MARKER, activity_metadata
from .models import ActivityLog, Lead

TASK = "task"
_HAS_DURATION = ("call", "meeting")
API_TASK_SOURCE = "api:activities"
DOCS = "docs/crm/phase3-engagement.md"


@dataclass
class ActivityRow:
    """One activity in ActivityLog's shape, read from either table."""

    obj: object  # the Communication or Task
    id: object
    created_at: object
    updated_at: object
    lead: object
    customer: object
    activity_type: str
    subject: str
    description: str
    performed_by: object
    activity_date: object
    duration_minutes: object
    metadata: object
    company: object


def _links(metadata):
    links = (metadata or {}).get(ACTIVITY_LINKS) or {}
    return links if isinstance(links, dict) else {}


def _original_metadata(metadata):
    links = _links(metadata)
    if "metadata" in links:
        return links["metadata"]
    return {k: v for k, v in (metadata or {}).items() if k not in (ACTIVITY_MARKER, ACTIVITY_LINKS)}


def row_for_communication(comm) -> ActivityRow:
    links = _links(comm.metadata)
    minutes = links.get("duration_minutes")
    if minutes is None and "duration_minutes" not in links and comm.duration_seconds is not None:
        minutes = comm.duration_seconds // 60
    return ActivityRow(
        obj=comm, id=comm.pk, created_at=comm.created_at, updated_at=comm.updated_at,
        lead=links.get("lead"), customer=links.get("customer"), activity_type=comm.kind,
        subject=comm.subject, description=comm.body_text, performed_by=comm.performed_by_id,
        activity_date=comm.occurred_at, duration_minutes=minutes,
        metadata=_original_metadata(comm.metadata), company=comm.company_id,
    )


def row_for_task(task) -> ActivityRow:
    links = _links(task.metadata)
    return ActivityRow(
        obj=task, id=task.pk, created_at=task.created_at, updated_at=task.updated_at,
        lead=links.get("lead"), customer=links.get("customer"), activity_type=TASK,
        subject=task.title, description=task.description,
        performed_by=task.completed_by_id or task.assignee_id,
        activity_date=task.completed_at or task.created_at,
        duration_minutes=links.get("duration_minutes"),
        metadata=_original_metadata(task.metadata), company=task.company_id,
    )


class ActivityCompatSerializer(serializers.Serializer):
    """ActivityLogSerializer's fields, in its order (``fields = "__all__"``)."""

    id = serializers.UUIDField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)
    lead = serializers.PrimaryKeyRelatedField(queryset=Lead._base_manager.all(), allow_null=True,
                                              required=False)
    customer = serializers.PrimaryKeyRelatedField(queryset=Customer._base_manager.all(),
                                                  allow_null=True, required=False)
    activity_type = serializers.ChoiceField(choices=ActivityLog.ActivityType.choices)
    subject = serializers.CharField(max_length=255)
    description = serializers.CharField(allow_blank=True, required=False)
    performed_by = serializers.PrimaryKeyRelatedField(queryset=UserProfile._base_manager.all(),
                                                      allow_null=True, required=False)
    activity_date = serializers.DateTimeField()
    duration_minutes = serializers.IntegerField(allow_null=True, required=False)
    metadata = serializers.JSONField(required=False)
    company = serializers.PrimaryKeyRelatedField(queryset=Company._base_manager.all(),
                                                 allow_null=True, required=False)

    def to_representation(self, row):
        out = {}
        for name, fld in self.fields.items():
            value = getattr(row, name)
            if value is None:
                out[name] = None
            elif name in ("lead", "customer", "performed_by", "company"):
                # A FK renders as its pk, like the ModelSerializer did.
                out[name] = str(value)
            else:
                out[name] = fld.to_representation(value)
        return out


# --- reads --------------------------------------------------------------------------


def querysets(*, scope):
    """``(communications, tasks)`` this endpoint serves, narrowed by ``scope``
    (a function applying the request's company scoping to a queryset)."""
    from communications.models import Communication

    from .models import Task

    marker = {f"metadata__{ACTIVITY_MARKER}": True}
    comms = scope(Communication._base_manager.filter(kind__in=ACTIVITY_COMM_KINDS, **marker))
    tasks = scope(Task._base_manager.filter(**marker))
    return comms, tasks


def filtered_rows(params, *, scope, lead=None):
    """Every served row matching the old viewset's filters, ordered."""
    validate_params(params)
    comms, tasks = querysets(scope=scope)
    lead = lead if lead is not None else params.get("lead")
    if lead:
        comms = comms.filter(**{f"metadata__{ACTIVITY_LINKS}__lead": str(lead)})
        tasks = tasks.filter(**{f"metadata__{ACTIVITY_LINKS}__lead": str(lead)})
    customer = params.get("customer")
    if customer:
        comms = comms.filter(**{f"metadata__{ACTIVITY_LINKS}__customer": str(customer)})
        tasks = tasks.filter(**{f"metadata__{ACTIVITY_LINKS}__customer": str(customer)})
    performer = params.get("performed_by")
    if performer:
        comms = comms.filter(performed_by_id=performer)
        tasks = tasks.filter(Q(completed_by_id=performer) | Q(assignee_id=performer, completed_by__isnull=True))
    kind = params.get("activity_type")
    if kind:
        if kind == TASK:
            comms = comms.none()
        else:
            comms, tasks = comms.filter(kind=kind), tasks.none()
    search = params.get("search")
    if search:
        comms = comms.filter(subject__icontains=search)
        tasks = tasks.filter(title__icontains=search)
    rows = [row_for_communication(c) for c in comms] + [row_for_task(t) for t in tasks]
    ordering = (params.get("ordering") or "-activity_date").strip()
    reverse = ordering.startswith("-")
    if ordering.lstrip("-") != "activity_date":
        ordering, reverse = "-activity_date", True
    rows.sort(key=lambda r: (r.activity_date, str(r.id)), reverse=reverse)
    return rows


def _uuid(value):
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def find(pk, *, scope):
    """The served row with id ``pk``, or None (also for a malformed id: a 404)."""
    pk = _uuid(pk)
    if pk is None:
        return None
    comms, tasks = querysets(scope=scope)
    comm = comms.filter(pk=pk).first()
    if comm is not None:
        return row_for_communication(comm)
    task = tasks.filter(pk=pk).first()
    return row_for_task(task) if task is not None else None


def validate_params(params):
    """400 on a malformed filter value, as django-filter answered the old viewset."""
    errors = {}
    for name in ("lead", "customer", "performed_by"):
        value = params.get(name)
        if value and _uuid(value) is None:
            errors[name] = ["Select a valid choice. That choice is not one of the available choices."]
    kind = params.get("activity_type")
    if kind and kind not in ActivityLog.ActivityType.values:
        errors["activity_type"] = [f"Select a valid choice. {kind} is not one of the available choices."]
    if errors:
        raise serializers.ValidationError(errors)


# --- writes -------------------------------------------------------------------------


def _targets(company, lead, customer):
    """``(engagement, party)`` as the backfill links them."""
    from .models import Engagement

    if lead is not None:
        engagement = lead.engagement if lead.engagement_id and lead.engagement.company_id == company.pk else None
        party = lead.party if lead.party_id and lead.party.company_id == company.pk else None
        return engagement, party
    if customer is not None and customer.party_id and customer.party.company_id == company.pk:
        party = customer.party
        open_ids = list(
            Engagement._base_manager.filter(company=company, party=party, status="open")
            .values_list("pk", flat=True)[:2]
        )
        engagement = Engagement._base_manager.get(pk=open_ids[0]) if len(open_ids) == 1 else None
        return engagement, party
    return None, None


def _merged_metadata(data, current=None):
    if current is not None:
        links = _links(current.metadata)
        lead_id = data["lead"].pk if "lead" in data and data["lead"] else (
            None if "lead" in data else links.get("lead"))
        customer_id = data["customer"].pk if "customer" in data and data["customer"] else (
            None if "customer" in data else links.get("customer"))
        minutes = data["duration_minutes"] if "duration_minutes" in data else links.get("duration_minutes")
        original = data["metadata"] if "metadata" in data else _original_metadata(current.metadata)
    else:
        lead_id = data["lead"].pk if data.get("lead") else None
        customer_id = data["customer"].pk if data.get("customer") else None
        minutes = data.get("duration_minutes")
        original = data.get("metadata", {})
    return activity_metadata(original if original is not None else {}, lead_id=lead_id,
                             customer_id=customer_id, duration_minutes=minutes)


def _seconds(kind, minutes):
    return minutes * 60 if kind in _HAS_DURATION and minutes is not None and minutes >= 0 else None


def create(company, data) -> ActivityRow:
    from communications.ingest import record

    from . import tasks as task_service
    from .models import Task

    lead, customer = data.get("lead"), data.get("customer")
    engagement, party = _targets(company, lead, customer)
    performer = data.get("performed_by")
    metadata = _merged_metadata(data)
    kind = data["activity_type"]
    with transaction.atomic():
        if kind == TASK:
            task = task_service.create(
                company=company, title=data["subject"], description=data.get("description", ""),
                engagement=engagement, party=party, assignee=performer, by=performer,
                source=API_TASK_SOURCE, metadata=metadata,
            )
            task_service.complete(task, by=performer)
            Task._base_manager.filter(pk=task.pk).update(completed_at=data["activity_date"])
            task.refresh_from_db()
            return row_for_task(task)
        comm = record(
            company, kind, direction="internal" if kind == "note" else "outbound",
            occurred_at=data["activity_date"], subject=data["subject"],
            body_text=data.get("description", ""),
            duration_seconds=_seconds(kind, data.get("duration_minutes")),
            engagement=engagement, party=party, performed_by=performer, metadata=metadata,
            link_engagement=False,
        ).communication
        return row_for_communication(comm)


def update(row: ActivityRow, company, data) -> ActivityRow:
    from communications.models import Communication

    from .models import Task

    kind = data.get("activity_type", row.activity_type)
    if (kind == TASK) != (row.activity_type == TASK):
        raise serializers.ValidationError(
            {"activity_type": "A task and a communication are different records since CRM "
                              "Phase 3 step 6; delete this activity and create a new one."}
        )
    obj = row.obj
    links = _links(obj.metadata)
    lead = data["lead"] if "lead" in data else (
        Lead._base_manager.filter(pk=links.get("lead")).first() if links.get("lead") else None)
    customer = data.get("customer") if "customer" in data else None
    if "customer" not in data and links.get("customer"):
        customer = Customer._base_manager.filter(pk=links["customer"]).first()
    metadata = _merged_metadata(data, current=obj)
    with transaction.atomic():
        if "lead" in data or "customer" in data:
            engagement, party = _targets(company, lead, customer)
            obj.engagement, obj.party = engagement, party
        if isinstance(obj, Task):
            if "subject" in data:
                obj.title = data["subject"]
            if "description" in data:
                obj.description = data["description"]
            if "performed_by" in data:
                obj.assignee = obj.completed_by = data["performed_by"]
            if "activity_date" in data:
                obj.completed_at = data["activity_date"]
            obj.metadata = metadata
            obj.full_clean()
            obj.save()
            return row_for_task(obj)
        assert isinstance(obj, Communication)
        obj.kind = kind
        obj.direction = "internal" if kind == "note" else "outbound"
        if "subject" in data:
            obj.subject = data["subject"]
        if "description" in data:
            obj.body_text = data["description"]
        if "performed_by" in data:
            obj.performed_by = data["performed_by"]
        if "activity_date" in data:
            obj.occurred_at = data["activity_date"]
        obj.duration_seconds = _seconds(kind, _links(metadata).get("duration_minutes"))
        obj.metadata = metadata
        obj.full_clean(validate_unique=False)
        obj.save()
        return row_for_communication(obj)


def lead_activities(lead, *, scope):
    """``LeadDetailSerializer.activities``: the lead's rows, newest first."""
    rows = filtered_rows({}, scope=scope, lead=lead.pk)
    return ActivityCompatSerializer(rows, many=True).data
