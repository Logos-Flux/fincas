"""What every CRM applier shares (docs/crm/phase5-importer.md 1.5, 4.5, 4.10).

``run(handler, company, staged, run=..., approval_row=...)`` is the
``unify`` apply hook for one row. Around the type's ``handler`` it:

1. finds the row's job (``ImportJobBatch``) and refuses a batch of another
   company (tenant isolation: ``unify.apply`` passes the batch's company,
   and every target the handler loads is filtered by it);
2. skips a record whose id **or any alias** belongs to an erased person
   (``unify.apply`` already checks the id; aliases are checked here), and
   scrubs the staged row as ``unify`` does;
3. returns the earlier ``applied_ref`` when a crash left this row's writes
   committed but its ``ApprovalRow`` unmarked (4.10: the write log names the
   approval row);
4. runs the handler in one savepoint inside ``suppressed()`` (no lifecycle
   signal, no webhook: the import sends nothing, hand-off 6), with the job
   lock taken first (``writelog.lock_job``: Job → Customer → Party → User);
5. turns failures into the coded errors of 4.1 (``RowError``):
   ``dependency_missing``, ``constraint``, ``conflict_stale``,
   ``invalid_value``, ``erased``.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .. import writelog

DEPENDENCY_MISSING = "dependency_missing"
CONSTRAINT = "constraint"
CONFLICT_STALE = "conflict_stale"
INVALID_VALUE = "invalid_value"
ERASED = "erased"
ERROR_CODES = (DEPENDENCY_MISSING, CONSTRAINT, CONFLICT_STALE, INVALID_VALUE, ERASED)


class RowError(ValueError):
    """A refused row. ``unify.apply`` stores ``str(exc)``, which starts with the code."""

    coded = True  # unify.apply: store the message as is

    def __init__(self, code: str, message: str):
        if code not in ERROR_CODES:
            raise ValueError(f"Unknown row error code {code!r}.")
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class Row:
    """One approved staged row, as a handler sees it."""

    company: object
    job: object
    staged: object
    approval_row: object
    session: writelog.WriteSession

    @property
    def record(self) -> dict:
        return self.staged.raw or {}

    @property
    def data(self) -> dict:
        return self.record.get("data") or {}

    @property
    def properties(self) -> dict:
        return self.record.get("properties") or {}

    @property
    def mapped(self) -> dict:
        return self.staged.mapped_values or {}

    @property
    def decision(self) -> str:
        return self.mapped.get("decision") or ""

    @property
    def values(self) -> dict:
        """``compare.project`` of the record, minus fields a reviewer kept."""
        return self.mapped.get("values") or {}

    @property
    def kept(self) -> set:
        return set(self.mapped.get("keep") or ())

    @property
    def provider(self) -> str:
        return self.job.source.split(":", 1)[0]

    @property
    def source_tag(self) -> str:
        """``source`` on contact points and relationships, ``marketable_source``."""
        return f"import:{self.provider}"[:64]

    @property
    def object_type(self) -> str:
        return self.record.get("object_type") or ""

    @property
    def source_object_type(self) -> str:
        return self.record.get("source_object_type") or self.object_type

    @property
    def keys(self) -> list[str]:
        """The record's source id, then its aliases."""
        keys = [str(self.record.get("source_id"))]
        keys += [str(a) for a in self.record.get("aliases") or [] if str(a) not in keys]
        return keys

    @cached_property
    def mapping(self) -> dict:
        return self.job.mapping or {}

    def property_targets(self) -> dict:
        """``{property name: target}`` from the mapping file (2.12); unnamed = sidecar."""
        section = self.mapping.get(self.object_type) or {}
        return dict(section.get("properties") or {})

    def target(self, model):
        """The approved ``target`` row of a link or update, from this tenant only."""
        target = self.mapped.get("target") or {}
        if not target.get("id"):
            raise RowError(INVALID_VALUE, "The approved decision names no target.")
        found = model._base_manager.filter(pk=target["id"], company_id=self.company.pk).first()
        if found is None:
            raise RowError(CONFLICT_STALE, f"The target {model._meta.verbose_name} is gone, or "
                                           "belongs to another company; re-run matching.")
        return found

    # --- ExternalRefs -------------------------------------------------------

    def ref_for(self, obj, source_id, *, object_type=None, metadata=None):
        """Add the ExternalRef ``(source, object_type, source_id) -> obj``, or
        return the one that already says so. One that names another row is a
        ``constraint`` error (two source records cannot claim one key)."""
        from core.models import ExternalRef

        object_type = object_type or self.source_object_type
        ct = ContentType.objects.get_for_model(type(obj))
        existing = ExternalRef._base_manager.filter(
            company_id=self.company.pk, source=self.job.source, object_type=object_type,
            source_id=str(source_id),
        ).first()
        if existing is not None:
            if existing.content_type_id == ct.pk and str(existing.object_id) == str(obj.pk):
                return existing
            raise RowError(CONSTRAINT, f"{object_type} {source_id} is already linked to another record.")
        ref = ExternalRef.objects.create(
            company_id=self.company.pk, source=self.job.source, object_type=object_type,
            source_id=str(source_id), content_type=ct, object_id=obj.pk,
            last_synced_at=timezone.now(), metadata=metadata or {},
        )
        return self.session.link(ref)

    def refs_for(self, obj, *, metadata=None) -> None:
        """One ExternalRef per id and alias (1.2), all to ``obj``."""
        for key in self.keys:
            self.ref_for(obj, key, metadata=metadata)

    def resolve(self, ref: dict | None, model, *, required: bool = True):
        """The golden row a ``*_ref`` names (``{object_type, source_id}``), by
        ExternalRef only (1.2: never by name). Unresolved and ``required`` is
        ``dependency_missing``, naming the ref."""
        if not ref:
            return None
        found = resolve_ref(self.job, ref, model)
        if found is None and required:
            raise RowError(DEPENDENCY_MISSING, f"{ref.get('object_type')} {ref.get('source_id')} is not "
                                               "imported yet (apply it, then retry this row).")
        return found


