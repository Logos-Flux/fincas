"""Deals → engagements (docs/crm/phase5-importer.md 2.5; hand-off sections 2-4, 6, 7).

An imported deal is **inserted in its final state**, its source history
becomes ``EngagementStageChange(source="import")`` rows with source
timestamps (``crm.engagements.record_imported_history``), its follow-up
fields become at most one open task, and nothing announces any of it (the
row runs inside ``suppressed()``, ``appliers.base.run``). It never creates a
Quote or an Order (6.2).

**Pipeline and stage, by ExternalRef.** ``pipeline_ref`` and ``stage_ref``
resolve through the refs the pipelines applier wrote (a stage's is
``<pipeline source id>/<stage source id>``); one that does not resolve is
``dependency_missing``.

**The outcome** (2.5 with the hand-off's correction, section 2). The canonical
``status`` comes from the source's closed/won flags; ``null`` is resolved by
the stage: a ``won`` / ``lost`` hint gives that outcome, an unhinted working
stage gives ``open``, a closed stage with no hint is a review item. A flag
that disagrees with the stage (won in a lost stage, open in a closed one) is
``outcome_conflict``: the row fails ``invalid_value`` until a reviewer picks
the outcome (``mapped_values["outcome"]``, ``stage.approve_row(outcome=…)``).
Never resolved silently, never stored open in a closed stage.

**The stage.** Open: the current stage. Closed in an unhinted stage ("lost
at Negotiation"): that stage. Closed in a won/lost stage: the last open stage
before closing from the history, else ``NULL`` with the closed stage's key in
the sidecar (``<provider>.import_closed_stage``).

**History** (hand-off section 3) is planned by :func:`plan_history` from the
``dealstage`` / ``pipeline`` entries (tickets: ``hs_pipeline_stage`` /
``hs_pipeline``), one chain, strictly increasing; an entry naming a stage the
export lacks is skipped and kept in ``<provider>.import_unmapped_history``.

**Amount and currency** (hand-off 7). A blank amount is ``NULL``, never zero;
a negative one is ``invalid_value``; more than two decimals is rounded half
up and flagged. The currency is always passed: the record's, else the
mapping file's ``default_currency``, else the row refuses (staging refuses
the job first, ``stage.check_deals``).

**Party** (2.5): the deal's primary company, else its only contact, else the
primary contact association, else ``NULL`` (``party_ambiguous`` when there
were several candidates). Extra companies are listed in
``<provider>.import_other_companies``. **Owner**: ``owner_ref``; one that is
not in the export (or was not imported) is ``NULL``, flagged
``owner_unresolved``; one still waiting for review is ``dependency_missing``.

**Follow-up** (hand-off 4): for an open engagement with any follow-up value,
one ``crm.tasks.create(kind="follow_up", source="import:<provider>:follow_up")``
due at the latest date or instant among the values (a date is all-day in the
assignee's zone), titled by HubSpot's ``hs_next_step``, else "Follow up" (a value that is
neither that nor a date stays in the sidecar). Already due → ``due_notified_at`` set to
the import time in the same transaction, so ``crm_emit_due`` stays quiet.

**Write log.** The engagement is one ``create`` entry; its refs, history rows,
follow-up task and task event follow; the next action the task sets on the
engagement is a final ``update`` entry whose ``before`` is the row as the
create left it (``WriteSession.checkpoint``), so undo clears the pointer
before it deletes the task.

**Re-import** (``update``): changed columns (title, amount, currency,
expected close date) with ``update()``; a changed outcome, stage or pipeline
through ``move`` / ``win`` / ``lose`` / ``reopen`` / ``change_pipeline`` with
``source="import"`` (stamped now: golden learns of it at import time); the
history rows they write are logged, so undo removes them and restores the
row exactly.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from ..sidecar import DERIVED_PREFIX, SidecarRefused, put
from .base import CONFLICT_STALE, DEPENDENCY_MISSING, INVALID_VALUE, Row, RowError, applied_ref

OPEN, WON, LOST = "open", "won", "lost"
CLOSED = "closed"  # a ticket's ``state``: closed, outcome unknown (the stage decides)
CLOSED_STATUSES = (WON, LOST)
OUTCOMES = (OPEN, WON, LOST)
FLAG_OUTCOME_CONFLICT = "outcome_conflict"
FLAG_PARTY_AMBIGUOUS = "party_ambiguous"
FLAG_CLOSED_AT_INFERRED = "closed_at_inferred"
FLAG_OWNER_UNRESOLVED = "owner_unresolved"
FLAG_AMOUNT_ROUNDED = "amount_rounded"
STAGE_PROPS = {"deal": ("dealstage",), "relationship": ("hs_pipeline_stage",)}
PIPELINE_PROPS = {"deal": ("pipeline",), "relationship": ("hs_pipeline",)}
#: ``column:engagement.<field>`` targets the mapping file may name (2.12).
ENGAGEMENT_COLUMN_TARGETS = ("probability", "source", "loss_competitor")
#: Follow-up fields whose value is the task's title (HubSpot's next step).
TITLE_FIELDS = ("hs_next_step",)
TWO_PLACES = Decimal("0.01")
ONE_MICROSECOND = dt.timedelta(microseconds=1)


# --- canonical values -------------------------------------------------------------


def parse_instant(value) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value
    parsed = parse_datetime(str(value).replace("Z", "+00:00"))
    if parsed is None or timezone.is_naive(parsed):
        return None
    return parsed.astimezone(dt.UTC)


def follow_up_due(value):
    """A follow-up value as a due: an aware instant, a date, or None (text)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if len(text) == 10:
        day = parse_date(text)
        if day is not None:
            return day
    return parse_instant(text)


