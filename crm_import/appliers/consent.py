"""Consent facts → ``ConsentRecord`` and ``Suppression`` (docs/crm/phase5-importer.md
2.11; phase2-party.md 1.6 "Import rules").

One canonical consent record is one source fact about one address:

==================================  ===============================================
``state=withdrawn`` (+ ``suppress``)  ``ConsentRecord(withdrawn)`` for the address,
                                      and a ``Suppression`` when ``suppress`` is set
                                      (a global opt-out is both: **never a grant**)
``suppress`` only (a hard bounce)     ``Suppression`` only
``state=granted``                     ``ConsentRecord(granted)`` only when the source
                                      recorded *when* (``occurred_at``) and *on what
                                      basis* (``evidence``), a live party holds the
                                      address, and the address has no active
                                      suppression (nor one in the same job);
                                      otherwise nothing is written (counted in
                                      the report)
==================================  ===============================================

The import rules, unchanged: records are appended, never edited; a
suppression is never lifted (an active one for the address is linked, not
duplicated); "latest wins" is ``can_contact``'s ``occurred_at`` order, so a
grant older than a withdrawal changes nothing; a fact without a time is
dated at the export (``manifest.exported_at``), never at the import. An
address under an erasure suppression gets nothing (``base.run`` skips the
row as ``erased``). ``Party.marketable`` is never consent (the contacts
applier sets it).

Parties: ``can_contact`` reads consent by party, so a withdrawal is written
once for **every** live party holding the address (over-applying a
withdrawal is safe); with none it is kept party-less (history; erasure finds
it by address) and the address-keyed suppression does the blocking. A grant
needs exactly one holder (``match-1``): never several, never none. Postal
facts (``channel="post"``) are not recorded: erasure cannot find a postal
address, so the fact could outlive the person.

Each written row gets an ``ExternalRef`` per source fact (the consent record,
else the suppression it created or linked), so a re-import is ``na
unchanged`` at staging. A changed fact (``update``) appends a new record and
re-points the ref; undo removes what the import added and points it back.
Undo deletes an imported consent record only while nothing newer exists for
the same address (4.5).

:func:`report` is the 2.11 consequence for the job report (staging writes it
to ``job.report["consent"]``; step 8's dry run reads it).
"""

from __future__ import annotations

import json

from django.contrib.contenttypes.models import ContentType

from .base import INVALID_VALUE, Row, RowError, applied_ref
from .deals import parse_instant

CONSEQUENCE = (
    "A contact imported without an explicit subscription grant cannot be sent marketing email from golden "
    "(can_contact says no_consent), even if the source CRM counted them as a marketing contact. That is the "
    "correct default. If the business relies on a lawful basis other than recorded consent, record it as a "
    "deliberate, documented bulk action after the import; the importer never grants consent by default."
)


def _match_channel(channel: str) -> str:
    return {"email": "email", "sms": "phone", "phone": "phone"}.get(channel, "")


def parties_for(company_id, channel: str, normalized: str) -> list:
    """Every live party holding the address on this channel (pk order)."""
    from communications.participants import contact_points
    from parties.models import Party

    kind = _match_channel(channel)
    if not kind or not normalized:
        return []
    ids = sorted({party_id for party_id, _point in contact_points(company_id, kind, normalized)}, key=str)
    return list(Party._base_manager.filter(pk__in=ids).order_by("pk"))


def _evidence(row: Row) -> dict:
    data = row.data
    out: dict = {"source": row.job.source, "source_id": str(row.record.get("source_id")),
                 "source_object_type": row.source_object_type}
    if data.get("subscription"):
        out["subscription"] = data["subscription"]
    text = data.get("evidence")
    if text:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        out["evidence"] = parsed if isinstance(parsed, dict) else str(text)[:2000]
    return out


def _occurred_at(row: Row):
    at = parse_instant(row.data.get("occurred_at"))
    if at is not None:
        return at, True
    exported = parse_instant((row.job.manifest or {}).get("exported_at"))
    if exported is None:
        raise RowError(INVALID_VALUE, "the fact has no time and the manifest no exported_at; golden never "
                                      "dates consent at the import.")
    return exported, False


_SUPPRESSED: dict = {}


def suppressed_in_job(job) -> set:
    """``(channel, normalised address)`` of every suppression fact the job
    stages (N/A rows left out): a grant for one of them is never written,
    whichever order the facts apply in. One query per job (memoised: the
    staged facts do not change while a job applies)."""
    from parties.consent import normalize_address
    from unify.models import StagedRow

    from ..stage import job_rows_filter

    key = str(job.pk)
    if key not in _SUPPRESSED:
        if len(_SUPPRESSED) > 16:
            _SUPPRESSED.clear()  # a long-running worker: keep the memo small
        found = set()
        rows = StagedRow.objects.filter(job_rows_filter([job.pk], "consent"), raw__data__suppress__isnull=False) \
            .exclude(mapping_state=StagedRow.MappingState.NA).values_list("raw", flat=True)
        for raw in rows:
            data = (raw or {}).get("data") or {}
            if data.get("suppress"):
                found.add((data.get("channel"), normalize_address(data.get("channel") or "", data.get("address") or "")))
        _SUPPRESSED[key] = found
    return _SUPPRESSED[key]


def _point_refs(row: Row, obj) -> None:
    """The fact's refs name ``obj``: added, or re-pointed (logged) on an update."""
    from core.models import ExternalRef

    ct = ContentType.objects.get_for_model(type(obj))
    for key in row.keys:
        existing = ExternalRef._base_manager.filter(
            company_id=row.company.pk, source=row.job.source, object_type=row.source_object_type,
            source_id=key,
        ).first()
        if existing is None:
            row.ref_for(obj, key)
        elif (existing.content_type_id, str(existing.object_id)) != (ct.pk, str(obj.pk)):
            row.session.update(existing, content_type_id=ct.pk, object_id=str(obj.pk))


