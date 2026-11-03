"""CRM data steps (Phase 2 step 1, Phase 3 step 5).

Plain functions so the data migrations, the management commands and the
tests share one implementation. They take historical models (the model
classes, or the migration's ``apps``), so they use only what historical
models have: fields, ``_base_manager``, ``bulk_create`` and ``update``, no
model methods, custom managers or signals, and they emit nothing.

Phase 2 step 1: tenancy backfill for ``crm.ActivityLog.company``
-----------------------------------------------------------------
``backfill_activity_company`` (also run by ``backfill_tenancy --also
crm.ActivityLog``). Rows with ``company`` NULL are filled from these sources, in order; a later
source only sees rows the earlier ones left NULL (spec section 3.1):

1. ``lead.company``
2. ``customer.company``
3. ``performed_by.company`` (the performer's UserProfile)
4. the fallback Company: the one passed in, else the only Company when the
   database holds exactly one.

Whatever is still NULL afterwards is counted and reported, never guessed.
Re-running is safe: rows that already have a company are never touched.

Phase 3 step 5: quotes onto engagements
---------------------------------------
``backfill_quote_engagements`` (also ``manage.py backfill_crm --step 5``):
one Engagement per quote revision chain (E5). The rules, from spec 1.6 and section 7:

- **Chain.** Quotes joined by ``original_quote`` or ``superseded_by``. When
  ``original_quote`` points at the chain root, as ``reporting.erp_views``
  assumes, this is exactly the report's chain; following both links also
  keeps an imported chain that set only one of them in one piece.
- **Head.** The chain's latest revision, as the report defines it: not
  ``superseded``, no ``superseded_by``, highest ``revision_number``. A chain
  whose every row is superseded falls back to its highest revision.
- **Which chains.** Only chains carrying pipeline data: any revision with a
  non-default pipeline field, a sales rep, a lead source or campaign, a
  status past ``draft``, or a conversion. Untouched drafts are left for
  ``ensure_for_quote`` (next save) or ``reconcile_engagements --fix``.
- **Outcome** (stored stage and status disagree, so stage alone is not
  trusted), priority won > lost > open: won if the head's stage is
  ``closed_won``, its status ``accepted``, or any revision converted; lost
  if the head's stage is ``closed_lost`` or its status is ``lost`` /
  ``expired`` / ``cancelled``.
- **Stage.** Raw ``sales_stage`` strings are normalised (``normalise_stage``,
  ``STAGE_ALIASES``) and mapped onto the default pipeline's stage keys. The
  stage kept is the head's mapped open stage, else the latest revision's
  that has one; a closed chain with none keeps ``stage=NULL``, an open chain
  with none starts at the pipeline's first stage with
  ``metadata["unmapped_stage"]``. Unmapped values stop the step unless
  ``accept_unmapped``, so a human reads the list first.
- **History.** ``EngagementStageChange`` rows with ``source=backfill``: the
  creation row at the chain's start, and a closing row when closed.
- Idempotent: a chain whose quotes already point at one engagement is
  skipped (its unlinked revisions and orders are attached to it). Reversible:
  ``reverse_quote_engagements`` deletes what the step created.
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F, OuterRef, Q, Subquery
from django.utils import timezone

from core.plugins.dispatch import suppressed

SOURCES = ("lead", "customer", "performed_by")



def _stamp_owner_teams(Engagement, UserProfile, engagements) -> None:
    """``bulk_create`` skips ``save()``'s owner-team stamp (phase4-fields.md
    2.1): stamp the new rows. The historical models of the migrations that
    run this predate ``owner_team``; there is nothing to stamp then."""
    if not engagements or not any(f.name == "owner_team" for f in Engagement._meta.get_fields()):
        return
    from core.teams import backfill_owner_teams

    ids = [eng.pk for eng in engagements]
    for start in range(0, len(ids), 500):  # SQLite's bound-variable limit
        backfill_owner_teams(Engagement, UserProfile, ids=ids[start:start + 500])

def _single_company(company_model):
    companies = list(company_model._base_manager.order_by("pk")[:2])
    return companies[0] if len(companies) == 1 else None


def backfill_activity_company(activity_model, company_model, fallback=None, now=None):
    """Fill NULL ``ActivityLog.company``; return the per-source counts.

    The result maps each source in order (``lead``, ``customer``,
    ``performed_by``, ``single_company``) to the number of rows it filled, plus
    ``still_null`` and ``lead_customer_conflicts`` (rows whose lead and
    customer belong to different companies). ``fallback`` overrides the single-Company rule.
    """
    now = now or timezone.now()
    pending = activity_model._base_manager.filter(company__isnull=True)
    counts = {}
    for source in SOURCES:
        related = activity_model._meta.get_field(source).related_model
        company_id = Subquery(
            related._base_manager.filter(pk=OuterRef(f"{source}_id"))
            .order_by()
            .values("company_id")[:1]
        )
        counts[source] = pending.filter(**{f"{source}__company__isnull": False}).update(
            company_id=company_id, updated_at=now
        )
    if fallback is None:
        fallback = _single_company(company_model)
    counts["single_company"] = (
        pending.update(company_id=fallback.pk, updated_at=now) if fallback is not None else 0
    )
    counts["still_null"] = pending.count()
    # Rows whose lead and customer sit in different tenants: the lead won, but
    # the row still hangs on the other tenant's customer. Reported, not fixed.
    counts["lead_customer_conflicts"] = (
        activity_model._base_manager.filter(
            lead__company__isnull=False, customer__company__isnull=False
        )
        .exclude(lead__company=F("customer__company"))
        .count()
    )
    return counts


def format_counts(counts):
    """One summary line, as the migration and the command print it."""
    filled = " ".join(f"{key}={counts[key]}" for key in (*SOURCES, "single_company"))
    return (
        f"crm.ActivityLog company backfill: {filled}; still NULL: {counts['still_null']}; "
        f"lead/customer company conflicts: {counts['lead_customer_conflicts']}"
    )


# --- Phase 3 step 5: quotes onto engagements ------------------------------------

BACKFILL_STEP = 5
TASK_SOURCE = "backfill:quote.follow_up_date"
#: Environment switch for the data migration, which takes no options.
ACCEPT_UNMAPPED_ENV = "GOLDEN_CRM_BACKFILL_ACCEPT_UNMAPPED"

CLOSED_WON = "closed_won"
CLOSED_LOST = "closed_lost"
# Quote.SalesStage's default, and what a blank stage means.
DEFAULT_RAW_STAGE = "prospecting"

#: Normalised dirty value -> canonical value (a stage key, or closed_won /
#: closed_lost). Applied after ``normalise_stage``'s strip / lower-case /
#: spaces-and-hyphens-to-underscore, so "Closed Won" and "closed-won" never
#: need an entry. Lead's vocabulary is included because chains were seeded
#: from leads by hand on some deployments.
STAGE_ALIASES = {
    "": DEFAULT_RAW_STAGE,
    # won
    "closedwon": CLOSED_WON,
    "won": CLOSED_WON,
    "win": CLOSED_WON,
    "closed_win": CLOSED_WON,
    "close_won": CLOSED_WON,
    # lost
    "closedlost": CLOSED_LOST,
    "lost": CLOSED_LOST,
    "loss": CLOSED_LOST,
    "closed_loss": CLOSED_LOST,
    "close_lost": CLOSED_LOST,
    # open stages
    "prospect": "prospecting",
    "prospects": "prospecting",
    "contacted": "prospecting",
    "qualify": "qualification",
    "qualified": "qualification",
    "qualifying": "qualification",
    "needsanalysis": "needs_analysis",
    "needs_assessment": "needs_analysis",
    "proposal": "proposal_sent",
    "proposalsent": "proposal_sent",
    "quote_sent": "proposal_sent",
    "negotiating": "negotiation",
    "negotiations": "negotiation",
    "verbal": "verbal_commit",
    "verbalcommit": "verbal_commit",
    "verbal_commitment": "verbal_commit",
}

_QUOTE_LOSS_STATUS_REASON = {"expired": "expired", "cancelled": "cancelled"}
_LOST_STATUSES = frozenset({"lost", "expired", "cancelled"})

_QUOTE_FIELDS = (
    "id", "company_id", "customer_id", "original_quote_id", "superseded_by_id",
    "revision_number", "status", "quote_number", "currency", "grand_total",
    "sales_rep_id", "converted_to_order_id", "converted_at", "accepted_at", "sent_at",
    "created_at", "updated_at", "engagement_id",
    "legacy_sales_stage", "legacy_win_probability", "legacy_estimated_close_date",
    "legacy_weighted_value", "legacy_deal_size_category", "legacy_loss_reason",
    "legacy_loss_competitor", "legacy_loss_notes", "legacy_lost_at",
    "legacy_follow_up_date", "legacy_last_contacted_at", "legacy_campaign",
    "legacy_lead_source",
)


def normalise_stage(raw) -> str:
    """Strip, lower-case, spaces and hyphens to ``_``, then the alias table."""
    value = re.sub(r"[\s\-]+", "_", str(raw or "").strip().lower())
    value = re.sub(r"_+", "_", value).strip("_")
    return STAGE_ALIASES.get(value, value)


def accept_unmapped_default() -> bool:
    """The data migration's ``--accept-unmapped``: a setting or an env var."""
    if getattr(settings, "GOLDEN_CRM_BACKFILL_ACCEPT_UNMAPPED", False):
        return True
    return os.environ.get(ACCEPT_UNMAPPED_ENV, "").strip().lower() in {"1", "true", "yes"}


