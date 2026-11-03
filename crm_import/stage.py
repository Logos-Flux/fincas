"""Staging and matching (docs/crm/phase5-importer.md 1.5, 3, 4.3, 4.10).

``stage(company, directory)`` turns a canonical export into an ``ImportJob``:

1. **Validate** (``validating``): the manifest and every file
   (``validate_canonical``). Any error ends the job as ``invalid`` with the
   issues in ``report["validation"]``; nothing is staged.
2. **Stage** (``staging``): one ``unify.ImportBatch`` per object type in
   step order (4.2), ``source_kind="crm"``, ``entity_kind="crm_<type>"``.
   Each file is streamed and written as ``StagedRow``s with
   ``bulk_create(batch_size=1000)``: ``raw`` = the canonical record,
   ``content_hash`` = the record minus ``raw_ref``. Not ``ingest_csv``: no
   10,000-row cap, no "hash applied anywhere" rule (re-import is per record).
3. **Match**, a second pass per batch in chunks of 1,000 rows with a fixed
   number of queries per chunk (``match.MatchContext``): the decision goes on
   ``proposed_mapping``, its confidence on ``mapping_confidence``.
4. **Auto-approve** rows at or above 0.95 with no flag (``auto_approve``).

The job ends in ``reviewing``. Re-running ``stage_job`` on a job resumes it:
staging continues after the last committed row of each batch, matching only
touches rows still ``unmapped``, approval only rows still ``proposed`` (4.10).

Property definitions are not a batch: their labels are kept on the job
(``report["property_labels"]``) for the review screen (2.12).

No appliers here: approved rows wait for steps 4-6.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from crm_import.canonical import OBJECT_TYPES, iter_records, read_manifest, validate_canonical
from unify.models import ImportBatch, StagedRow

from . import ai, compare, match

CHUNK = 1000
PROPERTY_DEF = "property_def"
# 4.2: the apply order, which is also the staging order.
STEP_ORDER: tuple[str, ...] = tuple(t for t in OBJECT_TYPES if t != PROPERTY_DEF)  # == models.STEPS order
MATCH_ACTOR = "system:crm_import.match"
ERASED_PAYLOAD = {"erased": True}  # parties.erasure.ERASED_PAYLOAD


def erased_payload(record: dict) -> dict:
    """What stays of an erased person's record: the fact, and its source id
    hashed the way erasure re-keys ExternalRefs (no id in clear: it may be a
    name)."""
    from core.provenance import erased_source_id

    sid = str((record or {}).get("source_id") or "")
    return {**ERASED_PAYLOAD, "source_key": erased_source_id(sid)} if sid else dict(ERASED_PAYLOAD)
MAX_PREVIOUS_CHAIN = 20


class StagingError(ValueError):
    """A staging request that cannot start. Message = problem + cause + fix."""


def entity_kind(object_type: str) -> str:
    from .models import entity_kind_for

    return entity_kind_for(object_type)


def auto_actor(job) -> str:
    return f"system:crm-auto-approve({job.pk}, {match.AUTO_APPROVE_THRESHOLD})"


# --- the job ------------------------------------------------------------------------


def stage(company, directory, *, previous_job=None, options=None, mapping=None, created_by=None):
    """Validate, stage, match and auto-approve one canonical export. Returns the job."""
    from .models import ImportJob

    directory = Path(directory)
    manifest = read_manifest(directory)  # refuses a missing manifest or a newer schema
    source = manifest.get("source") or ""
    if previous_job is not None:
        if previous_job.company_id != company.pk:
            raise StagingError(
                f"Job {previous_job.pk} belongs to another company — cause: --previous-job names "
                "another tenant's import. Fix: name a job of this company."
            )
        if previous_job.source != source:
            raise StagingError(
                f"Job {previous_job.pk} imported {previous_job.source!r}, this export is {source!r} — "
                "cause: a re-import compares against the same source only. Fix: name a job of this source."
            )
    job = ImportJob.objects.create(
        company=company, source=source[:64], provider=str(manifest.get("provider") or "")[:32],
        adapter_version=str((manifest.get("adapter") or {}).get("version") or "")[:32],
        manifest=manifest, mapping=mapping or {}, options=dict(options or {}),
        status=ImportJob.Status.VALIDATING, previous_job=previous_job, created_by=created_by,
    )
    result = validate_canonical(directory)
    job.coverage = "complete" if result.coverage and all(
        v == "complete" for v in result.coverage.values()) else "partial"
    if not result.ok:
        job.status = ImportJob.Status.INVALID
        job.report = {"validation": result.to_dict()}
        job.save(update_fields=["status", "coverage", "report", "updated_at"])
        return job
    job.report = {"validation": {"ok": True, "warnings": len(result.warnings), "counts": result.counts}}
    mapping_errors = check_mapping(job.mapping, source) + check_deals(job.mapping, directory, options=job.options)
    if mapping_errors:
        job.status = ImportJob.Status.INVALID
        job.report["mapping"] = {"ok": False, "errors": mapping_errors}
        job.save(update_fields=["status", "coverage", "report", "updated_at"])
        return job
    job.save(update_fields=["coverage", "report", "updated_at"])
    try:
        return stage_job(job, directory)
    except StagingError:
        job.refresh_from_db(fields=["status"])
        if job.status == ImportJob.Status.VALIDATING:  # lost the source lock: nothing staged
            job.status = ImportJob.Status.CANCELLED
            job.save(update_fields=["status", "updated_at"])
        raise


def check_mapping(mapping, source: str) -> list[str]:
    """The mapping file at staging (2.12): a TOML table for this export's
    ``source``. Its tables are the provider's (``[ticket.properties]``,
    ``[note.properties]``, …), so the provider's adapter checks them when it
    converts the export (``crm_export_adapt``: targets, and every property name
    against the provider's definitions); staging freezes the file onto the job
    so the appliers read the same one. Whether a ``column:`` target exists is
    the applier's check (steps 4-6)."""
    if not mapping:
        return []
    if not isinstance(mapping, dict):
        return ["The mapping file must be a TOML table."]
    if mapping.get("source") not in (None, source):
        return [f"The mapping file is for {mapping.get('source')!r}, the export is {source!r}."]
    return []


def check_deals(mapping, directory: Path, *, options=None) -> list[str]:
    """What the deal and ticket appliers need from the mapping file, checked
    before anything is staged (2.5, 2.8): every deal has a currency or the
    file names ``default_currency`` (the column is NOT NULL and golden never
    assumes one); ``[deal.loss_reasons]`` values are ``GOLDEN_LOSS_REASONS``
    keys; ``[tickets] relationship_kind`` is a relationship kind;
    ``[associations.deal_contact]`` values are engagement contact roles."""
    from crm.models import EngagementContact
    from crm.registries import LOSS_REASONS
    from parties.relationship_registry import get_relationship_kind

    mapping = mapping if isinstance(mapping, dict) else {}
    errors = []
    default = mapping.get("default_currency")
    if default is not None and not (isinstance(default, str) and len(default) == 3 and default.isalpha()):
        errors.append(f"default_currency = {default!r}: an ISO 4217 code such as \"USD\".")
    elif default is None:
        missing = sum(1 for _line, record in iter_records(directory, "deal")
                      if not (record.get("data") or {}).get("currency"))
        if (options or {}).get("tickets_as_engagements"):
            missing += sum(1 for _ in iter_records(directory, "relationship"))  # tickets carry no currency
        if missing:
            errors.append(f"{missing} deal(s) or ticket(s) have no currency and the mapping file names no "
                          "default_currency "
                          "(golden never assumes one). Fix: add default_currency = \"<ISO 4217>\".")
    for text, key in sorted(((mapping.get("deal") or {}).get("loss_reasons") or {}).items()):
        if key not in LOSS_REASONS:
            errors.append(f"[deal.loss_reasons] {text!r} = {key!r}: not a GOLDEN_LOSS_REASONS key.")
    kind = (mapping.get("tickets") or {}).get("relationship_kind")
    if kind is not None:
        try:
            get_relationship_kind(kind)
        except KeyError:
            errors.append(f"[tickets] relationship_kind = {kind!r}: not a GOLDEN_RELATIONSHIP_KINDS key.")
    for label, role in sorted(((mapping.get("associations") or {}).get("deal_contact") or {}).items()):
        if role not in EngagementContact.Role.values:
            errors.append(f"[associations.deal_contact] {label!r} = {role!r}: the roles are "
                          f"{', '.join(EngagementContact.Role.values)}.")
    return errors


def stage_job(job, directory, *, resume: bool = False, run_ai: bool = True):
    """Stage, match and auto-approve ``job`` from ``directory``.

    ``resume=True`` continues an interrupted staging (4.10): the job must be
    ``staging`` and the directory must be the export it was started from
    (same source and file digests). The source lock (``locks.begin_staging``)
    covers the move into ``staging``; ``job_run_lock`` keeps a second process
    off this job for the whole run. ``run_ai=False`` skips the AI suggestions
    a job that opted in would get (3.5): a rolled-back dry run must not make
    gateway calls whose budget spend the rollback would undo."""
    from .models import STEPS, ImportJob

    directory = Path(directory)
    if resume:
        _check_resume(job, directory)
    with job_run_lock(job):
        _begin(job)
        batches = {otype: _stage_type(job, directory, otype, step) for otype, step in STEPS.items()}
        job.report = {**job.report, "property_labels": _property_labels(directory)}
        chain = previous_chain(job)
        counts = {}
        for otype, batch in batches.items():
            match_batch(job, batch, otype, previous_jobs=chain)
            mark_shared_targets(batch)
            auto_approve(job, batch)
            counts[otype] = batch_counts(batch)
            counts[otype]["missing_in_source"] = missing_in_source(batch, otype, chain)
        job.status = ImportJob.Status.REVIEWING
        from .appliers.consent import report as consent_report

        # 2.11 / 4.4: the consent consequence is stated before anything is applied.
        job.report = {**job.report, "staging": counts, "consent": consent_report(job)}
        job.save(update_fields=["status", "report", "updated_at"])
        if run_ai and ai.enabled(job):
            # Suggestions only, after auto-approval: they never decide a row (3.5).
            ai.run(job)
    return job


def _begin(job) -> None:
    """Step 2's source lock: refuses while another job of the source is staging or applying."""
    from .locks import SourceBusy, begin_staging

    try:
        begin_staging(job)
    except SourceBusy as exc:
        raise StagingError(
            f"{exc} — cause: two imports of one source at once would race on its records. "
            "Fix: wait for that job, or cancel it."
        ) from exc
    except ValueError as exc:
        raise StagingError(str(exc)) from exc


# Two-int advisory keys: (this namespace, the job), so other users of advisory
# locks never share a keyspace with the importer.
ADVISORY_NAMESPACE = 0x43524D49  # "CRMI"


def _advisory_key(job) -> int:
    """The job's half of the key: a signed 32-bit int from its UUID."""
    return int.from_bytes(job.pk.bytes[:4], "big", signed=True)


@contextmanager
def job_run_lock(job, activity: str = "staged"):
    """Keep a second process off the job for the whole staging run (and, in
    step 8's runner, the whole apply, retry, undo or cancel: one key, so none
    of them overlaps another on the same job).

    Postgres: an advisory lock on (``ADVISORY_NAMESPACE``, job). Outside a
    transaction it is a session lock, released at the end or by Postgres when
    the session dies, so a crashed run resumes without waiting. Inside one
    (the step 8 dry run stages in a transaction it rolls back) it is a
    transaction lock, released with the transaction even when that aborts.
    Session locks need a direct connection: through a transaction-pooling
    proxy (PgBouncer) they do not hold. Other backends (SQLite: tests and
    development) have no second process to keep off: no lock."""
    from django.db import connection

    if connection.vendor != "postgresql":
        yield
        return
    key = _advisory_key(job)
    in_transaction = connection.in_atomic_block
    fn = "pg_try_advisory_xact_lock" if in_transaction else "pg_try_advisory_lock"
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {fn}(%s, %s)", [ADVISORY_NAMESPACE, key])
        if not cursor.fetchone()[0]:
            raise StagingError(
                f"Job {job.pk} is being {activity} by another process — cause: another run holds it. "
                "Fix: wait for it to finish; a crashed run releases it by itself."
            )
    if in_transaction:
        yield
        return
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [ADVISORY_NAMESPACE, key])


