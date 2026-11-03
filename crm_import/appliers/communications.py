"""Notes, meetings, calls, emails → communications (docs/crm/phase5-importer.md
2.10; hand-off sections 5 and 6).

Every record goes through ``communications.ingest.record``, one call per
row, inside the row's transaction and ``suppressed()`` (``base.run``):

- ``provider`` is the job's provider and ``external_id`` the record's source
  id, so ``record`` upserts by its own ``ExternalRef`` (``source=<provider>``,
  ``object_type`` ``message`` / ``call`` / ``meeting``): **a re-ingest whose
  payload hashes the same is a no-op**, and so is any re-ingest of a
  communication erasure scrubbed. The applier also links the job's own ref
  (``source=<provider>:<account>``, the canonical ``source_object_type``) so
  staging recognises the record next time (``na unchanged`` / ``update``).
- ``envelope`` is the canonical ``raw`` (``{schema_version, provider,
  payload}``); ``known_max_version`` is the newest payload version the
  provider's adapter writes (``raw_schema_version`` on the registered
  ``BaseCrmExportAdapter``, else ``KNOWN_RAW_VERSIONS``, else the mapping
  file's ``[communication] raw_schema_version``); otherwise the row fails
  ``invalid_value``.
- **Engagement linkage is explicit**: ``engagement=`` the associated deal's
  engagement (``activity.targets``), else ``link_engagement=False``. The
  hand-off's correction stands: there is no ``"explicit"`` mode.
- ``party=`` the first associated contact or company; else ``record``'s
  counterparty rule. ``performed_by`` is the owner. ``is_editable=False``.
- A null ``direction`` is the mapping file's ``[communication]
  default_direction``, else ``invalid_value`` (never guessed); a null
  ``status`` is ``scheduled`` for a future meeting or call, else
  ``completed``. Duration fields are dropped for kinds without
  ``has_duration``.
- ``last_activity_at``: ``record`` does not move it inside ``suppressed()``,
  so the applier calls ``touch_activity`` for a completed communication of a
  counting kind (``activity.touch_activity``, logged).
- Participants are resolved by ``record`` against the contact points already
  imported (contacts apply before communications, 4.2). The applier does
  **not** run ``resolve_unlinked`` over older participants: linking them to
  imported parties would make those parties' undo refuse. Run
  ``manage.py resolve_participants`` once the import is accepted
  (``docs/crm/importing.md``).

**Write log.** A new communication is a ``create`` with its participants,
raw sidecar and both refs. A re-ingest that changed snapshots the row and
its raw first; ``record`` replaces the participants, so the old ones are
logged as ``deleted`` (undo re-inserts them) and the new ones as creates.
"""

from __future__ import annotations

from django.contrib.contenttypes.models import ContentType
from django.utils import timezone

from core.plugins.payroll import AdapterError

from ..sidecar import SidecarRefused, put
from .activity import owner_of, targets, touch_activity
from .base import CONFLICT_STALE, INVALID_VALUE, Row, RowError, applied_ref
from .deals import parse_instant

#: Payload versions of providers whose adapter golden ships but a deployment
#: may not enable (the export can be adapted on another machine).
KNOWN_RAW_VERSIONS = {"hubspot": 1}


def known_max_version(provider: str, mapping: dict | None = None) -> int:
    """The newest raw payload version golden accepts from ``provider``: the
    registered adapter's ``raw_schema_version``, else ``KNOWN_RAW_VERSIONS``,
    else the mapping file's ``[communication] raw_schema_version`` (an
    operator importing another tool's export declares it)."""
    from core.plugins.registry import plugin_registry

    try:
        adapter = (plugin_registry.get_crm_export_adapters() or {}).get(provider)
    except Exception:  # noqa: BLE001 — a misconfigured registry is not this row's problem
        adapter = None
    version = getattr(adapter, "raw_schema_version", None) if adapter is not None else None
    version = version or KNOWN_RAW_VERSIONS.get(provider) or ((mapping or {}).get("communication") or {}).get(
        "raw_schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise RowError(INVALID_VALUE, f"golden does not know which raw payload versions the {provider!r} adapter "
                                      "writes: enable its plugin, or set [communication] raw_schema_version in the "
                                      "mapping file.")
    return version


