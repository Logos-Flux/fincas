"""The import write log and undo (docs/crm/phase5-importer.md 1.5, 4.5, I9).

Every row an applier writes goes through a :class:`WriteSession`:

- ``session.create(obj)`` after inserting ``obj``;
- ``session.update(obj, **fields)`` for a column change on an existing row
  (the full row is snapshotted first, then one ``QuerySet.update()`` writes,
  so no ``save()`` side effect runs between the snapshot and the write);
- ``session.touch(model, pk)`` before a write that changes an existing row
  indirectly (a sidecar merge, a denormalised column a helper refreshes);
- ``session.link(ref)`` after adding a ``core.ExternalRef``;
- ``session.deleted(model, pk, before)`` after a helper deleted a row this
  session snapshotted first (``communications.ingest.record`` replaces a
  re-imported communication's participants). It is an ``update`` entry whose
  ``after_digest`` is the digest of no row (``DELETED_DIGEST``); undo puts
  ``before`` back with one ``INSERT``.

``session.flush()`` (the applier calls it last, inside the row's
transaction) numbers the entries and inserts one ``ImportWriteLog`` per
written row: ``before`` (the full row as it was, ``update`` only) and
``after_digest`` (a fingerprint of the row as the import left it, taken at
flush, so every write of the row is in it). An existing row touched but left
unchanged writes no entry.

**Numbering.** ``seq`` orders every write of a job across all its batches.
``flush`` takes the job row's lock first (``lock_job``: ``SELECT ... FOR
UPDATE``; on SQLite, which ignores it, an ``UPDATE`` of the row, i.e. the
database write lock) and numbers from ``MAX(seq) + 1``. The lock is held to
the end of the row's transaction, so two appliers of one job (steps 8 and 9
run in parallel) never draw the same numbers, and one row's entries are
contiguous. Appliers take the job lock *before* any party lock
(``crm_import.appliers.base``), and undo does the same, so the lock order is
Job → Customer → Party → User everywhere (``core.identity.resolve``).

**Undo** (4.5) walks entries newest first and reverses each:

==========  ===================================================================
``create``  delete the row, only if its digest still equals ``after_digest``,
            nothing outside the import references it, it is not an erased
            party, and no newer import write touched it (a consent record:
            and nothing newer exists for its address); a row already gone is
            a no-op
``update``  write ``before`` back, under the same checks; a ``before`` that
            erasure scrubbed (``{}``) refuses
``link``    delete the ExternalRef the import added, under the digest check
            (erasure re-keys an erased person's refs, so those refuse)
deleted     (an ``update`` whose ``after_digest`` is ``DELETED_DIGEST``)
            re-insert ``before``, only if the row is still gone; a scrubbed
            ``before`` refuses
==========  ===================================================================

``undo_row(approval_row)`` reverses one applied row atomically (the unify
``undo`` hook, ``undo_run``); ``undo_job(job)`` walks the whole job, row by
row, newest first, and keeps going past a refusal: a row that depends on a
refused one refuses in turn (it is still referenced), the rest are undone.
Deletes are a single ``DELETE`` (``_raw_delete``): no ``post_delete``
receiver runs, so undoing a contact point does not re-derive the party's
primary columns behind the log's back, and the reference check has already
proved there is nothing to cascade to.

Undo writes inside ``core.plugins.dispatch.suppressed()``: it sends nothing.
"""

from __future__ import annotations

import datetime
import decimal
import hashlib
import json
import uuid
from dataclasses import dataclass, field

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, router, transaction
from django.db.models import Max, Q
from django.utils import timezone

from .models import ImportJob, ImportJobBatch, ImportWriteLog, WriteAction


class UndoRefused(ValueError):
    """One write could not be reversed; the message says why (4.5)."""


# --- rows as JSON -----------------------------------------------------------------


def _encode(value):
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()  # full microseconds
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _attnames(model) -> list[str]:
    return [f.attname for f in model._meta.concrete_fields]


def snapshot(model, pk) -> dict | None:
    """The full row (``{attname: value}``, JSON-safe), or None when it is gone."""
    row = model._base_manager.filter(pk=pk).values(*_attnames(model)).first()
    if row is None:
        return None
    return json.loads(json.dumps(row, default=_encode))


def digest_of(row: dict | None) -> str:
    payload = json.dumps(row, sort_keys=True, separators=(",", ":"), default=_encode)
    return hashlib.sha256(payload.encode()).hexdigest()