def _check_resume(job, directory: Path) -> None:
    from .models import ImportJob

    if job.status != ImportJob.Status.STAGING:
        raise StagingError(
            f"Job {job.pk} is {job.status}, not staging — cause: only an interrupted staging resumes. "
            "Fix: stage the export as a new job."
        )
    manifest = read_manifest(directory)
    if manifest.get("source") != job.source or manifest.get("files") != (job.manifest or {}).get("files"):
        raise StagingError(
            f"{directory} is not the export job {job.pk} was staged from — cause: its source or file "
            "digests differ from the job's manifest. Fix: resume with the original directory."
        )
    # The files themselves may have changed since (the validator re-hashes them).
    result = validate_canonical(directory)
    if not result.ok:
        raise StagingError(
            f"{directory} no longer validates ({len(result.errors)} error(s), e.g. "
            f"{result.errors[0].code if result.errors else 'suppressed'}) — cause: a file changed after "
            "staging started. Fix: restore the export, or stage it as a new job."
        )


def previous_chain(job) -> list:
    """The jobs this one re-imports over, newest first (3.4)."""
    chain, seen = [], {job.pk}
    current = job.previous_job
    while current is not None and current.pk not in seen and len(chain) < MAX_PREVIOUS_CHAIN:
        if current.company_id != job.company_id or current.source != job.source:
            break
        chain.append(current.pk)
        seen.add(current.pk)
        current = current.previous_job
    return chain


