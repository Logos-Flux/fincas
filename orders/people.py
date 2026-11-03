"""People on a quote or order (``DocumentContact``), beside its customer.

The people offered are the customer's own person and everyone with an open
relationship to the customer's party (``employment`` for a business,
``contact_for`` otherwise): the same graph ``parties.sync`` keeps from the
customer and its contacts. A person's title is that relationship's title,
so a new person is added through the existing "Add new contact" form and
then picked here. No roles (bug batch 2026-10-15).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from parties.constants import PERSON
from parties.relationship_registry import CONTACT_FOR, EMPLOYMENT

from .models import DocumentContact, Order, Quote


@dataclass(frozen=True)
class Person:
    party_id: object
    name: str
    title: str
    email: str
    phone: str
    contact_id: object = None  # the DocumentContact row, when on the document


def _doc_field(document) -> str:
    if isinstance(document, Quote):
        return "quote"
    if isinstance(document, Order):
        return "order"
    raise TypeError(f"not a quote or order: {document!r}")


def _titles(customer) -> dict:
    """``{person party id: title}`` for everyone related to the customer's party."""
    from parties.models import PartyRelationship

    if customer is None or customer.party_id is None:
        return {}
    titles = {}
    rels = PartyRelationship.objects.filter(
        to_party_id=customer.party_id, kind__in=(EMPLOYMENT, CONTACT_FOR), valid_to__isnull=True,
        from_party__kind=PERSON, from_party__merged_into__isnull=True,
    ).order_by("-is_primary", "created_at").values_list("from_party_id", "title")
    for party_id, title in rels:
        titles.setdefault(party_id, title)
    return titles


def candidates(document, titles=None) -> list[Person]:
    """The people who may be put on ``document``: its customer's people.

    The customer's own person is its party when that is a person (an
    individual), its account holder, or (a business) the employee whose
    relationship ``_titles`` already holds, as ``parties.sync.customer_person``
    resolves it, without the extra queries."""
    from parties.models import Party

    customer = document.customer
    if customer is None:
        return []
    titles = dict(_titles(customer) if titles is None else titles)
    for own in (customer.party_id, customer.account_holder_id):
        if own is not None:
            titles.setdefault(own, "")
    parties = Party.objects.filter(pk__in=titles, company_id=document.company_id, kind=PERSON)
    return sorted(
        (_person(p, titles[p.pk]) for p in parties), key=lambda person: person.name.lower(),
    )


def people(document, titles=None) -> list[Person]:
    """The people on ``document``, oldest first, with their current titles."""
    titles = _titles(document.customer) if titles is None else titles
    rows = DocumentContact.objects.filter(**{_doc_field(document): document}).select_related("party")
    return [_person(row.party, titles.get(row.party_id, ""), contact_id=row.pk) for row in rows]


def card(document) -> tuple[list[Person], list[Person]]:
    """``(people on the document, customer's people not on it yet)`` in three
    queries, for the editor's People card."""
    titles = _titles(document.customer)
    on_doc = people(document, titles)
    taken = {p.party_id for p in on_doc}
    return on_doc, [c for c in candidates(document, titles) if c.party_id not in taken]


def add(document, party_id) -> DocumentContact:
    """Put a person on the document. Only one of its customer's people;
    adding someone already on it is a no-op."""
    if str(party_id) not in {str(p.party_id) for p in candidates(document)}:
        raise ValidationError("That person is not one of this customer's people.")
    row, _ = DocumentContact.objects.get_or_create(
        **{_doc_field(document): document}, party_id=party_id,
        defaults={"company_id": document.company_id},
    )
    return row


def remove(document, contact_id) -> bool:
    deleted, _ = DocumentContact.objects.filter(
        **{_doc_field(document): document}, pk=contact_id,
    ).delete()
    return bool(deleted)


def prune(document) -> int:
    """Take off everyone who is no longer one of the customer's people (the
    document's customer changed, or a person left the company)."""
    keep = {c.party_id for c in candidates(document)}
    deleted, _ = DocumentContact.objects.filter(**{_doc_field(document): document}).exclude(
        party_id__in=keep,
    ).delete()
    return deleted


def copy(source, target) -> None:
    """Carry the people from ``source`` to ``target`` (duplicate, revision,
    quote → order). Only the target's customer's people are copied; anyone
    already on the target is skipped."""
    src, dst = _doc_field(source), _doc_field(target)
    have = set(DocumentContact.objects.filter(**{dst: target}).values_list("party_id", flat=True))
    allowed = {c.party_id for c in candidates(target)}
    with transaction.atomic():
        for party_id in DocumentContact.objects.filter(**{src: source}).values_list("party_id", flat=True):
            if party_id not in have and party_id in allowed:
                DocumentContact.objects.create(**{dst: target}, party_id=party_id,
                                               company_id=target.company_id)


def _person(party, title, contact_id=None) -> Person:
    return Person(
        party_id=party.pk, name=party.display_name, title=title or "",
        email=party.primary_email_normalized or "", phone=party.primary_phone_e164 or "",
        contact_id=contact_id,
    )