def source_object_types(job, object_type: str) -> list[str]:
    """``ExternalRef.object_type`` values a canonical type was staged under in
    ``job`` (``source_object_type``), plus the canonical name itself."""
    from crm_import.models import ImportJobBatch
    from unify.models import StagedRow

    if len(_TYPES) > 64 and str(job.pk) not in _TYPES:
        _TYPES.clear()  # a long-running worker: keep the memo small
    cache = _TYPES.setdefault(str(job.pk), {})
    if object_type not in cache:
        batch_ids = ImportJobBatch._base_manager.filter(job_id=job.pk, object_type=object_type).values("batch_id")
        found = set(
            StagedRow.objects.filter(batch_id__in=batch_ids)
            .values_list("raw__source_object_type", flat=True).distinct()
        )
        cache[object_type] = sorted({object_type, *(t for t in found if isinstance(t, str) and t)})
    return cache[object_type]


_TYPES: dict[str, dict[str, list[str]]] = {}


def resolve_ref(job, ref: dict, model):
    """The ``model`` row an ExternalRef of ``job``'s source names, or None."""
    from core.models import ExternalRef

    ct = ContentType.objects.get_for_model(model)
    sid = str(ref.get("source_id") or "")
    if not sid:
        return None
    hit = ExternalRef._base_manager.filter(
        company_id=job.company_id, source=job.source, source_id=sid, content_type=ct,
        object_type__in=source_object_types(job, ref.get("object_type") or ""),
    ).values_list("object_id", flat=True).first()
    if hit is None:
        return None
    return model._base_manager.filter(pk=hit, company_id=job.company_id).first()


# --- the hook ---------------------------------------------------------------------


def _erased_keys(job, record: dict) -> list[str]:
    from core.provenance import is_erased_source

    object_type = record.get("source_object_type") or record.get("object_type") or ""
    keys = [str(record.get("source_id") or "")] + [str(a) for a in record.get("aliases") or []]
    return [k for k in keys if k and is_erased_source(job.company_id, job.source, object_type, k)]


#: Target types an activity (task, communication) may name a person by.
PERSON_TARGET_TYPES = ("contact",)


def _erased_targets(job, record: dict) -> bool:
    """Whether a task or communication is about an erased person: one of its
    ``target_refs`` names an erased contact. Its text is that person's (a note
    has no participants to give it away), so it is skipped like the person."""
    from core.provenance import is_erased_source

    if record.get("object_type") not in ("task", "communication"):
        return False
    for ref in (record.get("data") or {}).get("target_refs") or []:
        if not ref or ref.get("object_type") not in PERSON_TARGET_TYPES or not ref.get("source_id"):
            continue
        for otype in source_object_types(job, ref["object_type"]):
            if is_erased_source(job.company_id, job.source, otype, str(ref["source_id"])):
                return True
    return False


#: Types whose record names a person by an address (match.address_hashes):
#: one under an active erasure suppression skips the whole record (3.1 step
#: 5). A consent fact is about its address; a communication with an erased
#: participant is that person's correspondence (staging skips it too).
PERSON_ADDRESS_TYPES = ("contact", "owner", "consent", "communication")