def job_rows_filter(job_ids, object_type: str) -> Q:
    """Staged rows of ``object_type`` in any of ``job_ids``."""
    return Q(batch__crm_import_job_batch__job_id__in=list(job_ids), batch__entity_kind=entity_kind(object_type))


def ai_budget_per_batch() -> int:
    return ai.budget_per_batch()


def _job_batch(job, object_type: str, step: int) -> ImportBatch:
    from .models import ImportJobBatch

    link = ImportJobBatch.objects.filter(job=job, object_type=object_type).select_related("batch").first()
    if link is not None:
        return link.batch
    with transaction.atomic():
        batch = ImportBatch.objects.create(
            company=job.company, source_kind=ImportBatch.SourceKind.CRM, source_name=job.source[:255],
            entity_kind=entity_kind(object_type), llm_call_budget=ai_budget_per_batch(),
        )
        ImportJobBatch.objects.create(job=job, batch=batch, object_type=object_type, step=step)
    return batch


def _stage_type(job, directory: Path, object_type: str, step: int) -> ImportBatch:
    """Stream one type's file into staged rows, 1,000 per committed insert."""
    batch = _job_batch(job, object_type, step)
    last = batch.rows.order_by("-row_number").values_list("row_number", flat=True).first() or 0
    pending: list[StagedRow] = []

    def flush():
        if pending:
            with transaction.atomic():
                StagedRow.objects.bulk_create(pending, batch_size=CHUNK)
            pending.clear()

    for line_no, record in iter_records(directory, object_type):
        if line_no <= last:
            continue  # committed by an earlier, interrupted run (4.10)
        pending.append(StagedRow(batch=batch, row_number=line_no, raw=record,
                                 content_hash=match.staged_hash(record)))
        if len(pending) >= CHUNK:
            flush()
    flush()
    return batch