def amount_of(value) -> tuple[Decimal | None, bool]:
    """``(amount, rounded)``. ``None`` stays ``None`` (never zero)."""
    if value is None or value == "":
        return None, False
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise RowError(INVALID_VALUE, f"amount {value!r} is not a decimal.") from None
    if not amount.is_finite():
        raise RowError(INVALID_VALUE, f"amount {value!r} is not a decimal.")
    if amount < 0:
        raise RowError(INVALID_VALUE, "a negative amount is refused, never clamped.")
    rounded = amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)
    if rounded.adjusted() >= 12:
        raise RowError(INVALID_VALUE, f"amount {value!r} does not fit 12 digits before the point.")
    return rounded, rounded != amount


def currency_of(row: Row) -> str:
    currency = row.data.get("currency") or row.mapping.get("default_currency")
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        raise RowError(INVALID_VALUE, "the deal has no currency and the mapping file names no "
                                      "default_currency; golden never assumes one.")
    return currency.upper()


# --- the outcome ------------------------------------------------------------------


def stage_outcome(stage, source: str) -> str:
    """The canonical outcome of an applied stage: its hint, or ``closed`` when
    the pipelines applier recorded a closed stage a reviewer left unhinted."""
    if stage.outcome_hint:
        return stage.outcome_hint
    from core.models import ExternalRef

    ct = ContentType.objects.get_for_model(type(stage))
    for meta in ExternalRef._base_manager.filter(content_type=ct, object_id=str(stage.pk), source=source,
                                                 company_id=stage.company_id).values_list("metadata", flat=True):
        if ((meta or {}).get("source_stage") or {}).get("outcome") == "closed":
            return "closed"
    return ""


def canonical_outcome_conflict(status, outcome) -> bool:
    """Whether a canonical status and a canonical stage outcome (``""`` /
    ``won`` / ``lost`` / ``closed`` / None = unknown) need a reviewer (2.5).
    ``status`` ``closed`` (a closed ticket) needs a won or lost stage."""
    if outcome is None:
        return False
    if status == CLOSED:
        return outcome not in CLOSED_STATUSES
    if status in CLOSED_STATUSES:
        return outcome in CLOSED_STATUSES and outcome != status
    if status == OPEN:
        return outcome != ""
    return outcome == "closed"  # null in a closed stage with no hint


def resolve_outcome(status, outcome: str, pick: str | None) -> str:
    """The engagement's status. ``outcome`` is :func:`stage_outcome`'s; ``pick``
    a reviewer's choice for a flagged row. Raises ``RowError`` for a conflict
    nobody resolved."""
    if pick is not None:
        if pick not in OUTCOMES:
            raise RowError(INVALID_VALUE, f"the reviewer's outcome {pick!r} is not open, won or lost.")
        if pick == OPEN and outcome in CLOSED_STATUSES:
            raise RowError(INVALID_VALUE, "an engagement is never stored open in a won/lost stage; "
                                          "pick won or lost.")
        return pick
    if canonical_outcome_conflict(status, outcome):
        raise RowError(INVALID_VALUE, f"{FLAG_OUTCOME_CONFLICT}: the source says {status or 'nothing'} "
                                      f"but the stage is {outcome}; a reviewer picks the outcome.")
    if status in OUTCOMES:
        return status
    return outcome if outcome in CLOSED_STATUSES else OPEN


# --- history ----------------------------------------------------------------------


@dataclass
class HistoryPlan:
    changes: list = field(default_factory=list)
    stage: object = None  # the engagement's stage column
    unmapped: list = field(default_factory=list)


@dataclass
class _State:
    pipeline: object = None
    stage: object = None  # the last open stage (the column a closed engagement keeps)
    status: str = ""


def _same(a, b) -> bool:
    return getattr(a, "pk", None) == getattr(b, "pk", None)