def row_digest(model, pk) -> str:
    return digest_of(snapshot(model, pk))


#: ``after_digest`` of an ``update`` entry that removed its row (``WriteSession.deleted``).
DELETED_DIGEST = digest_of(None)


def _decode(model, row: dict) -> dict:
    fields = {f.attname: f for f in model._meta.concrete_fields}
    return {name: fields[name].to_python(value) for name, value in row.items() if name in fields}


# --- the job lock -----------------------------------------------------------------


def lock_job(job_id) -> None:
    """Lock the job row until the enclosing transaction ends (seq allocation)."""
    db = router.db_for_write(ImportJob)
    if not connections[db].in_atomic_block:
        raise transaction.TransactionManagementError("lock_job() must run inside transaction.atomic().")
    if connections[db].features.has_select_for_update:
        list(ImportJob._base_manager.select_for_update().filter(pk=job_id).values_list("pk", flat=True))
    else:
        ImportJob._base_manager.filter(pk=job_id).update(updated_at=timezone.now())


def next_seq(job_id) -> int:
    """The next free ``seq`` of the job. Call under ``lock_job``."""
    top = ImportWriteLog._base_manager.filter(job_id=job_id).aggregate(top=Max("seq"))["top"]
    return (top or 0) + 1


# --- writing ---------------------------------------------------------------------


@dataclass
class _Entry:
    action: str
    model: type
    pk: object
    before: dict | None = None
    deleted: bool = False


@dataclass
class WriteSession:
    """Collects one approval row's writes; ``flush`` logs them (see the module docstring)."""

    job: ImportJob
    approval_row: object
    entries: list = field(default_factory=list)
    _touched: dict = field(default_factory=dict)

    def create(self, obj):
        self.entries.append(_Entry(WriteAction.CREATE, type(obj), obj.pk))
        return obj

    def link(self, ref):
        self.entries.append(_Entry(WriteAction.LINK, type(ref), ref.pk))
        return ref

    def touch(self, model, pk) -> None:
        """Snapshot an existing row before this session changes it (once)."""
        key = (model._meta.label_lower, str(pk))
        if key in self._touched or any(
            e.action == WriteAction.CREATE and e.model is model and str(e.pk) == str(pk) for e in self.entries
        ):
            return  # created here (its create entry covers it) or already snapshotted
        before = snapshot(model, pk)
        if before is None:
            raise ValidationError(f"{model._meta.label} {pk} no longer exists.")
        entry = _Entry(WriteAction.UPDATE, model, pk, before)
        self._touched[key] = entry
        self.entries.append(entry)

    def checkpoint(self, model, pk, before: dict) -> None:
        """Log a later change to a row this session already wrote, as its own
        ``update`` entry. ``before`` is the row as the earlier entries left it
        (the caller snapshots it before the change: a helper such as
        ``Task.save()`` writes the row behind the session's back). The entry
        sits after everything written since, so undo reverses the change
        first (an engagement's ``next_task`` is cleared before its task is
        deleted)."""
        self.entries.append(_Entry(WriteAction.UPDATE, model, pk, before))

    def deleted(self, model, pk, before: dict) -> None:
        """Log that a helper deleted an existing row; ``before`` is its
        ``snapshot`` taken before the delete. Undo re-inserts it."""
        if not before:
            raise ValueError("deleted() needs the row's snapshot.")
        self.entries.append(_Entry(WriteAction.UPDATE, model, pk, before, deleted=True))

    def update(self, obj, **fields) -> int:
        """Change columns of an existing row with one ``QuerySet.update()``."""
        model = type(obj)
        self.touch(model, obj.pk)
        if any(f.name == "updated_at" for f in model._meta.concrete_fields):
            fields.setdefault("updated_at", timezone.now())
        written = model._base_manager.filter(pk=obj.pk).update(**fields)
        for name, value in fields.items():
            setattr(obj, name, value)
        return written

    def flush(self) -> list[ImportWriteLog]:
        """Number the entries and insert their log rows. Inside the row's transaction."""
        rows = []
        for i, entry in enumerate(self.entries):
            if entry.deleted:
                rows.append((entry, DELETED_DIGEST))
                continue
            # The row as this entry left it: the next entry on the same row
            # starts from it (``checkpoint``), else the row as it is now.
            later = next((e for e in self.entries[i + 1:] if e.model is entry.model and str(e.pk) == str(entry.pk)
                          and e.action == WriteAction.UPDATE), None)
            after = later.before if later is not None else snapshot(entry.model, entry.pk)
            if entry.action == WriteAction.UPDATE and after == entry.before:
                continue  # touched, but nothing changed: nothing to undo
            if after is None:
                continue  # written and removed again in the same row
            rows.append((entry, digest_of(after)))
        if not rows:
            return []
        lock_job(self.job.pk)
        seq = next_seq(self.job.pk)
        types = ContentType.objects.get_for_models(*{e.model for e, _ in rows})
        logs = []
        for i, (entry, digest) in enumerate(rows):
            logs.append(ImportWriteLog(
                company_id=self.job.company_id, job_id=self.job.pk, approval_row_id=self.approval_row.pk,
                seq=seq + i, content_type=types[entry.model], object_id=str(entry.pk),
                action=entry.action, before=entry.before if entry.action == WriteAction.UPDATE else None,
                after_digest=digest,
            ))
        # bulk_create skips ImportWriteLog.save()'s checks: the applier checked
        # the approval row's job and every target's company (appliers.base).
        ImportWriteLog.objects.bulk_create(logs)
        self.entries.clear()
        self._touched.clear()
        return logs