def _property_labels(directory: Path) -> dict:
    """``{object_type: {name: {"label": ..., "options": {value: label}}}}``."""
    out: dict = {}
    for _line, record in iter_records(directory, PROPERTY_DEF):
        data = record.get("data") or {}
        out.setdefault(data.get("object_type") or "", {})[data.get("name") or ""] = {
            "label": data.get("label") or "",
            "options": {o.get("value"): o.get("label") for o in data.get("options") or []},
        }
    return out


# --- matching -----------------------------------------------------------------------


def match_batch(job, batch: ImportBatch, object_type: str, *, previous_jobs=None, rows=None) -> int:
    """Decide every ``unmapped`` row of ``batch`` (or only ``rows``), 1,000 at a time."""
    if previous_jobs is None:
        previous_jobs = previous_chain(job)
    ctx = match.MatchContext(job.company, object_type, source=job.source,
                             previous_jobs=previous_jobs, options=job.options)
    ctx.job = job
    ctx.warm()
    ctx.seen.update(str(s) for s in batch.rows.exclude(
        mapping_state=StagedRow.MappingState.UNMAPPED).exclude(raw__source_id=None)
        .values_list("raw__source_id", flat=True))
    qs = batch.rows.filter(mapping_state=StagedRow.MappingState.UNMAPPED)
    if rows is not None:
        qs = qs.filter(pk__in=[getattr(r, "pk", r) for r in rows])
    done, last = 0, 0
    while True:
        chunk = list(qs.filter(row_number__gt=last).order_by("row_number")[:CHUNK])
        if not chunk:
            return done
        last = chunk[-1].row_number
        _apply_decisions(chunk, ctx.decide_chunk([r.raw for r in chunk]))
        done += len(chunk)


