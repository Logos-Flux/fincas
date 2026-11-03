"""The one place an engagement's stage or status changes (spec 1.4, E3, E12).

Forms, API actions, imports and (from step 5) the quote conversion call these
functions. ``Engagement.save()`` refuses a changed ``stage``, ``status`` or
``pipeline`` unless it runs inside ``engagement_service_writes()``, which only
this module enters, and every real change writes one append-only
``EngagementStageChange`` row. A no-op writes nothing.

Closing keeps the stage, so "lost at Negotiation" is a fact. Open tasks on a
closed engagement are not cancelled: the closer chooses.

Signals (spec 5.1): ``Engagement.save()`` sends ``engagement_created`` and
each history row sends ``engagement_won`` / ``_lost`` / ``_reopened`` /
``_stage_changed`` (``crm.lifecycle``), all after commit.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.plugins.dispatch import suppressed

from .models import (
    Engagement,
    EngagementStageChange,
    Pipeline,
    Stage,
    engagement_service_writes,
)
from .pipelines import default_pipeline
from .registries import ENGAGEMENT_SOURCES, LOSS_REASONS, UNKNOWN_LOSS_REASON
from .tasks import recompute_next_action  # noqa: F401  (re-exported: spec 1.4 / 1.5)

Source = EngagementStageChange.Source
# Sources allowed to close with the ``unknown`` loss reason (1.1).
_BULK_SOURCES = (Source.IMPORT, Source.BACKFILL)
# Callers without a user (``by=None``) may reopen only as these.
_SYSTEM_SOURCES = (Source.SYSTEM, Source.IMPORT, Source.BACKFILL)
# create() makes an open engagement; outcome and timing fields are the service's.
_CREATE_REFUSED = frozenset({
    "status", "closed_at", "loss_reason", "loss_competitor", "loss_notes", "stage_entered_at",
    "next_task", "next_task_id", "next_action_due_at", "number",
})


def _check_stage_active(stage):
    if not stage.is_active:
        raise ValidationError({"stage": "The stage is archived; pick an active stage."})


@dataclass(frozen=True)
class MoveResult:
    """``change`` is the history row (None for a no-op). ``needs_close`` is the
    stage's ``outcome_hint`` ("won" / "lost" / ""): the caller must confirm,
    and call ``win`` / ``lose``; ``move`` never closes."""

    change: EngagementStageChange | None
    needs_close: str = ""


def _history(eng, *, from_pipeline_id, from_stage_id, from_status, by, source, note="",
             at=None) -> EngagementStageChange:
    return EngagementStageChange.objects.create(
        engagement=eng, company_id=eng.company_id,
        from_pipeline_id=from_pipeline_id, to_pipeline_id=eng.pipeline_id,
        from_stage_id=from_stage_id, to_stage_id=eng.stage_id,
        from_status=from_status, to_status=eng.status,
        changed_at=at or timezone.now(), changed_by=by, source=source, note=note[:255],
    )


def _check_source(source):
    if source not in Source.values:
        raise ValidationError({"source": f"Unknown history source {source!r}."})


def _lock(eng) -> Engagement:
    """Re-read the row under ``SELECT … FOR UPDATE`` so two moves serialise."""
    # NO KEY: concurrent task inserts on this engagement need its FK share lock.
    locked = Engagement._base_manager.select_for_update(no_key=True).get(pk=eng.pk)
    for attname in ("pipeline_id", "stage_id", "status", "closed_at", "loss_reason"):
        if getattr(eng, attname) != getattr(locked, attname):
            # Someone else moved it since the caller loaded it: work on the truth.
            eng.refresh_from_db()
            break
    return eng


def create(*, company, party=None, pipeline=None, stage=None, owner=None, by=None,
           source="erp", origin=Engagement.Origin.ERP, title="", engagement_source="",
           note="", **fields) -> Engagement:
    """Create an open engagement and its first history row.

    ``pipeline`` defaults to the company's default pipeline, ``stage`` to the
    pipeline's first active stage. ``engagement_source`` is the
    ``GOLDEN_ENGAGEMENT_SOURCES`` key (``Engagement.source``); ``source`` is
    the history row's writer, like every other function here.
    """
    _check_source(source)
    guarded = sorted(set(fields) & _CREATE_REFUSED)
    if guarded:
        raise ValidationError(
            {name: "Set by the service: create() makes an open engagement; close it with "
                   "win() / lose()." for name in guarded}
        )
    pipeline = pipeline or (stage.pipeline if stage is not None else default_pipeline(company))
    if pipeline is None:
        raise ValidationError({"pipeline": "The company has no default pipeline."})
    if not pipeline.is_active:
        raise ValidationError({"pipeline": "An inactive pipeline takes no new engagements."})
    stage = stage or pipeline.first_stage()
    if stage is None:
        raise ValidationError({"stage": "The pipeline has no active stage."})
    eng = Engagement(
        company=company, party=party, pipeline=pipeline, stage=stage, owner=owner,
        origin=origin, title=title, source=engagement_source, **fields,
    )
    eng.full_clean(exclude=["number", "title"])
    now = timezone.now()
    eng.stage_entered_at = now
    eng.last_activity_at = eng.last_activity_at or now
    with transaction.atomic(), engagement_service_writes():
        eng.save()
        _history(eng, from_pipeline_id=None, from_stage_id=None, from_status="", by=by,
                 source=source, note=note, at=now)
    return eng


_UNSET = object()


def reassign(eng, owner, *, owner_team=_UNSET) -> Engagement:
    """Give ``eng`` a new owner (or none). Phase 4 fields spec 2.1.

    ``owner_team`` is stamped from the new owner's team by ``save()``
    (``core.teams``) unless the caller names one, which is the direct edit
    staff with ``crm.reassign_engagement`` may make. Who may reassign is the
    caller's check (``crm.erp_views.can_change_owner``, the API's
    ``perform_update``); this function refuses only a cross-tenant owner or
    team. No history row: owner changes are not stage moves.
    """
    for name, value in (("owner", owner), ("owner_team", owner_team)):
        if value is not _UNSET and value is not None and value.company_id != eng.company_id:
            raise ValidationError({name: f"The {name.replace('_', ' ')} belongs to another company."})
    fields = ["owner", "updated_at"]
    eng.owner = owner
    if owner_team is not _UNSET:
        eng.owner_team = owner_team
        fields.append("owner_team")
    eng.save(update_fields=fields)
    return eng


def move(eng, stage, *, by=None, note="", source="erp") -> MoveResult:
    """Move an open engagement to ``stage`` in its own pipeline."""
    _check_source(source)
    _check_stage_active(stage)
    with transaction.atomic():
        _lock(eng)
        if eng.status != Engagement.Status.OPEN:
            raise ValidationError({"stage": "A closed engagement does not move; reopen it first."})
        if stage.pipeline_id != eng.pipeline_id:
            raise ValidationError(
                {"stage": "The stage belongs to another pipeline; use change_pipeline()."}
            )
        if stage.pk == eng.stage_id:
            return MoveResult(None, stage.outcome_hint)
        from_stage_id = eng.stage_id
        now = timezone.now()
        eng.stage, eng.stage_entered_at, eng.last_activity_at = stage, now, now
        with engagement_service_writes():
            eng.save(update_fields=["stage", "stage_entered_at", "last_activity_at", "updated_at"])
        change = _history(eng, from_pipeline_id=eng.pipeline_id, from_stage_id=from_stage_id,
                          from_status=eng.status, by=by, source=source, note=note, at=now)
    return MoveResult(change, stage.outcome_hint)


def _close(eng, status, *, by, source, note, loss=None):
    _check_source(source)
    with transaction.atomic():
        _lock(eng)
        if eng.status == status and not loss:
            return None
        if eng.status != Engagement.Status.OPEN:
            raise ValidationError({"status": f"The engagement is already {eng.status}; reopen it first."})
        now = timezone.now()
        eng.status, eng.closed_at, eng.last_activity_at = status, now, now
        fields = ["status", "closed_at", "last_activity_at", "updated_at"]
        if loss:
            eng.loss_reason, eng.loss_competitor, eng.loss_notes = loss
            fields += ["loss_reason", "loss_competitor", "loss_notes"]
        with engagement_service_writes():
            eng.save(update_fields=fields)
        return _history(eng, from_pipeline_id=eng.pipeline_id, from_stage_id=eng.stage_id,
                        from_status=Engagement.Status.OPEN, by=by, source=source, note=note, at=now)


def win(eng, *, by=None, source="erp", note="") -> EngagementStageChange | None:
    """``status=won``; the stage stays (E3); open tasks stay open."""
    return _close(eng, Engagement.Status.WON, by=by, source=source, note=note)


def lose(eng, reason, *, by=None, competitor="", notes="", source="erp",
         note="") -> EngagementStageChange | None:
    """``status=lost`` with a ``GOLDEN_LOSS_REASONS`` key.

    ``unknown`` is for backfill and import only (1.1).
    """
    if reason not in LOSS_REASONS:
        raise ValidationError({"loss_reason": f"Unknown loss reason {reason!r} (GOLDEN_LOSS_REASONS)."})
    if reason == UNKNOWN_LOSS_REASON and source not in _BULK_SOURCES:
        raise ValidationError({"loss_reason": "'unknown' is for backfill and import only; pick a reason."})
    return _close(eng, Engagement.Status.LOST, by=by, source=source, note=note,
                  loss=(reason, competitor[:255], notes))


def _last_open_stage(eng):
    change = (
        eng.stage_changes.filter(to_status=Engagement.Status.OPEN, to_stage__isnull=False,
                                 to_stage__pipeline_id=eng.pipeline_id)
        .order_by("-changed_at", "-created_at")
        .select_related("to_stage")
        .first()
    )
    return change.to_stage if change else None


def _check_reopen_permission(by, source):
    if by is None:
        if source not in _SYSTEM_SOURCES:
            raise PermissionDenied("Reopening needs a user with crm.reopen_engagement.")
        return
    user = getattr(by, "user", None)
    if user is None or not user.has_perm("crm.reopen_engagement"):
        raise PermissionDenied("Reopening needs the crm.reopen_engagement permission.")


def reopen(eng, *, by=None, stage=None, source="erp", note="") -> EngagementStageChange | None:
    """Back to ``open`` at ``stage``, else the kept stage, else the last open
    stage from history, else the pipeline's first stage. Needs
    ``crm.reopen_engagement`` (``by=None`` only for system / import / backfill)."""
    _check_source(source)
    _check_reopen_permission(by, source)
    with transaction.atomic():
        _lock(eng)
        if eng.status == Engagement.Status.OPEN:
            return None
        if stage is not None and stage.pipeline_id != eng.pipeline_id:
            raise ValidationError({"stage": "The stage belongs to another pipeline."})
        if stage is not None:
            _check_stage_active(stage)
        target = stage or eng.stage or _last_open_stage(eng) or eng.pipeline.first_stage()
        if target is None:
            raise ValidationError({"stage": "The pipeline has no active stage to reopen at."})
        previous_status, from_stage_id = eng.status, eng.stage_id
        now = timezone.now()
        eng.status, eng.closed_at = Engagement.Status.OPEN, None
        eng.loss_reason = eng.loss_competitor = eng.loss_notes = ""
        if target.pk != from_stage_id:
            eng.stage_entered_at = now
        eng.stage, eng.last_activity_at = target, now
        with engagement_service_writes():
            eng.save(update_fields=["status", "closed_at", "loss_reason", "loss_competitor",
                                    "loss_notes", "stage", "stage_entered_at", "last_activity_at",
                                    "updated_at"])
        return _history(eng, from_pipeline_id=eng.pipeline_id, from_stage_id=from_stage_id,
                        from_status=previous_status, by=by, source=source, note=note, at=now)


def change_pipeline(eng, pipeline: Pipeline, stage: Stage, *, by=None, source="erp",
                    note="") -> EngagementStageChange | None:
    """Move an open engagement to another pipeline: one history row with both."""
    _check_source(source)
    if stage.pipeline_id != pipeline.pk:
        raise ValidationError({"stage": "The stage belongs to another pipeline."})
    if pipeline.company_id != eng.company_id:
        raise ValidationError({"pipeline": "The pipeline belongs to another company."})
    if not pipeline.is_active:
        raise ValidationError({"pipeline": "An inactive pipeline takes no new engagements."})
    _check_stage_active(stage)
    with transaction.atomic():
        _lock(eng)
        if eng.status != Engagement.Status.OPEN:
            raise ValidationError({"pipeline": "A closed engagement does not move; reopen it first."})
        if pipeline.pk == eng.pipeline_id and stage.pk == eng.stage_id:
            return None
        from_pipeline_id, from_stage_id = eng.pipeline_id, eng.stage_id
        now = timezone.now()
        eng.pipeline, eng.stage, eng.stage_entered_at, eng.last_activity_at = pipeline, stage, now, now
        with engagement_service_writes():
            eng.save(update_fields=["pipeline", "stage", "stage_entered_at", "last_activity_at",
                                    "updated_at"])
        return _history(eng, from_pipeline_id=from_pipeline_id, from_stage_id=from_stage_id,
                        from_status=eng.status, by=by, source=source, note=note, at=now)


def touch_activity(engagement_id, at) -> bool:
    """Move an engagement's ``last_activity_at`` up to ``at``; never back
    (spec 1.3). True when it moved.

    One ``UPDATE … WHERE last_activity_at < at OR last_activity_at IS NULL``:
    no ``save()``, so no save guard, no history row and no signal. A stale
    ``at`` (an import replaying an old communication) is a no-op, so it is
    safe to call with every row of a batch. ``communications.ingest.record``
    calls it for live logging; an importer, whose ``record`` calls run inside
    ``suppressed()`` and do not bump, calls it once per touched engagement
    with the latest qualifying ``occurred_at`` (hand-off section 5).
    """
    if at is None:
        return False
    return bool(
        Engagement._base_manager.filter(pk=engagement_id)
        .filter(Q(last_activity_at__isnull=True) | Q(last_activity_at__lt=at))
        .update(last_activity_at=at)
    )


# --- imported history (Phase 5 step 5; hand-off docs/crm/phase3-to-phase5-handoff.md 3, 10) ---

_CHANGE_KEYS = frozenset({
    "from_pipeline", "to_pipeline", "from_stage", "to_stage", "from_status", "to_status",
    "changed_at", "note",
})
_STATUSES = frozenset(Engagement.Status.values)


def _pk(obj):
    return getattr(obj, "pk", None)


def _check_imported_change(eng, i, change, previous):
    errors = []
    unknown = sorted(set(change) - _CHANGE_KEYS)
    if unknown:
        # changed_by / source are the function's: NULL and "import", always.
        errors.append(f"unknown key(s) {', '.join(unknown)}")
    at = change.get("changed_at")
    if at is None or timezone.is_naive(at):
        errors.append("changed_at must be an aware instant")
    elif previous is not None and at <= previous["changed_at"]:
        errors.append("changed_at must be strictly after the previous row's")
    from_status, to_status = change.get("from_status", ""), change.get("to_status")
    if to_status not in _STATUSES:
        errors.append(f"to_status {to_status!r} is not a status")
    if i == 0 and from_status != "":
        errors.append("the first row must be the creation row (from_status blank)")
    if i > 0 and from_status not in _STATUSES:
        errors.append("only the first row may have a blank from_status")
    if change.get("to_pipeline") is None:
        errors.append("to_pipeline is required")
    if to_status == Engagement.Status.OPEN and change.get("to_stage") is None:
        errors.append("an open row needs a to_stage (only a closed row may leave it NULL, rule 7)")
    for end in ("from", "to"):
        pipeline, stage = change.get(f"{end}_pipeline"), change.get(f"{end}_stage")
        for name, obj in ((f"{end}_pipeline", pipeline), (f"{end}_stage", stage)):
            if obj is not None and obj.company_id != eng.company_id:
                errors.append(f"{name} belongs to another company")
        if stage is not None and (pipeline is None or stage.pipeline_id != pipeline.pk):
            errors.append(f"{end}_stage belongs to another pipeline than {end}_pipeline")
    if i == 0 and (change.get("from_pipeline") is not None or change.get("from_stage") is not None):
        errors.append("the creation row has no from_pipeline / from_stage")
    if previous is not None:
        # One chain: each row starts where the previous one ended.
        if (_pk(change.get("from_pipeline")), _pk(change.get("from_stage")), from_status) != (
                _pk(previous.get("to_pipeline")), _pk(previous.get("to_stage")), previous["to_status"]):
            errors.append("from_pipeline / from_stage / from_status must equal the previous row's to_*")
    if len(change.get("note") or "") > 255:
        errors.append("note is longer than 255 characters")
    if errors:
        raise ValidationError({"changes": f"Row {i}: " + "; ".join(errors) + "."})


def record_imported_history(eng, changes) -> list[EngagementStageChange]:
    """Write an imported engagement's source history (hand-off section 3 and 10).

    ``changes`` is the ordered list of rows (mappings with ``from_pipeline``,
    ``to_pipeline``, ``from_stage``, ``to_stage``, ``from_status``,
    ``to_status``, ``changed_at`` (aware) and an optional ``note``), oldest
    first. Every row is written with ``source="import"`` and
    ``changed_by=NULL``; neither may be passed.

    Refuses (``ValidationError``, nothing written) when the engagement already
    has history (first import only: later changes go through the services
    above), when ``changes`` is empty, when the rows are not one chain (each
    row's ``from_*`` is the previous row's ``to_*``), not strictly increasing
    in ``changed_at``, when the first row is not a creation row
    (``from_status=""``, no ``from_*``), when a stage belongs to another
    pipeline than its row's or a pipeline or stage to another company, and
    when the last row does not end at the engagement's ``pipeline``,
    ``stage`` and ``status`` (``to_stage`` NULL exactly when the engagement's
    stage is: the closed-stage-unknown shape, rule 7).

    Rows are inserted with one ``bulk_create`` (``company_id`` set on each), so
    no signal is sent whether or not the caller is inside ``suppressed()``.
    ``stage_entered_at`` is set with one ``update()`` to the ``changed_at`` of
    the last row that entered the current stage (None when the stage is
    NULL); ``stage``, ``status`` and ``pipeline`` are not touched, so the save
    guard is not involved. Returns the rows written.
    """
    changes = [dict(c) for c in changes]
    if not changes:
        raise ValidationError({"changes": "An imported engagement has at least its creation row."})
    if EngagementStageChange._base_manager.filter(engagement_id=eng.pk).exists():
        raise ValidationError({"changes": "The engagement already has history: imported history is "
                                          "written once, at first import; later changes go through "
                                          "move / win / lose / reopen / change_pipeline."})
    previous = None
    for i, change in enumerate(changes):
        change.setdefault("from_status", "")
        _check_imported_change(eng, i, change, previous)
        previous = change
    last = changes[-1]
    if (_pk(last.get("to_pipeline")), _pk(last.get("to_stage")), last["to_status"]) != (
            eng.pipeline_id, eng.stage_id, eng.status):
        raise ValidationError({"changes": "The last row must end at the engagement's pipeline, stage "
                                          "and status."})
    entered = None
    if eng.stage_id is not None:
        for change in changes:
            to_stage = _pk(change.get("to_stage"))
            if to_stage == eng.stage_id and (
                    change["from_status"] == "" or _pk(change.get("from_stage")) != to_stage
                    or _pk(change.get("from_pipeline")) != _pk(change.get("to_pipeline"))):
                entered = change["changed_at"]
    rows = [
        EngagementStageChange(
            engagement=eng, company_id=eng.company_id,
            from_pipeline=c.get("from_pipeline"), to_pipeline=c.get("to_pipeline"),
            from_stage=c.get("from_stage"), to_stage=c.get("to_stage"),
            from_status=c["from_status"], to_status=c["to_status"], changed_at=c["changed_at"],
            changed_by=None, source=Source.IMPORT, note=c.get("note") or "",
        )
        for c in changes
    ]
    with transaction.atomic():
        written = EngagementStageChange.objects.bulk_create(rows)
        if entered != eng.stage_entered_at:
            Engagement._base_manager.filter(pk=eng.pk).update(stage_entered_at=entered)
            eng.stage_entered_at = entered
    return written


def source_label(eng) -> str:
    entry = ENGAGEMENT_SOURCES.get(eng.source)
    return entry.label if entry else ""


# --- quotes and orders on the thread (spec 1.6, Phase 3 step 5) -------------------

#: Quote.SalesStage's open values: the old vocabulary the compat properties
#: map back to (spec 1.6). Literal, so this module never imports ``orders``.
OLD_OPEN_SALES_STAGES = frozenset({
    "prospecting", "qualification", "needs_analysis", "proposal_sent", "negotiation",
    "verbal_commit",
})
# Quote statuses that mean the document went out (the "sent" stage rule, 1.6).
_SENT_QUOTE_STATUSES = frozenset({"sent", "viewed", "negotiating", "accepted"})
_LOST_QUOTE_STATUSES = {"lost": None, "expired": "expired", "cancelled": "cancelled"}


def _quote_stage(pipeline, quote):
    """``proposal_sent`` once the quote is sent, else ``qualification`` (1.6);
    the pipeline's first stage when a replaced default pipeline lacks the key."""
    key = "proposal_sent" if (quote.sent_at or quote.status in _SENT_QUOTE_STATUSES) else "qualification"
    return pipeline.stages.filter(key=key, is_active=True).first() or pipeline.first_stage()