# --- undo -------------------------------------------------------------------------


def _references(model, pk) -> list[str]:
    """Tables with a row that points at the target (any FK, one-to-one or M2M,
    hidden ``related_name="+"`` ones included), or an ExternalRef to it."""
    from core.models import ExternalRef

    found = []
    for rel in model._meta.get_fields(include_hidden=True):
        if not (rel.auto_created and not rel.concrete):
            continue  # forward fields; reverse relations are auto-created and not concrete
        related = rel.related_model
        if rel.many_to_many:
            qs = related._base_manager.filter(**{rel.field.name: pk})
        else:
            qs = related._base_manager.filter(**{rel.field.attname: pk})
        if related is model:
            qs = qs.exclude(pk=pk)
        if qs.exists():
            found.append(related._meta.label)
    if model._meta.many_to_many:
        obj = model._base_manager.get(pk=pk)
        for m2m in model._meta.many_to_many:
            if getattr(obj, m2m.name).exists():
                found.append(m2m.related_model._meta.label)
    if model is not ExternalRef:
        ct = ContentType.objects.get_for_model(model)
        if ExternalRef._base_manager.filter(content_type=ct, object_id=str(pk)).exists():
            found.append(ExternalRef._meta.label)
    return sorted(set(found))


def _describe(model, pk) -> str:
    return f"{model._meta.verbose_name} {pk}"


def _newer_write(log) -> ImportWriteLog | None:
    """A newer, live import write of the same row (this job's later seq, or any later job)."""
    return (
        ImportWriteLog._base_manager.filter(
            content_type_id=log.content_type_id, object_id=log.object_id, undone_at__isnull=True,
        )
        .filter(Q(job_id=log.job_id, seq__gt=log.seq) | (~Q(job_id=log.job_id) & Q(created_at__gt=log.created_at)))
        .exclude(pk=log.pk)
        # The same row's own later entries (a ``checkpoint``) are reversed first
        # by the same walk, newest first, and marked undone together at its end.
        .exclude(approval_row_id=log.approval_row_id)
        .order_by("created_at")
        .first()
    )


def _lock_target(model, pk) -> None:
    """Parties are locked in the login order (Customer → Party → User): a party
    lock alone is a prefix-free subset of it, so no cycle with erasure or login."""
    from parties.models import Party

    if model is Party:
        from core.identity.resolve import lock_login_rows

        lock_login_rows(parties=[pk])
    elif connections[router.db_for_write(model)].features.has_select_for_update:
        list(model._base_manager.select_for_update().filter(pk=pk).values_list("pk", flat=True))


