"""``communications.ingest.record``: the one write path for communications
(docs/crm/phase3-engagement.md 2.2, 2.6).

Manual logging and Phase 6 provider adapters both call ``record``. It:

- validates the kind against the registry (``communications.kinds``), the
  direction against the kind, and that every linked row is in ``company``;
- for a provider import (``external_id`` given) requires the raw envelope
  ``{schema_version, provider, payload}``, calls ``require_known_version``,
  and upserts by ``core.ExternalRef`` (``source=provider``,
  ``object_type="message"`` / ``"call"`` / ``"meeting"``), so a re-import is
  idempotent; a re-fetch whose payload hashes the same is a no-op, and so
  is any re-fetch of a communication erasure scrubbed;
- writes the payload to the ``CommunicationRaw`` sidecar, never onto the row;
- resolves participants against the tenant's contact points (``match-1``,
  ``communications.participants``) and never creates a party;
- links an engagement only when given one, or when the participants' parties
  have exactly one open engagement between them (else party-only, never a
  guess).
- moves the linked engagement's ``last_activity_at`` up to ``occurred_at``
  (``crm.engagements.touch_activity``, never back) when the communication
  is completed, of a kind that ``counts_as_activity``, not in the future,
  and not written inside ``core.plugins.dispatch.suppressed()`` (imports
  and backfills keep source timestamps and call ``touch_activity``
  themselves).

Everything runs in one transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from core.plugins.dispatch import is_suppressed
from core.plugins.payroll import AdapterError, require_known_version

from . import participants as participants_module
from .kinds import get_kind
from .models import Communication, CommunicationRaw

MANUAL = "manual"
GOLDEN = "golden"

# ExternalRef.object_type per kind; every other kind is a message.
OBJECT_TYPES = {"call": "call", "meeting": "meeting"}

# ``CommunicationRaw.payload`` after erasure (``parties.erasure.ERASED_PAYLOAD``).
ERASED_PAYLOAD = {"erased": True}

# Roles that name the other side, by direction: the communication's ``party``
# is the first resolved, external participant in these roles.
_COUNTERPARTY_ROLES = {
    "inbound": ("from", "caller", "organizer"),
    "outbound": ("to", "callee", "attendee", "cc"),
    "internal": ("attendee", "to", "organizer"),
}


class IngestError(ValueError):
    """``record`` refused its input; the message says which argument and why."""


@dataclass(frozen=True)
class IngestResult:
    communication: Communication
    created: bool
    # False on a re-import whose payload hash matched: nothing was written.
    changed: bool


def object_type_for(kind: str) -> str:
    return OBJECT_TYPES.get(kind, "message")


def record(
    company,
    kind: str,
    *,
    direction: str,
    occurred_at,
    participants: Iterable[Any] = (),
    provider: str = MANUAL,
    external_id: str = "",
    envelope: dict | None = None,
    known_max_version: int | None = None,
    subject: str = "",
    body_text: str = "",
    summary: str = "",
    status: str = Communication.Status.COMPLETED,
    ended_at=None,
    duration_seconds: int | None = None,
    thread_key: str = "",
    engagement=None,
    party=None,
    performed_by=None,
    metadata: dict | None = None,
    is_editable: bool | None = None,
    link_engagement: bool = True,
) -> IngestResult:
    """Log one communication; see the module docstring for the rules.

    ``participants`` are ``participants.Participant`` instances or dicts of
    ``role``, ``address``, ``display_name``, ``party``. ``known_max_version``
    is the newest envelope ``schema_version`` the caller reads; it defaults
    to the kind's ``raw_schema_version``. ``is_editable`` defaults to True
    for manual logs and False for provider imports (records, not notes).
    ``link_engagement=False`` keeps a communication with no ``engagement``
    off every thread (a support request, spec 1.7): no open-engagement guess.
    """
    entry = get_kind(kind)
    if entry is None:
        raise IngestError(f"Unknown communication kind {kind!r}; register it in "
                          "GOLDEN_COMMUNICATION_KINDS or a plugin's get_communication_kinds().")
    if direction not in entry.directions:
        raise IngestError(f"A {kind!r} communication is {', '.join(entry.directions)}, not {direction!r}.")
    if not provider or len(provider) > 32:
        raise IngestError("provider must be 1-32 characters ('manual', 'golden' or the adapter's id).")
    if external_id and envelope is None:
        raise IngestError("A provider import (external_id) needs its raw envelope "
                          "{schema_version, provider, payload}.")
    specs = [participants_module.as_participant(p) for p in participants]
    for name, target in (("engagement", engagement), ("party", party), ("performed_by", performed_by)):
        if target is not None and target.company_id != company.pk:
            raise IngestError(f"The {name.replace('_', ' ')} belongs to another company.")

    raw_fields = None
    if envelope is not None:
        raw_fields = _raw_fields(envelope, provider, entry, known_max_version)

    fields = {
        "kind": kind, "direction": direction, "status": status,
        "subject": (subject or "")[:255], "body_text": body_text or "", "summary": summary or "",
        "occurred_at": occurred_at, "ended_at": ended_at, "duration_seconds": duration_seconds,
        "provider": provider, "thread_key": thread_key or "",
        "performed_by": performed_by, "metadata": dict(metadata or {}),
        "is_editable": (provider == MANUAL) if is_editable is None else is_editable,
    }

    try:
        with transaction.atomic():
            return _record(company, kind, provider, external_id, raw_fields, fields, specs,
                           direction, engagement, party, link_engagement)
    except IntegrityError:
        # A concurrent first import of the same external id won the
        # ExternalRef: retry once, now as an update or a no-op.
        if not external_id:
            raise
        with transaction.atomic():
            return _record(company, kind, provider, external_id, raw_fields, fields, specs,
                           direction, engagement, party, link_engagement)


def _record(company, kind, provider, external_id, raw_fields, fields, specs,
            direction, engagement, party, link_engagement=True) -> IngestResult:
    existing = _existing(company, provider, kind, external_id) if external_id else None
    if existing is not None:
        raw = CommunicationRaw._base_manager.filter(communication=existing).first()
        if raw is not None and raw_fields is not None and raw.content_hash == raw_fields["content_hash"]:
            return IngestResult(existing, created=False, changed=False)
        if raw is not None and raw.payload == ERASED_PAYLOAD:
            # Erasure scrubbed this communication (parties.erasure): a re-fetch
            # must not write the person back, whatever the provider now sends.
            return IngestResult(existing, created=False, changed=False)
        comm = existing
        for name, value in fields.items():
            setattr(comm, name, value)
        comm.participants.all().delete()
        created = False
    else:
        comm = Communication(company=company, **fields)
        created = True

    try:
        rows = [participants_module.build(comm, spec) for spec in specs]
    except ValueError as exc:
        raise IngestError(str(exc)) from exc
    if party is not None:
        comm.party = party
    elif created or comm.party_id is None:
        comm.party = _counterparty(rows, direction)
    if engagement is not None:
        comm.engagement = engagement
    elif comm.engagement_id is None and link_engagement:
        comm.engagement = _only_open_engagement(company, rows, comm.party)
    comm.full_clean(validate_unique=False)
    comm.save()
    for row in rows:
        row.communication = comm
        row.company_id = comm.company_id
        row.save()
    if raw_fields is not None:
        CommunicationRaw._base_manager.update_or_create(
            communication=comm,
            defaults={"company_id": comm.company_id, "provider": provider, **raw_fields},
        )
    if external_id:
        _write_ref(comm, provider, kind, external_id, created=created)
    if comm.engagement_id is not None and _counts_as_activity(comm):
        from crm.engagements import touch_activity

        touch_activity(comm.engagement_id, comm.occurred_at)
    return IngestResult(comm, created=created, changed=True)


def _counts_as_activity(comm) -> bool:
    """Whether ``comm`` moves its engagement's ``last_activity_at`` (spec 1.3,
    hand-off section 5): completed, a counting kind, not in the future, and
    not inside ``suppressed()`` (an import or backfill)."""
    if is_suppressed() or comm.status != Communication.Status.COMPLETED:
        return False
    if comm.occurred_at is None or comm.occurred_at > timezone.now():
        return False
    entry = comm.kind_entry
    return bool(entry is not None and entry.counts_as_activity)


def _raw_fields(envelope: Any, provider: str, entry, known_max_version: int | None) -> dict:
    from unify.models import content_hash

    if not isinstance(envelope, dict) or set(envelope) != {"schema_version", "provider", "payload"}:
        raise AdapterError(
            "Communication payload arrived without the raw envelope — cause: the adapter "
            "omitted or extended it. Fix: wrap payloads as {schema_version, provider, "
            "payload}. See docs/plugins.md#communication-kinds."
        )
    if envelope["provider"] != provider:
        raise AdapterError(
            f"Envelope provider {envelope['provider']!r} does not match provider={provider!r} — "
            "cause: the adapter passed another provider's payload. Fix: pass the envelope "
            "its own provider id. See docs/plugins.md#communication-kinds."
        )
    known_max = known_max_version if known_max_version is not None else entry.raw_schema_version
    if known_max is None:
        raise IngestError(
            f"Kind {entry.name!r} declares no raw_schema_version; pass known_max_version "
            "(the newest payload version this caller reads)."
        )
    version = require_known_version(envelope, known_max)
    if isinstance(version, bool) or version < 1:
        raise AdapterError(f"Payload schema_version {version!r} must be a positive int. "
                           "See docs/plugins.md#communication-kinds.")
    payload = envelope["payload"]
    return {
        "schema_version": version,
        "payload": payload,
        "content_hash": content_hash(payload if isinstance(payload, dict) else {"payload": payload}),
        "fetched_at": timezone.now(),
    }


def _existing(company, provider, kind, external_id):
    from core.models import ExternalRef

    # Looked up across object types: a provider re-sending an id under a kind
    # of another object type must not create a second communication.
    refs = list(ExternalRef._base_manager.filter(
        company=company, source=provider, source_id=external_id,
        content_type=ContentType.objects.get_for_model(Communication),
    ))
    if not refs:
        return None
    if any(ref.object_type != object_type_for(kind) for ref in refs):
        raise IngestError(
            f"{provider}:{external_id} was imported as a {refs[0].object_type!r}; it cannot "
            f"be re-imported as a {kind!r} ({object_type_for(kind)!r})."
        )
    return Communication._base_manager.select_for_update().filter(pk=refs[0].object_id).first()


def _write_ref(comm, provider, kind, external_id, *, created):
    from core.models import ExternalRef

    if not created:
        ExternalRef._base_manager.filter(
            company_id=comm.company_id, source=provider, object_type=object_type_for(kind),
            source_id=external_id,
        ).update(last_synced_at=timezone.now())
        return
    ExternalRef.objects.create(
        company_id=comm.company_id, source=provider, object_type=object_type_for(kind),
        source_id=external_id, content_type=ContentType.objects.get_for_model(Communication),
        object_id=comm.pk, last_synced_at=timezone.now(),
    )


def _is_internal(party) -> bool:
    if party.is_self:
        return True
    user = party.user if party.user_id else None
    return bool(user is not None and user.is_staff)


def _counterparty(rows, direction):
    """The main external party: the first resolved non-staff participant,
    preferring the roles that name the other side for this direction."""
    resolved = [r for r in rows if r.party_id is not None and not _is_internal(r.party)]
    preferred = _COUNTERPARTY_ROLES.get(direction, ())
    for row in resolved:
        if row.role in preferred:
            return row.party
    return resolved[0].party if resolved else None


def _only_open_engagement(company, rows, party):
    """The one open engagement of the participants' parties, else None."""
    from crm.models import Engagement

    party_ids = {r.party_id for r in rows if r.party_id is not None and not _is_internal(r.party)}
    if party is not None:
        party_ids.add(party.pk)
    if not party_ids:
        return None
    ids = list(
        Engagement._base_manager.filter(company=company, status=Engagement.Status.OPEN)
        .filter(Q(party_id__in=party_ids) | Q(contacts__party_id__in=party_ids))
        .values_list("pk", flat=True).distinct()[:2]
    )
    if len(ids) != 1:
        return None
    return Engagement._base_manager.get(pk=ids[0])