def _step(state: _State, rows: list, emit, at, target_pipeline, stage) -> None:
    """One mapped stage entry: the row it writes, if any (rules 1-6)."""
    hint = stage.outcome_hint
    moved = not _same(target_pipeline, state.pipeline)
    if not rows:
        if hint:
            emit(target_pipeline, None, hint, at)  # created closed: no open stage known
        else:
            emit(target_pipeline, stage, OPEN, at)  # 1
    elif state.status == OPEN:
        if not hint:
            if moved or not _same(stage, state.stage):
                emit(target_pipeline, stage, OPEN, at)  # 2, 3
        elif moved:
            emit(target_pipeline, None, hint, at)  # 3 + 4: closed straight into another pipeline
        else:
            emit(state.pipeline, state.stage, hint, at)  # 4: the stage does not move
    elif not hint:
        emit(target_pipeline, stage, OPEN, at)  # 5: reopened at an open stage
    elif hint != state.status or moved:
        emit(target_pipeline, state.stage if not moved else None, hint, at)  # 6


def plan_history(*, entries, stage_props, pipeline_props, pipeline, current_stage, status,
                 closed_at, created_at, pipeline_of, stage_of, stage_elsewhere=None) -> HistoryPlan:
    """The rows of hand-off section 3, oldest first, for one engagement.

    ``entries`` are the canonical record's ``history`` (``{property, value,
    at}``); only ``stage_props`` and ``pipeline_props`` are read.
    ``pipeline`` / ``current_stage`` / ``status`` are the engagement's final
    pipeline, its source's current stage and the resolved outcome;
    ``pipeline_of(source_id)`` and ``stage_of(pipeline, source_id)`` resolve
    a history value (None: not in the export, so the entry is skipped and
    kept in ``unmapped``); ``stage_elsewhere(source_id)`` (optional) finds a
    stage the deal's pipeline lacks in the one other pipeline that has it, for
    an export whose ``pipeline`` history is missing. The returned ``stage`` is the engagement's stage
    column. The rules, by number: 1 creation, 2 open move, 3 pipeline change
    (one row with both), 4 close (the stage stays; the final close at
    ``closed_at``), 5 reopen, 6 won↔lost directly, 7 closed with no usable
    open stage (``to_stage`` NULL).
    """
    plan = HistoryPlan()
    state = _State()
    rows = plan.changes

    def emit(to_pipeline, to_stage, to_status, at):
        rows.append({"from_pipeline": state.pipeline, "to_pipeline": to_pipeline,
                     "from_stage": state.stage, "to_stage": to_stage,
                     "from_status": state.status, "to_status": to_status, "changed_at": at})
        state.pipeline, state.stage, state.status = to_pipeline, to_stage, to_status

    events: dict[dt.datetime, dict] = {}
    for entry in entries or []:
        prop = entry.get("property")
        if prop not in stage_props and prop not in pipeline_props:
            continue
        at = parse_instant(entry.get("at"))
        value = entry.get("value")
        if at is None or value in (None, ""):
            continue
        slot = events.setdefault(at, {"pipeline": None, "stages": []})
        if prop in pipeline_props:
            slot["pipeline"] = str(value)
        else:
            slot["stages"].append(str(value))  # same instant: source order
    pending = None  # a pipeline entry waiting for the stage entry that moves the deal there
    for at in sorted(events):
        slot = events[at]
        if slot["pipeline"] is not None:
            found = pipeline_of(slot["pipeline"])
            if found is None:
                plan.unmapped.append(f"{pipeline_props[0]}={slot['pipeline']}@{at.isoformat()}")
            else:
                pending = found
        for value in slot["stages"]:
            target_pipeline = pending or state.pipeline or pipeline
            stage = stage_of(target_pipeline, value)
            if stage is None and pending is None and stage_elsewhere is not None:
                stage = stage_elsewhere(value)
                if stage is not None:
                    target_pipeline = stage.pipeline
            if stage is None:
                plan.unmapped.append(f"{stage_props[0]}={value}@{at.isoformat()}")
                continue
            pending = None
            _step(state, rows, emit, at, target_pipeline, stage)

    # The engagement's own state, which the last row must reach.
    if status == OPEN or not current_stage.outcome_hint:
        final_stage = current_stage
    elif _same(state.pipeline, pipeline) and state.stage is not None:
        final_stage = state.stage  # closed: the last open stage before closing
    else:
        final_stage = None
    plan.stage = final_stage
    if not rows:  # no usable history: creation, then closing (hand-off section 2's table)
        if final_stage is None:
            emit(pipeline, None, status, closed_at or created_at)  # 7
        else:
            emit(pipeline, final_stage, OPEN, created_at)
    if status == OPEN and state.status != OPEN:
        # Reopened after the last mapped entry: one rule-5 row, straight to the stage.
        emit(pipeline, final_stage, OPEN, rows[-1]["changed_at"] + ONE_MICROSECOND)
    if not (_same(state.pipeline, pipeline) and _same(state.stage, final_stage)):
        tail = rows[-1]["changed_at"] + ONE_MICROSECOND
        if final_stage is None:
            emit(pipeline, None, status, tail)
        else:
            emit(pipeline, final_stage, state.status, tail)
    if state.status != status:
        emit(pipeline, final_stage, status, closed_at if status in CLOSED_STATUSES else
             rows[-1]["changed_at"] + ONE_MICROSECOND)
    if status in CLOSED_STATUSES and closed_at is not None:
        # The final close happens at the engagement's closed_at (rule 4).
        for change in reversed(rows):
            if change["to_status"] == status and change["from_status"] != status:
                change["changed_at"] = closed_at
                break
    for i in range(1, len(rows)):
        if rows[i]["changed_at"] <= rows[i - 1]["changed_at"]:
            rows[i]["changed_at"] = rows[i - 1]["changed_at"] + ONE_MICROSECOND  # strictly increasing
    return plan


