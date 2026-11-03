"""Lead intake: a new lead onto the engagement thread (docs/crm/phase3-engagement.md 1.7).

A lead is the intake record (what came in, from where, when) and never the
place work happens. ``accept_lead`` runs from ``Lead.save()`` on create,
after Phase 2's party sync (``parties.sync.sync_from_lead``), in the same
transaction:

1. A ``Touchpoint`` from the lead's attribution fields (UTM tags, referrer,
   and the landing URL, click id and visitor id the lead API stores in
   ``lead.metadata``), when it carries any, written through
   ``crm.touchpoints.record`` (1.8; Phase 6 3.5; the IP address stays on
   the lead). The visitor's earlier party-less touches are linked to the
   lead's party first, so they count for first and last touch.
2. ``request_type == "support"``: no engagement. The message lands on the
   party's timeline as a Communication and nowhere else.
3. The party already has an open engagement in the default pipeline (as its
   counterparty or a contact): the lead attaches to it, the message is
   logged on it, and its ``last_activity_at`` moves. No second engagement.
4. Otherwise a new engagement at stage ``new``: ``origin="lead"``, owner =
   ``lead.assigned_to`` or ``GOLDEN_CRM["lead_default_owner"]``,
   ``expected_value``, ``source``, first and last touch, and one open Task
   "Respond to inquiry" due ``GOLDEN_CRM["lead_response_hours"]`` after
   intake, so no engagement is born without a next action. A lead whose
   party is unresolved (match-N) still gets one, with ``party=NULL``.

Every other request type (quote, info, partnership, other, blank) takes
steps 3-4. The message is a ``note`` Communication (``direction=internal``,
``provider="lead_form"``, read-only): the ``note`` kind is internal-only in
the kinds registry, so ``metadata.lead_direction`` records that the lead
itself came in. A failure is logged and leaves the lead without an
engagement; ``reconcile_engagements`` reports it and ``--fix`` repairs it,
like Phase 2's party sync (``GOLDEN_CRM_INTAKE_STRICT`` raises instead).
"""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from .conf import crm_setting, intake_strict

logger = logging.getLogger(__name__)

SUPPORT = "support"
#: ``Task.source`` of the intake task, ``Touchpoint.source`` of the intake touch.
TASK_SOURCE = "intake:lead"
TOUCH_SOURCE = "lead_form"
#: ``Communication.provider`` of a lead's message.
PROVIDER = "lead_form"
NEW_STAGE = "new"


def wants_engagement(request_type) -> bool:
    """Support requests never open or join a deal (spec 1.7, section 7)."""
    return (request_type or "") != SUPPORT


@dataclass(frozen=True)
class IntakeResult:
    """``outcome``: ``created`` / ``attached`` / ``support`` / ``skipped``."""

    outcome: str
    engagement: object = None
    detail: str = ""


#: ``Lead.metadata`` keys the lead API writes at create (registered in
#: core.sidecar; ``crm.serializers.LeadDetailSerializer``).
LEAD_ATTRIBUTION_KEYS = ("landing_url", "click_id_kind", "click_id", "utm_term", "utm_content",
                         "visitor_id")


def _lead_attribution(lead) -> dict:
    meta = getattr(lead, "metadata", None)
    meta = meta if isinstance(meta, dict) else {}
    return {key: str(meta.get(key) or "") for key in LEAD_ATTRIBUTION_KEYS}


def has_attribution(lead) -> bool:
    extra = _lead_attribution(lead)
    return any((lead.utm_source, lead.utm_medium, lead.utm_campaign, lead.referrer_url,
                extra["landing_url"], extra["click_id"]))


