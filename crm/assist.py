"""Read-only facts for the assistant's CRM tools (docs/crm/phase3-engagement.md 5.3).

Three things the ``erp_assist`` tools cannot compute over the plain resource
API, all served by ``crm.api`` and all read-only:

- ``staff_envelope``: the staff directory as a Phase 2 2.7 match envelope.
  The assistant's shared key is not a person, so a tool acting "for" someone
  names them; an email resolves to exactly one staff profile or the envelope
  comes back ``ambiguous`` with the candidates, never a guess.
- ``queue_for``: ``crm/me/``'s three sections for a named staff profile, in
  that person's time zone.
- ``follow_up_facts``: who a follow-up on an engagement would go to, on which
  contact point, and the ``parties.consent.can_contact`` decision for it. A
  suppressed address is a refusal and its address is withheld; an erased (or
  merged-away) party is never a recipient or a listed contact.

Outputs are machine-facing: canonical keys and field names only, never the
label layer (spec 3.4).
"""

from __future__ import annotations

import datetime
import uuid

from django.db.models import Exists, F, OuterRef, Q
from django.utils import timezone

from core import tz
from core.models import UserProfile

from .models import OPEN_TASK_STATUSES, Engagement, EngagementContact, Task

EXACT, FUZZY, AMBIGUOUS, NONE = "exact", "fuzzy", "ambiguous", "none"
CANDIDATE_LIMIT = 10
QUEUE_LIMIT = 100
FOLLOW_UP_CHANNELS = ("email", "sms")
#: Days from today (the owner's local date) a suggested follow-up task is due.
SUGGESTED_TASK_DAYS = 3


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


# --- staff directory ----------------------------------------------------------------


def is_staff_person(profile) -> bool:
    """A staff human: an active login holding a staff role, not a service account."""
    from core.permissions import is_staff_principal
    from core.service_accounts import is_service_account

    user = profile.user
    if user is None or not user.is_active or is_service_account(user):
        return False
    return is_staff_principal(user)


def staff_candidate(profile) -> dict:
    user = profile.user
    return {
        "id": str(profile.pk),
        "display_name": profile.display_name or (user.get_full_name() if user else "") or
        (user.username if user else ""),
        "email": (user.email if user else "") or "",
        "username": user.username if user else "",
        "company": str(profile.company_id) if profile.company_id else None,
        "company_name": profile.company.name if profile.company_id else None,
        "timezone": tz.resolve(profile, profile.company).key,
    }


def staff_profiles(company_ids):
    """Profiles with a company in ``company_ids`` (None = every tenant)."""
    qs = UserProfile.objects.filter(company__isnull=False, user__isnull=False).select_related(
        "user", "company")
    if company_ids is not None:
        qs = qs.filter(company_id__in=company_ids)
    return qs


def staff_envelope(company_ids, query: str) -> dict:
    """The 2.7 envelope over staff profiles for an email, a profile id, or a name.

    ``exact``: the profile id, or one staff email equal (case-insensitive) to
    the query. Several staff with that email (two tenants) is ``ambiguous``.
    A name (display name, full name or username) is never exact: one hit is
    ``fuzzy``, several ``ambiguous``.
    """
    query = (query or "").strip()
    base = staff_profiles(company_ids)
    if not query:
        return _envelope(NONE, [], "Pass q: a staff email, profile id or name.")
    if _is_uuid(query):
        hits = [p for p in base.filter(pk=query) if is_staff_person(p)]
        return _envelope(EXACT if hits else NONE, hits)
    if "@" in query:
        hits = [p for p in base.filter(user__email__iexact=query).order_by("company__name", "pk")
                if is_staff_person(p)]
        if len(hits) == 1:
            return _envelope(EXACT, hits)
        if len(hits) > 1:
            return _envelope(AMBIGUOUS, hits, f"{len(hits)} staff accounts have {query!r}.")
        return _envelope(NONE, [], f"No staff account has {query!r}.")
    words = query.split()
    name_q = Q(display_name__iexact=query) | Q(user__username__iexact=query) | Q(display_name__istartswith=query)
    if len(words) >= 2:
        name_q |= Q(user__first_name__iexact=words[0], user__last_name__iexact=" ".join(words[1:]))
    else:
        name_q |= Q(user__first_name__iexact=query) | Q(user__last_name__iexact=query)
    hits = [p for p in base.filter(name_q).order_by("display_name", "pk")[:CANDIDATE_LIMIT * 5]
            if is_staff_person(p)][:CANDIDATE_LIMIT]
    if len(hits) == 1:
        return _envelope(FUZZY, hits, "A name match only: confirm this person before acting for them.")
    if hits:
        return _envelope(AMBIGUOUS, hits, f"{len(hits)} staff match {query!r}.")
    return _envelope(NONE, [], f"No staff account matches {query!r}.")


