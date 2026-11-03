"""``task_due``: the time-driven CRM signal (docs/crm/phase3-engagement.md 5.1).

``manage.py crm_emit_due`` (every 5 minutes, from the deployment's
scheduler) calls :func:`emit_due`. It selects open tasks with
``due_at <= now`` and ``due_notified_at IS NULL`` and, per task, claims the
marker with a conditional ``UPDATE … SET due_notified_at = now WHERE pk = …
AND due_notified_at IS NULL``. Only the run whose UPDATE touched the row
sends ``task_due`` (after its commit), so two overlapping runs send once.

Durable consumers of ``task_due`` (the ``task.due`` webhook, Phase 6
adapters) get their ``core.outbox`` row inside the claim's transaction
(``emit`` writes it there), so a crash after the claim commits no longer
loses the event: the row is delivered by ``process_outbox``. Only plain
in-process receivers stay best-effort.

``crm.tasks.reschedule`` clears ``due_notified_at``: a task moved to a later
time fires again when it crosses that time. A missed run sends late, never
twice for one due time. A task completed before a run never fires.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from .lifecycle import task_due
from .models import OPEN_TASK_STATUSES, Task


@dataclass
class DueResult:
    candidates: int = 0
    sent: int = 0

    @property
    def skipped(self) -> int:
        """Claimed by a concurrent run (or completed) between select and claim."""
        return self.candidates - self.sent


def due_candidates(now):
    return (
        Task._base_manager.filter(
            status__in=OPEN_TASK_STATUSES, due_at__isnull=False, due_at__lte=now,
            due_notified_at__isnull=True,
        )
        .order_by("due_at", "pk")
    )


def claim(task_pk, now) -> bool:
    """Set the marker iff nobody has; True when this caller won the row.

    Re-checks the due time too: a task rescheduled to later between the
    select and this claim (``reschedule`` clears the marker) is not due yet,
    and claiming it would fire early and then never at the new time.
    """
    return bool(
        Task._base_manager.filter(
            pk=task_pk, due_notified_at__isnull=True, status__in=OPEN_TASK_STATUSES,
            due_at__isnull=False, due_at__lte=now,
        ).update(due_notified_at=now)
    )


def emit_due(now=None, *, limit=None) -> DueResult:
    """Send ``task_due`` once for each open task whose due time has passed."""
    now = now or timezone.now()
    result = DueResult()
    pks = due_candidates(now).values_list("pk", flat=True)
    pks = list(pks[:limit] if limit else pks)
    result.candidates = len(pks)
    for pk in pks:
        # One transaction per task: the signal follows its own claim's
        # commit, and a failure on one task never un-claims another.
        with transaction.atomic():
            if not claim(pk, now):
                continue
            task = Task._base_manager.select_related("engagement").get(pk=pk)
            task_due(task)
            result.sent += 1
    return result
