"""Bulk apply: approved staged rows → domain objects (Steps 3-4, ED-4).

Chunked (browser posts chunks of 200 per DD-8): each chunk carries run_id +
row ids; per-row apply is idempotent (unique run/staged constraint + APPLIED
short-circuit), so refresh/tab-close resumes safely and cancel only stops
future chunks. One undo token per run deactivates everything it created —
non-destructive by design.

What a row *does* lives in ``unify.appliers``: the batch's ``entity_kind``
picks the applier (``product`` here, ``party_merge`` from ``parties``).
A row whose source key (``source_key``) belongs to an erased person is
skipped before any applier runs (docs/crm/phase2-party.md 1.7).
"""

from __future__ import annotations

import secrets

from django.db import transaction

from .appliers import _dec, apply_product, get_applier  # noqa: F401  # _dec: old import path
from .models import ApprovalRow, ApprovalRun, ImportBatch, StagedRow

CHUNK_SIZE = 200


def start_apply_run(batch: ImportBatch, actor: str, *, rows=None) -> ApprovalRun:
    """Open a run over the batch's APPROVED rows (only those in ``rows``, a
    queryset or ids of staged rows, when given: a retry of failed rows)."""
    if not actor:
        raise ValueError("Apply runs always name their decider — actor required.")
    with transaction.atomic():
        run = ApprovalRun.objects.create(
            batch=batch, kind=ApprovalRun.Kind.APPLY, actor=actor,
            undo_token=secrets.token_hex(16),
        )
        approved = batch.rows.filter(
            mapping_state=StagedRow.MappingState.APPROVED
        )
        if rows is not None:
            approved = approved.filter(pk__in=rows)
        ApprovalRow.objects.bulk_create(
            [ApprovalRow(run=run, staged_row=row) for row in approved],
            ignore_conflicts=True,
        )
    return run


def apply_chunk(run_id: str, row_ids: list[str], chunk_size: int = CHUNK_SIZE):
    """Apply up to chunk_size PENDING rows. Returns per-row results."""

    run = ApprovalRun.objects.select_related("batch").get(pk=run_id)
    if run.status == ApprovalRun.Status.CANCELLED:
        return {"run": str(run.pk), "status": "cancelled", "results": []}
    if run.status == ApprovalRun.Status.OPEN:
        run.status = ApprovalRun.Status.APPLYING
        run.save(update_fields=["status", "updated_at"])

    applier = get_applier(run.batch.entity_kind)
    results = []
    # Ordered: a slice of an unordered queryset is whichever rows the
    # database returns first (on Postgres not file order, and not stable).
    pending = (
        run.rows.filter(state=ApprovalRow.State.PENDING)
        .select_related("staged_row")
        .order_by("staged_row__row_number", "pk")[:chunk_size]
    )
    if row_ids:
        pending = [r for r in pending if str(r.staged_row_id) in set(row_ids)]
    for approval_row in pending:
        staged = approval_row.staged_row
        if staged.mapping_state == StagedRow.MappingState.APPLIED:
            _mark(approval_row, ApprovalRow.State.APPLIED)
            results.append({"row": staged.row_number, "state": "already-applied"})
            continue
        if staged.mapping_state != StagedRow.MappingState.APPROVED:
            # Un-approved (rejected, dismissed) after the run opened: honour that.
            _mark(approval_row, ApprovalRow.State.SKIPPED)
            results.append({"row": staged.row_number, "state": "skipped"})
            continue
        if erased_source(run.batch.company_id, staged):
            # phase2-party.md 1.7: never resurrect an erased person.
            _mark(approval_row, ApprovalRow.State.SKIPPED)
            staged.mapping_state = StagedRow.MappingState.NA
            staged.error = ERASED_SOURCE_ERROR
            # The row is the erased person's data again: keep only the fact.
            staged.raw, staged.mapped_values, staged.proposed_mapping = {"erased": True}, {}, {}
            staged.save(update_fields=["mapping_state", "error", "raw", "mapped_values",
                                       "proposed_mapping", "updated_at"])
            results.append({"row": staged.row_number, "state": "skipped-erased"})
            continue
        try:
            ref = applier.apply(
                run.batch.company, staged, run=run, approval_row=approval_row
            )
        except Exception as exc:  # noqa: BLE001 — recorded per row, never raised
            approval_row.state = ApprovalRow.State.FAILED
            # An applier's coded error (``coded = True``) already starts with
            # its code ("dependency_missing: ..."); others get the type name.
            approval_row.error = (str(exc) if getattr(exc, "coded", False)
                                  else f"{type(exc).__name__}: {exc}")[:1000]
            approval_row.save(update_fields=["state", "error", "updated_at"])
            staged.mapping_state = StagedRow.MappingState.FAILED
            staged.error = approval_row.error
            staged.save(
                update_fields=["mapping_state", "error", "updated_at"]
            )
            results.append(
                {"row": staged.row_number, "state": "failed",
                 "error": approval_row.error}
            )
            continue
        staged.mapping_state = StagedRow.MappingState.APPLIED
        staged.applied_ref = ref
        staged.error = ""
        staged.save(
            update_fields=["mapping_state", "applied_ref", "error", "updated_at"]
        )
        _mark(approval_row, ApprovalRow.State.APPLIED)
        results.append({"row": staged.row_number, "state": "applied", "ref": ref})
    return {"run": str(run.pk), "status": run.status, "results": results}


