"""The job runner: apply, dry run, retry, undo and cancel (docs/crm/phase5-importer.md 4.1-4.5, 4.10).

``run_job(job, actor=...)`` applies a reviewed job's approved rows, one batch
per step in the 4.2 order (owners → pipelines → companies → contacts → deals →
associations → relationships → tasks → communications → consent):

- **The step-order gate** (4.2). A step may start only when every step it
  needs has no approved row left unapplied. Undecided rows upstream do not
  block (a deal whose company is still in review fails ``dependency_missing``
  and is retried later). A step whose gate is shut stops the run there and
  names the blocking steps (``GateClosed``).
- **Resume** (4.10). A step with nothing approved left is complete and
  skipped; an apply run a crashed process left open is driven to its end
  before a new one opens, so a re-run picks up where the last one died.
- **Locks.** The job's advisory lock (``stage.job_run_lock``, Postgres) keeps
  a second runner, stager, undo or cancel off the job for the whole run; the
  move into ``applying`` / ``undoing`` goes through the source lock and busy
  check (``locks.begin_state``), so two jobs of one source never write at
  once. A run that dies leaves the job ``applying``: resumable, and the
  source stays busy until it is resumed (or the job undone).
- **No signals.** The whole run is inside ``core.plugins.dispatch.suppressed()``
  (hand-off 6); each row's applier runs in its own savepoint (``appliers.base``).

``dry_run=True`` does all of it inside one transaction that is rolled back
(4.4, I10): the same appliers, so the report cannot drift from what apply
does. Nothing the dry run did survives, the job's status included; its report
is returned and written to a file by the command, never stored.

``retry_job`` re-applies failed rows only (optionally by error code), through
the same gate. ``undo_job`` walks the write log newest first across the job
(``writelog.undo_job``); ``undo_batch`` undoes one step's runs, refused while
a step that depends on it still has applied rows (4.5). ``cancel_job`` stops
a job before anything was applied, which frees its source.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

from django.db import connection, transaction
from django.db.models import Count, Q

from core.plugins.dispatch import suppressed
from unify.apply import CHUNK_SIZE, apply_chunk, start_apply_run, undo_run
from unify.models import ApprovalRow, ApprovalRun, ImportBatch, StagedRow

from . import locks, report, writelog
from .models import STEPS, ImportJob, ImportWriteLog
from .stage import StagingError, auto_approve, job_run_lock

Status = ImportJob.Status
MS = StagedRow.MappingState

#: The apply order (4.2).
STEP_ORDER: tuple[str, ...] = tuple(sorted(STEPS, key=STEPS.get))

#: 4.2's "needs applied first". ``tickets_as_engagements`` adds the pipelines
#: to tickets and the tickets to tasks and communications (their engagement
#: may be a ticket's): see ``dependencies``.
DEPENDS: dict[str, tuple[str, ...]] = {
    "owner": (),
    "pipeline": (),
    "company": ("owner",),
    "contact": ("owner", "company"),
    "deal": ("pipeline", "owner", "company", "contact"),
    "association": ("company", "contact", "deal"),
    "relationship": ("contact", "company", "owner"),
    "task": ("deal", "contact", "company", "owner"),
    "communication": ("deal", "contact", "company", "owner"),
    "consent": ("contact",),
}

#: Where ``run`` may start from. ``applying``: a crashed run resuming;
#: ``undone``: re-applying an undone job is one run (4.5).
RUNNABLE = (Status.REVIEWING, Status.APPLYING, Status.APPLIED, Status.PARTIALLY_APPLIED, Status.UNDONE)
UNDOABLE = (Status.REVIEWING, Status.APPLIED, Status.PARTIALLY_APPLIED, Status.UNDOING, Status.UNDONE)
CANCELLABLE = (Status.VALIDATING, Status.STAGING, Status.REVIEWING)


class RunnerError(ValueError):
    """A run that cannot start or go on. Message = problem + cause + fix."""


class GateClosed(RunnerError):
    """A step's dependencies still hold approved rows (4.2)."""

    def __init__(self, object_type: str, blockers: dict[str, int]):
        self.object_type = object_type
        self.blockers = blockers
        names = ", ".join(f"{t} ({n} approved, not applied)" for t, n in blockers.items())
        super().__init__(
            f"{object_type} cannot be applied yet — cause: it needs {names} applied first (4.2). "
            f"Fix: apply those steps (`crm_import run --job <id>` does, in order)."
        )


