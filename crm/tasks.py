"""Task lifecycle and the next action (docs/crm/phase3-engagement.md 1.5).

Every lifecycle change writes a ``TaskEvent``; every save that touches an
engagement recomputes its next action in the same transaction (``Task.save``
calls ``recompute_next_action``; a delete does through ``post_delete``).

Due times: ``due`` may be a ``date`` (an all-day task, due at the last instant
of that local day in ``due_tz``) or a ``datetime`` (naive = wall time in
``due_tz``). ``due_tz`` defaults to ``core.tz.resolve(assignee, company)``:
the assignee's zone, else the company's, else ``settings.TIME_ZONE``.
Stored ``due_at`` is UTC; "overdue" is always ``due_at < now()``.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone

from core import tz

from .models import OPEN_TASK_STATUSES, Engagement, Task, TaskEvent
from .registries import TASK_KINDS

# --- the next action -------------------------------------------------------------


def next_open_task(engagement_id):
    """The open task with the earliest ``due_at`` (nulls last, then oldest)."""
    return (
        Task._base_manager.filter(engagement_id=engagement_id, status__in=OPEN_TASK_STATUSES)
        .order_by(F("due_at").asc(nulls_last=True), "created_at", "pk")
        .only("pk", "due_at")
        .first()
    )


def recompute_next_action(engagement) -> tuple:
    """Set ``next_task`` / ``next_action_due_at`` from the open tasks (E4).

    Accepts an Engagement or its pk. Writes with ``update()`` (no ``save()``:
    the denormalised pair is not a stage move and must not bump
    ``updated_at``), and refreshes the pair on a passed instance. Returns
    ``(next_task_id, next_action_due_at)``.
    """
    engagement_id = getattr(engagement, "pk", engagement)
    with transaction.atomic():
        # Lock the engagement first, so two transactions adding tasks to it
        # serialise instead of each writing a next action that ignores the
        # other's task (READ COMMITTED). NO KEY: task inserts still pass
        # their FK check. A no-op on SQLite (one writer at a time).
        Engagement._base_manager.select_for_update(no_key=True).filter(pk=engagement_id).first()
        task = next_open_task(engagement_id)
        values = (task.pk if task else None, task.due_at if task else None)
        Engagement._base_manager.filter(pk=engagement_id).exclude(
            next_task_id=values[0], next_action_due_at=values[1]
        ).update(next_task_id=values[0], next_action_due_at=values[1])
    if isinstance(engagement, Engagement):
        engagement.next_task_id, engagement.next_action_due_at = values
    return values


# --- due times -------------------------------------------------------------------


def zone_for(assignee=None, company=None, due_tz=None) -> ZoneInfo:
    """The zone a new due time is set in (``due_tz`` if given, validated)."""
    if due_tz:
        tz.validate_timezone(due_tz)
        return ZoneInfo(due_tz)
    return tz.resolve(assignee, company)


def end_of_local_day(day: datetime.date, zone: ZoneInfo) -> datetime.datetime:
    """The last instant of ``day`` in ``zone``, as UTC (E7).

    Computed with ``zoneinfo`` at write time, so a DST-change day still ends
    at that day's local midnight.
    """
    # fold=1: if 23:59 occurs twice (a fall-back at midnight), the later one.
    local = datetime.datetime.combine(day, datetime.time.max, tzinfo=zone).replace(fold=1)
    return local.astimezone(datetime.UTC)


def due_instant(due, zone: ZoneInfo) -> tuple[datetime.datetime | None, bool]:
    """``(due_at in UTC, all_day)`` for a ``date`` / ``datetime`` / ``None``."""
    if due is None:
        return None, True
    if isinstance(due, datetime.datetime):
        if timezone.is_naive(due):
            due = due.replace(tzinfo=zone)
        return due.astimezone(datetime.UTC), False
    if isinstance(due, datetime.date):
        return end_of_local_day(due, zone), True
    raise TypeError(f"due must be a date, a datetime or None, not {type(due).__name__}")


def local_due_date(task: Task):
    """The calendar day the task is due on, in the zone it was set in."""
    if task.due_at is None:
        return None
    # A due_tz written around validation falls back like core.tz: never raise.
    zone = ZoneInfo(task.due_tz) if tz.is_valid_timezone(task.due_tz) else tz.resolve(None, None)
    return task.due_at.astimezone(zone).date()


# --- lifecycle -------------------------------------------------------------------
#
# Every service takes ``at=``: when the change happened. Golden's own callers
# leave it out (now); an inbound sync passes the provider's timestamp
# (phase6-adapters.md 3.1), so the TaskEvent, ``completed_at`` and the
# engagement's activity clock say when the work happened, not when golden
# heard of it. A future ``at`` (provider clock skew) is clamped to now.


def _when(at=None):
    now = timezone.now()
    return now if at is None or at > now else at


def _event(task, event, *, by=None, from_value="", to_value="", at=None):
    return TaskEvent.objects.create(
        task=task, company_id=task.company_id, event=event, by=by,
        from_value=str(from_value or "")[:64], to_value=str(to_value or "")[:64],
        at=_when(at),
    )


def _iso(value):
    return value.isoformat() if value else ""


def create(*, company=None, title, engagement=None, party=None, assignee=None, by=None,
           due=None, due_tz=None, kind="todo", priority=Task.Priority.NORMAL,
           description="", source="erp", metadata=None, at=None) -> Task:
    """Create an open task (and its ``created`` event, dated ``at``)."""
    if kind not in TASK_KINDS:
        raise ValidationError({"kind": f"Unknown task kind {kind!r} (GOLDEN_TASK_KINDS)."})
    if engagement is not None:
        company = engagement.company
    if company is None:
        raise ValueError("create() needs a company or an engagement")
    zone = zone_for(assignee, company, due_tz)
    due_at, all_day = due_instant(due, zone)
    task = Task(
        company=company, title=title, description=description, kind=kind, priority=priority,
        assignee=assignee, created_by=by, engagement=engagement, party=party,
        due_at=due_at, all_day=all_day, due_tz=zone.key if due_at else "",
        source=source, metadata=metadata or {},
    )
    task.full_clean()
    with transaction.atomic():
        task.save()
        _event(task, TaskEvent.Event.CREATED, by=by, to_value=_iso(due_at), at=at)
    return task


def _set_status(task, status, event, *, by, extra=None, at=None):
    previous = task.status
    if previous == status:
        return task
    if previous not in OPEN_TASK_STATUSES and event != TaskEvent.Event.REOPENED:
        # Leaving done / cancelled is a reopen, and shows in reopen history.
        raise ValidationError({"status": f"The task is {previous}; reopen it first."})
    task.status = status
    fields = {"status", "updated_at"}
    if status == Task.Status.DONE:
        task.completed_at, task.completed_by = _when(at), by
    else:
        task.completed_at, task.completed_by = None, None
    fields |= {"completed_at", "completed_by"}
    for name, value in (extra or {}).items():
        setattr(task, name, value)
        fields.add(name)
    with transaction.atomic():
        task.save(update_fields=fields)
        _event(task, event, by=by, from_value=previous, to_value=status, at=at)
    return task


def complete(task, *, by=None, at=None) -> Task:
    """Mark done at ``at`` (default now); moves the engagement's
    ``last_activity_at`` forward to it, never backwards (a completion
    mirrored late from a provider must not undo newer activity)."""
    if task.status == Task.Status.DONE:
        return task
    with transaction.atomic():
        _set_status(task, Task.Status.DONE, TaskEvent.Event.COMPLETED, by=by, at=at)
        if task.engagement_id:
            Engagement._base_manager.filter(pk=task.engagement_id).filter(
                Q(last_activity_at__isnull=True) | Q(last_activity_at__lt=task.completed_at)
            ).update(last_activity_at=task.completed_at)
    return task


def start(task, *, by=None, at=None) -> Task:
    return _set_status(task, Task.Status.IN_PROGRESS, TaskEvent.Event.STARTED, by=by, at=at)


def cancel(task, *, by=None, at=None) -> Task:
    return _set_status(task, Task.Status.CANCELLED, TaskEvent.Event.CANCELLED, by=by, at=at)


def reopen(task, *, by=None, status=Task.Status.OPEN, at=None) -> Task:
    """Back to an open status from ``done`` or ``cancelled``."""
    if status not in OPEN_TASK_STATUSES:
        raise ValidationError({"status": f"reopen needs an open status, not {status!r}."})
    if task.status in OPEN_TASK_STATUSES:
        return task
    return _set_status(task, status, TaskEvent.Event.REOPENED, by=by, at=at)


def set_status(task, status, *, by=None, at=None) -> Task:
    """Any status, through the service that owns the transition.

    ``done`` completes (a cancelled task is reopened first: two events),
    ``cancelled`` cancels, an open status from a closed one reopens,
    ``in_progress`` starts. A move between other open statuses has no event
    of its own: it is written as ``started`` with the real ``from``/``to``
    values (TaskEvent.Event has no ``moved``, and a new choice would need a
    migration). A done task asked to cancel is refused by ``cancel`` (its
    completion is history).
    """
    if status not in Task.Status.values:
        raise ValidationError({"status": f"Unknown task status {status!r}."})
    if status == task.status:
        return task
    if status == Task.Status.DONE:
        with transaction.atomic():
            if task.status == Task.Status.CANCELLED:
                reopen(task, by=by, at=at)
            return complete(task, by=by, at=at)
    if status == Task.Status.CANCELLED:
        return cancel(task, by=by, at=at)
    if task.status not in OPEN_TASK_STATUSES:
        return reopen(task, by=by, status=status, at=at)
    return _set_status(task, status, TaskEvent.Event.STARTED, by=by, at=at)


def reschedule(task, due, *, by=None, due_tz=None, at=None) -> Task:
    """Move the due time; clears ``due_notified_at`` so ``task_due`` fires again."""
    zone = zone_for(task.assignee, task.company, due_tz or None) if (due_tz or not task.due_tz) \
        else ZoneInfo(task.due_tz)
    due_at, all_day = due_instant(due, zone)
    if due_at == task.due_at and all_day == task.all_day:
        return task
    previous = task.due_at
    task.due_at, task.all_day = due_at, all_day
    task.due_tz = zone.key if due_at else ""
    task.due_notified_at = None
    with transaction.atomic():
        task.save(update_fields={"due_at", "all_day", "due_tz", "due_notified_at", "updated_at"})
        _event(task, TaskEvent.Event.RESCHEDULED, by=by, from_value=_iso(previous),
               to_value=_iso(due_at), at=at)
    return task


def assign(task, assignee, *, by=None, at=None) -> Task:
    """Change the assignee. The due instant does not move (a deadline is an instant)."""
    if task.assignee_id == getattr(assignee, "pk", None):
        return task
    previous = task.assignee_id
    task.assignee = assignee
    with transaction.atomic():
        task.save(update_fields={"assignee", "updated_at"})
        _event(task, TaskEvent.Event.ASSIGNED, by=by, from_value=previous or "",
               to_value=getattr(assignee, "pk", "") or "", at=at)
    return task


EDITABLE_FIELDS = ("title", "description", "priority")


def edit(task, *, by=None, at=None, **changes) -> Task:
    """Change ``title``, ``description`` or ``priority``; only what differs is written.

    No TaskEvent (history records lifecycle, not wording); the save sends
    ``task_updated`` with the written columns. ``by`` / ``at`` are accepted
    for symmetry with the other services and unused.
    """
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise ValueError(f"edit() changes {', '.join(EDITABLE_FIELDS)} only, not {', '.join(sorted(unknown))}")
    written = {name for name, value in changes.items() if getattr(task, name) != value}
    if not written:
        return task
    for name in written:
        setattr(task, name, changes[name])
    task.full_clean(exclude=[f.name for f in task._meta.fields if f.name not in written])
    task.save(update_fields=written | {"updated_at"})
    return task


# --- queues ----------------------------------------------------------------------


def open_tasks(company):
    return Task.objects.for_company(company).filter(status__in=OPEN_TASK_STATUSES)


def due_queue(company, *, assignee=None, unassigned=False, before=None):
    """Open tasks due at or before ``before`` (default now), earliest first.

    ``assignee`` narrows to one user's queue; ``unassigned=True`` to the
    tenant's unassigned queue. Undated tasks are never "due".
    """
    qs = open_tasks(company).filter(due_at__isnull=False, due_at__lte=before or timezone.now())
    if unassigned:
        qs = qs.filter(assignee__isnull=True)
    elif assignee is not None:
        qs = qs.filter(assignee=assignee)
    return qs.order_by("due_at", "created_at")


def due_on_local_day(company, day: datetime.date, zone: ZoneInfo, *, assignee=None):
    """Open tasks due on ``day`` in the viewer's ``zone`` ("My day", spec 7)."""
    start = datetime.datetime.combine(day, datetime.time.min, tzinfo=zone)
    end = start + datetime.timedelta(days=1)
    qs = open_tasks(company).filter(due_at__gte=start, due_at__lt=end)
    if assignee is not None:
        qs = qs.filter(assignee=assignee)
    return qs.order_by("due_at", "created_at")