def undo_entry(log: ImportWriteLog) -> bool:
    """Reverse one write. True when something was reverted, False for a no-op
    (the row is already gone). Raises ``UndoRefused``."""
    from parties.models import Party

    model = log.content_type.model_class()
    if model is None:
        raise UndoRefused(f"#{log.seq}: the target model is not installed.")
    pk = model._meta.pk.to_python(log.object_id)
    _lock_target(model, pk)
    newer = _newer_write(log)
    if newer is not None:
        raise UndoRefused(
            f"{_describe(model, pk)} was written again by import job {newer.job_id} "
            f"(#{newer.seq}); undo that first."
        )
    current = snapshot(model, pk)
    if log.action == WriteAction.UPDATE and not log.before:
        raise UndoRefused(
            f"{_describe(model, pk)}: the snapshot was scrubbed by an erasure; it cannot be restored."
        )
    if log.action == WriteAction.UPDATE and log.after_digest == DELETED_DIGEST:
        return _reinsert(model, pk, log, current)
    if current is None:
        if log.action == WriteAction.UPDATE:
            raise UndoRefused(f"{_describe(model, pk)} has been deleted since the import.")
        return False  # created or linked, and already gone: nothing to reverse
    if model is Party and current.get("erased_at"):
        raise UndoRefused(f"{_describe(model, pk)} has been erased; undo never brings it back.")
    if model._meta.label == "core.ExternalRef" and (current.get("metadata") or {}).get("erased"):
        raise UndoRefused(f"{_describe(model, pk)} belongs to an erased person; it stays.")
    if digest_of(current) != log.after_digest:
        when = current.get("updated_at") or "since"
        raise UndoRefused(f"{_describe(model, pk)} was changed after the import ({when}); it stays as it is.")
    if log.action == WriteAction.UPDATE:
        values = _decode(model, log.before)
        values.pop(model._meta.pk.attname, None)
        try:
            with transaction.atomic():
                model._base_manager.filter(pk=pk).update(**values)
        except IntegrityError as exc:
            raise UndoRefused(f"{_describe(model, pk)}: restoring it clashes with newer data "
                              f"({_first_line(exc)}).") from exc
        return True
    if model._meta.label == "parties.ConsentRecord":
        _newer_consent(model, pk, current)
    refs = _references(model, pk)
    if refs:
        raise UndoRefused(
            f"{_describe(model, pk)} is still referenced outside the import ({', '.join(refs)}); it stays."
        )
    qs = model._base_manager.filter(pk=pk)
    try:
        with transaction.atomic(using=qs.db):
            qs._raw_delete(qs.db)
            _check_constraints_now(qs.db)
    except IntegrityError as exc:
        raise UndoRefused(f"{_describe(model, pk)} is still referenced ({_first_line(exc)}); it stays.") from exc
    return True


def _newer_consent(model, pk, current) -> None:
    """4.5: an imported consent record is deleted only while nothing newer
    exists for the same address (a later grant or withdrawal stands on it)."""
    newer = model._base_manager.filter(
        company_id=current["company_id"], channel=current["channel"],
        address_normalized=current["address_normalized"], recorded_at__gt=current["recorded_at"],
    ).exclude(pk=pk)
    if current["address_normalized"] and newer.exists():
        raise UndoRefused(f"{_describe(model, pk)}: a newer consent record exists for the same address; "
                          "it stays.")


def _reinsert(model, pk, log, current) -> bool:
    """Undo a ``deleted`` entry: put the row back as it was, with its pk."""
    if current is not None:
        raise UndoRefused(f"{_describe(model, pk)} exists again; the import's delete is not undone over it.")
    from parties.models import Party

    party_id = log.before.get("party_id")
    if party_id and Party._base_manager.filter(pk=party_id, erased_at__isnull=False).exists():
        raise UndoRefused(f"{_describe(model, pk)} belongs to an erased party; it is not put back.")
    values = _decode(model, log.before)
    obj = model(**values)
    values.pop(model._meta.pk.attname, None)
    try:
        with transaction.atomic(using=router.db_for_write(model)):
            model._base_manager.bulk_create([obj])  # no save(): no signal, no side effect
            # The INSERT stamps auto_now(_add) columns; write the row's own back.
            model._base_manager.filter(pk=pk).update(**values)
            _check_constraints_now(router.db_for_write(model))
    except IntegrityError as exc:
        raise UndoRefused(f"{_describe(model, pk)}: putting it back clashes with newer data "
                          f"({_first_line(exc)}).") from exc
    return True


def _first_line(exc) -> str:
    """The database's message without its DETAIL line (which quotes values)."""
    return str(exc).splitlines()[0][:300] if str(exc) else type(exc).__name__


def _check_constraints_now(db) -> None:
    """Deferred FK checks fire at commit; make them fire inside the savepoint."""
    conn = connections[db]
    if conn.vendor == "postgresql":
        with conn.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")


