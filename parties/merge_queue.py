"""The party merge queue on ``unify`` (docs/crm/phase2-party.md 3.4, D12).

One dedupe scan is an ``ImportBatch`` (``source_kind="dedupe"``,
``entity_kind="party_merge"``); each candidate cluster is a ``StagedRow``:

- ``raw`` = ``{"party_ids": [...], "evidence": [{"rule", "value"}], "flags":
  [...], "blocked": [...], "rule", "weakest_rule", "bulk_approvable"}``.
  Evidence values are masked (``j…@example.com``): the screen shows the live
  contact points, so the queue never holds a second copy of the PII.
- ``content_hash`` = sha256 of the sorted party ids; ``mapping_confidence``
  = the cluster's strongest rule.
- ``mapping_state``: ``proposed`` → ``approved`` (``mapped_values = {"survivor":
  id, "field_picks": {...}}``) or ``na`` ("not a duplicate").

Re-proposal suppression: a scan skips any cluster whose hash already has a
row in ``na`` or ``applied`` for the company (and one still queued, in
``proposed`` / ``approved``, so a second scan does not double the queue).
A queued, unapproved row whose cluster a new scan no longer finds is marked
``failed`` ("stale"), which does not suppress it if it reappears.

Approving goes through ``approve`` (never ``unify.propose.approve_rows``,
which only knows product fields): a blocked cluster cannot be approved, and
``bulk_approve`` takes only bulk-approvable rows in the ≥0.95 band. Approved
rows apply through ``unify.apply`` (the screen, or ``manage.py unify_apply``)
with the ``party_merge`` applier registered below.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from .dedupe import find_clusters
from .merge import MergeError, merge, merge_blocks, unmerge_group

ENTITY_KIND = "party_merge"
SCAN_ACTOR = "system:party_dedupe"
BULK_THRESHOLD = Decimal("0.950")
REF_PREFIX = "party_merge:"


def mask(rule: str, value: str) -> str:
    """Evidence for display without the full value (3.4: "j…@example.com")."""
    if rule == "R1" and "@" in value:
        local, _, domain = value.rpartition("@")
        return f"{local[:1]}…@{domain}"
    if rule in ("R2", "R3"):
        number = value.split("|", 1)[0]
        return f"…{number[-4:]}"
    if rule == "R4":
        return "same name and postal code"
    if rule == "C1":
        return "converted lead"
    return value  # R5 org names, R6 domains: business facts, not personal data


def cluster_hash(party_ids) -> str:
    from unify.models import content_hash

    return content_hash({"party_ids": sorted(str(p) for p in party_ids)})


def _party_id(node_key: str) -> str:
    return node_key.split(":", 1)[1]


def default_survivors(company, party_ids) -> dict:
    """Rank key per party: account holder, then login, then most orders, then oldest (6.3)."""
    from customers.models import Customer

    from .models import Party

    ids = list(party_ids)
    holders = set(Customer.objects.filter(company=company, account_holder_id__in=ids)
                  .values_list("account_holder_id", flat=True))
    orders: dict = {}
    for party_id, holder_id, n in (
        Customer.objects.filter(company=company)
        .filter(Q(party_id__in=ids) | Q(account_holder_id__in=ids))
        .annotate(n=Count("orders")).values_list("party_id", "account_holder_id", "n")
    ):
        credit = holder_id if holder_id in set(ids) else party_id
        orders[credit] = orders.get(credit, 0) + n
    rank = {}
    for pk, user_id, created in Party.objects.filter(pk__in=ids).values_list("pk", "user_id", "created_at"):
        rank[pk] = (pk not in holders, user_id is None, -orders.get(pk, 0), created, str(pk))
    return rank


def pick_survivor(rank: dict, party_ids):
    known = [p for p in party_ids if p in rank]
    return min(known, key=lambda p: rank[p]) if known else None


# --- scan ----------------------------------------------------------------------


def scan(company, *, actor: str = SCAN_ACTOR, dry_run: bool = False) -> dict:
    """Run the dedupe rules over the company's live parties and queue new clusters."""
    import uuid

    from unify.models import ImportBatch, StagedRow

    from .dedupe_parties import party_nodes

    nodes, _stats = party_nodes(company)
    clusters = find_clusters(nodes)
    queued = StagedRow.objects.filter(batch__company=company, batch__entity_kind=ENTITY_KIND)
    suppress = set(queued.filter(mapping_state__in=(
        StagedRow.MappingState.NA, StagedRow.MappingState.APPLIED,
        StagedRow.MappingState.PROPOSED, StagedRow.MappingState.APPROVED,
    )).values_list("content_hash", flat=True))

    fresh, found = [], set()
    for c in clusters:
        ids = [uuid.UUID(_party_id(k)) for k in c.members]
        digest = cluster_hash(ids)
        found.add(digest)
        if digest not in suppress:
            fresh.append((c, ids, digest))
    out = {"clusters": len(clusters), "new": len(fresh), "suppressed": len(clusters) - len(fresh),
           "stale": 0, "batch": None}
    if dry_run:
        return out

    with transaction.atomic():
        stale = queued.filter(mapping_state=StagedRow.MappingState.PROPOSED).exclude(content_hash__in=found)
        out["stale"] = stale.update(
            mapping_state=StagedRow.MappingState.FAILED,
            error="stale: a later scan no longer finds this cluster",
            mapping_actor=actor[:255], updated_at=timezone.now(),
        )
        batch = ImportBatch.objects.create(
            company=company, source_kind=ImportBatch.SourceKind.DEDUPE, entity_kind=ENTITY_KIND,
            source_name=f"Party duplicate scan {timezone.now():%Y-%m-%d %H:%M}",
            status=ImportBatch.Status.REVIEWING, llm_call_budget=0,
        )
        rank = default_survivors(company, [p for _, ids, _ in fresh for p in ids])
        rows = []
        for number, (c, ids, digest) in enumerate(fresh, start=1):
            survivor = pick_survivor(rank, ids)
            rows.append(StagedRow(
                batch=batch, row_number=number, content_hash=digest,
                raw={
                    "party_ids": [str(p) for p in ids],
                    "kind": c.kind,
                    "evidence": [{"rule": e.rule, "value": mask(e.rule, e.value)} for e in c.evidence],
                    "flags": list(c.flags),
                    "needs_review": bool(c.flags),
                    "blocked": list(c.blocked),
                    "rule": c.rule,
                    "weakest_rule": c.weakest_rule,
                    "bulk_approvable": c.bulk_approvable,
                },
                mapping_state=StagedRow.MappingState.PROPOSED,
                proposed_mapping={"survivor": str(survivor) if survivor else ""},
                mapping_confidence=Decimal(str(c.confidence)).quantize(Decimal("0.001")),
                mapping_actor=actor[:255],
            ))
        StagedRow.objects.bulk_create(rows, batch_size=1000)
    out["batch"] = batch
    return out