class UnmappedStagesError(RuntimeError):
    """Raw ``sales_stage`` values that map to no stage; nothing was written."""

    def __init__(self, unmapped: Counter):
        self.unmapped = unmapped
        listing = ", ".join(f"{raw!r} x{n}" for raw, n in sorted(unmapped.items(), key=str))
        super().__init__(
            "CRM step 5 backfill: these Quote.sales_stage values map to no pipeline stage: "
            f"{listing}. Add them to crm.backfill.STAGE_ALIASES, or accept them "
            "(`manage.py backfill_crm --step 5 --accept-unmapped`, or for `migrate` set "
            f"{ACCEPT_UNMAPPED_ENV}=1): an open chain then starts at the pipeline's first "
            "stage and a closed one keeps stage NULL, each with metadata.unmapped_stage."
        )


@dataclass
class QuoteBackfillSummary:
    quotes: int = 0
    chains: int = 0
    already_linked: int = 0
    untouched: int = 0
    created: Counter = field(default_factory=Counter)  # by status
    attached_quotes: int = 0
    linked_quotes: int = 0
    linked_orders: int = 0
    history_rows: int = 0
    tasks: int = 0
    conflicts: int = 0
    no_party: int = 0
    raw_stages: Counter = field(default_factory=Counter)
    unmapped: Counter = field(default_factory=Counter)
    dry_run: bool = False

    def lines(self) -> list[str]:
        created = sum(self.created.values())
        by_status = ", ".join(f"{k} {v}" for k, v in sorted(self.created.items())) or "none"
        out = [
            f"quotes: {self.quotes} in {self.chains} revision chain(s)",
            f"engagements created: {created} ({by_status}){' [dry run]' if self.dry_run else ''}",
            f"chains already linked: {self.already_linked}; revisions attached to an existing "
            f"engagement: {self.attached_quotes}",
            f"untouched draft chains left for ensure_for_quote: {self.untouched}",
            f"quotes linked: {self.linked_quotes}; orders linked: {self.linked_orders}",
            f"history rows: {self.history_rows}; follow-up tasks: {self.tasks}",
            f"engagements without a party: {self.no_party}; chains split across engagements "
            f"(left alone): {self.conflicts}",
            "raw sales_stage values: "
            + (", ".join(f"{raw!r} x{n}" for raw, n in sorted(self.raw_stages.items(), key=str))
               or "none"),
            "unmapped: "
            + (", ".join(f"{raw!r} x{n}" for raw, n in sorted(self.unmapped.items(), key=str))
               or "none"),
        ]
        return out


def _registry(apps):
    if apps is None:
        from django.apps import apps as live_apps

        return live_apps
    return apps


def _chains(rows):
    """Union quotes over ``original_quote`` and ``superseded_by`` links."""
    parent = {pk: pk for pk in rows}

    def find(pk):
        while parent[pk] != pk:
            parent[pk] = parent[parent[pk]]
            pk = parent[pk]
        return pk

    for pk, row in rows.items():
        for linked in (row["original_quote_id"], row["superseded_by_id"]):
            # Never across tenants: a cross-company link is dirty data, and
            # each side keeps its own thread.
            if (linked is not None and linked in parent
                    and rows[linked]["company_id"] == row["company_id"]):
                a, b = find(pk), find(linked)
                if a != b:
                    parent[b] = a
    chains = defaultdict(list)
    for pk in rows:
        chains[find(pk)].append(rows[pk])
    return list(chains.values())


def _order_key(row):
    return (row["revision_number"] or 0, row["created_at"], str(row["id"]))


def chain_head(chain):
    """The latest revision (``reporting.erp_views.latest_quote_revisions``)."""
    live = [r for r in chain if r["status"] != "superseded" and r["superseded_by_id"] is None]
    return max(live or chain, key=_order_key)


def chain_root(chain):
    return min(chain, key=_order_key)


def _touched(row) -> bool:
    """Does this quote carry pipeline data (spec 7, "what counts")?"""
    return bool(
        (row["legacy_sales_stage"] or DEFAULT_RAW_STAGE) != DEFAULT_RAW_STAGE
        or row["legacy_win_probability"]
        or row["legacy_estimated_close_date"]
        or (row["legacy_weighted_value"] or 0)
        or row["legacy_deal_size_category"]
        or row["legacy_loss_reason"] or row["legacy_loss_competitor"] or row["legacy_loss_notes"]
        or row["legacy_lost_at"]
        or row["legacy_follow_up_date"] or row["legacy_last_contacted_at"]
        or row["sales_rep_id"]
        or row["legacy_lead_source"] or row["legacy_campaign"]
        or row["status"] != "draft"
        or row["converted_to_order_id"] or row["converted_at"]
    )


@dataclass
class _Plan:
    """What one chain becomes."""

    chain: list
    head: dict
    root: dict
    status: str
    stage_key: str | None
    unmapped_raw: str | None
    loss_reason: str = ""
    closed_at: object = None


def plan_chain(chain, stage_keys, first_stage_key, loss_reasons) -> _Plan:
    """Decide outcome and stage for one chain (pure; tested directly)."""
    head, root = chain_head(chain), chain_root(chain)
    head_norm = normalise_stage(head["legacy_sales_stage"])
    converted = [r for r in chain if r["converted_to_order_id"] or r["converted_at"]]
    if head_norm == CLOSED_WON or head["status"] == "accepted" or converted:
        status = "won"
    elif head_norm == CLOSED_LOST or head["status"] in _LOST_STATUSES:
        status = "lost"
    else:
        status = "open"

    stage_key = None
    ordered = [head] + sorted((r for r in chain if r is not head), key=_order_key, reverse=True)
    for row in ordered:
        norm = normalise_stage(row["legacy_sales_stage"])
        if norm in stage_keys:
            stage_key = norm
            break
    unmapped_raw = None
    if head_norm not in stage_keys and head_norm not in (CLOSED_WON, CLOSED_LOST):
        unmapped_raw = head["legacy_sales_stage"]
    if status == "open" and stage_key is None:
        stage_key = first_stage_key

    plan = _Plan(chain=chain, head=head, root=root, status=status, stage_key=stage_key,
                 unmapped_raw=unmapped_raw)
    if status == "won":
        stamps = [r["converted_at"] for r in converted if r["converted_at"]]
        stamps += [head["accepted_at"]] if head["accepted_at"] else []
        plan.closed_at = max(stamps) if stamps else head["updated_at"]
    elif status == "lost":
        reason = head["legacy_loss_reason"]
        if reason not in loss_reasons:
            reason = _QUOTE_LOSS_STATUS_REASON.get(head["status"], "unknown")
        plan.loss_reason = reason
        plan.closed_at = head["legacy_lost_at"] or head["updated_at"]
    return plan


