"""Source tasks → ``crm.Task`` (docs/crm/phase5-importer.md 2.9; hand-off section 4).

``crm.tasks.create`` makes open tasks stamped now, so the applier inserts
the task in its final state itself (as the ActivityLog backfill does):

- **Status** is the canonical one (the adapter mapped the provider's:
  ``NOT_STARTED`` → ``open``, ``WAITING`` / ``DEFERRED`` → ``waiting``, …);
  ``done`` keeps ``completed_at`` (``task_completed_at_iff_done``). There is
  no ``cancelled`` in the canonical format.
- **Kind** must be a ``GOLDEN_TASK_KINDS`` key; another is ``todo`` with the
  original in the sidecar (``import_kind``). **Priority** null is ``normal``.
- **Due**: an instant is timed (``all_day=False``); a date is all-day, due at
  the end of that day in the assignee's zone (``crm.tasks.due_instant``).
  ``due_tz`` is ``crm.tasks.zone_for(assignee, company)``.
- **No ``task_due`` storm.** An open task already due at the import gets
  ``due_notified_at`` = the import time in the same insert, so
  ``crm_emit_due`` never selects it; one due later keeps NULL and fires once
  when it falls due, like a task made by hand (hand-off 4's correction).
- **History**: ``TaskEvent(created)`` at the source's create time and, for a
  done task, ``TaskEvent(completed)`` at ``completed_at``. Never a fabricated
  ``rescheduled``. ``created_at`` is the source's too. A record with no
  create, update, completion or due time fails ``invalid_value``: imported
  history is never dated at the import.
- **Links**: ``activity.targets`` (explicit; the engagement's party fills a
  missing party, as ``Task.save`` does). The assignee is the owner.
- The engagement's next action is recomputed by ``Task.save`` and logged as a
  checkpoint; a done task moves ``last_activity_at`` forward to its
  completion, as ``crm.tasks.complete`` does.

**Re-import** (``update``): title, description, priority, due and status
with ``QuerySet.update()``; a change into ``done`` writes its ``completed``
event at the source's completion time, a change out of it a ``reopened``
event stamped now (golden learns of it at the import). ``due_notified_at``
follows the rule above for the new due time.
"""

from __future__ import annotations

import datetime as dt

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_date

from ..sidecar import SidecarRefused, put
from .activity import EngagementCheckpoint, owner_of, targets, touch_activity
from .base import CONFLICT_STALE, INVALID_VALUE, Row, RowError, applied_ref
from .deals import parse_instant

OPEN_STATUSES = ("open", "in_progress", "waiting")
STATUSES = (*OPEN_STATUSES, "done")
UPDATE_FIELDS = ("title", "body_text", "priority", "due_at", "due_is_date", "status", "completed_at", "kind")


def _due(row: Row, zone):
    """``(due_at, all_day)`` from the canonical ``due_at`` / ``due_is_date``."""
    from crm.tasks import due_instant

    value = row.data.get("due_at")
    if not value:
        return None, True
    if row.data.get("due_is_date"):
        day = parse_date(str(value))
        if day is None:
            raise RowError(INVALID_VALUE, f"due_at {value!r} is not a date.")
        return due_instant(day, zone)
    at = parse_instant(value)
    if at is None:
        raise RowError(INVALID_VALUE, f"due_at {value!r} is not a UTC instant.")
    return due_instant(at, zone)


def _status(row: Row):
    status = row.data.get("status")
    if status not in STATUSES:
        raise RowError(INVALID_VALUE, f"task status {status!r} is not one of {', '.join(STATUSES)}.")
    completed = parse_instant(row.data.get("completed_at")) if status == "done" else None
    if status == "done" and completed is None:
        raise RowError(INVALID_VALUE, "a done task needs completed_at.")
    return status, completed


def _kind(row: Row) -> tuple[str, str | None]:
    from crm.registries import TASK_KINDS

    kind = row.data.get("kind") or "todo"
    return (kind, None) if kind in TASK_KINDS else ("todo", kind)


def _created_at(row: Row, *candidates) -> dt.datetime:
    for value in (row.record.get("created_at"), row.record.get("updated_at"), *candidates):
        at = parse_instant(value) if not isinstance(value, dt.datetime) else value
        if at is not None:
            return at
    raise RowError(INVALID_VALUE, "the task has no created, updated, completed or due time; golden never "
                                  "dates imported history at the import.")


def _notified(status, due_at, now):
    """The pre-notified rule: an open task already due is marked now."""
    return now if status in OPEN_STATUSES and due_at is not None and due_at <= now else None


def _lock(party, engagement) -> None:
    """The parties the task points at, right after the job lock (Job → Party,
    as erasure); then ``Task.save`` / ``recompute_next_action`` lock the
    engagement, never the other way round."""
    from core.identity.resolve import lock_login_rows

    ids = {getattr(party, "pk", None), getattr(engagement, "party_id", None)} - {None}
    if ids:
        lock_login_rows(parties=sorted(ids, key=str))


def _sidecar(row: Row, task, derived: dict) -> None:
    targets_ = row.property_targets()
    values = {n: v for n, v in row.properties.items() if targets_.get(n, "sidecar") == "sidecar"}
    try:
        put(task, row.provider, values, derived={f"import_{k}": v for k, v in derived.items() if v},
            session=row.session)
    except SidecarRefused as exc:
        raise RowError(INVALID_VALUE, str(exc)) from exc