def _default_pipeline_or_seed(company):
    pipeline = default_pipeline(company)
    if pipeline is None:
        # A company created around post_save (bulk_create, raw fixtures).
        from .pipelines import seed_default_pipelines

        seed_default_pipelines(company)
        pipeline = default_pipeline(company)
    return pipeline


def _close_like_quote(eng, quote, *, source):
    """A quote that is already decided (a legacy row ensured lazily, or one
    created closed through the API) closes its new engagement to match."""
    if quote.status == "accepted" or quote.converted_to_order_id:
        win(eng, source=source, note=f"Quote {quote.quote_number} is {quote.status}")
    elif quote.status in _LOST_QUOTE_STATUSES:
        reason = getattr(quote, "legacy_loss_reason", "") or _LOST_QUOTE_STATUSES[quote.status]
        if reason not in LOSS_REASONS:
            # ``unknown`` is for backfill and import only (1.1).
            reason = UNKNOWN_LOSS_REASON if source in _BULK_SOURCES else "other"
        lose(eng, reason, source=source, note=f"Quote {quote.quote_number} is {quote.status}")


def ensure_for_quote(quote, *, engagement=None) -> Engagement:
    """The engagement ``quote`` belongs on, creating it when needed (spec 1.6).

    Does not save ``quote`` (``Quote.save()`` calls this and sets the FK).

    - already linked: that engagement;
    - ``engagement`` given (a caller that knows the thread): that one;
    - a revision (``original_quote`` set) inherits its original's engagement,
      ensuring the original first when it predates the backfill;
    - otherwise, a new quote or a duplicate (``amended_from`` is an
      independent quote) gets a new engagement: ``origin=quote``, party =
      the customer's party, owner = the sales rep, the default pipeline at
      ``proposal_sent`` if sent, else ``qualification``. A quote that is
      already accepted, converted, lost, expired or cancelled closes it.
    """
    if quote.engagement_id is not None:
        return quote.engagement
    if engagement is not None:
        if engagement.company_id != quote.company_id:
            raise ValidationError({"engagement": "The engagement belongs to another company."})
        return engagement
    original_id = quote.original_quote_id
    if original_id is not None and original_id != quote.pk:
        from orders.models import Quote

        original = Quote._base_manager.select_related("engagement").get(pk=original_id)
        if original.company_id != quote.company_id:
            raise ValidationError({"original_quote": "The original quote belongs to another company."})
        if original.engagement_id is None:
            eng = ensure_for_quote(original)
            Quote._base_manager.filter(pk=original.pk).update(engagement=eng)
            return eng
        return original.engagement

    company = quote.company
    pipeline = _default_pipeline_or_seed(company)
    if pipeline is None:
        raise ValidationError({"pipeline": "The company has no default pipeline."})
    customer = quote.customer if quote.customer_id else None
    party = customer.party if customer is not None and customer.party_id else None
    grand_total = quote.grand_total or 0
    who = party.display_name if party is not None else "No counterparty"
    # A legacy quote ensured lazily (its next save, or reconcile --fix) is the
    # deferred end of the step-5 backfill, not a new deal: it announces
    # nothing (spec 5.1, 7), exactly like the backfill would have.
    # ... and its history row is reconstructed, not observed: ``backfill``,
    # so the timeline marks it and the stage report discloses it (R11).
    # Until 2026-10-17 it was written as ``system`` with no actor, which
    # read like an observed change; reconcile_engagements --fix repairs
    # rows of that shape.
    lazy = not quote._state.adding
    quiet = suppressed() if lazy else contextlib.nullcontext()
    with quiet:
        eng = create(
            company=company, party=party, pipeline=pipeline, stage=_quote_stage(pipeline, quote),
            owner=quote.sales_rep if quote.sales_rep_id else None,
            source=Source.BACKFILL if lazy else Source.SYSTEM, origin=Engagement.Origin.QUOTE,
            title=f"{who} — {quote.quote_number}"[:255] if quote.quote_number else "",
            currency=quote.currency or "USD",
            expected_value=grand_total if grand_total > 0 else None,
            note=f"Quote {quote.quote_number}".strip(),
        )
        # A legacy row ensured lazily may already be decided; a brand-new quote
        # normally starts as a draft and closes nothing here.
        _close_like_quote(eng, quote, source=Source.BACKFILL if lazy else Source.SYSTEM)
    return eng