def _direction(row: Row, kind_entry) -> str:
    direction = row.data.get("direction") or ((row.mapping.get("communication") or {}).get("default_direction"))
    if direction is None and len(kind_entry.directions) == 1:
        direction = kind_entry.directions[0]  # a note is internal
    if direction not in kind_entry.directions:
        raise RowError(INVALID_VALUE, f"a {kind_entry.name} is {', '.join(kind_entry.directions)}, not "
                                      f"{direction!r}; set [communication] default_direction in the mapping file "
                                      "to import records without one.")
    return direction


def _status(row: Row, occurred_at) -> str:
    from communications.models import Communication

    status = row.data.get("status")
    if status is None:
        future = occurred_at > timezone.now()
        return Communication.Status.SCHEDULED if future else Communication.Status.COMPLETED
    if status not in Communication.Status.values:
        raise RowError(INVALID_VALUE, f"communication status {status!r} is not one of "
                                      f"{', '.join(Communication.Status.values)}.")
    return status


def _existing(row: Row, provider: str, external_id: str):
    from communications.models import Communication
    from core.models import ExternalRef

    hit = ExternalRef._base_manager.filter(
        company_id=row.company.pk, source=provider, source_id=external_id,
        content_type=ContentType.objects.get_for_model(Communication),
    ).values_list("object_id", flat=True).first()
    if hit is None:
        return None
    return Communication._base_manager.filter(pk=hit, company_id=row.company.pk).first()


def _targets(row: Row) -> dict:
    """The mapping file's property targets: its tables are the provider's
    (``[note.properties]``, ``[call.properties]``, …), so by the record's
    ``source_object_type``."""
    section = row.mapping.get(row.source_object_type) or {}
    return dict(section.get("properties") or {}) if isinstance(section, dict) else {}


def _lock_parties(row: Row, linked, existing) -> None:
    """Lock every party the row will point at (the targets, the participants
    ``record`` will resolve, the existing row's party) right after the job
    lock and before ``record`` locks the communication: Job → Party, the order
    erasure takes too."""
    from communications.participants import match, normalize_address
    from core.identity.resolve import lock_login_rows

    ids = {getattr(linked.party, "pk", None), getattr(linked.engagement, "party_id", None),
           getattr(existing, "party_id", None)}
    for p in row.data.get("participants") or []:
        channel, normalized = normalize_address(p.get("address") or "")
        if normalized:
            ids.add(match(row.company.pk, channel, normalized)[0])
    ids.discard(None)
    if ids:
        lock_login_rows(parties=sorted(ids, key=str))


def _participants(row: Row) -> list[dict]:
    out = []
    for p in row.data.get("participants") or []:
        out.append({"role": p.get("role"), "address": p.get("address") or "", "display_name": p.get("name") or ""})
    return out