# --- decisions -------------------------------------------------------------------


def _row_parties(staged) -> list:
    import uuid

    return [uuid.UUID(p) for p in (staged.raw or {}).get("party_ids", [])]


def approvable(staged) -> list[str]:
    """Why this row cannot be approved ([] when it can): the scan's blocks plus live ones."""
    reasons = list((staged.raw or {}).get("blocked") or [])
    for reason in merge_blocks(_row_parties(staged)):
        if reason not in reasons:
            reasons.append(reason)
    return reasons


def approve(staged, *, actor: str, survivor=None, field_picks: dict | None = None):
    from unify.models import StagedRow

    from .merge import BLOCK_MESSAGES

    if not actor:
        raise MergeError("Approval always names its decider: actor is required.")
    if staged.batch.entity_kind != ENTITY_KIND:
        raise MergeError("Not a party merge candidate.")
    if staged.mapping_state not in (StagedRow.MappingState.PROPOSED, StagedRow.MappingState.APPROVED):
        raise MergeError(f"This candidate is {staged.get_mapping_state_display().lower()}; nothing to approve.")
    blocked = approvable(staged)
    if blocked:
        raise MergeError("Blocked: " + " ".join(BLOCK_MESSAGES.get(b, b) for b in blocked))
    ids = [str(p) for p in _row_parties(staged)]
    survivor = str(survivor or (staged.proposed_mapping or {}).get("survivor") or "")
    if survivor not in ids:
        raise MergeError("Pick the surviving party from this cluster.")
    picks = {k: str(v) for k, v in (field_picks or {}).items() if v}
    staged.mapped_values = {"survivor": survivor, "field_picks": picks}
    staged.mapping_state = StagedRow.MappingState.APPROVED
    staged.mapping_actor = actor[:255]
    staged.error = ""
    staged.save(update_fields=["mapped_values", "mapping_state", "mapping_actor", "error", "updated_at"])
    return staged


def dismiss(staged, *, actor: str):
    """"Not a duplicate": remembered by hash, so the cluster is never re-proposed."""
    from unify.models import StagedRow

    if staged.mapping_state not in (StagedRow.MappingState.PROPOSED, StagedRow.MappingState.APPROVED,
                                    StagedRow.MappingState.FAILED):
        raise MergeError("Only a queued candidate can be dismissed.")
    staged.mapping_state = StagedRow.MappingState.NA
    staged.mapped_values = {}
    staged.mapping_actor = actor[:255]
    staged.save(update_fields=["mapping_state", "mapped_values", "mapping_actor", "updated_at"])
    return staged


def is_bulk_approvable(staged) -> bool:
    raw = staged.raw or {}
    return bool(raw.get("bulk_approvable")) and not raw.get("blocked") and not raw.get("flags") \
        and (staged.mapping_confidence or 0) >= BULK_THRESHOLD


def bulk_approve(rows, *, actor: str) -> dict:
    """Approve the bulk-approvable rows among ``rows``; others are skipped, never approved."""
    out = {"approved": 0, "skipped": 0}
    for staged in rows:
        if not is_bulk_approvable(staged):
            out["skipped"] += 1
            continue
        try:
            approve(staged, actor=actor)
        except MergeError:
            out["skipped"] += 1
            continue
        out["approved"] += 1
    return out


# --- the unify applier -------------------------------------------------------------


def apply_merge(company, staged, *, run=None, approval_row=None, **_context) -> str:
    from .models import Party

    mapped = staged.mapped_values or {}
    ids = _row_parties(staged)
    survivor_id = mapped.get("survivor")
    if not survivor_id or survivor_id not in {str(p) for p in ids}:
        raise MergeError("Approved without a survivor from the cluster.")
    if Party.objects.filter(pk__in=ids).exclude(company=company).exists():
        raise MergeError("A party of this cluster belongs to another company.")
    losers = [p for p in ids if str(p) != survivor_id]
    logs = merge(survivor_id, losers, actor=(run.actor if run else staged.mapping_actor) or SCAN_ACTOR,
                 field_picks=mapped.get("field_picks") or {}, approval_row=approval_row)
    return f"{REF_PREFIX}{logs[0].merge_id}"


def undo_merge(approval_row) -> int:
    ref = approval_row.staged_row.applied_ref
    if not ref.startswith(REF_PREFIX):
        return 0
    return len(unmerge_group(ref[len(REF_PREFIX):], actor=f"undo-run:{approval_row.run_id}"))


def register():
    from unify.appliers import register_applier

    register_applier(ENTITY_KIND, apply_merge, undo_merge, permission="parties.merge_party",
                     undo_newest_first=True, undo_state="proposed")