@dataclass
class StepResult:
    object_type: str
    step: int
    state: str  # complete (nothing to do) / applied / blocked / not_reached
    applied: int = 0
    failed: int = 0
    skipped: int = 0
    runs: list = field(default_factory=list)
    blockers: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"object_type": self.object_type, "step": self.step, "state": self.state,
                "applied": self.applied, "failed": self.failed, "skipped": self.skipped,
                "blockers": self.blockers}


@dataclass
class RunResult:
    job: ImportJob
    dry_run: bool
    steps: list[StepResult]
    report: dict
    blocked: GateClosed | None = None


def dependencies(job, object_type: str) -> tuple[str, ...]:
    deps = DEPENDS[object_type]
    if (job.options or {}).get("tickets_as_engagements"):
        if object_type == "relationship":
            deps = (*deps, "pipeline")
        elif object_type in ("task", "communication"):
            deps = (*deps, "relationship")
    return deps


def dependents(job, object_type: str) -> list[str]:
    """Every step that needs ``object_type``, directly or not (undo order, 4.5)."""
    out: list[str] = []
    frontier = [object_type]
    while frontier:
        current = frontier.pop()
        for otype in STEP_ORDER:
            if current in dependencies(job, otype) and otype not in out:
                out.append(otype)
                frontier.append(otype)
    return sorted(out, key=STEPS.get)


def batches(job) -> dict[str, ImportBatch]:
    """The job's batches by object type, in step order."""
    links = job.job_batches.select_related("batch").order_by("step")
    return {link.object_type: link.batch for link in links}


def state_counts(job) -> dict[str, dict[str, int]]:
    """Staged rows by state, per object type (one query)."""
    out: dict[str, dict[str, int]] = {otype: {} for otype in STEP_ORDER}
    rows = (
        StagedRow.objects.filter(batch__crm_import_job_batch__job=job)
        .values("batch__crm_import_job_batch__object_type", "mapping_state")
        .annotate(n=Count("id"))
    )
    for item in rows:
        out[item["batch__crm_import_job_batch__object_type"]][item["mapping_state"]] = item["n"]
    return out


def gate(job, object_type: str, counts=None) -> dict[str, int]:
    """The steps ``object_type`` waits for, with their approved-unapplied rows ({} = open)."""
    counts = counts if counts is not None else state_counts(job)
    return {dep: counts[dep].get(MS.APPROVED, 0) for dep in dependencies(job, object_type)
            if counts[dep].get(MS.APPROVED, 0)}


def undo_blockers(job, object_type: str, counts=None) -> dict[str, int]:
    """The later steps that still hold applied rows depending on ``object_type``."""
    counts = counts if counts is not None else state_counts(job)
    return {dep: counts[dep].get(MS.APPLIED, 0) for dep in dependents(job, object_type)
            if counts[dep].get(MS.APPLIED, 0)}


def recompute_status(job) -> str:
    """The job's state from its rows (4.1), once a run or undo is over."""
    counts = state_counts(job)
    total = {}
    for per_type in counts.values():
        for state, n in per_type.items():
            total[state] = total.get(state, 0) + n
    if total.get(MS.UNMAPPED) or total.get(MS.PROPOSED) or total.get(MS.APPROVED):
        return Status.REVIEWING
    if total.get(MS.FAILED):
        return Status.PARTIALLY_APPLIED
    return Status.APPLIED


def _set_status(job, status: str) -> None:
    ImportJob.objects.filter(pk=job.pk).update(status=status)
    job.status = status


def _set_batch_status(batch: ImportBatch) -> None:
    states = set(batch.rows.values_list("mapping_state", flat=True).distinct())
    status = ImportBatch.Status.APPLIED if states <= {MS.APPLIED, MS.NA} else ImportBatch.Status.PARTIAL
    if not states & {MS.APPLIED, MS.FAILED}:
        status = ImportBatch.Status.REVIEWING if states - {MS.NA} else ImportBatch.Status.APPLIED
    if batch.status != status:
        ImportBatch.objects.filter(pk=batch.pk).update(status=status)
        batch.status = status



@contextmanager
def _job_session(job, activity: str):
    """The job's advisory lock for the whole operation, with signals suppressed."""
    try:
        with job_run_lock(job, activity), suppressed():
            yield
    except StagingError as exc:  # the advisory lock is held elsewhere
        raise RunnerError(str(exc)) from exc