def apply(row: Row) -> str:
    from communications import ingest
    from communications.kinds import get_kind
    from communications.models import Communication, CommunicationParticipant, CommunicationRaw
    from core.models import ExternalRef

    from .. import writelog

    if row.decision not in ("create", "update"):
        raise RowError(INVALID_VALUE, f"a communication row cannot be applied as {row.decision!r}.")
    data = row.data
    kind = data.get("kind")
    entry = get_kind(kind)
    if entry is None:
        raise RowError(INVALID_VALUE, f"communication kind {kind!r} is not registered "
                                      "(GOLDEN_COMMUNICATION_KINDS).")
    occurred_at = parse_instant(data.get("occurred_at"))
    if occurred_at is None:
        raise RowError(INVALID_VALUE, "occurred_at is not a UTC instant.")
    ended_at = parse_instant(data.get("ended_at")) if entry.has_duration else None
    if ended_at is not None and ended_at < occurred_at:
        ended_at = None
    provider = row.provider
    external_id = str(row.record.get("source_id"))
    owner, owner_flags = owner_of(row)
    linked = targets(row)
    existing = _existing(row, provider, external_id)
    _lock_parties(row, linked, existing)
    old_parts, raw = {}, None
    direction, status = _direction(row, entry), _status(row, occurred_at)
    if existing is not None:
        # Snapshot what record() may change before it runs: the row, its raw, its participants.
        row.session.touch(Communication, existing.pk)
        raw = CommunicationRaw._base_manager.filter(communication=existing).first()
        if raw is not None:
            row.session.touch(CommunicationRaw, raw.pk)
        old_parts = {p.pk: writelog.snapshot(CommunicationParticipant, p.pk)
                     for p in CommunicationParticipant._base_manager.filter(communication=existing)}
        for ref_pk in ExternalRef._base_manager.filter(
                company_id=row.company.pk, source=provider, source_id=external_id,
                content_type=ContentType.objects.get_for_model(Communication)).values_list("pk", flat=True):
            row.session.touch(ExternalRef, ref_pk)  # record() bumps its last_synced_at
    try:
        result = ingest.record(
            row.company, kind, direction=direction, occurred_at=occurred_at,
            participants=_participants(row), provider=provider, external_id=external_id,
            envelope=data.get("raw"), known_max_version=known_max_version(provider, row.mapping),
            subject=data.get("subject") or "", body_text=data.get("body_text") or "",
            summary=data.get("summary") or "", status=status, ended_at=ended_at,
            duration_seconds=data.get("duration_seconds") if entry.has_duration else None,
            engagement=linked.engagement, party=linked.party, performed_by=owner,
            metadata=dict(existing.metadata or {}) if existing is not None else {},
            is_editable=False, link_engagement=False,
        )
    except (ingest.IngestError, AdapterError, ValueError) as exc:
        if isinstance(exc, RowError):
            raise
        raise RowError(INVALID_VALUE, str(exc)[:500]) from exc
    comm = result.communication
    if existing is None and not result.created:
        # record() lost a race for its ref to a concurrent first import and
        # rewrote that row: nothing here snapshotted it, so write nothing.
        raise RowError(CONFLICT_STALE, "this communication was imported concurrently; retry the row.")
    scrubbed = CommunicationRaw._base_manager.filter(communication=comm, payload=ingest.ERASED_PAYLOAD).exists()
    if result.created:
        row.session.create(comm)
        for part in CommunicationParticipant._base_manager.filter(communication=comm):
            row.session.create(part)
        raw = CommunicationRaw._base_manager.filter(communication=comm).first()
        if raw is not None:
            row.session.create(raw)
        ref = ExternalRef._base_manager.get(company_id=row.company.pk, source=provider,
                                            object_type=ingest.object_type_for(kind), source_id=external_id)
        row.session.link(ref)
    elif result.changed:
        for pk, before in old_parts.items():
            row.session.deleted(CommunicationParticipant, pk, before)
        for part in CommunicationParticipant._base_manager.filter(communication=comm):
            row.session.create(part)
        if raw is None:
            raw = CommunicationRaw._base_manager.filter(communication=comm).first()
            if raw is not None:
                row.session.create(raw)
    elif not scrubbed:
        # The payload hashes the same, but what lives outside it (the
        # associations, the owner, a mapped direction) may not: an approved
        # update applies those columns, logged (``touch`` above).
        fixes = {"engagement_id": getattr(linked.engagement, "pk", None),
                 "performed_by_id": getattr(owner, "pk", None), "direction": direction, "status": status}
        if linked.party is not None:
            fixes["party_id"] = linked.party.pk
        fixes = {k: v for k, v in fixes.items() if getattr(comm, k) != v}
        if fixes:
            row.session.update(comm, **fixes)
    if not scrubbed and (result.changed or row.decision == "update"):
        if comm.engagement_id and comm.status == Communication.Status.COMPLETED and entry.counts_as_activity:
            touch_activity(row, comm.engagement_id, comm.occurred_at)
        values = {n: v for n, v in row.properties.items() if _targets(row).get(n, "sidecar") == "sidecar"}
        # body_html stays in the raw payload (CommunicationRaw, view_raw_communication), not here.
        derived = {"import_flags": owner_flags, "import_unresolved_targets": linked.unresolved,
                   "import_other_targets": linked.others}
        try:
            put(comm, provider, values, derived={k: v for k, v in derived.items() if v}, session=row.session)
        except SidecarRefused as exc:
            raise RowError(INVALID_VALUE, str(exc)) from exc
    if not scrubbed:  # erasure scrubbed it: add nothing that points at it
        row.refs_for(comm)
    return applied_ref(Communication, comm.pk)