# --- references --------------------------------------------------------------------


def staged_state(job, object_type: str, source_id) -> str | None:
    """The mapping state of a record of ``object_type`` in ``job``'s export
    (None: the export does not have it)."""
    from crm_import.models import ImportJobBatch
    from unify.models import StagedRow

    batch_ids = ImportJobBatch._base_manager.filter(job_id=job.pk, object_type=object_type).values("batch_id")
    return (StagedRow.objects.filter(batch_id__in=batch_ids, raw__source_id=str(source_id))
            .values_list("mapping_state", flat=True).first())


def is_erased_ref(row: Row, ref: dict | None) -> bool:
    """Whether a ``*_ref`` names a record of an erased person (its refs are
    re-keyed by the erasure, so it no longer resolves)."""
    from core.provenance import is_erased_source

    from .base import source_object_types

    if not ref or not ref.get("source_id"):
        return False
    return any(is_erased_source(row.company.pk, row.job.source, otype, ref["source_id"])
               for otype in source_object_types(row.job, ref.get("object_type") or ""))


def resolve_optional(row: Row, ref: dict | None, model):
    """``(obj, absent)``: the row a ref names, or ``(None, True)`` when the
    export lacks the record, it was not imported (N/A) or its person was
    erased. A record still waiting for review or apply is
    ``dependency_missing`` (retry later)."""
    from unify.models import StagedRow

    if not ref:
        return None, False
    found = row.resolve(ref, model, required=False)
    if found is not None:
        return found, False
    if is_erased_ref(row, ref):
        return None, True
    state = staged_state(row.job, ref.get("object_type") or "", ref.get("source_id"))
    if state in (None, StagedRow.MappingState.NA):
        return None, True
    raise RowError(DEPENDENCY_MISSING, f"{ref.get('object_type')} {ref.get('source_id')} is not imported yet "
                                       "(apply it, then retry this row).")


def live_party(party):
    """Follow a merge to the survivor; None for an erased party (never a counterparty)."""
    from parties.models import Party

    seen = set()
    while party is not None and party.merged_into_id is not None and party.pk not in seen:
        seen.add(party.pk)
        party = Party._base_manager.filter(pk=party.merged_into_id).first()
    if party is None or party.erased_at is not None:
        return None
    return party


_PRIMARY: dict = {}


def primary_associations(job, object_type: str, source_id, *, run_id=None) -> dict:
    """``{"contact": sid, "company": sid}``: the ends the job's association rows
    mark primary for one deal (or ticket). One query per apply run (memoised
    per ``(job, run)``: a new run, a resumed staging included, reads afresh)."""
    from crm_import.models import ImportJobBatch
    from unify.models import StagedRow

    key = (str(job.pk), str(run_id))
    if key not in _PRIMARY:
        _PRIMARY.clear()  # one run at a time per process: keep only the current one
        index: dict = {}
        batch_ids = ImportJobBatch._base_manager.filter(job_id=job.pk, object_type="association").values("batch_id")
        for raw in StagedRow.objects.filter(batch_id__in=batch_ids, raw__data__primary=True).values_list(
                "raw", flat=True):
            data = (raw or {}).get("data") or {}
            a, b = data.get("from_ref") or {}, data.get("to_ref") or {}
            for mine, other in ((a, b), (b, a)):
                if other.get("object_type") in ("contact", "company"):
                    slot = index.setdefault((mine.get("object_type"), str(mine.get("source_id"))), {})
                    slot.setdefault(other["object_type"], str(other.get("source_id")))
        _PRIMARY[key] = index
    return dict(_PRIMARY[key].get((object_type, str(source_id)), {}))