def _enter(job, status: str, allowed_from, refusal: str, *, dry_run: bool = False) -> None:
    try:
        if dry_run:
            # The same checks, without keeping the source row locked for the
            # whole dry run: another job's staging gets SourceBusy at once
            # instead of waiting. The status change is rolled back with the rest.
            locks.check_state(job, status, allowed_from, refusal=refusal)
            ImportJob.objects.filter(pk=job.pk).update(status=status)
            job.status = status
            return
        locks.begin_state(job, status, allowed_from, refusal=refusal)
    except locks.SourceBusy as exc:
        raise RunnerError(
            f"{exc} — cause: two imports of one source at once would race on its records. "
            "Fix: wait for that job, or cancel it."
        ) from exc
    except ValueError as exc:
        raise RunnerError(str(exc)) from exc


# --- run ------------------------------------------------------------------------------


def run_job(job, *, actor: str, dry_run: bool = False, through: str | None = None,
            retry: bool = False, codes=None, chunk_size: int = CHUNK_SIZE,
            progress: Callable[[str], None] | None = None) -> RunResult:
    """Apply ``job`` step by step (see the module docstring); ``dry_run`` rolls it all back."""
    if not actor:
        raise ValueError("A run always names its decider: actor required.")
    if through is not None and through not in STEPS:
        raise RunnerError(
            f"--through {through!r} is not a step — cause: steps are object types. "
            f"Fix: one of {', '.join(STEP_ORDER)}."
        )
    if chunk_size < 1:
        raise RunnerError("chunk_size must be at least 1.")
    if not dry_run:
        # TestCase's own wrapping transactions are allowed (they are marked).
        if any(not getattr(block, "_from_testcase", False) for block in connection.atomic_blocks):
            raise RunnerError(
                "A real run cannot start inside a transaction — cause: every chunk must commit on its "
                "own (a crash keeps what is applied) and the job lock must be a session lock. "
                "Fix: call run_job outside transaction.atomic() (no ATOMIC_REQUESTS view)."
            )
        return _run(job, actor=actor, dry_run=False, through=through, retry=retry, codes=codes,
                    chunk_size=chunk_size, progress=progress)
    status = job.status
    with transaction.atomic():
        result = _run(job, actor=actor, dry_run=True, through=through, retry=retry, codes=codes,
                      chunk_size=chunk_size, progress=progress)
        transaction.set_rollback(True)
    job.status = status
    return result


def retry_job(job, *, actor: str, codes=None, through: str | None = None, dry_run: bool = False,
              chunk_size: int = CHUNK_SIZE, progress=None) -> RunResult:
    """Re-apply the job's failed rows only (by error code when ``codes`` names some)."""
    return run_job(job, actor=actor, retry=True, codes=codes, through=through, dry_run=dry_run,
                   chunk_size=chunk_size, progress=progress)


def _run(job, *, actor, dry_run, through, retry, codes, chunk_size, progress) -> RunResult:
    say = progress or (lambda _msg: None)
    last = STEPS[through] if through else max(STEPS.values())
    steps: list[StepResult] = []
    blocked = None
    with _job_session(job, "applied"):
        job.refresh_from_db()  # under the lock: what another process changed is seen
        _enter(job, Status.APPLYING, RUNNABLE,
               "only a reviewed job (or one applying, applied, partially applied or undone) can run. "
               "Fix: stage it first, or stage the export again as a new job.", dry_run=dry_run)
        by_type = batches(job)
        for otype in STEP_ORDER:
            step = STEPS[otype]
            batch = by_type.get(otype)
            if batch is None or step > last or blocked is not None:
                steps.append(StepResult(otype, step, "not_reached"))
                continue
            retry_rows = _failed_rows(batch, codes) if retry else None
            if not retry:
                auto_approve(job, batch)  # rows re-matched since staging; the same rule
            if not _has_work(batch, retry_rows):
                steps.append(StepResult(otype, step, "complete"))
                continue
            blockers = gate(job, otype)
            if blockers:
                blocked = GateClosed(otype, blockers)
                steps.append(StepResult(otype, step, "blocked", blockers=blockers))
                say(f"{otype}: blocked by {', '.join(blockers)}")
                continue
            steps.append(_apply_step(job, batch, otype, actor=actor, retry_rows=retry_rows,
                                     chunk_size=chunk_size, say=say))
        _set_status(job, recompute_status(job))
        built = report.build(job, dry_run=dry_run, steps=[s.as_dict() for s in steps])
        if not dry_run:
            job.report = {**(job.report or {}), "last_run": report.summary(built)}
            ImportJob.objects.filter(pk=job.pk).update(report=job.report)
    return RunResult(job=job, dry_run=dry_run, steps=steps, report=built, blocked=blocked)


def _failed_rows(batch, codes) -> list:
    rows = batch.rows.filter(mapping_state=MS.FAILED)
    if codes:
        rows = rows.filter(_code_q(codes))
    return list(rows.values_list("pk", flat=True))


