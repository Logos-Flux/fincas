"""CRM lifecycle signals (docs/crm/phase3-engagement.md 5.1).

The models call these from ``save()`` so every write path emits: the
``crm.engagements`` / ``crm.tasks`` services, lead intake, the API, the
admin. Delivery is ``core.plugins.dispatch.emit``: after commit, dropped on
rollback, isolated per receiver. Backfills write with ``bulk_create`` (no
``save()``) or run inside ``core.plugins.dispatch.suppressed()``, so they
send nothing.

Payloads are model instances, as for every other lifecycle signal; the
webhook layer (``webhooks.events``) turns them into ids, keys and
timestamps.
"""

from __future__ import annotations

from core.plugins import signals
from core.plugins.dispatch import emit

OPEN, WON, LOST = "open", "won", "lost"
TASK_OPEN_STATUSES = ("open", "in_progress", "waiting")
TASK_CLOSED_STATUSES = ("done", "cancelled")


def engagement_created(engagement) -> None:
    emit(signals.engagement_created, type(engagement), engagement, engagement=engagement)


def stage_change_written(change) -> None:
    """One history row: the status signal (won / lost / reopened), then
    ``engagement_stage_changed`` when the stage moved. The creation row
    (``"" -> open``) sends nothing here: ``engagement_created`` covers it."""
    if not change.from_status:
        return
    engagement = change.engagement
    sender = type(engagement)
    if change.from_status == OPEN and change.to_status == WON:
        emit(signals.engagement_won, sender, engagement, engagement=engagement, change=change)
    elif change.from_status == OPEN and change.to_status == LOST:
        emit(signals.engagement_lost, sender, engagement, engagement=engagement, change=change,
             loss_reason=engagement.loss_reason)
    elif change.from_status in (WON, LOST) and change.to_status == OPEN:
        emit(signals.engagement_reopened, sender, engagement, engagement=engagement, change=change,
             previous_status=change.from_status)
    if change.from_stage_id != change.to_stage_id and change.to_stage_id is not None:
        to_stage = engagement.stage if engagement.stage_id == change.to_stage_id else change.to_stage
        emit(signals.engagement_stage_changed, sender, engagement, engagement=engagement,
             from_stage=change.from_stage, to_stage=to_stage, change=change)


def task_saved(task, *, adding, loaded_status, loaded_assignee_id, written, loaded=None) -> None:
    """``written``: the column names the save wrote (None = all of them);
    ``loaded``: ``Task.SNAPSHOT_ATTNAMES`` values as last read (``UNKNOWN`` when not)."""
    sender = type(task)
    if adding:
        emit(signals.task_created, sender, task, task=task)
        return

    def wrote(name):
        # update_fields may name the field or its attname ("assignee_id").
        return written is None or name in written or f"{name}_id" in written

    if wrote("assignee") and loaded_assignee_id is not _UNKNOWN \
            and task.assignee_id != loaded_assignee_id:
        emit(signals.task_assigned, sender, task, task=task, previous_assignee=loaded_assignee_id)
    if wrote("status") and loaded_status is not _UNKNOWN and task.status != loaded_status:
        if task.status == "done":
            emit(signals.task_completed, sender, task, task=task)
        elif loaded_status in TASK_CLOSED_STATUSES and task.status in TASK_OPEN_STATUSES:
            emit(signals.task_reopened, sender, task, task=task)
    # Every update of an existing row, whatever it changed (phase6-adapters.md
    # 3.1): the party_updated pattern. Reschedule, cancel and edit have no
    # signal of their own; an outbound sync pins the written columns' values.
    columns = written_columns(task, written)
    emit(signals.task_updated, sender, task, task=task, update_fields=columns,
         changed_fields=changed_columns(task, columns, loaded or {}))


def changed_columns(task, columns, loaded) -> tuple:
    """``columns`` minus the snapshotted ones whose value did not change.

    A full ``save()`` (the admin, the activity compat endpoint) writes every
    column; a consumer that pins values (the ClickUp push) must not resend
    the ones nobody touched. A column with no snapshot is kept.
    """
    out = []
    for name in columns:
        field = task._meta.get_field(name)
        attname = getattr(field, "attname", name)
        before = loaded.get(attname, _UNKNOWN)
        if before is _UNKNOWN or getattr(task, attname) != before:
            out.append(name)
    return tuple(out)


def written_columns(task, written) -> tuple:
    """The sorted field names a save wrote (``written`` None = every concrete column)."""
    fields = [f for f in task._meta.concrete_fields if not f.primary_key]
    if written is None:
        return tuple(sorted(f.name for f in fields))
    by_attname = {f.attname: f.name for f in fields}
    return tuple(sorted({by_attname.get(name, name) for name in written}))


def task_due(task) -> None:
    emit(signals.task_due, type(task), task, task=task)


def communication_logged(communication) -> None:
    emit(signals.communication_logged, type(communication), communication,
         communication=communication)


class _Unknown:
    def __repr__(self):
        return "<unknown>"


#: A snapshot value never loaded (a row built in memory, a deferred column).
_UNKNOWN = _Unknown()
UNKNOWN = _UNKNOWN