def _apply_decisions(rows: list[StagedRow], decisions: list[match.Decision]) -> None:
    now = timezone.now()
    erased = []
    for row, decision in zip(rows, decisions):
        row.proposed_mapping = decision.proposal()
        row.mapping_confidence = decision.confidence
        row.mapping_state = StagedRow.MappingState.NA if decision.is_na else StagedRow.MappingState.PROPOSED
        row.mapping_actor = MATCH_ACTOR
        row.updated_at = now
        if decision.drop_raw:
            row.raw = erased_payload(row.raw)
            row.mapped_values = {}
            erased.append(row)
        elif decision.replace_raw is not None:
            row.raw = decision.replace_raw
            erased.append(row)
    with transaction.atomic():
        StagedRow.objects.bulk_update(
            rows, ["proposed_mapping", "mapping_confidence", "mapping_state", "mapping_actor", "updated_at"],
            batch_size=CHUNK,
        )
        # Phase 2 7.5: the erased person's data does not stay in staging.
        StagedRow.objects.bulk_update(erased, ["raw", "mapped_values"], batch_size=CHUNK)


def mark_shared_targets(batch: ImportBatch) -> int:
    """Flag every proposed link, update or conflict whose target another row
    of the batch also writes to (3.3 "many-to-many, made explicit"; two refs
    reach one party after a merge): allowed, never automatic."""
    by_target: dict[str, list] = {}
    rows = batch.rows.filter(mapping_state=StagedRow.MappingState.PROPOSED,
                             proposed_mapping__decision__in=[match.LINK, match.UPDATE, match.CONFLICT])
    for pk, proposal in rows.values_list("pk", "proposed_mapping"):
        target = (proposal.get("target") or {}).get("id")
        if target:
            by_target.setdefault(target, []).append((pk, proposal))
    shared = [(pk, p) for group in by_target.values() if len(group) > 1 for pk, p in group]
    if not shared:
        return 0
    objs = []
    for pk, proposal in shared:
        proposal["flags"] = sorted(set(proposal.get("flags") or []) | {match.FLAG_SHARED_TARGET})
        objs.append(StagedRow(pk=pk, proposed_mapping=proposal))
    StagedRow.objects.bulk_update(objs, ["proposed_mapping"], batch_size=CHUNK)
    return len(objs)


# --- approval -----------------------------------------------------------------------


def approval_values(record: dict, object_type: str, decision: str, target: dict | None, *,
                    rule: str | None = None, keep: list | None = None) -> dict:
    """``mapped_values`` of an approved row: the decision, its target, the
    ``external_ref`` (so ``unify.apply`` skips an erased source unchanged)
    and the values the applier writes (minus fields kept as golden has them)."""
    values = compare.project(object_type, record)
    for name in keep or []:
        values.pop(name, None)
    out = {"decision": decision, "target": target, "rule": rule,
           "external_ref": match.external_ref_key(record), "values": values}
    if keep:
        out["keep"] = sorted(keep)
    return out