def engagements_for_customer(company, customer):
    """The engagements a new quote for ``customer`` may be put on (R1.06).

    The customer's party's open or won deals in ``company`` (counterparty or
    contact), most recently active first. Lost deals take no new quote; a
    revision inherits its original's engagement regardless.
    """
    party_id = getattr(customer, "party_id", None)
    if party_id is None:
        return Engagement.objects.none()
    from django.db.models import F, Q

    return (
        Engagement.objects.filter(company=company, status__in=(Engagement.Status.OPEN, Engagement.Status.WON))
        .filter(Q(party_id=party_id) | Q(contacts__party_id=party_id))
        .select_related("stage")
        .order_by(F("last_activity_at").desc(nulls_last=True), "-created_at", "-pk")
        .distinct()
    )


def check_quote_engagement(engagement, *, company, customer) -> None:
    """Refuse an engagement a quote for ``customer`` cannot join.

    Raises ``ValidationError`` when the engagement belongs to another
    company, is lost, or has a counterparty other than the customer's party
    (an engagement with no counterparty accepts the quote).
    """
    if engagement.company_id != company.pk:
        raise ValidationError({"engagement": "The engagement belongs to another company."})
    if engagement.status == Engagement.Status.LOST:
        raise ValidationError({"engagement": "The engagement is lost; reopen it first, or let the quote start a new one."})
    party_id = getattr(customer, "party_id", None)
    if engagement.party_id is not None and party_id is not None and engagement.party_id != party_id:
        if not engagement.contacts.filter(party_id=party_id).exists():
            raise ValidationError({"engagement": "The engagement is with another party than this customer's."})