def add_participants(communication, participants: Iterable[Any] = ()) -> int:
    """Add the participants of ``participants`` that ``communication`` does not have yet.

    For a provider that sees one message more than once (Gmail: the same
    email in several staff mailboxes, only the sender's copy carrying the
    Bcc): a later copy may add who took part, and must change nothing else —
    not the subject, body, time or raw payload of the copy already stored.
    A participant is new when its (role, normalised address) is not stored.
    Resolution is ``build``'s (match-1, never a new party). A communication
    erasure scrubbed is never touched. Returns the number of rows added.
    """
    raw = CommunicationRaw._base_manager.filter(communication=communication).values_list(
        "payload", flat=True).first()
    if raw == ERASED_PAYLOAD:
        return 0
    have = {(role, address) for role, address in communication.participants.values_list(
        "role", "address_normalized")}
    added = 0
    with transaction.atomic():
        for spec in (participants_module.as_participant(p) for p in participants):
            try:
                row = participants_module.build(communication, spec)
            except ValueError as exc:
                raise IngestError(str(exc)) from exc
            key = (row.role, row.address_normalized)
            if not row.address_normalized or key in have:
                continue
            row.save()
            have.add(key)
            added += 1
        if added and communication.party_id is None:
            rows = list(communication.participants.select_related("party"))
            party = _counterparty(rows, communication.direction)
            if party is not None:
                type(communication)._base_manager.filter(pk=communication.pk, party__isnull=True).update(
                    party=party)
    return added