def _mint_number(apps, company_id, taken):
    """``DocumentSequence.get_next_number`` for historical models (prefix EN)."""
    DocumentSequence = apps.get_model("core", "DocumentSequence")
    Engagement = apps.get_model("crm", "Engagement")
    for _ in range(1000):
        seq, _created = DocumentSequence._base_manager.select_for_update().get_or_create(
            company_id=company_id
        )
        num = seq.next_number
        DocumentSequence._base_manager.filter(pk=seq.pk).update(next_number=num + 1)
        candidate = f"EN-{num:05d}"
        if candidate not in taken and not Engagement._base_manager.filter(number=candidate).exists():
            taken.add(candidate)
            return candidate
    raise RuntimeError(f"Could not mint a free EN- number for company {company_id}")


def _title(party_name, root):
    who = party_name or "No counterparty"
    return f"{who} — {root['quote_number']}"[:255]


def _party_names(apps, party_ids):
    Party = apps.get_model("parties", "Party")
    return dict(
        Party._base_manager.filter(pk__in=list(party_ids)).values_list("pk", "display_name")
    )


def _metadata(plan):
    head, root = plan.head, plan.root
    legacy = {
        name: _json(head[f"legacy_{name}"])
        for name in ("sales_stage", "win_probability", "estimated_close_date", "weighted_value",
                     "deal_size_category", "loss_reason", "loss_competitor", "loss_notes",
                     "lost_at", "follow_up_date", "last_contacted_at", "campaign", "lead_source")
        if head[f"legacy_{name}"] not in (None, "", 0, Decimal("0"))
    }
    meta = {
        "backfill": {
            "step": BACKFILL_STEP,
            "chain_root": str(root["id"]),
            "head": str(head["id"]),
            "quotes": len(plan.chain),
        },
        "quote_legacy": legacy,
    }
    if plan.unmapped_raw is not None:
        meta["unmapped_stage"] = plan.unmapped_raw
    return meta


def _json(value):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _source_key(raw, sources):
    norm = normalise_stage(raw) if raw else ""
    return norm if norm in sources else ""


@suppressed()  # data steps announce nothing (spec 5.1, 7)
def backfill_quote_engagements(apps=None, *, dry_run=False, accept_unmapped=False,
                               log=None) -> QuoteBackfillSummary:
    """Step 5 forward. Returns the summary; raises ``UnmappedStagesError``
    (writing nothing) on unmapped stages unless ``accept_unmapped``.

    One transaction from read to write, so a concurrent ``Quote.save()``
    cannot thread a chain between planning and linking.
    """
    with transaction.atomic():
        return _backfill_quote_engagements(apps, dry_run=dry_run,
                                           accept_unmapped=accept_unmapped, log=log)


def _backfill_quote_engagements(apps, *, dry_run, accept_unmapped, log):
    from core import tz
    from crm.registries import ENGAGEMENT_SOURCES, LOSS_REASONS
    from crm.tasks import end_of_local_day

    apps = _registry(apps)
    Quote = apps.get_model("orders", "Quote")
    Order = apps.get_model("orders", "Order")
    Customer = apps.get_model("customers", "Customer")
    Company = apps.get_model("core", "Company")
    UserProfile = apps.get_model("core", "UserProfile")
    Pipeline = apps.get_model("crm", "Pipeline")
    Stage = apps.get_model("crm", "Stage")
    Engagement = apps.get_model("crm", "Engagement")
    Change = apps.get_model("crm", "EngagementStageChange")
    Task = apps.get_model("crm", "Task")
    TaskEvent = apps.get_model("crm", "TaskEvent")

    summary = QuoteBackfillSummary(dry_run=dry_run)
    rows = {r["id"]: r for r in Quote._base_manager.select_for_update().values(*_QUOTE_FIELDS)}
    summary.quotes = len(rows)
    for row in rows.values():
        summary.raw_stages[row["legacy_sales_stage"]] += 1
    chains = _chains(rows)
    summary.chains = len(chains)

    pipelines = {
        p.company_id: p for p in Pipeline._base_manager.filter(is_default=True)
    }
    stages = defaultdict(dict)  # pipeline id -> key -> stage
    first_stage = {}
    for stage in Stage._base_manager.filter(is_active=True).order_by("sort_order", "pk"):
        stages[stage.pipeline_id][stage.key] = stage
        first_stage.setdefault(stage.pipeline_id, stage)

    to_create, to_attach = [], []
    for chain in chains:
        linked = {r["engagement_id"] for r in chain if r["engagement_id"] is not None}
        if len(linked) > 1:
            summary.conflicts += 1
            continue
        if linked:
            missing = [r for r in chain if r["engagement_id"] is None]
            if missing:
                to_attach.append((next(iter(linked)), chain))
            else:
                summary.already_linked += 1
            continue
        if not any(_touched(r) for r in chain):
            summary.untouched += 1
            continue
        head = chain_head(chain)
        pipeline = pipelines.get(head["company_id"])
        if pipeline is None:
            raise RuntimeError(
                f"Company {head['company_id']} has no default pipeline; run crm migration "
                "0008 (seed_default_pipelines) first."
            )
        keys = stages[pipeline.pk]
        first = first_stage.get(pipeline.pk)
        plan = plan_chain(chain, keys, first.key if first else None, LOSS_REASONS)
        for row in chain:
            norm = normalise_stage(row["legacy_sales_stage"])
            if norm not in keys and norm not in (CLOSED_WON, CLOSED_LOST):
                summary.unmapped[row["legacy_sales_stage"]] += 1
        to_create.append((pipeline, plan))

    if summary.unmapped and not accept_unmapped and not dry_run:
        raise UnmappedStagesError(summary.unmapped)
    for _pipeline, plan in to_create:
        summary.created[plan.status] += 1
    if dry_run:
        _log(log, summary)
        return summary

    customers = dict(
        Customer._base_manager.filter(
            pk__in={plan.head["customer_id"] for _p, plan in to_create}
        ).values_list("pk", "party_id")
    )
    party_names = _party_names(apps, {v for v in customers.values() if v})
    companies = {c.pk: c for c in Company._base_manager.filter(
        pk__in={plan.head["company_id"] for _p, plan in to_create})}
    profiles = {p.pk: p for p in UserProfile._base_manager.filter(
        pk__in={plan.head["sales_rep_id"] for _p, plan in to_create if plan.head["sales_rep_id"]})}

    with transaction.atomic():
        taken: set[str] = set()
        engagements, changes, tasks, events, links = [], [], [], [], []
        for pipeline, plan in to_create:
            head, root = plan.head, plan.root
            party_id = customers.get(head["customer_id"])
            if party_id is None:
                summary.no_party += 1
            stage = stages[pipeline.pk].get(plan.stage_key) if plan.stage_key else None
            probability = head["legacy_win_probability"] or None
            if probability is not None:
                probability = max(0, min(int(probability), 100))
            started = root["created_at"]
            last_activity = max(r["updated_at"] for r in plan.chain)
            eng = Engagement(
                company_id=head["company_id"],
                number=_mint_number(apps, head["company_id"], taken),
                title=_title(party_names.get(party_id), root),
                pipeline_id=pipeline.pk,
                stage_id=stage.pk if stage else None,
                stage_entered_at=head["created_at"] if stage else None,
                status=plan.status,
                closed_at=plan.closed_at if plan.status != "open" else None,
                loss_reason=plan.loss_reason,
                loss_competitor=(head["legacy_loss_competitor"] or "")[:255]
                if plan.status == "lost" else "",
                loss_notes=(head["legacy_loss_notes"] or "") if plan.status == "lost" else "",
                owner_id=head["sales_rep_id"],
                party_id=party_id,
                expected_value=head["grand_total"] if (head["grand_total"] or 0) > 0 else None,
                currency=root["currency"] or "USD",
                probability=probability,
                expected_close_date=head["legacy_estimated_close_date"],
                source=_source_key(head["legacy_lead_source"], ENGAGEMENT_SOURCES),
                last_activity_at=last_activity,
                origin="backfill",
                metadata=_metadata(plan),
            )
            engagements.append(eng)
            closed = plan.status != "open"
            if closed and stage is None:
                changes.append(Change(
                    engagement=eng, company_id=eng.company_id, to_pipeline_id=pipeline.pk,
                    from_status="", to_status=plan.status, changed_at=plan.closed_at,
                    source="backfill", note="Backfilled from quote chain (stage unknown)",
                ))
            else:
                changes.append(Change(
                    engagement=eng, company_id=eng.company_id, to_pipeline_id=pipeline.pk,
                    to_stage_id=eng.stage_id, from_status="", to_status="open",
                    changed_at=started, source="backfill",
                    note="Backfilled from quote chain",
                ))
                if closed:
                    changes.append(Change(
                        engagement=eng, company_id=eng.company_id,
                        from_pipeline_id=pipeline.pk, to_pipeline_id=pipeline.pk,
                        from_stage_id=eng.stage_id, to_stage_id=eng.stage_id,
                        from_status="open", to_status=plan.status,
                        # Never before the creation row, whatever the stamps say.
                        changed_at=max(plan.closed_at, started),
                        source="backfill", note="Backfilled outcome",
                    ))
            follow_up = next(
                (r["legacy_follow_up_date"] for r in
                 [head] + sorted(plan.chain, key=_order_key, reverse=True)
                 if r["legacy_follow_up_date"]), None,
            )
            if follow_up and not closed:
                owner = profiles.get(head["sales_rep_id"])
                zone = tz.resolve(owner, companies.get(head["company_id"]))
                due_at = end_of_local_day(follow_up, zone)
                task = Task(
                    company_id=eng.company_id, title=f"Follow up on {head['quote_number']}",
                    kind="follow_up", status="open", priority="normal",
                    assignee_id=head["sales_rep_id"], engagement=eng, party_id=party_id,
                    due_at=due_at, all_day=True, due_tz=zone.key, source=TASK_SOURCE,
                )
                tasks.append(task)
                events.append(TaskEvent(task=task, company_id=eng.company_id, event="created",
                                        to_value=due_at.isoformat()[:64], at=started))
                eng.next_task = task
                eng.next_action_due_at = due_at
            links.append((eng, plan.chain))

        # next_task is set after the tasks exist (the engagement is inserted first).
        pending_next = {eng.pk: (eng.next_task, eng.next_action_due_at) for eng in engagements}
        for eng in engagements:
            eng.next_task, eng.next_action_due_at = None, None
        Engagement._base_manager.bulk_create(engagements)
        _stamp_owner_teams(Engagement, UserProfile, engagements)
        Change._base_manager.bulk_create(changes)
        Task._base_manager.bulk_create(tasks)
        TaskEvent._base_manager.bulk_create(events)
        for pk, (task, due_at) in pending_next.items():
            if task is not None:
                Engagement._base_manager.filter(pk=pk).update(next_task_id=task.pk,
                                                              next_action_due_at=due_at)
        summary.history_rows = len(changes)
        summary.tasks = len(tasks)

        for eng, chain in links:
            q, o = _link(Quote, Order, eng.pk, chain)
            summary.linked_quotes += q
            summary.linked_orders += o
        for engagement_id, chain in to_attach:
            q, o = _link(Quote, Order, engagement_id, chain)
            summary.attached_quotes += q
            summary.linked_quotes += q
            summary.linked_orders += o
    _log(log, summary)
    return summary