def _envelope(quality, profiles, message="") -> dict:
    candidates = [staff_candidate(p) for p in profiles]
    item = candidates[0] if quality in (EXACT, FUZZY) and candidates else None
    return {"match_quality": quality, "item": item, "candidates": candidates, "message": message}


# --- the queue ------------------------------------------------------------------------


def queue_for(profile, *, now=None) -> dict:
    """``crm/me/`` for ``profile``: open tasks overdue and due by the end of the
    person's local day, and their open engagements with no open task.

    Returns querysets; the API serialises them. Rows are the profile's own
    company only.
    """
    now = now or timezone.now()
    company = profile.company
    zone = tz.resolve(profile, company)
    start = datetime.datetime.combine(now.astimezone(zone).date(), datetime.time.min, tzinfo=zone)
    end_of_today = start + datetime.timedelta(days=1)
    tasks = Task.objects.filter(company=company, assignee=profile,
                                status__in=OPEN_TASK_STATUSES).select_related("engagement")
    idle = (
        Engagement.objects.filter(company=company, owner=profile, status=Engagement.Status.OPEN)
        .filter(~Exists(Task.objects.filter(engagement=OuterRef("pk"), status__in=OPEN_TASK_STATUSES)))
        .select_related("pipeline", "stage", "party", "owner").order_by("created_at", "pk")
    )
    return {
        "timezone": zone.key,
        "overdue": tasks.filter(due_at__lt=now).order_by("due_at", "pk")[:QUEUE_LIMIT],
        "today": tasks.filter(due_at__gte=now, due_at__lt=end_of_today).order_by("due_at", "pk")[:QUEUE_LIMIT],
        "needs_next_action": idle[:QUEUE_LIMIT],
    }


# --- follow-up facts --------------------------------------------------------------------


def _live(party) -> bool:
    return party is not None and party.erased_at is None and party.merged_into_id is None


def _address(party, channel):
    """``(address, contact_point)`` on ``channel``: the primary active one first."""
    if channel == "email":
        row = party.emails.exclude(status="bounced").exclude(status="obsolete").order_by(
            "-is_primary", "created_at").first()
        return (row.email, {"type": "party_email", "id": str(row.pk)}) if row else (None, None)
    # SMS goes to a mobile number only; a landline is "no address".
    row = party.phones.exclude(number_e164="").filter(line_type="mobile").order_by(
        "-is_primary", "created_at").first()
    return (row.number_e164, {"type": "party_phone", "id": str(row.pk)}) if row else (None, None)


def _decisions(party, channel, address) -> dict:
    """``can_contact`` for both purposes. A follow-up on a deal is neither bulk
    marketing nor a transactional receipt, so any active suppression of the
    address refuses it (the marketing decision sees every reason; the
    transactional one only hard bounces and legal holds)."""
    from parties.consent import can_contact

    marketing = can_contact(party, channel, "marketing", address)
    transactional = can_contact(party, channel, "transactional", address)
    suppressed = marketing.reason.startswith("suppressed:")
    return {
        "suppressed": suppressed,
        "reason": marketing.reason if suppressed else transactional.reason if not transactional.allowed
        else marketing.reason,
        "marketing": {"allowed": marketing.allowed, "reason": marketing.reason},
        "transactional": {"allowed": transactional.allowed, "reason": transactional.reason},
    }


def _people(engagement):
    """``(party, role, is_primary)`` in recipient order: a person counterparty,
    the primary contact, the other contacts, then an organisation counterparty."""
    out, seen = [], set()
    party = engagement.party
    if party is not None and party.kind == "person":
        out.append((party, "counterparty", True))
        seen.add(party.pk)
    contacts = EngagementContact._base_manager.filter(engagement=engagement).select_related(
        "party").order_by("-is_primary", "created_at", "pk")
    for c in contacts:
        if c.party_id not in seen:
            out.append((c.party, c.role, c.is_primary))
            seen.add(c.party_id)
    if party is not None and party.kind != "person" and party.pk not in seen:
        out.append((party, "counterparty", False))
    return out