def auto_approve(job, batch: ImportBatch, *, actor: str | None = None) -> int:
    """Approve every proposed row at or above 0.95 with no flag (I7, 4.8)."""
    from core.models import AuditLog

    actor = actor or auto_actor(job)
    otype = batch.entity_kind.removeprefix("crm_")
    qs = batch.rows.filter(mapping_state=StagedRow.MappingState.PROPOSED,
                           mapping_confidence__gte=match.AUTO_APPROVE_THRESHOLD)
    approved, last, now = 0, 0, timezone.now()
    while True:
        with transaction.atomic():
            # Locked and re-read: a reviewer's decision committed meanwhile
            # (step 8 re-runs this rule while review is open) is never overwritten.
            chunk = list(qs.filter(row_number__gt=last).order_by("row_number").select_for_update()[:CHUNK])
            if not chunk:
                break
            last = chunk[-1].row_number
            approved += _auto_approve_chunk(chunk, otype, actor, now)
    if approved:
        AuditLog.objects.create(action="approve", model_name="unify.ImportBatch", object_id=batch.pk,
                                changes={"actor": actor, "approved": approved, "job": str(job.pk)})
    return approved


def _auto_approve_chunk(chunk, otype: str, actor: str, now) -> int:
    picked = []
    for row in chunk:
        proposal = row.proposed_mapping or {}
        if not match.bulk_approvable(proposal, row.mapping_confidence):
            continue
        row.mapped_values = approval_values(row.raw, otype, proposal["decision"], proposal.get("target"),
                                            rule=proposal.get("rule"))
        row.mapping_state = StagedRow.MappingState.APPROVED
        row.mapping_actor = actor
        row.updated_at = now
        picked.append(row)
    StagedRow.objects.bulk_update(picked, ["mapped_values", "mapping_state", "mapping_actor", "updated_at"],
                                  batch_size=CHUNK)
    return len(picked)


class DecisionError(ValueError):
    pass


def approve_row(row: StagedRow, actor: str, *, candidate: str | None = None, create: bool = False,
                keep: list | None = None, take: list | None = None, outcome: str | None = None) -> StagedRow:
    """A reviewer's decision on one proposed row (4.6): approve the proposal,
    pick a candidate, create new instead, or settle a conflict field by field
    (``keep`` golden's value / ``take`` the source's, every conflict field named).
    ``outcome`` (deals and ticket engagements: ``open`` / ``won`` / ``lost``)
    settles an ``outcome_conflict`` (2.5); the applier reads it from
    ``mapped_values["outcome"]``."""
    if not actor:
        raise DecisionError("A decision always names its decider: actor required.")
    failed_retry = outcome is not None and row.mapping_state == StagedRow.MappingState.FAILED
    if row.mapping_state != StagedRow.MappingState.PROPOSED and not failed_retry:
        # A deal that failed outcome_conflict at apply (the golden stage's hint
        # differs from the export's) is re-decided with an outcome.
        raise DecisionError(f"Row {row.row_number} is {row.mapping_state}, not proposed.")
    if match.is_erased(row.raw):
        raise DecisionError(f"Row {row.row_number} belongs to an erased person: it is never imported.")
    proposal = row.proposed_mapping or {}
    otype = row.batch.entity_kind.removeprefix("crm_")
    decision, target, rule = proposal.get("decision"), proposal.get("target"), proposal.get("rule")
    if create:
        if decision in (match.UPDATE, match.CONFLICT):
            raise DecisionError("A record golden already holds (ExternalRef) is updated, never created again.")
        decision, target, rule = match.CREATE, None, None
    elif candidate is not None:
        chosen = next((c for c in proposal.get("candidates") or [] if str(c.get("id")) == str(candidate)), None)
        if chosen is None:
            raise DecisionError(f"{candidate} is not one of this row's candidates.")
        model = (target or {}).get("model") or {"owner": match.PROFILE_MODEL, "pipeline": match.PIPELINE_MODEL}.get(
            otype, match.PARTY_MODEL)
        decision, target, rule = match.LINK, {"model": model, "id": str(chosen["id"])}, chosen.get("rule")
    elif decision is None:
        raise DecisionError("This row has no proposal: pick a candidate or create new.")
    keep = list(keep or [])
    if decision == match.CONFLICT:
        conflicts = {f for f, s in ((proposal.get("diff") or {}).get("fields") or {}).items() if s == compare.CONFLICT}
        settled = set(keep) | set(take or [])
        if conflicts - settled:
            raise DecisionError(f"Settle every conflicting field (keep or take): {sorted(conflicts - settled)}.")
        decision = match.UPDATE
    if outcome is not None and (otype not in ("deal", "relationship") or outcome not in ("open", "won", "lost")):
        raise DecisionError("An outcome is open, won or lost, and only for deals and tickets.")
    row.mapped_values = approval_values(row.raw, otype, decision, target, rule=rule, keep=keep)
    if outcome is not None:
        row.mapped_values["outcome"] = outcome
    row.mapping_state = StagedRow.MappingState.APPROVED
    row.mapping_actor = actor
    fields = ["mapped_values", "mapping_state", "mapping_actor", "updated_at"]
    if failed_retry:
        row.error = ""
        fields.append("error")
    row.save(update_fields=fields)
    return row