@dataclass
class Counterparty:
    party: object = None
    other_companies: list = field(default_factory=list)
    flags: list = field(default_factory=list)


def counterparty(row: Row, *, object_type: str = "deal") -> Counterparty:
    """``Engagement.party`` (2.5): the primary company; else the only contact;
    else the primary contact association; else NULL."""
    from parties.models import Party

    data = row.data
    out = Counterparty()
    companies = [r for r in data.get("company_refs") or [] if r]
    contacts = [r for r in data.get("contact_refs") or [] if r]
    primary = {}
    if len(companies) > 1 or (not companies and len(contacts) > 1):
        primary = primary_associations(row.job, object_type, row.record.get("source_id"),
                                       run_id=row.approval_row.run_id)
    chosen = None
    if len(companies) == 1:
        chosen = companies[0]
    elif companies:
        chosen = next((r for r in companies if str(r.get("source_id")) == primary.get("company")), None)
        if chosen is None:
            out.flags.append(FLAG_PARTY_AMBIGUOUS)
    elif len(contacts) == 1:
        chosen = contacts[0]
    elif contacts:
        chosen = next((r for r in contacts if str(r.get("source_id")) == primary.get("contact")), None)
        if chosen is None:
            out.flags.append(FLAG_PARTY_AMBIGUOUS)
    if chosen is not None:
        party, _absent = resolve_optional(row, chosen, Party)
        out.party = live_party(party)
    out.other_companies = [str(r.get("source_id")) for r in companies if r is not chosen]
    return out


def owner_of(row: Row):
    """``(profile, flags)``; an owner the export lacks is NULL and flagged."""
    from core.models import UserProfile

    ref = row.data.get("owner_ref")
    owner, absent = resolve_optional(row, ref, UserProfile)
    return owner, ([FLAG_OWNER_UNRESOLVED] if absent else [])


def pipeline_and_stage(row: Row):
    from crm.models import Pipeline, Stage

    from .pipelines import STAGE_TYPE, stage_source_id

    pipeline_ref = row.data.get("pipeline_ref") or {}
    stage_ref = row.data.get("stage_ref") or {}
    pipeline = row.resolve(pipeline_ref, Pipeline)
    stage = row.resolve({"object_type": STAGE_TYPE,
                         "source_id": stage_source_id(pipeline_ref.get("source_id"), stage_ref.get("source_id"))},
                        Stage)
    if stage.pipeline_id != pipeline.pk:
        raise RowError(INVALID_VALUE, "the deal's stage belongs to another pipeline.")
    return pipeline, stage


def history_lookups(row: Row):
    """``pipeline_of`` / ``stage_of`` for :func:`plan_history`, by ExternalRef."""
    from crm.models import Pipeline, Stage

    from .base import resolve_ref
    from .pipelines import STAGE_TYPE, stage_source_id

    pipelines: dict = {}
    sids: dict = {}
    stages: dict = {}

    def pipeline_of(sid):
        if sid not in pipelines:
            found = resolve_ref(row.job, {"object_type": "pipeline", "source_id": sid}, Pipeline)
            pipelines[sid] = found
            if found is not None:
                sids[found.pk] = sid
        return pipelines[sid]

    def stage_of(pipeline, sid):
        if pipeline is None:
            return None
        psid = sids.get(pipeline.pk)
        if psid is None:
            psid = str((row.data.get("pipeline_ref") or {}).get("source_id"))
            if pipeline_of(psid) is None or pipeline_of(psid).pk != pipeline.pk:
                return None
        key = (psid, sid)
        if key not in stages:
            stage = resolve_ref(row.job, {"object_type": STAGE_TYPE, "source_id": stage_source_id(psid, sid)},
                                Stage)
            stages[key] = stage if stage is not None and stage.pipeline_id == pipeline.pk else None
        return stages[key]

    def stage_elsewhere(sid):
        """The one stage of this source whose own id is ``sid`` (any pipeline)."""
        from django.contrib.contenttypes.models import ContentType as CT

        from core.models import ExternalRef

        if ("*", sid) not in stages:
            hits = list(ExternalRef._base_manager.filter(
                company_id=row.company.pk, source=row.job.source, object_type=STAGE_TYPE,
                source_id__endswith=f"/{sid}", content_type=CT.objects.get_for_model(Stage),
            ).values_list("source_id", "object_id")[:2])
            hits = [h for h in hits if h[0].split("/", 1)[1:] == [str(sid)]]
            found = None
            if len(hits) == 1:
                found = Stage._base_manager.select_related("pipeline").filter(
                    pk=hits[0][1], company_id=row.company.pk).first()
                if found is not None:
                    pipelines.setdefault(hits[0][0].split("/", 1)[0], found.pipeline)
                    sids.setdefault(found.pipeline_id, hits[0][0].split("/", 1)[0])
            stages[("*", sid)] = found
        return stages[("*", sid)]

    return pipeline_of, stage_of, stage_elsewhere


