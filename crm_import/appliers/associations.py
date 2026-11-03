"""Associations → relationships (docs/crm/phase5-importer.md 2.7).

Contact↔company (either direction in the file) becomes
``PartyRelationship(kind="employment", from=person, to=organisation,
is_primary=<the association's primary flag>, valid_from=NULL,
source="import:<provider>")``. A labelled association can map to another
kind in the mapping file (``[associations.contact_company] "Billing contact"
= "contact_for"``); the kind must allow person → organisation.

An open relationship of that kind between the two parties already present
(staff entered it, or the same pair is associated twice) is linked, not
duplicated: only the ExternalRef is written. Both parties are locked first
(after the job lock, in the login order), and an erased party refuses.

Either end not imported yet fails the row ``dependency_missing`` naming it,
retryable once the record is applied. Activity↔object pairs are not rows of
their own (2.7). Any other pair is ``invalid_value``.

Deal pairs (step 5):

- **deal↔contact** becomes ``EngagementContact(role=<label mapped>,
  is_primary=<primary flag>)``. The role comes from ``[associations.deal_contact]
  "<label>" = "<role>"`` in the mapping file, else the label when it names a
  role (its key or its label, any case: HubSpot's "Decision maker"), else
  ``other``. When the person *is* the engagement's
  party nothing is written but the ExternalRef (the hub person is implicitly a
  contact). An existing contact row for the pair is linked, not duplicated,
  and a second primary is stored as not primary (one per engagement).
- **deal↔company** writes only the ExternalRef: the primary company is the
  engagement's party, and the others are listed in its sidecar by the deal
  applier (``import_other_companies``); an engagement has one counterparty.
- An end that was not imported and never will be (N/A: archived, skipped,
  erased-address, or not in the export) makes the row a no-op instead of a
  ``dependency_missing`` no retry could satisfy; an end still in review is
  ``dependency_missing``.
- **ticket↔contact / ticket↔company** write nothing: the relationship
  applier (step 7 of 4.2, after this one) reads the same ends from the
  ticket's ``contact_refs`` / ``company_refs``. The row checks the party end
  is imported and is applied as a no-op (``applied_ref`` names the job, as
  for a ticket pipeline kept on the job).
"""

from __future__ import annotations

from parties.constants import ORGANISATION, PERSON

from .base import CONFLICT_STALE, ERASED, INVALID_VALUE, Row, RowError, applied_ref

PARTY_TYPES = {"contact": PERSON, "company": ORGANISATION}
DEFAULT_KIND = "employment"


def _ends(row: Row) -> tuple[dict, dict]:
    a, b = row.data.get("from_ref") or {}, row.data.get("to_ref") or {}
    types = {a.get("object_type"), b.get("object_type")}
    if types == {"contact", "company"}:
        return (a, b) if a.get("object_type") == "contact" else (b, a)
    raise RowError(INVALID_VALUE, f"a {'↔'.join(sorted(t or '?' for t in types))} association is not imported.")


def _live(party):
    """Follow a merge to the survivor: the source still names the loser."""
    from parties.models import Party

    seen = set()
    while party.merged_into_id is not None and party.pk not in seen:
        seen.add(party.pk)
        party = Party._base_manager.get(pk=party.merged_into_id)
    return party


def _other_end(row: Row, mine: str) -> tuple[dict, dict] | None:
    a, b = row.data.get("from_ref") or {}, row.data.get("to_ref") or {}
    if a.get("object_type") == mine:
        return a, b
    if b.get("object_type") == mine:
        return b, a
    return None


def _contact_role(row: Row) -> str:
    from crm.models import EngagementContact

    label = row.data.get("label") or ""
    labels = (row.mapping.get("associations") or {}).get("deal_contact") or {}
    role = labels.get(label)
    if role is None:
        # A label that names a role ("Decision maker", "champion"), in any case.
        by_name = {str(name).casefold(): value for value, name in EngagementContact.Role.choices}
        by_name.update({value.casefold(): value for value in EngagementContact.Role.values})
        role = by_name.get(label.strip().casefold(), EngagementContact.Role.OTHER)
    if role not in EngagementContact.Role.values:
        raise RowError(INVALID_VALUE, f"the mapping names engagement contact role {role!r}; the roles are "
                                      f"{', '.join(EngagementContact.Role.values)}.")
    return role