def apply(row: Row) -> str:
    from parties.consent import address_hash, normalize_address
    from parties.erp_views import mask_address
    from parties.models import ConsentRecord, ConsentSource, Suppression

    if row.decision not in ("create", "update"):
        raise RowError(INVALID_VALUE, f"a consent row cannot be applied as {row.decision!r}.")
    data = row.data
    channel, state, suppress = data.get("channel"), data.get("state"), data.get("suppress")
    if suppress is not None and state == ConsentRecord.State.GRANTED:
        raise RowError(INVALID_VALUE, "a suppression cannot come with a grant.")
    if channel == "post":
        # Erasure neither suppresses nor finds postal addresses: an imported
        # postal fact could outlive the person. Not recorded (documented).
        return applied_ref(type(row.job), row.job.pk)
    normalized = normalize_address(channel or "", data.get("address") or "")
    if not normalized:
        raise RowError(INVALID_VALUE, f"the {channel} address does not parse; nothing is recorded for it.")
    digest = address_hash(normalized)
    occurred_at, dated = _occurred_at(row)
    detail = f"{row.job.source} {row.source_object_type}"[:255]
    holders = parties_for(row.company.pk, channel, normalized)
    if holders:
        from core.identity.resolve import lock_login_rows

        lock_login_rows(parties=[p.pk for p in holders])  # after the job lock: Job → Party
    active = Suppression.objects.filter(company_id=row.company.pk, channel=channel, address_hash=digest,
                                        lifted_at__isnull=True).first()
    records = []
    if state is not None:
        if not data.get("purpose"):
            raise RowError(INVALID_VALUE, "a consent state needs a purpose.")
        if state == ConsentRecord.State.GRANTED:
            blocked = active is not None or (channel, normalized) in suppressed_in_job(row.job)
            # A grant golden can stand behind: dated, with a basis, for exactly one
            # party, on an address nothing suppresses (2.11). Else nothing.
            parties = holders if dated and data.get("evidence") and len(holders) == 1 and not blocked else []
        else:
            # A withdrawal binds every party holding the address (can_contact reads by
            # party); with none it is kept party-less, and the suppression blocks.
            parties = holders or [None]
        for party in parties:
            record = ConsentRecord(
                company_id=row.company.pk, party=party, channel=channel, purpose=data["purpose"], state=state,
                address_normalized=normalized[:254], occurred_at=occurred_at, source=ConsentSource.IMPORT,
                source_detail=detail, evidence=_evidence(row),
            )
            record.save()
            row.session.create(record)
            records.append(record)
    suppression = None
    if suppress is not None:
        if active is None:
            suppression = Suppression.objects.create(
                company_id=row.company.pk, channel=channel, address_hash=digest, reason=suppress,
                address_hint=mask_address(normalized)[:64], source=ConsentSource.IMPORT, source_detail=detail,
            )
            row.session.create(suppression)
        else:
            suppression = active  # never duplicated, never lifted: the fact links to it
    target = records[0] if records else suppression
    if target is None:
        return applied_ref(type(row.job), row.job.pk)  # nothing golden could record (an unbacked grant)
    _point_refs(row, target)
    return applied_ref(type(target), target.pk)


# --- the report (2.11, 4.4) ---------------------------------------------------------


def report(job) -> dict:
    """Withdrawals, suppressions, grants and the contacts that will be
    ``no_consent`` for marketing, from the job's staged rows (N/A rows left
    out), with the 2.11 consequence stated plainly. ``grants`` counts the
    grants with a time and a basis: at most that many are written (apply
    also needs a party and no active suppression); ``grants_not_recorded``
    the ones without. A contact counts as consenting when any of its emails
    has such a marketing grant."""
    from parties.consent import normalize_address
    from unify.models import StagedRow

    from ..stage import job_rows_filter

    withdrawals = suppressions = grants = grants_unbacked = 0
    by_reason: dict[str, int] = {}
    granted: set[str] = set()
    rows = StagedRow.objects.filter(job_rows_filter([job.pk], "consent")).exclude(
        mapping_state=StagedRow.MappingState.NA).values_list("raw", flat=True)
    for raw in rows.iterator():
        data = (raw or {}).get("data") or {}
        if data.get("state") == "withdrawn":
            withdrawals += 1
        if data.get("suppress"):
            suppressions += 1
            by_reason[data["suppress"]] = by_reason.get(data["suppress"], 0) + 1
        if data.get("state") == "granted":
            if data.get("occurred_at") and data.get("evidence"):
                grants += 1
                if data.get("purpose") == "marketing":
                    granted.add(normalize_address(data.get("channel") or "", data.get("address") or ""))
            else:
                grants_unbacked += 1
    contacts = no_consent = 0
    for raw in StagedRow.objects.filter(job_rows_filter([job.pk], "contact")).exclude(
            mapping_state=StagedRow.MappingState.NA).values_list("raw", flat=True).iterator():
        emails = {normalize_address("email", e.get("address") or "") for e in ((raw or {}).get("data") or {})
                  .get("emails") or []}
        contacts += 1
        if not (emails - {""}) & granted:
            no_consent += 1
    return {
        "withdrawals": withdrawals, "suppressions": suppressions, "suppressions_by_reason": by_reason,
        "grants": grants, "grants_not_recorded": grants_unbacked, "contacts": contacts,
        "contacts_no_consent_for_marketing": no_consent, "consequence": CONSEQUENCE,
    }