def _link(Quote, Order, engagement_id, chain):
    ids = [r["id"] for r in chain]
    quotes = Quote._base_manager.filter(pk__in=ids, engagement__isnull=True).update(
        engagement_id=engagement_id
    )
    order_ids = {r["converted_to_order_id"] for r in chain if r["converted_to_order_id"]}
    orders = Order._base_manager.filter(engagement__isnull=True).filter(
        pk__in=order_ids
    ).update(engagement_id=engagement_id)
    orders += Order._base_manager.filter(engagement__isnull=True, quote_id__in=ids).update(
        engagement_id=engagement_id
    )
    return quotes, orders


class ReverseRefused(RuntimeError):
    """Backfilled engagements carry work added after the backfill."""


@suppressed()  # data steps announce nothing (spec 5.1, 7)
def reverse_quote_engagements(apps=None, *, log=None, force=False) -> int:
    """Step 5 backward: delete the engagements the step created (and their
    history and tasks), unlinking their quotes and orders first.

    Refuses (unless ``force``) once people have worked on those threads: a
    history row, task, contact or communication the backfill did not write
    would be lost (or, for a communication, unlinked).
    """
    apps = _registry(apps)
    Quote = apps.get_model("orders", "Quote")
    Order = apps.get_model("orders", "Order")
    Engagement = apps.get_model("crm", "Engagement")
    Change = apps.get_model("crm", "EngagementStageChange")
    Task = apps.get_model("crm", "Task")
    TaskEvent = apps.get_model("crm", "TaskEvent")
    Contact = apps.get_model("crm", "EngagementContact")
    Communication = apps.get_model("communications", "Communication")
    with transaction.atomic():
        ids = list(
            Engagement._base_manager.filter(origin="backfill", metadata__backfill__step=BACKFILL_STEP)
            .values_list("pk", flat=True)
        )
        later = (
            Change._base_manager.filter(engagement_id__in=ids).exclude(source="backfill").count()
            + Task._base_manager.filter(engagement_id__in=ids).exclude(source=TASK_SOURCE).count()
            + TaskEvent._base_manager.filter(task__engagement_id__in=ids).exclude(event="created").count()
            + Contact._base_manager.filter(engagement_id__in=ids).count()
            + Communication._base_manager.filter(engagement_id__in=ids).count()
        )
        if later and not force:
            raise ReverseRefused(
                f"CRM step 5 reverse: {later} history row(s), task(s), contact(s) or communication(s) were added to "
                "backfilled engagements after the backfill; reversing would delete them. Run "
                "`manage.py backfill_crm --step 5 --reverse --force` to discard them anyway."
            )
        Quote._base_manager.filter(engagement_id__in=ids).update(engagement_id=None)
        Order._base_manager.filter(engagement_id__in=ids).update(engagement_id=None)
        Engagement._base_manager.filter(pk__in=ids).update(next_task_id=None, next_action_due_at=None)
        task_ids = list(Task._base_manager.filter(engagement_id__in=ids).values_list("pk", flat=True))
        TaskEvent._base_manager.filter(task_id__in=task_ids).delete()
        Task._base_manager.filter(pk__in=task_ids).delete()
        Change._base_manager.filter(engagement_id__in=ids).delete()
        Engagement._base_manager.filter(pk__in=ids).delete()
    if log:
        log(f"engagements removed: {len(ids)}")
    return len(ids)


def _log(log, summary):
    if log:
        for line in summary.lines():
            log(line)


# --- Phase 3 step 6: leads onto engagements --------------------------------------
#
# ``backfill_lead_engagements`` (also ``manage.py backfill_crm --step 6``), the
# intake rule (spec 1.7) applied to every lead without an engagement, oldest
# first, after step 5 so a converted lead finds its quote's engagement:
#
# - no company: skipped (reported); ``request_type="support"``: no engagement;
# - ``converted_to_quote`` set: attached to that quote's engagement. A quote
#   still without one (an untouched draft step 5 left for ``ensure_for_quote``)
#   leaves the lead waiting; ``reconcile_engagements --fix`` ensures the quote
#   first, then attaches the lead;
# - an open lead (new / contacted / qualified / proposal_sent) whose party has
#   an open engagement in the default pipeline, including one this run just
#   created for an earlier lead: attached to it (a repeat enquiry);
# - otherwise a new engagement (``origin=backfill``) from the lead's status:
#   converted -> won, closed_lost -> lost (the lead's ``loss_reason`` when it
#   is a ``GOLDEN_LOSS_REASONS`` key, else ``unknown``), closed_junk -> lost
#   ``disqualified``; a closed one keeps ``stage=NULL`` (stage unknown, E3).
#   An open one sits at the stage its status names (``LEAD_STATUS_TO_STAGE``)
#   with one open task: due on ``follow_up_date`` (end of that day, the
#   owner's zone) when set, else undated, so it has a next action.
#
# Every lead it handles also gets its message as a ``note`` Communication and
# a Touchpoint when it carries attribution, as intake writes them. The step's
# communications (``metadata.backfill == 6``) mark which leads it linked, so the
# reverse unlinks exactly those. Idempotent: leads with an engagement are skipped.