def follow_up_facts(engagement, channel: str = "email", *, now=None) -> dict:
    """Everything a follow-up proposal needs, without writing a row.

    ``refusal``: ``suppressed`` (the recipient's address is suppressed; the
    address is withheld), ``no_recipient`` (no live party on the thread), or
    ``no_address`` (no usable contact point on ``channel``); else None.
    """
    now = now or timezone.now()
    owner = engagement.owner
    zone = tz.resolve(owner, engagement.company)
    contacts, recipient, consent = [], None, None
    for party, role, is_primary in _people(engagement):
        if not _live(party):
            continue  # erased or merged away: never a recipient, never listed
        address, point = _address(party, channel)
        decision = _decisions(party, channel, address) if address else None
        row = {"party": str(party.pk), "display_name": party.display_name, "kind": party.kind,
               "role": role, "is_primary": is_primary, "has_address": address is not None,
               "can_contact": decision}
        contacts.append(row)
        if recipient is None and address is not None:
            recipient, consent = (row, address, point), decision
    refusal = None
    to = None
    if not contacts:
        refusal = "no_recipient"
    elif recipient is None:
        refusal = "no_address"
    elif consent["suppressed"]:
        refusal = "suppressed"
    if recipient is not None:
        row, address, point = recipient
        to = {"party": row["party"], "display_name": row["display_name"], "role": row["role"],
              "channel": channel, "address": None if refusal else address,
              "contact_point": None if refusal else point}
    # Erasure scrubs only some communications of an erased person (activity-log copies,
    # lead messages), so a natively logged one may still carry their subject and summary.
    # Never hand that to the assistant: leave out communications of erased or merged-away
    # parties, and name those parties (ids only) so the tool drops their timeline items.
    from communications.models import Communication

    hidden = set(
        Communication._base_manager.filter(engagement=engagement, party__isnull=False)
        .filter(Q(party__erased_at__isnull=False) | Q(party__merged_into__isnull=False))
        .values_list("party_id", flat=True).distinct()
    )
    last = (engagement.communications.exclude(status="scheduled", occurred_at__gt=now)
            .exclude(party_id__in=hidden).order_by("-occurred_at", "pk").first())
    open_tasks = Task._base_manager.filter(engagement=engagement, status__in=OPEN_TASK_STATUSES).order_by(
        F("due_at").asc(nulls_last=True), "created_at", "pk")[:20]
    return {
        "engagement": {
            "id": str(engagement.pk), "company": str(engagement.company_id), "number": engagement.number,
            "title": engagement.title, "status": engagement.status,
            "pipeline_key": engagement.pipeline.key,
            "stage_key": engagement.stage.key if engagement.stage_id else None,
            "stage_name": engagement.stage.name if engagement.stage_id else None,
            "owner": str(owner.pk) if owner else None,
            "owner_name": owner.display_name if owner else None,
            "party": str(engagement.party_id) if engagement.party_id else None,
            "next_action_due_at": _iso(engagement.next_action_due_at),
            "last_activity_at": _iso(engagement.last_activity_at),
            "expected_close_date": _iso(engagement.expected_close_date),
        },
        "channel": channel,
        "refusal": refusal,
        "to": to,
        "consent": consent,
        "contacts": contacts,
        "last_communication": None if last is None else {
            "id": str(last.pk), "kind": last.kind, "direction": last.direction, "status": last.status,
            "occurred_at": _iso(last.occurred_at), "subject": last.subject, "summary": last.summary,
            "party": str(last.party_id) if last.party_id else None,
        },
        "hidden_parties": sorted(str(pk) for pk in hidden),
        "open_tasks": [
            {"id": str(t.pk), "title": t.title, "kind": t.kind, "status": t.status,
             "due_at": _iso(t.due_at), "assignee": str(t.assignee_id) if t.assignee_id else None}
            for t in open_tasks
        ],
        "owner_timezone": zone.key,
        "suggested_due_date": (now.astimezone(zone).date()
                               + datetime.timedelta(days=SUGGESTED_TASK_DAYS)).isoformat(),
    }


def _iso(value):
    return value.isoformat() if value is not None else None