# --- the engagement ----------------------------------------------------------------


def loss_fields(row: Row) -> dict:
    from crm.registries import LOSS_REASONS, UNKNOWN_LOSS_REASON

    raw = row.data.get("loss_reason_raw")
    mapped = ((row.mapping.get("deal") or {}).get("loss_reasons") or {}).get(raw) if raw else None
    if mapped is not None:
        if mapped not in LOSS_REASONS:
            raise RowError(INVALID_VALUE, f"the mapping file maps a loss reason to {mapped!r}, which is not "
                                          "a GOLDEN_LOSS_REASONS key.")
        return {"loss_reason": mapped, "loss_notes": ""}
    return {"loss_reason": UNKNOWN_LOSS_REASON, "loss_notes": raw or ""}  # never parsed (2.5)


def column_values(row: Row) -> dict:
    out = {}
    for name, target in row.property_targets().items():
        if not isinstance(target, str) or not target.startswith("column:"):
            continue
        model, _, column = target.removeprefix("column:").partition(".")
        if model != "engagement":
            continue
        if column not in ENGAGEMENT_COLUMN_TARGETS:
            raise RowError(INVALID_VALUE, f"{target!r} is not a writable engagement column "
                                          f"({', '.join(ENGAGEMENT_COLUMN_TARGETS)}).")
        value = row.properties.get(name)
        if value is None:
            continue
        if column == "probability":
            try:
                value = int(Decimal(str(value)))
            except (InvalidOperation, ValueError):
                raise RowError(INVALID_VALUE, f"{name} = {value!r} is not a probability.") from None
        out[column] = value if column == "probability" else str(value)[:255]
    return out


def sidecar_values(row: Row, *, targets: dict | None = None) -> dict:
    """The source properties the mapping file leaves to the sidecar (2.12)."""
    targets = row.property_targets() if targets is None else targets
    return {n: v for n, v in row.properties.items() if targets.get(n, "sidecar") == "sidecar"}


def write_sidecar(row: Row, obj, derived: dict, *, targets: dict | None = None) -> None:
    """Source properties plus the importer's own values (``import_<name>``)."""
    own = {f"{DERIVED_PREFIX}{k}": v for k, v in derived.items() if v not in (None, "", [])}
    try:
        put(obj, row.provider, sidecar_values(row, targets=targets), derived=own, session=row.session)
    except SidecarRefused as exc:
        raise RowError(INVALID_VALUE, str(exc)) from exc


@dataclass
class Plan:
    """Everything the insert needs, resolved before anything is written."""

    pipeline: object
    stage: object  # the engagement's stage column
    current_stage: object
    status: str
    closed_at: object
    history: HistoryPlan
    created_at: object
    flags: list


def _no_time():
    raise RowError(INVALID_VALUE, "the record has no created, updated or closed time; golden never "
                                  "dates imported history at the import.")


def plan_engagement(row: Row, *, status, closed_at) -> Plan:
    """Resolve pipeline, stage, outcome, closed_at and history for an insert."""
    pipeline, current = pipeline_and_stage(row)
    outcome = resolve_outcome(status, stage_outcome(current, row.job.source), row.mapped.get("outcome"))
    if outcome == OPEN and current.outcome_hint:
        raise RowError(INVALID_VALUE, f"{FLAG_OUTCOME_CONFLICT}: an open engagement in the won/lost stage "
                                      f"{current.key!r}; a reviewer picks the outcome.")
    flags = []
    record = row.record
    stage_props = STAGE_PROPS[row.object_type]
    entries = record.get("history") or []
    closed = parse_instant(closed_at) if outcome in CLOSED_STATUSES else None
    if outcome in CLOSED_STATUSES and closed is None:
        # The time of its last stage change, else the source's last update (2.5); never now.
        changes = [parse_instant(e.get("at")) for e in entries if e.get("property") in stage_props]
        changes = [c for c in changes if c is not None]
        closed = max(changes) if changes else parse_instant(record.get("updated_at"))
        if closed is None:
            raise RowError(INVALID_VALUE, "a closed deal with no close date, no stage history and no "
                                          "last-modified time: golden never stamps now.")
        flags.append(FLAG_CLOSED_AT_INFERRED)
    created_at = parse_instant(record.get("created_at"))
    pipeline_of, stage_of, stage_elsewhere = history_lookups(row)
    history = plan_history(
        entries=entries, stage_props=stage_props, pipeline_props=PIPELINE_PROPS[row.object_type],
        pipeline=pipeline, current_stage=current, status=outcome, closed_at=closed,
        created_at=created_at or parse_instant(record.get("updated_at")) or closed or _no_time(),
        pipeline_of=pipeline_of, stage_of=stage_of, stage_elsewhere=stage_elsewhere,
    )
    return Plan(pipeline=pipeline, stage=history.stage, current_stage=current, status=outcome, closed_at=closed,
                history=history, created_at=created_at, flags=flags)