LEAD_STEP = 6
LEAD_TASK_SOURCE = "backfill:lead"
LEAD_FOLLOW_UP_SOURCE = "backfill:lead.follow_up_date"
LEAD_TOUCH_SOURCE = "backfill:lead"
LEAD_PROVIDER = "lead_form"
_OPEN_LEAD_STATUSES = frozenset({"new", "contacted", "qualified", "proposal_sent"})
_LEAD_FIELDS = (
    "id", "company_id", "lead_number", "status", "request_type", "source", "message",
    "first_name", "last_name", "party_id", "assigned_to_id", "estimated_value",
    "follow_up_date", "last_contacted_at", "contact_count", "converted_to_quote_id",
    "converted_at", "loss_reason", "loss_notes", "lost_at", "utm_source", "utm_medium",
    "utm_campaign", "referrer_url", "created_at", "updated_at",
)


@dataclass
class LeadBackfillSummary:
    leads: int = 0
    no_company: int = 0
    support: int = 0
    attached_to_quote: int = 0
    waiting_on_quote: int = 0
    attached_repeat: int = 0
    created: Counter = field(default_factory=Counter)  # by status
    tasks: int = 0
    touchpoints: int = 0
    communications: int = 0
    no_party: int = 0
    dry_run: bool = False

    def lines(self) -> list[str]:
        created = sum(self.created.values())
        by_status = ", ".join(f"{k} {v}" for k, v in sorted(self.created.items())) or "none"
        return [
            f"leads without an engagement: {self.leads}",
            f"engagements created: {created} ({by_status}){' [dry run]' if self.dry_run else ''}",
            f"attached to their quote's engagement: {self.attached_to_quote}; repeat inquiries "
            f"attached to an open engagement: {self.attached_repeat}",
            f"support requests (no engagement): {self.support}; no company (skipped): "
            f"{self.no_company}; waiting on their quote's engagement: {self.waiting_on_quote}",
            f"open tasks: {self.tasks}; touchpoints: {self.touchpoints}; message "
            f"communications: {self.communications}; engagements without a party: {self.no_party}",
        ]


def _lead_plan(row, loss_reasons):
    """``(status, stage_key, loss_reason, closed_at)`` for a lead's new engagement."""
    from crm.intake import JUNK_REASON, LEAD_STATUS_TO_STAGE

    status = row["status"]
    if status == "converted":
        return "won", None, "", row["converted_at"] or row["updated_at"]
    if status == "closed_junk":
        return "lost", None, JUNK_REASON, row["lost_at"] or row["updated_at"]
    if status == "closed_lost":
        reason = normalise_stage(row["loss_reason"])
        return ("lost", None, reason if reason in loss_reasons else "unknown",
                row["lost_at"] or row["updated_at"])
    return "open", LEAD_STATUS_TO_STAGE.get(status), "", None


def _lead_title(party_name, row):
    who = party_name or "No counterparty"
    return f"{who} — {row['lead_number']}"[:255]


@suppressed()  # data steps announce nothing (spec 5.1, 7)
def backfill_lead_engagements(apps=None, *, dry_run=False, log=None) -> LeadBackfillSummary:
    """Step 6, leads. One transaction; leads read ``FOR UPDATE``."""
    with transaction.atomic():
        return _backfill_lead_engagements(apps, dry_run=dry_run, log=log)