def skip_row(row: StagedRow, actor: str, reason: str = match.REJECTED) -> StagedRow:
    """Mark a proposed row N/A with a reason (4.8 "Skip selected")."""
    if not actor:
        raise DecisionError("A decision always names its decider: actor required.")
    if reason not in match.NA_REASONS:
        raise DecisionError(f"{reason!r} is not an N/A reason ({', '.join(match.NA_REASONS)}).")
    if row.mapping_state not in (StagedRow.MappingState.PROPOSED, StagedRow.MappingState.APPROVED):
        raise DecisionError(f"Row {row.row_number} is {row.mapping_state}.")
    row.proposed_mapping = {**(row.proposed_mapping or {}), "decision": match.NA, "reason": reason}
    row.mapped_values = {}
    row.mapping_state = StagedRow.MappingState.NA
    row.mapping_actor = actor
    row.save(update_fields=["proposed_mapping", "mapped_values", "mapping_state", "mapping_actor", "updated_at"])
    return row


# --- counts -------------------------------------------------------------------------


def missing_in_source(batch: ImportBatch, object_type: str, previous_jobs) -> int:
    """Records an earlier job of the chain applied that this export no longer
    has (3.4): reported, never deleted or deactivated."""
    if not previous_jobs:
        return 0
    from core.provenance import erased_source_id

    # A JSON key path decodes "18" to the number 18 on SQLite: compare as text.
    before = {str(s) for s in StagedRow.objects.filter(
        job_rows_filter(previous_jobs, object_type), mapping_state=StagedRow.MappingState.APPLIED,
    ).values_list("raw__source_id", flat=True) if s is not None}
    now: set = set()
    for sid, aliases, key in batch.rows.values_list("raw__source_id", "raw__aliases", "raw__source_key"):
        if isinstance(aliases, str):  # a JSON array key path comes back as text on some backends
            aliases = json.loads(aliases)
        now |= {str(x) for x in (sid, key, *(aliases or [])) if x is not None}
    return sum(1 for sid in before if sid not in now and erased_source_id(str(sid)) not in now)


def batch_counts(batch: ImportBatch) -> dict:
    """Rows by state, by decision and by N/A reason (for the job report)."""
    by_state: dict[str, int] = {}
    by_decision: dict[str, int] = {}
    na: dict[str, int] = {}
    removed: dict[str, int] = {}
    for state, decision, reason, gone in batch.rows.values_list(
            "mapping_state", "proposed_mapping__decision", "proposed_mapping__reason",
            "proposed_mapping__diff__removed"):
        for field, n in (gone or {}).items():
            removed[field] = removed.get(field, 0) + n
        by_state[state] = by_state.get(state, 0) + 1
        by_decision[str(decision)] = by_decision.get(str(decision), 0) + 1
        if state == StagedRow.MappingState.NA:
            na[reason or ""] = na.get(reason or "", 0) + 1
    return {"rows": sum(by_state.values()), "by_state": by_state, "by_decision": by_decision, "na": na,
            "removed_in_source": removed}


__all__ = [
    "CHUNK", "DecisionError", "STEP_ORDER", "StagingError", "approve_row", "approval_values", "auto_approve",
    "batch_counts", "entity_kind", "mark_shared_targets", "match_batch", "previous_chain", "skip_row",
    "stage", "stage_job",
]