def record_quote_added(quote, *, by=None, source="erp") -> EngagementStageChange | None:
    """The history row for a quote put on an existing engagement (R1.06).

    Same stage, same status: the row says the quote arrived, nothing moved.
    ``ensure_for_quote`` writes the first row of a NEW engagement through
    ``create``; this is the counterpart for a quote that joined one. No-op
    for a quote with no engagement.
    """
    _check_source(source)
    eng = quote.engagement if quote.engagement_id else None
    if eng is None:
        return None
    return _history(eng, from_pipeline_id=eng.pipeline_id, from_stage_id=eng.stage_id,
                    from_status=eng.status, by=by, source=source,
                    note=f"Quote {quote.quote_number} added".strip())


def win_for_conversion(quote) -> Engagement | None:
    """Conversion wins the quote's thread (spec 1.6) when it is still open.

    Other open quotes on the engagement are left alone: the thread view shows
    them, nothing is cancelled for the user.
    """
    if quote.engagement_id is None:
        return None
    eng = quote.engagement
    if eng.status == Engagement.Status.OPEN:
        win(eng, source=Source.SYSTEM, note=f"Quote {quote.quote_number} converted to an order")
    return eng


def quote_compat_value(quote, name):
    """An old Quote pipeline field, derived from the engagement (spec 1.6).

    ``sales_stage`` maps back to the old vocabulary: won -> ``closed_won``,
    lost -> ``closed_lost``, an open stage -> its key when the old enum had
    it, else ``prospecting``. A quote with no engagement yet (an untouched
    legacy draft) answers from its ``legacy_*`` column.
    """
    eng = quote.engagement if quote.engagement_id else None
    if eng is None:
        return getattr(quote, f"legacy_{name}")
    if name == "sales_stage":
        if eng.status == Engagement.Status.WON:
            return "closed_won"
        if eng.status == Engagement.Status.LOST:
            return "closed_lost"
        key = eng.stage.key if eng.stage_id else ""
        return key if key in OLD_OPEN_SALES_STAGES else "prospecting"
    if name == "win_probability":
        probability = eng.effective_probability
        return probability if probability is not None else 0
    if name == "estimated_close_date":
        return eng.expected_close_date
    if name == "loss_reason":
        return eng.loss_reason if eng.status == Engagement.Status.LOST else ""
    if name == "follow_up_date":
        if eng.next_task_id is None:
            return None
        from .tasks import local_due_date

        return local_due_date(eng.next_task)
    raise AttributeError(f"Quote has no compat field {name!r}")