def _backfill_lead_engagements(apps, *, dry_run, log):  # noqa: C901 - one linear rule table
    import datetime

    from core import tz
    from crm.conf import crm_setting
    from crm.intake import has_attribution
    from crm.registries import ENGAGEMENT_SOURCES, LOSS_REASONS
    from crm.tasks import end_of_local_day

    apps = _registry(apps)
    Lead = apps.get_model("crm", "Lead")
    Quote = apps.get_model("orders", "Quote")
    Company = apps.get_model("core", "Company")
    UserProfile = apps.get_model("core", "UserProfile")
    Party = apps.get_model("parties", "Party")
    Pipeline = apps.get_model("crm", "Pipeline")
    Stage = apps.get_model("crm", "Stage")
    Engagement = apps.get_model("crm", "Engagement")
    Change = apps.get_model("crm", "EngagementStageChange")
    Task = apps.get_model("crm", "Task")
    TaskEvent = apps.get_model("crm", "TaskEvent")
    Touchpoint = apps.get_model("crm", "Touchpoint")
    Communication = apps.get_model("communications", "Communication")

    summary = LeadBackfillSummary(dry_run=dry_run)
    rows = list(
        Lead._base_manager.select_for_update().filter(engagement__isnull=True)
        .order_by("created_at", "pk").values(*_LEAD_FIELDS)
    )
    summary.leads = len(rows)
    if not rows:
        _log(log, summary)
        return summary

    pipelines = {p.company_id: p for p in Pipeline._base_manager.filter(is_default=True)}
    stages = defaultdict(dict)
    first_stage = {}
    for stage in Stage._base_manager.filter(is_active=True).order_by("sort_order", "pk"):
        stages[stage.pipeline_id][stage.key] = stage
        first_stage.setdefault(stage.pipeline_id, stage)
    quote_rows = {
        pk: (engagement_id, quote_company)
        for pk, engagement_id, quote_company in Quote._base_manager.filter(
            pk__in={r["converted_to_quote_id"] for r in rows if r["converted_to_quote_id"]}
        ).values_list("pk", "engagement_id", "company_id")
    }
    # Leads whose message is already a note (intake ran, or a support lead an
    # earlier run handled): never a second note or touchpoint.
    noted = {
        m.get("lead") for m in Communication._base_manager.filter(
            provider=LEAD_PROVIDER, metadata__lead__in=[str(r["id"]) for r in rows],
        ).values_list("metadata", flat=True)
    }
    party_rows = {
        p["id"]: p for p in Party._base_manager.filter(
            pk__in={r["party_id"] for r in rows if r["party_id"]}).values("id", "company_id", "display_name")
    }
    profiles = {p.pk: p for p in UserProfile._base_manager.filter(
        pk__in={r["assigned_to_id"] for r in rows if r["assigned_to_id"]})}
    companies = {c.pk: c for c in Company._base_manager.filter(
        pk__in={r["company_id"] for r in rows if r["company_id"]})}
    lookback = datetime.timedelta(days=crm_setting("attribution_lookback_days"))

    # Open engagements per (company, party) in the default pipeline, most
    # recently active first; this run's new open ones are added as it goes.
    open_by_party = {}
    party_ids = {r["party_id"] for r in rows if r["party_id"]}
    if party_ids:
        Contact = apps.get_model("crm", "EngagementContact")
        default_ids = [p.pk for p in pipelines.values()]
        candidates = list(
            Engagement._base_manager.filter(status="open", pipeline_id__in=default_ids)
            .filter(party_id__in=party_ids)
            .values_list("company_id", "party_id", "pk", "last_activity_at", "created_at")
        )
        candidates += list(
            Contact._base_manager.filter(party_id__in=party_ids, engagement__status="open",
                                         engagement__pipeline_id__in=default_ids)
            .values_list("engagement__company_id", "party_id", "engagement_id",
                         "engagement__last_activity_at", "engagement__created_at")
        )
        # As open_engagement_for_party: counterparty or contact, most recently
        # active first, then newest.
        epoch = timezone.make_aware(datetime.datetime(1970, 1, 1), datetime.UTC)
        candidates.sort(key=lambda c: (c[3] or epoch, c[4]), reverse=True)
        for company_id, party_id, eng_id, _active, _created in candidates:
            open_by_party.setdefault((company_id, party_id), eng_id)
    existing_touches = defaultdict(list)
    for t in Touchpoint._base_manager.filter(party_id__in=party_ids).values(
            "id", "company_id", "party_id", "occurred_at"):
        existing_touches[(t["company_id"], t["party_id"])].append((t["occurred_at"], t["id"]))

    taken: set[str] = set()
    engagements, changes, tasks, events, touches, comms = [], [], [], [], [], []
    links = []  # (lead id, engagement id or object)
    bumps = defaultdict(list)  # existing engagement id -> activity instants
    for row in rows:
        company_id = row["company_id"]
        if company_id is None:
            summary.no_company += 1
            continue
        party = party_rows.get(row["party_id"])
        party_id = party["id"] if party and party["company_id"] == company_id else None
        is_support = row["request_type"] == "support"
        already_noted = str(row["id"]) in noted
        if is_support and already_noted:
            summary.support += 1  # handled before: nothing more to write
            continue
        quote = quote_rows.get(row["converted_to_quote_id"])
        if quote is not None and quote[1] != company_id:
            quote = None  # another tenant's quote never threads this lead
        if not is_support and quote is not None and quote[0] is None:
            # Its quote has no engagement yet: reconcile_engagements --fix
            # ensures the quote, then the lead attaches to it.
            summary.waiting_on_quote += 1
            continue
        touch = None
        if not already_noted and has_attribution(_Attr(row)):
            touch = Touchpoint(
                company_id=company_id, party_id=party_id, occurred_at=row["created_at"],
                channel=row["source"] if row["source"] in ENGAGEMENT_SOURCES else "",
                utm_source=(row["utm_source"] or "")[:255], utm_medium=(row["utm_medium"] or "")[:255],
                utm_campaign=(row["utm_campaign"] or "")[:255], referrer_url=row["referrer_url"] or "",
                source=LEAD_TOUCH_SOURCE, metadata={"lead": str(row["id"])},
            )
            touches.append(touch)
            if party_id:
                existing_touches[(company_id, party_id)].append((row["created_at"], touch))
        target = None  # an engagement id (existing) or a new Engagement
        if is_support:
            summary.support += 1
        elif quote is not None:
            target = quote[0]
            summary.attached_to_quote += 1
        elif row["status"] in _OPEN_LEAD_STATUSES and (company_id, party_id) in open_by_party \
                and party_id is not None:
            target = open_by_party[(company_id, party_id)]
            summary.attached_repeat += 1
        else:
            pipeline = pipelines.get(company_id)
            if pipeline is None:
                raise RuntimeError(
                    f"Company {company_id} has no default pipeline; run crm migration 0008 "
                    "(seed_default_pipelines) first."
                )
            status, stage_key, loss_reason, closed_at = _lead_plan(row, LOSS_REASONS)
            keys = stages[pipeline.pk]
            stage = keys.get(stage_key) if stage_key else None
            unmapped = None
            if status == "open" and stage is None:
                stage = first_stage.get(pipeline.pk)
                unmapped = row["status"]
            owner = profiles.get(row["assigned_to_id"])
            owner_id = owner.pk if owner is not None and owner.company_id == company_id else None
            value = row["estimated_value"]
            first_touch = last_touch = touch
            if party_id:
                window = sorted(
                    (t for t in existing_touches[(company_id, party_id)]
                     if row["created_at"] - lookback <= t[0] <= row["created_at"]),
                    key=lambda t: t[0],
                )
                if window:
                    first_touch, last_touch = window[0][1], window[-1][1]
            legacy = {k: _json(row[k]) for k in ("status", "loss_reason", "follow_up_date",
                                                  "last_contacted_at", "contact_count")
                      if row[k] not in (None, "", 0)}
            meta = {"backfill": {"step": LEAD_STEP, "lead": str(row["id"]),
                                 "lead_number": row["lead_number"]},
                    "lead_legacy": legacy}
            if unmapped:
                meta["unmapped_lead_status"] = unmapped
            eng = Engagement(
                company_id=company_id, number=_mint_number(apps, company_id, taken),
                title=_lead_title(party["display_name"] if party_id else "", row),
                pipeline_id=pipeline.pk, stage_id=stage.pk if stage else None,
                stage_entered_at=row["created_at"] if stage else None, status=status,
                closed_at=max(closed_at, row["created_at"]) if closed_at else None,
                loss_reason=loss_reason if status == "lost" else "",
                loss_notes=(row["loss_notes"] or "") if status == "lost" else "",
                owner_id=owner_id, party_id=party_id,
                expected_value=value if value is not None and value >= 0 else None,
                source=row["source"] if row["source"] in ENGAGEMENT_SOURCES else "",
                last_activity_at=max(filter(None, (row["updated_at"], row["last_contacted_at"]))),
                origin="backfill", metadata=meta,
            )
            # Resolved after insert (the touches are created in the same batch).
            eng._touches = (first_touch, last_touch)
            engagements.append(eng)
            summary.created[status] += 1
            if party_id is None:
                summary.no_party += 1
            if stage is None:
                changes.append(Change(
                    engagement=eng, company_id=company_id, to_pipeline_id=pipeline.pk,
                    from_status="", to_status=status, changed_at=eng.closed_at,
                    source="backfill", note=f"Backfilled from lead {row['lead_number']} (stage unknown)",
                ))
            else:
                changes.append(Change(
                    engagement=eng, company_id=company_id, to_pipeline_id=pipeline.pk,
                    to_stage_id=stage.pk, from_status="", to_status="open",
                    changed_at=row["created_at"], source="backfill",
                    note=f"Backfilled from lead {row['lead_number']}",
                ))
            if status == "open":
                follow_up = row["follow_up_date"]
                zone = tz.resolve(owner if owner_id else None, companies.get(company_id))
                due_at = end_of_local_day(follow_up, zone) if follow_up else None
                task = Task(
                    company_id=company_id, engagement=eng, party_id=party_id,
                    title=(f"Follow up on {row['lead_number']}" if follow_up
                           else f"Respond to inquiry {row['lead_number']}")[:255],
                    kind="follow_up", status="open", priority="normal", assignee_id=owner_id,
                    due_at=due_at, all_day=True, due_tz=zone.key if due_at else "",
                    source=LEAD_FOLLOW_UP_SOURCE if follow_up else LEAD_TASK_SOURCE,
                    metadata={"lead": str(row["id"])},
                )
                tasks.append(task)
                events.append(TaskEvent(task=task, company_id=company_id, event="created",
                                        to_value=due_at.isoformat()[:64] if due_at else "",
                                        at=row["created_at"]))
                eng._next = (task, due_at)
                if party_id is not None:
                    open_by_party.setdefault((company_id, party_id), eng)
            target = eng
        if target is not None and touch is not None:
            touch._target = target
        if target is not None:
            links.append((row["id"], target))
            if not isinstance(target, Engagement):
                bumps[target].append(row["created_at"])
        if already_noted:
            continue
        comms.append((target, Communication(
            company_id=company_id, kind="note", direction="internal", status="completed",
            subject=f"{_REQUEST_LABELS.get(row['request_type'], 'Inquiry')} {row['lead_number']}"[:255],
            body_text=row["message"] or "", occurred_at=row["created_at"], provider=LEAD_PROVIDER,
            party_id=party_id, is_editable=False,
            metadata={"lead": str(row["id"]), "lead_number": row["lead_number"],
                      "request_type": row["request_type"], "lead_source": row["source"],
                      "lead_direction": "inbound", "backfill": LEAD_STEP},
        )))

    summary.tasks = len(tasks)
    summary.touchpoints = len(touches)
    summary.communications = len(comms)
    if dry_run:
        _log(log, summary)
        return summary

    pending = {}
    for eng in engagements:
        pending[id(eng)] = getattr(eng, "_next", (None, None))
        eng.next_task, eng.next_action_due_at = None, None
    Engagement._base_manager.bulk_create(engagements)
    _stamp_owner_teams(Engagement, UserProfile, engagements)
    Change._base_manager.bulk_create(changes)
    Task._base_manager.bulk_create(tasks)
    TaskEvent._base_manager.bulk_create(events)
    for touch in touches:
        target = getattr(touch, "_target", None)
        touch.engagement_id = target.pk if isinstance(target, Engagement) else target
    Touchpoint._base_manager.bulk_create(touches)
    for eng in engagements:
        task, due_at = pending[id(eng)]
        first, last = eng._touches
        Engagement._base_manager.filter(pk=eng.pk).update(
            next_task_id=task.pk if task else None, next_action_due_at=due_at,
            first_touch_id=first.pk if first is not None and hasattr(first, "pk") else first,
            last_touch_id=last.pk if last is not None and hasattr(last, "pk") else last,
        )
    for target, comm in comms:
        comm.engagement_id = target.pk if isinstance(target, Engagement) else target
    Communication._base_manager.bulk_create([c for _t, c in comms])
    for lead_id, target in links:
        Lead._base_manager.filter(pk=lead_id, engagement__isnull=True).update(
            engagement_id=target.pk if isinstance(target, Engagement) else target
        )
    for eng_id, instants in bumps.items():
        latest = max(instants)
        Engagement._base_manager.filter(pk=eng_id).filter(
            Q(last_activity_at__isnull=True) | Q(last_activity_at__lt=latest)
        ).update(last_activity_at=latest)
    _log(log, summary)
    return summary


