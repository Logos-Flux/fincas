"""The per-(company, source) import lock (docs/crm/phase5-importer.md section 7).

Two imports of the same source must not stage at once: ``begin_staging``
takes the source's lock row (``select_for_update`` on ``ImportSourceLock``)
and refuses while another job of the same company and source is ``staging``
or ``applying``. The lock is held to the end of the enclosing transaction, so
the busy check and the status change cannot interleave with another job's.

On a backend without ``SELECT ... FOR UPDATE`` (SQLite) the lock is an
``UPDATE`` of the same row instead, which takes SQLite's database-wide write
lock. Nothing double-stages there either, but a contender may get
``OperationalError: database is locked`` instead of waiting and then
``SourceBusy``; SQLite is for tests and development only.

Scope: the lock covers the *transition*. The stager (step 3) and the runner
(step 8) also keep a second process off the same job while they run
(``stage.job_run_lock``). The runner enters ``applying`` and ``undoing``, and
``cancel`` leaves a job, through the same lock and busy check
(``begin_state``).
"""

from __future__ import annotations

from django.db import connections, router, transaction
from django.db.transaction import TransactionManagementError
from django.utils import timezone

from .models import ImportJob, ImportSourceLock

#: A job in one of these states holds its source (spec 7). ``undoing`` too:
#: an undo racing another job's staging would compare against rows it is
#: about to delete.
BUSY_STATES = (ImportJob.Status.STAGING, ImportJob.Status.APPLYING, ImportJob.Status.UNDOING)
#: States ``begin_staging`` starts from: a fresh job, or a crashed staging
#: resuming (spec 4.10).
STAGEABLE_STATES = (ImportJob.Status.VALIDATING, ImportJob.Status.STAGING)


class SourceBusy(Exception):
    """Another job of the same company and source is staging or applying."""

    def __init__(self, job: ImportJob, busy: ImportJob):
        self.job = job
        self.busy = busy
        super().__init__(
            f"{job.source} is already being imported by job {busy.pk} "
            f"({busy.get_status_display().lower()}); wait for it to finish or cancel it."
        )


def lock_source(company, source: str) -> ImportSourceLock:
    """Lock (company, source) until the enclosing transaction ends."""
    db = router.db_for_write(ImportSourceLock)
    if not connections[db].in_atomic_block:
        raise TransactionManagementError("lock_source() must run inside transaction.atomic().")
    lock, _ = ImportSourceLock.objects.get_or_create(company=company, source=source)
    if connections[db].features.has_select_for_update:
        return ImportSourceLock.objects.select_for_update().get(pk=lock.pk)
    ImportSourceLock.objects.filter(pk=lock.pk).update(updated_at=timezone.now())
    return lock


def begin_staging(job: ImportJob) -> ImportJob:
    """Move ``job`` to ``staging`` under its source's lock, or raise ``SourceBusy``."""
    return begin_state(
        job, ImportJob.Status.STAGING, STAGEABLE_STATES,
        refusal="only a validating job (or a staging one resuming) can start staging.",
    )


def check_state(job: ImportJob, status: str, allowed_from, *, refusal: str) -> ImportJob:
    """``begin_state``'s checks without its lock or its write (the dry run,
    which rolls its status change back and must not hold the source row)."""
    current = ImportJob.objects.get(pk=job.pk)
    if current.status not in allowed_from:
        raise ValueError(f"Job {job.pk} is {current.status}; {refusal}")
    if status in BUSY_STATES:
        busy = (ImportJob.objects.filter(company_id=current.company_id, source=current.source,
                                         status__in=BUSY_STATES).exclude(pk=current.pk)
                .order_by("created_at").first())
        if busy is not None:
            raise SourceBusy(current, busy)
    return current


def begin_state(job: ImportJob, status: str, allowed_from, *, refusal: str) -> ImportJob:
    """Move ``job`` to ``status`` under its source's lock.

    Raises ``ValueError`` (``"Job <id> is <status>; <refusal>"``) when the job
    is not in ``allowed_from``, and ``SourceBusy`` while another job of the
    same company and source is staging, applying or undoing. Moving into a
    state that is not busy (``cancelled``) skips the busy check."""
    with transaction.atomic():
        lock_source(job.company, job.source)
        current = ImportJob.objects.get(pk=job.pk)
        if current.status not in allowed_from:
            raise ValueError(f"Job {job.pk} is {current.status}; {refusal}")
        busy = None if status not in BUSY_STATES else (
            ImportJob.objects.filter(
                company_id=current.company_id, source=current.source, status__in=BUSY_STATES
            )
            .exclude(pk=current.pk)
            .order_by("created_at")
            .first()
        )
        if busy is not None:
            raise SourceBusy(current, busy)
        if current.status != status:
            ImportJob.objects.filter(pk=current.pk).update(status=status, updated_at=timezone.now())
    job.status = status
    return job