def create(row: Row):
    from crm import tasks as crm_tasks
    from crm.models import Task, TaskEvent

    data = row.data
    status, completed = _status(row)
    kind, other_kind = _kind(row)
    owner, owner_flags = owner_of(row)
    linked = targets(row)
    zone = crm_tasks.zone_for(owner, row.company)
    due_at, all_day = _due(row, zone)
    created_at = _created_at(row, completed, due_at)
    now = timezone.now()
    task = Task(
        company_id=row.company.pk, title=(data.get("title") or "").strip()[:255] or "Task",
        description=data.get("body_text") or "", kind=kind, status=status,
        priority=data.get("priority") or Task.Priority.NORMAL, assignee=owner,
        engagement=linked.engagement, party=linked.party, due_at=due_at, all_day=all_day,
        due_tz=zone.key if due_at else "", completed_at=completed, due_notified_at=_notified(status, due_at, now),
        source=f"import:{row.provider}"[:64], metadata={},
    )
    try:
        task.full_clean()
    except ValidationError as exc:
        raise RowError(INVALID_VALUE, "; ".join(exc.messages)) from exc
    _lock(linked.party, linked.engagement)
    mark = EngagementCheckpoint(row, task.engagement_id)
    task.save()  # task_created is dropped (suppressed)
    row.session.create(task)
    Task._base_manager.filter(pk=task.pk).update(created_at=created_at)
    if task.engagement_id:
        crm_tasks.recompute_next_action(task.engagement_id)  # again: created_at breaks ties
    events = [TaskEvent(task=task, company_id=task.company_id, event=TaskEvent.Event.CREATED,
                        to_value=due_at.isoformat() if due_at else "", at=created_at)]
    if status == "done":
        events.append(TaskEvent(task=task, company_id=task.company_id, event=TaskEvent.Event.COMPLETED,
                                from_value="open", to_value="done", at=completed))
    for event in TaskEvent.objects.bulk_create(events):
        row.session.create(event)
    mark.done()
    if status == "done":
        touch_activity(row, task.engagement_id, completed)
    _sidecar(row, task, {"kind": other_kind, "flags": owner_flags,
                         "unresolved_targets": linked.unresolved, "other_targets": linked.others})
    row.refs_for(task)
    return task


def update(row: Row):
    from crm import tasks as crm_tasks
    from crm.models import Task, TaskEvent

    from .parties import _stale_fields

    task = row.target(Task)
    _lock(task.party, task.engagement)
    if task.engagement_id:
        from crm.models import Engagement

        # The engagement before the task, as erasure's scrub writes them.
        Engagement._base_manager.select_for_update(no_key=True).filter(pk=task.engagement_id).first()
    stale = _stale_fields(row, task)
    if stale:
        raise RowError(CONFLICT_STALE, f"golden changed {', '.join(stale)} after the decision; review it again.")
    diff = ((row.staged.proposed_mapping or {}).get("diff") or {}).get("fields") or {}
    changed = {f for f in diff if f not in row.kept} & set(UPDATE_FIELDS)
    columns: dict = {}
    events = []
    now = timezone.now()
    if "title" in changed:
        columns["title"] = (row.data.get("title") or "").strip()[:255] or task.title
    if "body_text" in changed:
        columns["description"] = row.data.get("body_text") or ""
    if "priority" in changed:
        columns["priority"] = row.data.get("priority") or Task.Priority.NORMAL
    if "kind" in changed:
        columns["kind"] = _kind(row)[0]
    status, completed = task.status, task.completed_at
    if changed & {"status", "completed_at"}:
        status, completed = _status(row)
        columns.update(status=status, completed_at=completed)
        if status == "done" and task.status != "done":
            events.append(TaskEvent(task=task, company_id=task.company_id, event=TaskEvent.Event.COMPLETED,
                                    from_value=task.status, to_value="done", at=completed))
        elif status != "done" and task.status == "done":
            columns["completed_by"] = None
            events.append(TaskEvent(task=task, company_id=task.company_id, event=TaskEvent.Event.REOPENED,
                                    from_value="done", to_value=status, at=now))
    due_at = task.due_at
    if changed & {"due_at", "due_is_date"}:
        zone = crm_tasks.zone_for(task.assignee, row.company, task.due_tz or None)
        due_at, all_day = _due(row, zone)
        columns.update(due_at=due_at, all_day=all_day, due_tz=zone.key if due_at else "")
    reopened = status in OPEN_STATUSES and task.status not in OPEN_STATUSES
    if due_at != task.due_at or reopened:
        # A new due time (or a reopened task) is marked only when already due.
        columns["due_notified_at"] = _notified(status, due_at, now)
    mark = EngagementCheckpoint(row, task.engagement_id)
    if columns:
        for name, value in columns.items():
            setattr(task, name, value)
        try:
            task.full_clean()
        except ValidationError as exc:
            raise RowError(INVALID_VALUE, "; ".join(exc.messages)) from exc
        row.session.update(task, **columns)
    for event in TaskEvent.objects.bulk_create(events):
        row.session.create(event)
    if task.engagement_id and columns.keys() & {"status", "due_at"}:
        crm_tasks.recompute_next_action(task.engagement_id)
        mark.done()
    if status == "done" and "completed_at" in columns:
        touch_activity(row, task.engagement_id, completed)
    _sidecar(row, task, {})
    row.refs_for(task)
    return task


def apply(row: Row) -> str:
    from crm.models import Task

    if row.decision == "create":
        task = create(row)
    elif row.decision == "update":
        task = update(row)
    else:
        raise RowError(INVALID_VALUE, f"a task row cannot be applied as {row.decision!r}.")
    return applied_ref(Task, task.pk)
