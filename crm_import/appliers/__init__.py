"""CRM appliers: approved staged rows → the domain (docs/crm/phase5-importer.md 1.5, 2, 4.5).

One applier per canonical object type, registered with ``unify``'s applier
registry from ``CrmImportConfig.ready`` as ``crm_<type>``. Each declares
``permission="crm_import.apply_import"``, ``undo_newest_first=True`` and
``undo_state="approved"`` (1.5), so the generic ``/erp/imports/`` screen
cannot apply or undo one without the permission, and an undone row goes back
to ``approved`` (re-applying is one run).

Built so far: owners, pipelines, companies, contacts and contact↔company
associations (step 4); deals, tickets (relationships) and the deal and
ticket associations (step 5); tasks, communications and consent (step 6).

Every applier runs through ``base.run`` (tenant check, erased-alias skip,
crash idempotency, ``suppressed()``, the job lock, coded errors) and writes
through a ``writelog.WriteSession``; ``undo`` is ``writelog.undo_row``.
"""

from __future__ import annotations

from .. import writelog
from . import (
    associations,
    base,
    communications,
    consent,
    deals,
    owners,
    parties,
    pipelines,
    relationships,
    tasks,
)

PERMISSION = "crm_import.apply_import"

HANDLERS = {
    "owner": owners.apply,
    "pipeline": pipelines.apply,
    "company": parties.apply,
    "contact": parties.apply,
    "deal": deals.apply,
    "association": associations.apply,
    "relationship": relationships.apply,
    "task": tasks.apply,
    "communication": communications.apply,
    "consent": consent.apply,
}


def undo(approval_row) -> int:
    try:
        return writelog.undo_row(approval_row, actor=f"undo-run:{approval_row.run_id}")
    except writelog.UndoRefused as exc:
        raise ValueError(str(exc)) from exc


def gate(batch, action: str) -> str:
    """``unify``'s gate: the generic screens send CRM batches to the job runner (step 8)."""
    from ..runner import generic_refusal

    return generic_refusal(batch, action)


def _apply_for(object_type: str):
    def apply(company, staged, **context) -> str:
        return base.run(HANDLERS[object_type], company, staged, **context)

    apply.__name__ = f"apply_{object_type}"
    return apply


APPLY = {otype: _apply_for(otype) for otype in HANDLERS}


def register() -> None:
    from unify.appliers import register_applier

    from ..models import entity_kind_for

    for otype in HANDLERS:
        register_applier(entity_kind_for(otype), APPLY[otype], undo, permission=PERMISSION,
                         undo_newest_first=True, undo_state="approved", gate=gate)