_REQUEST_LABELS = {"quote": "Quote Request", "info": "Information", "support": "Support",
                   "partnership": "Partnership", "other": "Other"}


class _Attr:
    """``has_attribution`` reads attributes; a values() row is a dict."""

    def __init__(self, row):
        self.__dict__.update(row)


@suppressed()  # data steps announce nothing (spec 5.1, 7)
def reverse_lead_engagements(apps=None, *, log=None, force=False) -> int:
    """Step 6 (leads) backward: unlink the leads it linked, delete the
    engagements, tasks, touchpoints and message communications it created.

    Refuses (unless ``force``) once people worked on those engagements.
    """
    apps = _registry(apps)
    Lead = apps.get_model("crm", "Lead")
    Engagement = apps.get_model("crm", "Engagement")
    Change = apps.get_model("crm", "EngagementStageChange")
    Task = apps.get_model("crm", "Task")
    TaskEvent = apps.get_model("crm", "TaskEvent")
    Contact = apps.get_model("crm", "EngagementContact")
    Touchpoint = apps.get_model("crm", "Touchpoint")
    Communication = apps.get_model("communications", "Communication")
    Quote = apps.get_model("orders", "Quote")
    Order = apps.get_model("orders", "Order")
    with transaction.atomic():
        ids = list(
            Engagement._base_manager.filter(origin="backfill", metadata__backfill__step=LEAD_STEP)
            .values_list("pk", flat=True)
        )
        mine = Communication._base_manager.filter(provider=LEAD_PROVIDER, metadata__backfill=LEAD_STEP)
        later = (
            Change._base_manager.filter(engagement_id__in=ids).exclude(source="backfill").count()
            + Task._base_manager.filter(engagement_id__in=ids)
            .exclude(source__in=[LEAD_TASK_SOURCE, LEAD_FOLLOW_UP_SOURCE]).count()
            + TaskEvent._base_manager.filter(task__engagement_id__in=ids).exclude(event="created").count()
            + Contact._base_manager.filter(engagement_id__in=ids).count()
            + Quote._base_manager.filter(engagement_id__in=ids).count()
            + Order._base_manager.filter(engagement_id__in=ids).count()
            + Communication._base_manager.filter(engagement_id__in=ids).exclude(pk__in=mine).count()
        )
        if later and not force:
            raise ReverseRefused(
                f"CRM step 6 reverse: {later} history row(s), task(s), contact(s), document(s) or "
                "communication(s) were added to backfilled lead engagements; reversing would "
                "delete or orphan them. Run `manage.py backfill_crm --step 6 --reverse --force` "
                "to discard them anyway."
            )
        lead_ids = [m.get("lead") for m in mine.values_list("metadata", flat=True)]
        unlinked = Lead._base_manager.filter(pk__in=[i for i in lead_ids if i]).update(engagement_id=None)
        unlinked += Lead._base_manager.filter(engagement_id__in=ids).update(engagement_id=None)
        mine.delete()
        Touchpoint._base_manager.filter(source=LEAD_TOUCH_SOURCE).delete()
        Engagement._base_manager.filter(pk__in=ids).update(
            next_task_id=None, next_action_due_at=None, first_touch_id=None, last_touch_id=None)
        Quote._base_manager.filter(engagement_id__in=ids).update(engagement_id=None)
        Order._base_manager.filter(engagement_id__in=ids).update(engagement_id=None)
        Communication._base_manager.filter(engagement_id__in=ids).update(engagement_id=None)
        task_ids = list(Task._base_manager.filter(engagement_id__in=ids).values_list("pk", flat=True))
        TaskEvent._base_manager.filter(task_id__in=task_ids).delete()
        Task._base_manager.filter(pk__in=task_ids).delete()
        Contact._base_manager.filter(engagement_id__in=ids).delete()
        Change._base_manager.filter(engagement_id__in=ids).delete()
        Engagement._base_manager.filter(pk__in=ids).delete()
    if log:
        log(f"lead engagements removed: {len(ids)}; leads unlinked: {unlinked}")
    return len(ids)


# --- Phase 3 step 6: ActivityLog onto the thread (spec 2.4, E8) -------------------
#
# ``backfill_activity_communications`` (also ``backfill_crm --step 6``), after the
# lead step so ``lead.engagement`` is set. Each ActivityLog row becomes, with
# **the same id**:
#
# - ``call`` / ``email`` / ``meeting`` / ``note``: a ``Communication`` of that kind,
#   ``direction=outbound`` (``internal`` for a note), ``status=completed``,
#   ``body_text`` = description, ``occurred_at`` = activity_date,
#   ``duration_seconds`` = minutes x 60 (calls and meetings; the kinds registry
#   gives the others no duration), ``provider="manual"``;
# - ``task``: a done ``Task`` (``completed_at`` = activity_date, assignee and
#   completed_by = performer, ``source="backfill:activitylog"``) with one
#   ``completed`` event. A log row records something done: never an open task.
#
# Links: a lead's row takes ``lead.engagement`` and ``lead.party``; a customer's
# takes ``customer.party`` and, when that party has exactly one open
# engagement, it. A link into another tenant is dropped. ``metadata`` is the
# row's plus ``from_activitylog: true`` and ``activitylog`` (the lead,
# customer and raw duration the compat endpoint serves back); created_at /
# updated_at are kept, so the compat JSON is the pre-migration JSON. Linked
# engagements' ``last_activity_at`` moves up to the latest activity. Rows with
# no company are skipped (reported). Idempotent: an id already present is skipped.

ACTIVITY_TASK_SOURCE = "backfill:activitylog"
ACTIVITY_COMM_KINDS = ("call", "email", "meeting", "note")
#: ``metadata`` keys the mirror adds; the compat endpoint strips them.
ACTIVITY_MARKER = "from_activitylog"
ACTIVITY_LINKS = "activitylog"
_HAS_DURATION = ("call", "meeting")


@dataclass
class ActivityBackfillSummary:
    rows: int = 0
    already: int = 0
    communications: Counter = field(default_factory=Counter)  # by kind
    tasks: int = 0
    no_company: int = 0
    on_engagement: int = 0
    dropped_cross_tenant: int = 0
    dry_run: bool = False

    def lines(self) -> list[str]:
        by_kind = ", ".join(f"{k} {v}" for k, v in sorted(self.communications.items())) or "none"
        return [
            f"activity rows: {self.rows}; already mirrored: {self.already}"
            f"{' [dry run]' if self.dry_run else ''}",
            f"communications: {sum(self.communications.values())} ({by_kind}); done tasks: {self.tasks}",
            f"on an engagement: {self.on_engagement}; no company (skipped): {self.no_company}; "
            f"cross-tenant links dropped: {self.dropped_cross_tenant}",
        ]