def _touchpoint(lead, party, *, source=TOUCH_SOURCE):
    from django.core.exceptions import ValidationError

    from .registries import ENGAGEMENT_SOURCES
    from .touchpoints import link_visitor, record

    extra = _lead_attribution(lead)
    if party is not None and extra["visitor_id"]:
        try:
            with transaction.atomic():
                link_visitor(lead.company_id, extra["visitor_id"], party)
        except Exception:  # noqa: BLE001 - linking old visits must never lose the lead
            logger.warning("lead %s: visitor link failed; earlier visits stay party-less", lead.pk)
    if not has_attribution(lead):
        return None
    common = dict(
        party=party, occurred_at=lead.created_at, source=source,
        channel=lead.source if lead.source in ENGAGEMENT_SOURCES else "",
        utm={"source": lead.utm_source, "medium": lead.utm_medium, "campaign": lead.utm_campaign,
             "term": extra["utm_term"], "content": extra["utm_content"]},
        referrer_url=lead.referrer_url or "", visitor_id=extra["visitor_id"],
        metadata={"lead": str(lead.pk)},
    )
    try:
        touch, _ = record(lead.company_id, landing_url=extra["landing_url"],
                          click_id_kind=extra["click_id_kind"], click_id=extra["click_id"], **common)
    except ValidationError:
        # A lead form must never be lost to a bad tracking value: keep the
        # tags that validate (the lead itself keeps everything it was sent).
        logger.warning("lead %s: attribution fields refused; touchpoint written without them", lead.pk)
        try:
            touch, _ = record(lead.company_id, **common)
        except ValidationError:
            touch, _ = record(lead.company_id, **{**common, "referrer_url": "", "visitor_id": ""})
    return touch


def first_and_last_touch(company_id, party, touch, at):
    """First = the party's earliest touchpoint within the lookback, last =
    the latest, both at or before ``at`` (1.8). Fixed at creation.
    (``crm.touchpoints.first_and_last_touch``: one rule for intake, the
    reports and the Google Ads adapter.)"""
    from .touchpoints import first_and_last_touch as rule

    return rule(company_id, party, touch, at)


def _owner(lead):
    from core.models import UserProfile

    if lead.assigned_to_id is not None and lead.assigned_to.company_id == lead.company_id:
        return lead.assigned_to
    owner_id = crm_setting("lead_default_owner")
    if not owner_id:
        return None
    return UserProfile.objects.filter(pk=owner_id, company_id=lead.company_id).first()


def _log_message(lead, party, engagement):
    from communications.ingest import record

    label = lead.get_request_type_display() if lead.request_type else "Inquiry"
    return record(
        lead.company, "note", direction="internal", occurred_at=lead.created_at,
        provider=PROVIDER, subject=f"{label} {lead.lead_number}"[:255],
        body_text=lead.message or "", party=party, engagement=engagement,
        is_editable=False, link_engagement=False,
        metadata={"lead": str(lead.pk), "lead_number": lead.lead_number,
                  "request_type": lead.request_type, "lead_source": lead.source,
                  "lead_direction": "inbound"},
    ).communication


def _party(lead):
    from parties.models import Party

    from .models import Lead

    # The party sync may have linked it through ``update()``: read it back.

    party_id = Lead._base_manager.filter(pk=lead.pk).values_list("party_id", flat=True).first()
    lead.party_id = party_id
    return Party._base_manager.filter(pk=party_id).first() if party_id else None