def _code_q(codes) -> Q:
    q = Q()
    for code in codes:
        q |= Q(error__startswith=f"{code}:")
    return q


def _has_work(batch, retry_rows) -> bool:
    if retry_rows:
        return True
    if retry_rows is None and batch.rows.filter(mapping_state=MS.APPROVED).exists():
        return True
    # A run a crashed process left open (a crashed retry included) is driven first.
    return ApprovalRow.objects.filter(run__batch=batch, state=ApprovalRow.State.PENDING).exclude(
        run__status=ApprovalRun.Status.CANCELLED).exists()


def _apply_step(job, batch, otype, *, actor, retry_rows, chunk_size, say) -> StepResult:
    result = StepResult(otype, STEPS[otype], "applied")
    ImportBatch.objects.filter(pk=batch.pk).update(status=ImportBatch.Status.APPLYING)
    # A run a crashed process left open goes first (4.10); apply_chunk
    # short-circuits any of its rows that is no longer approved.
    for run in batch.approval_runs.filter(
            kind=ApprovalRun.Kind.APPLY,
            status__in=(ApprovalRun.Status.OPEN, ApprovalRun.Status.APPLYING)).order_by("created_at"):
        _drive(run, result, chunk_size, say)
    if retry_rows:
        with transaction.atomic():
            StagedRow.objects.filter(pk__in=retry_rows, mapping_state=MS.FAILED).update(
                mapping_state=MS.APPROVED, error="")
            run = start_apply_run(batch, actor, rows=retry_rows)
        _drive(run, result, chunk_size, say)
    elif retry_rows is None and batch.rows.filter(mapping_state=MS.APPROVED).exists():
        _drive(start_apply_run(batch, actor), result, chunk_size, say)
    _set_batch_status(batch)
    return result


def _drive(run, result: StepResult, chunk_size: int, say) -> None:
    """Apply the run's pending rows chunk by chunk, then close it."""
    chunks = 0
    while True:
        out = apply_chunk(str(run.pk), [], chunk_size=chunk_size)
        if out["status"] == ApprovalRun.Status.CANCELLED or not out["results"]:
            break
        chunks += 1
        for item in out["results"]:
            if item["state"] in ("applied", "already-applied"):
                result.applied += 1
            elif item["state"] == "failed":
                result.failed += 1
            else:
                result.skipped += 1
        say(f"{result.object_type}: chunk {chunks}, {result.applied} applied, {result.failed} failed")
    if not run.rows.filter(state=ApprovalRow.State.PENDING).exists():
        ApprovalRun.objects.filter(pk=run.pk).exclude(status=ApprovalRun.Status.CANCELLED).update(
            status=ApprovalRun.Status.DONE)
    result.runs.append(str(run.pk))


# --- undo -----------------------------------------------------------------------------


@dataclass
class UndoResult:
    job: ImportJob
    undone_rows: int
    reverted: int
    refused: list
    report: dict


def undo_job(job, *, actor: str) -> UndoResult:
    """Undo the whole job, newest write first (4.5). Refused rows stay applied and are listed."""
    if not actor:
        raise ValueError("An undo always names its decider: actor required.")
    with _job_session(job, "undone"):
        job.refresh_from_db()
        _enter(job, Status.UNDOING, UNDOABLE,
               "only a job that is not running can be undone. Fix: let the run finish "
               "(`crm_import run` resumes a crashed one), then undo.")
        out = writelog.undo_job(job, actor=actor)
        undone = out.undone_rows + _reopen_noops(job)
        for batch in batches(job).values():
            _set_batch_status(batch)
        return _after_undo(job, undone, out.reverted, out.refused, actor=actor, scope="job")


def _reopen_noops(job) -> int:
    """Rows applied with no write at all (a ticket pipeline without
    ``tickets_as_engagements``, a grant that could not be recorded, a pair
    with an end that is never imported): ``writelog.undo_job`` walks the log
    and never meets them, so they go back to ``approved`` here (4.5)."""
    from unify.appliers import get_applier
    from unify.apply import reopen

    logged = ImportWriteLog._base_manager.filter(job_id=job.pk).values("approval_row_id")
    rows = ApprovalRow.objects.filter(
        run__batch__crm_import_job_batch__job=job, state=ApprovalRow.State.APPLIED,
        staged_row__mapping_state=MS.APPLIED,
    ).exclude(pk__in=logged).select_related("staged_row", "run__batch")
    n = 0
    for approval_row in rows:
        with transaction.atomic():
            approval_row.state = ApprovalRow.State.SKIPPED
            approval_row.save(update_fields=["state", "updated_at"])
            reopen(approval_row.staged_row, get_applier(approval_row.run.batch.entity_kind))
        n += 1
    return n