def _deal_pair(row: Row, deal_ref: dict, other: dict) -> str:
    from core.identity.resolve import lock_login_rows
    from crm.models import Engagement, EngagementContact
    from parties.models import Party

    from .deals import is_erased_ref, live_party, resolve_optional

    if other.get("object_type") not in PARTY_TYPES:
        raise RowError(INVALID_VALUE, f"a deal↔{other.get('object_type')} association is not imported.")
    if is_erased_ref(row, other):
        raise RowError(ERASED, f"the {other['object_type']} of this association has been erased; it is not imported.")
    eng, eng_absent = resolve_optional(row, deal_ref, Engagement)
    party, party_absent = resolve_optional(row, other, Party)
    if eng_absent or party_absent:
        # One end was not imported and never will be (archived, skipped, not in
        # the export): nothing to link, and nothing to retry.
        return f"crm_import.importjob:{row.job.pk}"
    if other.get("object_type") == "company":
        row.refs_for(eng)  # the deal applier used the company already
        return applied_ref(Engagement, eng.pk)
    lock_login_rows(parties=[party.pk])
    person = live_party(_live(Party._base_manager.get(pk=party.pk)))
    if person is None:
        raise RowError(ERASED, "the contact of this association has been erased; it is not imported.")
    if person.kind != PERSON:
        raise RowError(INVALID_VALUE, "an engagement contact is a person.")
    if person.pk == eng.party_id:
        row.refs_for(eng)  # the hub person is implicitly a contact (2.7)
        return applied_ref(Engagement, eng.pk)
    contact = EngagementContact._base_manager.filter(engagement_id=eng.pk, party_id=person.pk).first()
    if contact is None:
        primary = bool(row.data.get("primary")) and not EngagementContact._base_manager.filter(
            engagement_id=eng.pk, is_primary=True).exists()
        contact = EngagementContact(engagement=eng, company_id=eng.company_id, party=person, role=_contact_role(row),
                                    is_primary=primary)
        contact.full_clean()
        contact.save()
        row.session.create(contact)
    row.refs_for(contact)
    return applied_ref(EngagementContact, contact.pk)


def _ticket_pair(row: Row, other: dict) -> str:
    from parties.models import Party

    from .deals import resolve_optional

    if other.get("object_type") not in PARTY_TYPES:
        raise RowError(INVALID_VALUE, f"a ticket↔{other.get('object_type')} association is not imported.")
    resolve_optional(row, other, Party)  # waits for a party still in review; an absent one is fine
    return f"crm_import.importjob:{row.job.pk}"  # consumed by the relationship applier


def apply(row: Row) -> str:
    from core.identity.resolve import lock_login_rows
    from parties.models import Party, PartyRelationship
    from parties.relationship_registry import get_relationship_kind

    if row.decision not in ("create", "update", "link"):
        raise RowError(INVALID_VALUE, f"an association row cannot be applied as {row.decision!r}.")
    deal = _other_end(row, "deal")
    if deal is not None:
        return _deal_pair(row, *deal)
    ticket = _other_end(row, "relationship")
    if ticket is not None:
        return _ticket_pair(row, ticket[1])
    person_ref, org_ref = _ends(row)
    person = _live(row.resolve(person_ref, Party))
    org = _live(row.resolve(org_ref, Party))
    lock_login_rows(parties=[person.pk, org.pk])
    person, org = Party._base_manager.get(pk=person.pk), Party._base_manager.get(pk=org.pk)
    for party in (person, org):
        if party.erased_at is not None:
            raise RowError(ERASED, "one end of this association has been erased; it is not imported.")
        if party.merged_into_id is not None:
            raise RowError(CONFLICT_STALE, "one end of this association was merged away just now; retry the row.")
    if person.kind != PERSON or org.kind != ORGANISATION:
        raise RowError(INVALID_VALUE, "a contact↔company association links a person to an organization.")
    labels = (row.mapping.get("associations") or {}).get("contact_company") or {}
    kind = labels.get(row.data.get("label") or "", DEFAULT_KIND)
    try:
        config = get_relationship_kind(kind)
    except KeyError:
        raise RowError(INVALID_VALUE, f"{kind!r} is not a relationship kind (GOLDEN_RELATIONSHIP_KINDS).") from None
    if not config.allows(PERSON, ORGANISATION) or config.symmetric:
        raise RowError(INVALID_VALUE, f"{kind!r} cannot link a person to an organization.")
    rel = PartyRelationship._base_manager.filter(from_party_id=person.pk, to_party_id=org.pk, kind=kind,
                                                 valid_to__isnull=True).first()
    if rel is None:
        rel = PartyRelationship(from_party=person, to_party=org, company_id=person.company_id, kind=kind,
                                is_primary=bool(row.data.get("primary")), source=row.source_tag)
        rel.save()
        row.session.create(rel)
    row.refs_for(rel)
    return applied_ref(PartyRelationship, rel.pk)