def insert_engagement(row: Row, plan: Plan, *, party, owner, fields: dict, derived: dict, targets=None):
    """Insert the engagement in its final state, write its history, stamp
    ``created_at``, write the sidecar and the refs (hand-off section 2)."""
    from crm.engagements import record_imported_history
    from crm.models import Engagement

    eng = Engagement(
        company_id=row.company.pk, pipeline=plan.pipeline, stage=plan.stage, status=plan.status,
        closed_at=plan.closed_at, owner=owner, party=party, origin=Engagement.Origin.IMPORT,
        stage_entered_at=plan.history.changes[-1]["changed_at"] if plan.stage is not None else None,
        last_activity_at=plan.history.changes[-1]["changed_at"], metadata={}, **fields,
    )
    if plan.status != LOST:
        eng.loss_reason = eng.loss_competitor = eng.loss_notes = ""
    eng.full_clean(exclude=["number", "title"])
    eng.save()  # mints number and title; engagement_created is dropped (suppressed)
    row.session.create(eng)
    for change in record_imported_history(eng, plan.history.changes):
        row.session.create(change)
    if plan.created_at is not None:
        Engagement._base_manager.filter(pk=eng.pk).update(created_at=plan.created_at)
    if plan.status in CLOSED_STATUSES and plan.stage is None:
        derived["closed_stage"] = plan.current_stage.key
    if plan.history.unmapped:
        derived["unmapped_history"] = list(plan.history.unmapped)
    write_sidecar(row, eng, derived, targets=targets)
    return eng


def follow_up_task(row: Row, eng, *, provider: str):
    """One open task for an open engagement with any follow-up value (hand-off 4)."""
    from crm import tasks
    from crm.models import Engagement, Task

    from .. import writelog

    items = [i for i in row.data.get("follow_up") or [] if isinstance(i, dict) and i.get("value")]
    if eng.status != OPEN or not items:
        return None
    zone = tasks.zone_for(eng.owner, row.company)
    title, best, best_at = "", None, None
    for item in items:
        if item.get("field") in TITLE_FIELDS:
            title = title or str(item["value"]).strip()
            continue
        due = follow_up_due(item.get("value"))
        if due is None:
            continue  # not a date: kept in the sidecar, never a title by accident
        at = tasks.due_instant(due, zone)[0]
        if best_at is None or at > best_at:
            best, best_at = due, at
    before = writelog.snapshot(Engagement, eng.pk)  # the row as the create left it
    task = tasks.create(engagement=eng, title=(title or "Follow up")[:255], assignee=eng.owner, due=best,
                        kind="follow_up", source=f"import:{provider}:follow_up"[:64])
    row.session.create(task)
    for event in task.events.all():
        row.session.create(event)
    now = timezone.now()
    if task.due_at is not None and task.due_at <= now:
        Task._base_manager.filter(pk=task.pk).update(due_notified_at=now)  # no task_due storm
    row.session.checkpoint(Engagement, eng.pk, before)  # next_task / next_action_due_at
    return task


def _derived(cp: Counterparty, flags: list, rounded: bool) -> dict:
    flags = sorted(set(flags) | set(cp.flags) | ({FLAG_AMOUNT_ROUNDED} if rounded else set()))
    return {"other_companies": cp.other_companies, "flags": flags}


def create(row: Row):
    data = row.data
    plan = plan_engagement(row, status=data.get("status"), closed_at=data.get("closed_at"))
    owner, owner_flags = owner_of(row)
    cp = counterparty(row)
    amount, rounded = amount_of(data.get("amount"))
    fields = {
        "title": (data.get("title") or "")[:255], "expected_value": amount, "currency": currency_of(row),
        "expected_close_date": parse_date(data["expected_close_date"]) if data.get("expected_close_date") else None,
    }
    if plan.status == LOST:
        fields.update(loss_fields(row))
    fields.update(column_values(row))
    eng = insert_engagement(row, plan, party=cp.party, owner=owner, fields=fields,
                            derived=_derived(cp, plan.flags + owner_flags, rounded))
    row.refs_for(eng)
    follow_up_task(row, eng, provider=row.provider)
    return eng


# --- re-import ------------------------------------------------------------------------

UPDATE_COLUMNS = {"title": "title", "amount": "expected_value", "currency": "currency",
                  "expected_close_date": "expected_close_date"}
STATE_FIELDS = ("status", "stage_ref", "pipeline_ref", "closed_at", "loss_reason_raw")