def undo_batch(job, object_type: str, *, actor: str) -> UndoResult:
    """Undo one step's apply runs, newest first, once no later step depends on its rows (4.5)."""
    if not actor:
        raise ValueError("An undo always names its decider: actor required.")
    if object_type not in STEPS:
        raise RunnerError(f"{object_type!r} is not a step. Fix: one of {', '.join(STEP_ORDER)}.")
    with _job_session(job, "undone"):
        job.refresh_from_db()
        blockers = undo_blockers(job, object_type)
        if blockers:
            names = ", ".join(f"{t} ({n} applied)" for t, n in blockers.items())
            raise RunnerError(
                f"{object_type} cannot be undone yet — cause: {names} still point at its rows (4.5). "
                f"Fix: undo those steps first, newest first, or undo the whole job."
            )
        _enter(job, Status.UNDOING, UNDOABLE,
               "only a job that is not running can be undone. Fix: let the run finish, then undo.")
        batch = batches(job).get(object_type)
        undone = reverted = 0
        refused: list = []
        if batch is not None:
            for run in batch.approval_runs.filter(kind=ApprovalRun.Kind.APPLY).order_by("-created_at"):
                if not run.rows.filter(state=ApprovalRow.State.APPLIED).exists():
                    continue
                before = run.rows.filter(state=ApprovalRow.State.APPLIED).count()
                out = undo_run(str(run.pk), run.undo_token)
                undone += before - len(out["refused"])
                reverted += out["undone"]
                refused += [{"row": item["row"], "error": item["error"]} for item in out["refused"]]
            _set_batch_status(batch)
        return _after_undo(job, undone, reverted, refused, actor=actor, scope=object_type)


def _after_undo(job, undone_rows, reverted, refused, *, actor: str, scope: str) -> UndoResult:
    """``undone`` once nothing applied is left; else ``partially_applied`` (4.1)."""
    from core.models import AuditLog

    live = ImportWriteLog._base_manager.filter(job_id=job.pk, undone_at__isnull=True).exists()
    applied = StagedRow.objects.filter(batch__crm_import_job_batch__job=job, mapping_state=MS.APPLIED).exists()
    _set_status(job, Status.PARTIALLY_APPLIED if live or applied else Status.UNDONE)
    built = report.build(job)
    summary = {"actor": actor, "scope": scope, "undone_rows": undone_rows, "reverted": reverted,
               "refused": len(refused)}
    job.report = {**(job.report or {}), "last_undo": summary}
    ImportJob.objects.filter(pk=job.pk).update(report=job.report)
    # Who undid it: the write log names the run (``undo-run:<id>``) for a per-step undo.
    AuditLog.objects.create(action="undo", model_name="crm_import.ImportJob", object_id=job.pk,
                            changes={**summary, "job": str(job.pk)})
    return UndoResult(job=job, undone_rows=undone_rows, reverted=reverted, refused=refused, report=built)


# --- cancel ---------------------------------------------------------------------------


def cancel_job(job) -> ImportJob:
    """Stop a job before anything was applied (4.1). Its staged rows stay as the record."""
    with _job_session(job, "cancelled"):
        job.refresh_from_db()
        if ImportWriteLog._base_manager.filter(job_id=job.pk).exists() or StagedRow.objects.filter(
                batch__crm_import_job_batch__job=job, mapping_state=MS.APPLIED).exists():
            raise RunnerError(
                f"Job {job.pk} has applied rows — cause: cancel is for a job nothing was applied from. "
                "Fix: undo it (`crm_import undo --job`)."
            )
        _enter(job, Status.CANCELLED, CANCELLABLE,
               "only a validating, staging or reviewing job can be canceled.")
    return job


# --- the generic screens' gate ----------------------------------------------------------


def generic_refusal(batch, action: str) -> str:
    """``unify``'s gate for ``crm_<type>`` batches: the generic batch screen and
    ``unify_apply --batch`` neither apply nor undo them, because they hold no
    source lock and know no step order."""
    from .models import ImportJobBatch

    try:
        job = writelog.job_for_batch(batch)
    except ImportJobBatch.DoesNotExist:
        return "This CRM import batch belongs to no import job; it cannot be applied or undone here."
    verb = "applied" if action == "apply" else "undone"
    return (
        f"CRM import batches are {verb} by the job's runner, which keeps the step order and the "
        f"source lock: `manage.py crm_import {'run' if action == 'apply' else 'undo'} --job {job.pk}`."
    )