def accept_lead(lead) -> IntakeResult:
    """Put a new lead on the thread (module docstring). Idempotent on ``lead.engagement``."""
    from . import engagements, tasks
    from .models import Engagement, Lead

    if lead.company_id is None:
        return IntakeResult("skipped", detail="lead has no company")
    if lead.engagement_id is not None:
        return IntakeResult("skipped", lead.engagement, "already on an engagement")
    with transaction.atomic():
        party = _party(lead)
        touch = _touchpoint(lead, party)
        if not wants_engagement(lead.request_type):
            _log_message(lead, party, None)
            return IntakeResult("support")
        company = lead.company
        eng = engagements.open_engagement_for_party(company, party)
        created = eng is None
        if created:
            owner = _owner(lead)
            first, last = first_and_last_touch(company.pk, party, touch, timezone.now())
            value = lead.estimated_value
            eng, _ = engagements.ensure_for_party(
                company, party, stage_key=NEW_STAGE, origin=Engagement.Origin.LEAD,
                owner=owner, expected_value=value if value is not None and value >= 0 else None,
                engagement_source=_source_key(lead.source), first_touch=first, last_touch=last,
                note=f"Lead {lead.lead_number}",
            )
            hours = crm_setting("lead_response_hours")
            tasks.create(
                engagement=eng, title=f"Respond to inquiry {lead.lead_number}"[:255],
                kind="follow_up", assignee=owner,
                due=timezone.now() + datetime.timedelta(hours=hours),
                source=TASK_SOURCE, metadata={"lead": str(lead.pk)},
            )
        Lead._base_manager.filter(pk=lead.pk).update(engagement=eng)
        lead.engagement = eng
        if touch is not None:
            type(touch)._base_manager.filter(pk=touch.pk).update(engagement=eng)
        comm = _log_message(lead, party, eng)
        if not created:
            Engagement._base_manager.filter(pk=eng.pk).update(
                last_activity_at=max(filter(None, (eng.last_activity_at, comm.occurred_at)))
            )
    return IntakeResult("created" if created else "attached", eng)


def _source_key(source):
    from .registries import ENGAGEMENT_SOURCES

    return source if source in ENGAGEMENT_SOURCES else ""


def run_intake(lead):
    """``accept_lead`` in a savepoint: logged, never raised (unless strict).

    A rolled-back intake also undoes what it set on ``lead`` in memory, so a
    later ``save()`` of the same instance never writes a deleted engagement.
    """
    before = lead.engagement_id
    try:
        with transaction.atomic():
            return accept_lead(lead)
    except Exception:  # noqa: BLE001 - a CRM hook must never lose a lead form submission
        # The party it read stays: the party sync wrote it outside this savepoint.
        lead.engagement_id = before
        if intake_strict():
            raise
        logger.exception("lead intake failed for %s; reconcile_engagements --fix repairs it", lead.pk)
        return None



# --- the frozen lead's derived status (spec 1.7) -----------------------------------

#: Open stage key -> the Lead.Status it maps back to; any other open stage
#: reads as ``qualified``.
STAGE_TO_LEAD_STATUS = {"new": "new", "qualification": "qualified", "proposal_sent": "proposal_sent"}
#: The reverse, for the backfill: a lead's open status -> its stage key.
LEAD_STATUS_TO_STAGE = {"new": "new", "contacted": "prospecting", "qualified": "qualification",
                        "proposal_sent": "proposal_sent"}
JUNK_REASON = "disqualified"


def lead_status_for(engagement) -> str:
    """won -> converted; lost -> closed_lost (closed_junk when disqualified);
    open -> the stage mapped back where it exists, else qualified."""
    if engagement.status == "won":
        return "converted"
    if engagement.status == "lost":
        return "closed_junk" if engagement.loss_reason == JUNK_REASON else "closed_lost"
    key = engagement.stage.key if engagement.stage_id else ""
    return STAGE_TO_LEAD_STATUS.get(key, "qualified")


def lead_status_q(status):
    """``Q`` matching leads whose (derived) status is ``status``: the stored
    column before triage, ``lead_status_for(engagement)`` after."""
    from django.db.models import Q

    untriaged = Q(engagement__isnull=True, status=status)
    open_keys = list(STAGE_TO_LEAD_STATUS)
    on = {
        "converted": Q(engagement__status="won"),
        "closed_lost": Q(engagement__status="lost") & ~Q(engagement__loss_reason=JUNK_REASON),
        "closed_junk": Q(engagement__status="lost", engagement__loss_reason=JUNK_REASON),
        "qualified": Q(engagement__status="open") & ~Q(engagement__stage__key__in=[
            k for k, v in STAGE_TO_LEAD_STATUS.items() if v != "qualified"]),
    }.get(status)
    if on is None:
        keys = [k for k in open_keys if STAGE_TO_LEAD_STATUS[k] == status]
        on = Q(engagement__status="open", engagement__stage__key__in=keys) if keys else None
    return untriaged | on if on is not None else untriaged