def activity_metadata(metadata, *, lead_id, customer_id, duration_minutes):
    """The mirrored row's ``metadata``: the original plus the compat keys."""
    links = {"lead": str(lead_id) if lead_id else None,
             "customer": str(customer_id) if customer_id else None,
             "duration_minutes": duration_minutes}
    if isinstance(metadata, dict):
        out = dict(metadata)
    else:
        out = {}
        links["metadata"] = metadata
    out[ACTIVITY_MARKER] = True
    out[ACTIVITY_LINKS] = links
    return out


@suppressed()  # data steps announce nothing (spec 5.1, 7)
def backfill_activity_communications(apps=None, *, dry_run=False, log=None) -> ActivityBackfillSummary:
    with transaction.atomic():
        return _backfill_activity(apps, dry_run=dry_run, log=log)


def _backfill_activity(apps, *, dry_run, log):
    apps = _registry(apps)
    ActivityLog = apps.get_model("crm", "ActivityLog")
    Lead = apps.get_model("crm", "Lead")
    Customer = apps.get_model("customers", "Customer")
    UserProfile = apps.get_model("core", "UserProfile")
    Party = apps.get_model("parties", "Party")
    Engagement = apps.get_model("crm", "Engagement")
    Task = apps.get_model("crm", "Task")
    TaskEvent = apps.get_model("crm", "TaskEvent")
    Communication = apps.get_model("communications", "Communication")

    summary = ActivityBackfillSummary(dry_run=dry_run)
    rows = list(ActivityLog._base_manager.order_by("activity_date", "pk").values(
        "id", "company_id", "lead_id", "customer_id", "activity_type", "subject", "description",
        "performed_by_id", "activity_date", "duration_minutes", "metadata", "created_at", "updated_at",
    ))
    summary.rows = len(rows)
    ids = [r["id"] for r in rows]
    done = set(Communication._base_manager.filter(pk__in=ids).values_list("pk", flat=True))
    done |= set(Task._base_manager.filter(pk__in=ids).values_list("pk", flat=True))
    summary.already = len(done)
    rows = [r for r in rows if r["id"] not in done]
    if not rows:
        _log(log, summary)
        return summary

    leads = {r["id"]: r for r in Lead._base_manager.filter(
        pk__in={r["lead_id"] for r in rows if r["lead_id"]}).values("id", "engagement_id", "party_id")}
    customers = dict(Customer._base_manager.filter(
        pk__in={r["customer_id"] for r in rows if r["customer_id"]}).values_list("pk", "party_id"))
    party_company = dict(Party._base_manager.filter(
        pk__in={p for p in [*customers.values(), *(lead["party_id"] for lead in leads.values())] if p}
    ).values_list("pk", "company_id"))
    eng_company = dict(Engagement._base_manager.filter(
        pk__in={lead["engagement_id"] for lead in leads.values() if lead["engagement_id"]}
    ).values_list("pk", "company_id"))
    open_engs = defaultdict(list)
    for pk, party_id, company_id in Engagement._base_manager.filter(
            status="open", party_id__in=set(customers.values()) - {None}
    ).values_list("pk", "party_id", "company_id"):
        open_engs[party_id].append((pk, company_id))
    profile_company = dict(UserProfile._base_manager.filter(
        pk__in={r["performed_by_id"] for r in rows if r["performed_by_id"]}).values_list("pk", "company_id"))

    comms, tasks, events, stamps = [], [], [], []
    bumps = defaultdict(list)
    for row in rows:
        company_id = row["company_id"]
        if company_id is None:
            summary.no_company += 1
            continue
        engagement_id = party_id = None
        lead = leads.get(row["lead_id"])
        if lead is not None:
            engagement_id, party_id = lead["engagement_id"], lead["party_id"]
        elif row["customer_id"]:
            party_id = customers.get(row["customer_id"])
            only = [pk for pk, co in open_engs.get(party_id, ()) if co == company_id]
            engagement_id = only[0] if len(only) == 1 else None
        if engagement_id and eng_company.get(engagement_id, company_id) != company_id:
            engagement_id = None
            summary.dropped_cross_tenant += 1
        if party_id and party_company.get(party_id) != company_id:
            party_id = None
            summary.dropped_cross_tenant += 1
        performer = row["performed_by_id"]
        if performer and profile_company.get(performer) != company_id:
            performer = None
        minutes = row["duration_minutes"]
        metadata = activity_metadata(row["metadata"], lead_id=row["lead_id"],
                                     customer_id=row["customer_id"], duration_minutes=minutes)
        kind = row["activity_type"]
        if engagement_id:
            summary.on_engagement += 1
            bumps[engagement_id].append(row["activity_date"])
        if kind == "task":
            task = Task(
                id=row["id"], company_id=company_id, title=(row["subject"] or "")[:255] or "Task",
                description=row["description"] or "", kind="todo", status="done",
                priority="normal", assignee_id=performer, completed_by_id=performer,
                completed_at=row["activity_date"], engagement_id=engagement_id, party_id=party_id,
                due_at=None, all_day=True, due_tz="", source=ACTIVITY_TASK_SOURCE, metadata=metadata,
            )
            tasks.append(task)
            events.append(TaskEvent(task=task, company_id=company_id, event="completed",
                                    from_value="open", to_value="done", at=row["activity_date"],
                                    by_id=performer))
            summary.tasks += 1
            stamps.append((Task, row))
            continue
        if kind not in ACTIVITY_COMM_KINDS:
            kind = "note"
        seconds = minutes * 60 if kind in _HAS_DURATION and minutes is not None and minutes >= 0 else None
        comms.append(Communication(
            id=row["id"], company_id=company_id, kind=kind,
            direction="internal" if kind == "note" else "outbound", status="completed",
            subject=(row["subject"] or "")[:255], body_text=row["description"] or "",
            occurred_at=row["activity_date"], duration_seconds=seconds, provider="manual",
            engagement_id=engagement_id, party_id=party_id, performed_by_id=performer,
            is_editable=True, metadata=metadata,
        ))
        summary.communications[kind] += 1
        stamps.append((Communication, row))

    if dry_run:
        _log(log, summary)
        return summary
    Communication._base_manager.bulk_create(comms)
    Task._base_manager.bulk_create(tasks)
    TaskEvent._base_manager.bulk_create(events)
    for model, row in stamps:  # bulk_create stamps "now"; keep the original times
        model._base_manager.filter(pk=row["id"]).update(created_at=row["created_at"],
                                                         updated_at=row["updated_at"])
    for eng_id, instants in bumps.items():
        latest = max(instants)
        Engagement._base_manager.filter(pk=eng_id).filter(
            Q(last_activity_at__isnull=True) | Q(last_activity_at__lt=latest)
        ).update(last_activity_at=latest)
    _log(log, summary)
    return summary


@suppressed()  # data steps announce nothing (spec 5.1, 7)
def reverse_activity_communications(apps=None, *, log=None) -> int:
    """Step 6 (ActivityLog) backward: delete the mirrors (same ids). Rows the
    compat endpoint created since have new ids and stay."""
    apps = _registry(apps)
    ActivityLog = apps.get_model("crm", "ActivityLog")
    Task = apps.get_model("crm", "Task")
    TaskEvent = apps.get_model("crm", "TaskEvent")
    Communication = apps.get_model("communications", "Communication")
    with transaction.atomic():
        ids = list(ActivityLog._base_manager.values_list("pk", flat=True))
        _total, per_model = Communication._base_manager.filter(
            pk__in=ids, metadata__from_activitylog=True).delete()
        removed = per_model.get(Communication._meta.label, 0)
        task_ids = list(Task._base_manager.filter(pk__in=ids, source=ACTIVITY_TASK_SOURCE)
                        .values_list("pk", flat=True))
        TaskEvent._base_manager.filter(task_id__in=task_ids).delete()
        Task._base_manager.filter(pk__in=task_ids).delete()
    if log:
        log(f"activity mirrors removed: {removed} communication(s), {len(task_ids)} task(s)")
    return len(task_ids)
