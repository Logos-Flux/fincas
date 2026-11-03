"""Pipelines and stages, by key (docs/crm/phase5-importer.md 2.4; hand-off section 1).

- **create**: a ``crm.Pipeline`` (``is_default=False``: the tenant's default
  stays where lead intake works) and its stages, keys as the canonical file
  carries them (the validator checked the grammar; the applier never derives
  or "fixes" a key). ``probability`` → ``default_probability``; ``outcome``
  ``won`` / ``lost`` → ``outcome_hint``. A stage whose outcome is ``closed``
  (neither won nor lost) needs a reviewer's pick in
  ``mapped_values["stage_outcomes"][<stage key>]`` (``won``, ``lost`` or
  ``""``); without one the row fails ``invalid_value``: the importer does not
  guess whether "Closed — referred out" is a win.
- **link** (the key exists and its stages are a subset of the source's,
  2.4): ExternalRefs for the pipeline and for each stage it already has; the
  stages it lacks are created. An existing stage is never edited.
- **update** (re-import): as link; the pipeline's ``name`` / ``is_active``
  follow the source where the compare said so.

Stage ExternalRefs are pipeline-qualified, ``object_type="stage"``,
``source_id="<pipeline source id>/<stage source id>"`` (stage ids are unique
only inside their pipeline). ``Stage`` has no sidecar, so each stage's source
id, label and display order are kept on its ExternalRef
(``metadata["source_stage"]``); the pipeline's own properties go to its sidecar.

Ticket pipelines (``applies_to="ticket"``) are kept on the job, not written,
unless the job has ``tickets_as_engagements`` (2.8): tickets become
relationships, and their stages live in the staged rows for step 5.
"""

from __future__ import annotations

from ..sidecar import SidecarRefused, put
from .base import INVALID_VALUE, Row, RowError, applied_ref

STAGE_TYPE = "stage"
OUTCOMES = {"": "", "won": "won", "lost": "lost"}


def stage_source_id(pipeline_sid, stage_sid) -> str:
    return f"{pipeline_sid}/{stage_sid}"


def _outcome(row: Row, spec: dict) -> str:
    outcome = spec.get("outcome") or ""
    if outcome in OUTCOMES:
        return OUTCOMES[outcome]
    picked = (row.mapped.get("stage_outcomes") or {}).get(spec.get("key"))
    if picked in OUTCOMES:
        return OUTCOMES[picked]
    raise RowError(INVALID_VALUE, f"stage {spec.get('key')!r} is closed but neither won nor lost; "
                                  "a reviewer picks won, lost or none.")


def _stages(row: Row, pipeline) -> None:
    from crm.models import Stage

    existing = {s.key: s for s in Stage._base_manager.filter(pipeline_id=pipeline.pk)}
    for spec in row.data.get("stages") or []:
        stage = existing.get(spec.get("key"))
        if stage is None:
            stage = Stage(pipeline=pipeline, company_id=pipeline.company_id, key=spec["key"],
                          name=(spec.get("name") or spec["key"])[:100], sort_order=spec.get("sort_order") or 0,
                          default_probability=spec.get("probability"), outcome_hint=_outcome(row, spec))
            stage.save()
            row.session.create(stage)
        meta = {"source_stage": {"source_id": str(spec.get("source_id")), "name": spec.get("name") or "",
                          "sort_order": spec.get("sort_order"), "outcome": spec.get("outcome") or ""}}
        row.ref_for(stage, stage_source_id(row.record.get("source_id"), spec.get("source_id")),
                    object_type=STAGE_TYPE, metadata=meta)


def _sidecar(row: Row, pipeline) -> dict:
    """Source properties into the sidecar. A nested object (HubSpot's
    ``stage_metadata``, a dict per stage) is not a sidecar value (phase4-fields
    3.2); it is returned, to be kept on the pipeline's ExternalRef instead."""
    targets = row.property_targets()
    values = {k: v for k, v in row.properties.items() if targets.get(k, "sidecar") == "sidecar"}
    nested = {k: v for k, v in values.items() if isinstance(v, dict)}
    values = {k: v for k, v in values.items() if k not in nested}
    try:
        put(pipeline, row.provider, values, session=row.session)
    except SidecarRefused as exc:
        raise RowError(INVALID_VALUE, str(exc)) from exc
    return nested


def apply(row: Row) -> str:
    from crm.models import Pipeline

    data = row.data
    if data.get("applies_to") == "ticket" and not (row.job.options or {}).get("tickets_as_engagements"):
        return f"crm_import.importjob:{row.job.pk}"  # kept on the job (2.8)
    if row.decision == "create":
        pipeline = Pipeline(company_id=row.company.pk, key=data["key"], name=(data.get("name") or data["key"])[:100],
                            is_default=False, is_active=bool(data.get("active", True)),
                            sort_order=data.get("sort_order") or 0)
        pipeline.save()
        row.session.create(pipeline)
    elif row.decision in ("link", "update"):
        pipeline = row.target(Pipeline)
        if pipeline.key != data.get("key"):
            raise RowError(INVALID_VALUE, f"the matched pipeline's key is {pipeline.key!r}, not {data.get('key')!r}.")
        if row.decision == "update":
            diff = ((row.staged.proposed_mapping or {}).get("diff") or {}).get("fields") or {}
            columns = {}
            if "name" in diff and "name" not in row.kept:
                columns["name"] = (data.get("name") or pipeline.name)[:100]
            if "active" in diff and "active" not in row.kept:
                columns["is_active"] = bool(data.get("active"))
            if columns:
                row.session.update(pipeline, **columns)
    else:
        raise RowError(INVALID_VALUE, f"a pipeline row cannot be applied as {row.decision!r}.")
    _stages(row, pipeline)
    nested = _sidecar(row, pipeline)
    row.refs_for(pipeline, metadata={"source_properties": nested} if nested else None)
    return applied_ref(Pipeline, pipeline.pk)
