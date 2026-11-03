"""Tickets → partner relationships (docs/crm/phase5-importer.md 2.8, 6.3).

In the showcase portal tickets are the partners who refer work, not support
cases. Each ticket becomes a ``PartyRelationship`` from the partner (the
ticket's primary contact by the export's association flags, else its primary
or first company, else its first contact) to the tenant's ``is_self`` party:

- ``kind`` from the mapping file (``[tickets] relationship_kind``), default
  the built-in ``partnership`` (symmetric: stored in canonical order, the
  smaller party id first);
- ``valid_from`` = the ticket's open date (the tenant's zone); ``valid_to``
  stays NULL unless the ticket's stage key is one of ``[tickets]
  ended_stages`` (then its close date): a closed onboarding ticket usually
  means the partnership is active;
- ``source="import:<provider>"``; the ticket's pipeline and stage key and its
  subject in the relationship's sidecar (``<provider>.import_ticket_*``) with
  its source properties; the full ticket facts (description and stage
  history too) on the ticket's ExternalRef (``metadata["source_ticket"]``), so a
  second ticket for the same partner, which links to the open relationship
  rather than duplicating it, loses nothing either.

**Engagement only when opted in.** With the job option
``tickets_as_engagements`` the ticket *also* becomes an Engagement in the
pipeline imported from its ticket pipeline, with the same party, through the
deal applier's insert: an open ticket is open; a closed one takes its
stage's won/lost hint, and a closed ticket in an unhinted stage is
``outcome_conflict`` for a reviewer; history; created_at; no amount; the
mapping file's ``default_currency``, which staging then requires. Its
ExternalRef is ``object_type="ticket_engagement"``. Default off: no
engagement.
"""

from __future__ import annotations

from core import tz

from . import deals
from .base import DEPENDENCY_MISSING, ERASED, INVALID_VALUE, Row, RowError, applied_ref

DEFAULT_KIND = "partnership"
ENGAGEMENT_TYPE = "ticket_engagement"


def _staged_pipeline(row: Row, pipeline_sid) -> dict:
    """The ticket pipeline's canonical record from this job (ticket pipelines
    are not written unless tickets become engagements)."""
    from crm_import.models import ImportJobBatch
    from unify.models import StagedRow

    batch_ids = ImportJobBatch._base_manager.filter(job_id=row.job.pk, object_type="pipeline").values("batch_id")
    raw = (StagedRow.objects.filter(batch_id__in=batch_ids, raw__source_id=str(pipeline_sid))
           .values_list("raw", flat=True).first())
    return raw or {}


def _keys(row: Row) -> tuple[str, str]:
    """``(pipeline key, stage key)`` of the ticket, from the export."""
    pipeline_sid = (row.data.get("pipeline_ref") or {}).get("source_id")
    stage_sid = str((row.data.get("stage_ref") or {}).get("source_id"))
    pipeline = (_staged_pipeline(row, pipeline_sid).get("data") or {})
    stage = next((s for s in pipeline.get("stages") or [] if str(s.get("source_id")) == stage_sid), {})
    return pipeline.get("key") or "", stage.get("key") or ""


def _partner_refs(row: Row) -> list[dict]:
    """The partner candidates in 2.8's order: the primary contact (by the
    job's association flags), else the company (the primary one, else the
    first), else the first contact."""
    contacts = [r for r in row.data.get("contact_refs") or [] if r]
    companies = [r for r in row.data.get("company_refs") or [] if r]
    primary = deals.primary_associations(row.job, "relationship", row.record.get("source_id"),
                                         run_id=row.approval_row.run_id)
    by_sid = {str(r.get("source_id")): r for r in contacts}
    ordered = [by_sid[primary["contact"]]] if primary.get("contact") in by_sid else []
    company_sids = {str(r.get("source_id")): r for r in companies}
    if primary.get("company") in company_sids:
        ordered.append(company_sids[primary["company"]])
    ordered += [r for r in companies if r not in ordered] + [r for r in contacts if r not in ordered]
    return ordered[:1]


def _partner(row: Row):
    from parties.models import Party

    refs = _partner_refs(row)
    for ref in refs:
        party, _absent = deals.resolve_optional(row, ref, Party)
        party = deals.live_party(party)
        if party is not None:
            return party
    if refs and all(deals.is_erased_ref(row, ref) for ref in refs):
        raise RowError(ERASED, "the ticket's partner has been erased; it is not imported.")
    if refs:
        raise RowError(DEPENDENCY_MISSING, "the ticket's partner was not imported (or was erased); "
                                           "apply it, then retry this row.")
    raise RowError(INVALID_VALUE, "a ticket with no contact and no company names no partner.")


def _self_party(row: Row):
    from parties.models import Party

    me = Party._base_manager.filter(company_id=row.company.pk, is_self=True).first()
    if me is None:
        raise RowError(INVALID_VALUE, "the tenant has no self party (run the parties backfill).")
    return me


def _local_date(value, company):
    instant = deals.parse_instant(value)
    if instant is None:
        return None
    return instant.astimezone(tz.resolve(None, company)).date()


def _ticket_facts(row: Row, pipeline_key: str, stage_key: str) -> dict:
    data = row.data
    return {
        "pipeline": pipeline_key, "stage": stage_key, "state": data.get("state"),
        "subject": data.get("subject") or "", "description": data.get("description") or "",
        "opened_at": data.get("opened_at"), "closed_at": data.get("closed_at"),
        "history": [h for h in row.record.get("history") or []
                    if h.get("property") in (*deals.STAGE_PROPS["relationship"],
                                             *deals.PIPELINE_PROPS["relationship"])],
    }