# --- leads on the thread (spec 1.7, Phase 3 step 6) ------------------------------------


def open_engagement_for_party(company, party, *, pipeline=None) -> Engagement | None:
    """The party's open engagement in ``pipeline`` (default: the company's default).

    The party is the engagement's counterparty or one of its contacts. With
    several, the most recently active one (then the newest): a repeat enquiry
    joins the deal the team is working, it never opens a second one.
    """
    if party is None:
        return None
    pipeline = pipeline or default_pipeline(company)
    if pipeline is None:
        return None
    from django.db.models import F, Q

    return (
        Engagement._base_manager.filter(company=company, pipeline=pipeline,
                                        status=Engagement.Status.OPEN)
        .filter(Q(party=party) | Q(contacts__party=party))
        .order_by(F("last_activity_at").desc(nulls_last=True), "-created_at", "-pk")
        .distinct()
        .first()
    )


def ensure_for_party(company, party, *, stage_key="", by=None, source=Source.SYSTEM,
                     origin=Engagement.Origin.ERP, **fields) -> tuple[Engagement, bool]:
    """``(engagement, created)``: the party's open engagement in the default
    pipeline, else a new one there (spec 1.7).

    ``stage_key`` picks the new engagement's stage (the pipeline's first
    active stage when blank or missing); ``fields`` go to ``create()``. A
    ``party`` of None always creates (the "no counterparty" queue, 6.1).
    """
    existing = open_engagement_for_party(company, party)
    if existing is not None:
        return existing, False
    pipeline = _default_pipeline_or_seed(company)
    if pipeline is None:
        raise ValidationError({"pipeline": "The company has no default pipeline."})
    stage = None
    if stage_key:
        stage = pipeline.stages.filter(key=stage_key, is_active=True).first()
    eng = create(company=company, party=party, pipeline=pipeline, stage=stage, by=by,
                 source=source, origin=origin, **fields)
    return eng, True