def _erased_points(job, record: dict) -> set:
    """Address hashes of the record that carry an active erasure suppression
    (3.1 step 5): the person may have been erased after staging, or entered by
    hand and erased, with no ExternalRef to say so."""
    from crm_import.match import address_hashes
    from parties.erasure import erasure_suppression_q
    from parties.models import Suppression

    otype = record.get("object_type") or ""
    if otype not in (*PERSON_ADDRESS_TYPES, "company"):
        return set()
    hashes = address_hashes(otype, record)
    if not hashes:
        return set()
    return set(Suppression.objects.filter(
        erasure_suppression_q(), company_id=job.company_id, lifted_at__isnull=True, address_hash__in=hashes,
    ).values_list("address_hash", flat=True))


def _scrub_erased(staged) -> None:
    from unify.apply import ERASED_SOURCE_ERROR
    from unify.models import StagedRow

    StagedRow.objects.filter(pk=staged.pk).update(raw={"erased": True}, mapped_values={}, proposed_mapping={},
                                                   error=ERASED_SOURCE_ERROR, updated_at=timezone.now())
    staged.raw, staged.mapped_values, staged.proposed_mapping = {"erased": True}, {}, {}


def _previous_ref(approval_row, record: dict) -> str | None:
    """4.10: this row's writes committed in a run whose ``ApprovalRow`` update
    did not. The applied record is the one the row's own ExternalRef (its
    source id) names, whatever else the row wrote first (a team, a ref).

    Looked up by the staged row, not the approval row: a crashed run may be
    cancelled and the row applied by a new run with a new ``ApprovalRow``
    (step 8's resume), which must not write it a second time."""
    from core.models import ExternalRef
    from crm_import.models import ImportWriteLog

    links = ImportWriteLog._base_manager.filter(approval_row__staged_row_id=approval_row.staged_row_id,
                                                      undone_at__isnull=True)
    first = links.select_related("content_type").order_by("seq").first()
    if first is None:
        return None
    job = first.job
    ref = ExternalRef._base_manager.filter(
        company_id=job.company_id, source=job.source, source_id=str(record.get("source_id")),
        object_type=record.get("source_object_type") or record.get("object_type"),
    ).select_related("content_type").first()
    if ref is None:
        return applied_ref(first.content_type.model_class(), first.object_id)
    return applied_ref(ref.content_type.model_class(), ref.object_id)


def applied_ref(model, pk) -> str:
    return f"{model._meta.label_lower}:{pk}"


def run(handler, company, staged, *, run=None, approval_row=None, **_context) -> str:
    from core.plugins.dispatch import suppressed

    if run is None or approval_row is None:
        raise RowError(INVALID_VALUE, "CRM rows are applied through an approval run.")
    job = writelog.job_for_batch(run.batch)
    if job.company_id != company.pk or run.batch.company_id != company.pk:
        raise RowError(INVALID_VALUE, "The batch belongs to another company.")
    if approval_row.run_id != run.pk or approval_row.staged_row_id != staged.pk:
        raise RowError(INVALID_VALUE, "The approval row is not this run's row.")
    record = staged.raw or {}
    erased_points = _erased_points(job, record) if not record.get("erased") else set()
    if record.get("erased") or _erased_keys(job, record) or _erased_targets(job, record) or (
            erased_points and record.get("object_type") in PERSON_ADDRESS_TYPES):
        _scrub_erased(staged)
        raise RowError(ERASED, "this source record belongs to a person whose data was erased; "
                               "it is not imported again.")
    if erased_points:
        # An organisation is not the person: it only loses the erased numbers.
        from crm_import.match import strip_erased_points

        staged.raw = record = strip_erased_points(record, erased_points)
    previous = _previous_ref(approval_row, record)
    if previous is not None:
        return previous
    row = Row(company=company, job=job, staged=staged, approval_row=approval_row,
              session=writelog.WriteSession(job, approval_row))
    try:
        with suppressed(), transaction.atomic():
            writelog.lock_job(job.pk)  # first: Job → Customer → Party → User
            ref = handler(row)
            if record.get("object_type") == "company" and _erased_points(job, record) - erased_points:
                raise RowError(CONFLICT_STALE, "a number of this company was erased while it was being "
                                               "imported; retry the row.")
            if _erased_keys(job, record) or _erased_targets(job, record) or (
                    record.get("object_type") in PERSON_ADDRESS_TYPES and _erased_points(job, record)):
                # Erased while this row was being written (erasure re-keys the
                # refs under its own locks): write nothing.
                raise RowError(ERASED, "this source record was erased while it was being imported.")
            row.session.flush()
    except RowError:
        raise
    except IntegrityError as exc:
        raise RowError(CONSTRAINT, str(exc).splitlines()[0][:500]) from exc
    except ValidationError as exc:
        raise RowError(INVALID_VALUE, "; ".join(exc.messages)[:500]) from exc
    return ref