def ticket_targets(row: Row) -> dict:
    """The mapping file's ``[ticket.properties]`` (its tables use the provider's
    object names: a ticket, not a relationship)."""
    section = row.mapping.get(row.source_object_type) or row.mapping.get("ticket") or {}
    return dict(section.get("properties") or {})


def _relationship(row: Row, partner, me, kind: str, stage_key: str):
    from parties.models import PartyRelationship
    from parties.relationship_registry import get_relationship_kind

    try:
        config = get_relationship_kind(kind)
    except KeyError:
        raise RowError(INVALID_VALUE, f"{kind!r} is not a relationship kind (GOLDEN_RELATIONSHIP_KINDS).") from None
    a, b = partner, me
    if config.symmetric and str(a.pk) > str(b.pk):
        a, b = b, a
    if not config.allows(a.kind, b.kind):
        raise RowError(INVALID_VALUE, f"{kind!r} cannot link a {a.kind} to a {b.kind}.")
    tickets = row.mapping.get("tickets") or {}
    ended = stage_key in (tickets.get("ended_stages") or [])
    valid_from = _local_date(row.data.get("opened_at") or row.record.get("created_at"), row.company)
    valid_to = _local_date(row.data.get("closed_at"), row.company) if ended else None
    if valid_to is not None and valid_from is not None and valid_to < valid_from:
        valid_to = valid_from
    if valid_to is None:
        existing = PartyRelationship._base_manager.filter(
            from_party_id=a.pk, to_party_id=b.pk, kind=kind, valid_to__isnull=True).first()
        if existing is not None:
            return existing, False
    rel = PartyRelationship(company_id=row.company.pk, from_party=a, to_party=b, kind=kind,
                            valid_from=valid_from, valid_to=valid_to, source=row.source_tag,
                            is_symmetric=config.symmetric)
    rel.full_clean()
    rel.save()
    row.session.create(rel)
    return rel, True


def _engagement(row: Row, party):
    """``tickets_as_engagements``: the ticket as an engagement too (2.8)."""
    data = row.data
    status = deals.OPEN if data.get("state") == "open" else deals.CLOSED  # closed: the stage's hint decides
    plan = deals.plan_engagement(row, status=status, closed_at=data.get("closed_at"))
    owner, owner_flags = deals.owner_of(row)
    currency = row.mapping.get("default_currency")
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isalpha():
        # The column is NOT NULL and golden never assumes a currency (hand-off 7);
        # staging refuses the job first (stage.check_deals).
        raise RowError(INVALID_VALUE, "tickets as engagements need the mapping file's default_currency.")
    fields = {"title": (data.get("subject") or "")[:255], "expected_value": None, "currency": currency.upper()}
    if plan.status == deals.LOST:
        fields.update({"loss_reason": "unknown", "loss_notes": ""})
    eng = deals.insert_engagement(row, plan, party=party, owner=owner, fields=fields,
                                  derived={"flags": sorted(set(plan.flags + owner_flags))},
                                  targets=ticket_targets(row))
    for key in row.keys:
        row.ref_for(eng, key, object_type=ENGAGEMENT_TYPE)
    return eng


def _update(row: Row) -> str:
    """Re-import: the ticket's facts on its refs follow the source; the
    relationship itself (who, kind, dates) is staff's from the first import
    on, and is never re-derived or ended by a refresh (3.4)."""
    from core.models import ExternalRef
    from parties.models import PartyRelationship

    rel = row.target(PartyRelationship)
    pipeline_key, stage_key = _keys(row)
    meta = {"source_ticket": _ticket_facts(row, pipeline_key, stage_key)}
    for ref in ExternalRef._base_manager.filter(company_id=row.company.pk, source=row.job.source,
                                                object_type=row.source_object_type, source_id__in=row.keys):
        merged = {**(ref.metadata or {}), **meta}
        if merged != ref.metadata:
            row.session.update(ref, metadata=merged)
    row.refs_for(rel)
    return applied_ref(PartyRelationship, rel.pk)


def apply(row: Row) -> str:
    from parties.models import PartyRelationship

    if row.decision not in ("create", "update"):
        raise RowError(INVALID_VALUE, f"a ticket row cannot be applied as {row.decision!r}.")
    if row.decision == "update":
        return _update(row)
    from core.identity.resolve import lock_login_rows

    partner = _partner(row)
    me = _self_party(row)
    lock_login_rows(parties=[partner.pk, me.pk])
    kind = (row.mapping.get("tickets") or {}).get("relationship_kind") or DEFAULT_KIND
    pipeline_key, stage_key = _keys(row)
    rel, created = _relationship(row, partner, me, kind, stage_key)
    facts = _ticket_facts(row, pipeline_key, stage_key)
    if created:
        deals.write_sidecar(row, rel, {"ticket_pipeline": pipeline_key, "ticket_stage": stage_key,
                                       "ticket_subject": facts["subject"][:2000]}, targets=ticket_targets(row))
    meta = {"source_ticket": facts}
    if not created and row.properties:
        meta["source_properties"] = dict(row.properties)  # the relationship's sidecar holds the first ticket's
    for key in row.keys:
        row.ref_for(rel, key, metadata=meta)
    if (row.job.options or {}).get("tickets_as_engagements"):
        _engagement(row, partner)
    return applied_ref(PartyRelationship, rel.pk)


__all__ = ["apply"]