def _new_history(row: Row, eng, since) -> None:
    from crm.models import EngagementStageChange

    for change in EngagementStageChange._base_manager.filter(engagement_id=eng.pk, created_at__gte=since) \
            .exclude(pk__in=[e.pk for e in row.session.entries]).order_by("changed_at", "created_at"):
        row.session.create(change)


def _move_to(row: Row, eng, pipeline, stage, status) -> None:
    """A changed outcome or stage on re-import, through the services
    (``source="import"``, stamped now; hand-off section 2). ``stage`` None:
    keep the engagement's own (a close in a won/lost stage)."""
    from crm import engagements

    source = "import"
    same_place = _same(pipeline, eng.pipeline) and (stage is None or _same(stage, eng.stage))
    if eng.status != OPEN and (status != eng.status or not same_place):
        engagements.reopen(eng, stage=stage if _same(pipeline, eng.pipeline) else None, source=source)
    if eng.status == OPEN:
        if not _same(pipeline, eng.pipeline):
            if stage is None:
                raise RowError(INVALID_VALUE, "the deal moved pipeline straight into a won/lost stage; "
                                              "a closed engagement keeps a stage of its own pipeline.")
            engagements.change_pipeline(eng, pipeline, stage, source=source)
        elif stage is not None and not _same(stage, eng.stage):
            engagements.move(eng, stage, source=source)
    if status == WON and eng.status == OPEN:
        engagements.win(eng, source=source)
    elif status == LOST and eng.status == OPEN:
        loss = loss_fields(row)
        engagements.lose(eng, loss["loss_reason"], notes=loss["loss_notes"], source=source)
    if eng.status != status:
        raise RowError(INVALID_VALUE, f"could not bring the engagement to {status}.")


def update(row: Row):
    from crm.models import Engagement

    from .parties import _stale_fields

    eng = row.target(Engagement)
    stale = _stale_fields(row, eng)
    if stale:
        raise RowError(CONFLICT_STALE, f"golden changed {', '.join(stale)} after the decision; review it again.")
    diff = ((row.staged.proposed_mapping or {}).get("diff") or {}).get("fields") or {}
    changed = {f for f in diff if f not in row.kept}
    columns = {}
    for name, column in UPDATE_COLUMNS.items():
        if name not in changed:
            continue
        if name == "amount":
            columns[column], _rounded = amount_of(row.data.get("amount"))
        elif name == "currency":
            columns[column] = currency_of(row)
        elif name == "expected_close_date":
            value = row.data.get("expected_close_date")
            columns[column] = parse_date(value) if value else None
        else:
            columns[column] = (row.data.get("title") or eng.title)[:255]
    since = timezone.now()
    row.session.touch(Engagement, eng.pk)
    if columns:
        for name, value in columns.items():
            setattr(eng, name, value)
        try:
            eng.full_clean(exclude=["number"])
        except ValidationError as exc:
            raise RowError(INVALID_VALUE, "; ".join(exc.messages)) from exc
        row.session.update(eng, **columns)
    if changed & set(STATE_FIELDS):
        plan_pipeline, current = pipeline_and_stage(row)
        status = resolve_outcome(row.data.get("status"), stage_outcome(current, row.job.source), row.mapped.get("outcome"))
        stage = current if (status == OPEN or not current.outcome_hint) else (
            eng.stage if _same(plan_pipeline, eng.pipeline) else None)
        if status == OPEN and current.outcome_hint:
            raise RowError(INVALID_VALUE, f"{FLAG_OUTCOME_CONFLICT}: an open engagement in a won/lost stage.")
        moved = eng.status != status or not _same(plan_pipeline, eng.pipeline) or (
            stage is not None and not _same(stage, eng.stage))
        if moved:
            _move_to(row, eng, plan_pipeline, stage, status)
            _new_history(row, eng, since)
        elif status in CLOSED_STATUSES:
            # Same outcome: the close date and the loss reason are plain columns.
            closed = parse_instant(row.data.get("closed_at"))
            fixes = {"closed_at": closed} if closed is not None and "closed_at" in changed else {}
            if status == LOST and "loss_reason_raw" in changed:
                fixes.update(loss_fields(row))
            if fixes:
                row.session.update(eng, **fixes)
    write_sidecar(row, eng, {})
    row.refs_for(eng)
    return eng


def apply(row: Row) -> str:
    from crm.models import Engagement

    if row.decision == "create":
        eng = create(row)
    elif row.decision == "update":
        eng = update(row)
    else:
        raise RowError(INVALID_VALUE, f"a deal row cannot be applied as {row.decision!r}.")
    return applied_ref(Engagement, eng.pk)


__all__ = ["apply", "canonical_outcome_conflict", "counterparty", "plan_history", "resolve_outcome"]