ERASED_SOURCE_ERROR = (
    "This source record belongs to a person whose data was erased; it is not imported again."
)


def source_key(staged: StagedRow) -> tuple[str, str, str] | None:
    """The row's source-record key, when its importer recorded one.

    Importers of people (customers, contacts, leads) put
    ``{"source": ..., "object_type": ..., "source_id": ...}`` under
    ``external_ref`` in ``mapped_values`` (or ``raw``): the same key as
    ``core.ExternalRef``.
    """
    for payload in (staged.mapped_values, staged.raw):
        ref = payload.get("external_ref") if isinstance(payload, dict) else None
        if isinstance(ref, dict) and ref.get("source") and ref.get("object_type") and ref.get("source_id"):
            return str(ref["source"]), str(ref["object_type"]), str(ref["source_id"])
    return None


def erased_source(company_id, staged: StagedRow) -> bool:
    """Whether the row's source record was erased (``core.provenance``).

    Also true when erasure already scrubbed the row (``raw`` is
    ``{"erased": True}``: the decision may have been taken before the
    person was erased), and when any ``external_ref["aliases"]`` (earlier
    ids of the same source record) is an erased one.
    """
    from core.provenance import is_erased_source

    if isinstance(staged.raw, dict) and staged.raw.get("erased") is True:
        return True
    key = source_key(staged)
    if key is None:
        return False
    if is_erased_source(company_id, *key):
        return True
    ref = (staged.mapped_values or {}).get("external_ref") if isinstance(staged.mapped_values, dict) else None
    aliases = ref.get("aliases") if isinstance(ref, dict) else None
    return any(is_erased_source(company_id, key[0], key[1], alias) for alias in aliases or ())


def _mark(approval_row: ApprovalRow, state: str):
    approval_row.state = state
    approval_row.error = ""
    approval_row.save(update_fields=["state", "error", "updated_at"])


# The product applier, under its Steps 3-4 name.
_apply_row = apply_product


def cancel_run(run_id: str) -> ApprovalRun:
    """Stop future chunks. Applied rows stay applied (undo is separate)."""
    run = ApprovalRun.objects.get(pk=run_id)
    run.status = ApprovalRun.Status.CANCELLED
    run.save(update_fields=["status", "updated_at"])
    return run


def undo_run(run_id: str, undo_token: str) -> dict:
    """Reverse every applied row through its applier.

    Products are deactivated, never deleted. An applier may refuse a row
    (``ValueError``): that row stays applied and is listed in ``refused``.
    """
    run = ApprovalRun.objects.select_related("batch").get(pk=run_id)
    if not run.undo_token or undo_token != run.undo_token:
        raise ValueError("Undo token mismatch — refusing to undo.")
    applier = get_applier(run.batch.entity_kind)
    rows = run.rows.filter(state=ApprovalRow.State.APPLIED).select_related(
        "staged_row"
    )
    if applier.undo_newest_first:
        rows = rows.order_by("-updated_at", "-staged_row__row_number")
    undone = 0
    refused = []
    for approval_row in rows:
        try:
            with transaction.atomic():
                undone += applier.undo(approval_row)
        except ValueError as exc:
            refused.append(
                {"row": approval_row.staged_row.row_number, "error": str(exc)}
            )
            continue
        approval_row.state = ApprovalRow.State.SKIPPED
        approval_row.save(update_fields=["state", "updated_at"])
        reopen(approval_row.staged_row, applier)
    return {"run": str(run.pk), "undone": undone, "refused": refused}


def reopen(staged: StagedRow, applier) -> None:
    """Put an undone row back where its applier says (``undo_state``)."""
    staged.applied_ref = ""
    fields = ["mapping_state", "applied_ref", "updated_at"]
    if applier.undo_state == "proposed":
        staged.mapping_state = StagedRow.MappingState.PROPOSED
        staged.mapped_values = {}
        fields.append("mapped_values")
    else:
        staged.mapping_state = StagedRow.MappingState.APPROVED
    staged.save(update_fields=fields)