def _mark(logs, actor: str) -> None:
    now = timezone.now()
    ImportWriteLog._base_manager.filter(pk__in=[log.pk for log in logs]).update(
        undone_at=now, undone_by=(actor or "")[:255], updated_at=now,
    )


def undo_row(approval_row, *, actor: str = "") -> int:
    """Reverse every live write of one applied row, newest first, all or nothing.

    Returns how many rows were reverted. Raises ``UndoRefused`` (nothing
    changed) when any one of them refuses.
    """
    from core.plugins.dispatch import suppressed

    logs = list(
        ImportWriteLog._base_manager.filter(approval_row_id=approval_row.pk, undone_at__isnull=True)
        .select_related("content_type").order_by("-seq")
    )
    if not logs:
        return 0
    with suppressed(), transaction.atomic():
        lock_job(logs[0].job_id)
        # Every party behind these rows, before any of their rows (Job → Customer →
        # Party → User): erasure locks the party first and then writes its refs and
        # contact points, so locking those first here would deadlock against it.
        from core.identity.resolve import lock_login_rows

        lock_login_rows(parties=_parties_behind(logs))
        logs = list(
            ImportWriteLog._base_manager.filter(pk__in=[log.pk for log in logs], undone_at__isnull=True)
            .select_related("content_type").order_by("-seq")
        )  # re-read under the locks: erasure may have scrubbed ``before`` meanwhile
        undone = 0
        for log in logs:
            undone += undo_entry(log)
        _mark(logs, actor or f"undo-row:{approval_row.pk}")
    return undone


def _parties_behind(logs) -> set:
    """Party ids a set of entries touches: parties, their contact points,
    roles and relationships, and the targets of refs."""
    from core.models import ExternalRef
    from parties.models import Party

    party_ct = ContentType.objects.get_for_model(Party)
    by_model: dict = {}
    for log in logs:
        by_model.setdefault(log.content_type.model_class(), set()).add(log.object_id)
    parties = set()
    for model, ids in by_model.items():
        if model is None:
            continue
        if model is Party:
            parties |= {model._meta.pk.to_python(i) for i in ids}
        elif model is ExternalRef:
            parties |= set(ExternalRef._base_manager.filter(pk__in=ids, content_type=party_ct)
                           .values_list("object_id", flat=True))
        else:
            for column in ("party_id", "from_party_id", "to_party_id"):
                if any(f.attname == column for f in model._meta.concrete_fields):
                    parties |= set(model._base_manager.filter(pk__in=ids).values_list(column, flat=True))
    return {p for p in parties if p is not None}


@dataclass
class UndoReport:
    undone_rows: int = 0
    reverted: int = 0
    refused: list = field(default_factory=list)  # {"row": <staged row id>, "seq": n, "error": "..."}


def undo_job(job: ImportJob, *, actor: str) -> UndoReport:
    """Undo the whole job newest first (4.5), row by row; refusals are listed.

    An undone row's staged row goes back to ``approved`` (``undo_state``), so
    re-applying it is one run.
    """
    from unify.appliers import get_applier
    from unify.apply import reopen
    from unify.models import ApprovalRow

    if not actor:
        raise ValueError("An undo always names its decider: actor required.")
    report = UndoReport()
    order = (
        ImportWriteLog._base_manager.filter(job_id=job.pk, undone_at__isnull=True)
        .values("approval_row_id").annotate(top=Max("seq")).order_by("-top")
    )
    for item in list(order):
        approval_row = ApprovalRow.objects.select_related("staged_row", "run__batch").get(pk=item["approval_row_id"])
        try:
            with transaction.atomic():  # the undo and the row's state change commit together
                reverted = undo_row(approval_row, actor=actor)
                if approval_row.state == ApprovalRow.State.APPLIED:
                    approval_row.state = ApprovalRow.State.SKIPPED
                    approval_row.save(update_fields=["state", "updated_at"])
                    reopen(approval_row.staged_row, get_applier(approval_row.run.batch.entity_kind))
        except UndoRefused as exc:
            report.refused.append({"row": str(approval_row.staged_row_id), "seq": item["top"], "error": str(exc)})
            continue
        report.reverted += reverted
        report.undone_rows += 1
    return report


def job_for_batch(batch) -> ImportJob:
    link = ImportJobBatch._base_manager.select_related("job").get(batch_id=batch.pk)
    return link.job
