"""Which record owns a party-side fact (docs/crm/phase2-party.md decision P2).

While a Customer (or a CustomerContact) exists, it is the one writer of the
facts ``parties.sync`` mirrors onto its parties: the person's names, the
organisation's trading name, the commerce email and phone, the mirrored
postal addresses and the relationships the sync opened. The staff API
(5.2) and the ERP forms refuse to edit those facts on the party side and
name the owning record instead, so an edit is made once, at its source.

Pure lookups; nothing here writes.
"""

from __future__ import annotations

from .constants import PERSON
from .mirror import CONTACT_LINK_SOURCE, CUSTOMER_LINK_SOURCE, STAFF_LINK_SOURCE

# Party columns the sync writes from its source record (``_set_person_names``
# for a person, the organisation-name branch of ``sync_from_customer``).
MIRRORED_PERSON_FIELDS = ("given_name", "family_name", "display_name")
MIRRORED_ORGANISATION_FIELDS = ("display_name",)

# PartyEmail / PartyPhone.source prefixes written by the sync and the backfill
# (``mirror.customer_source`` / ``contact_source`` / ``user_source``).
MIRRORED_CONTACT_PREFIXES = ("customer:", "contact:", "user:")
# PartyRelationship.source values the sync and the backfill own.
MIRRORED_RELATIONSHIP_SOURCES = (CUSTOMER_LINK_SOURCE, CONTACT_LINK_SOURCE, STAFF_LINK_SOURCE)


def _ref(model_label: str, pk) -> dict:
    return {"model": model_label, "id": str(pk)}


def party_owner(party) -> dict | None:
    """The record that owns ``party``'s mirrored fields, or None.

    A person is owned by the Customer it holds or countersigns, by the
    business Customer whose ``employment`` link the sync opened for it, or by
    the CustomerContact that created it. An organisation is owned by the
    business Customer it countersigns.
    """
    from customers.models import Customer, CustomerContact

    from .models import PartyRelationship

    customer_id = (
        Customer.objects.filter(party=party).values_list("pk", flat=True).first()
        or Customer.objects.filter(account_holder=party).values_list("pk", flat=True).first()
    )
    if customer_id:
        return _ref("customers.Customer", customer_id)
    if party.kind != PERSON:
        return None
    rel = (
        PartyRelationship.objects.filter(
            from_party=party, source__in=(CUSTOMER_LINK_SOURCE, CONTACT_LINK_SOURCE),
            valid_to__isnull=True,
        ).order_by("created_at").values("source", "metadata").first()
    )
    if rel is not None:
        if rel["source"] == CUSTOMER_LINK_SOURCE and rel["metadata"].get("customer"):
            return _ref("customers.Customer", rel["metadata"]["customer"])
        if rel["source"] == CONTACT_LINK_SOURCE and rel["metadata"].get("contact"):
            return _ref("customers.CustomerContact", rel["metadata"]["contact"])
    contact_id = CustomerContact.objects.filter(party=party).values_list("pk", flat=True).first()
    if contact_id:
        return _ref("customers.CustomerContact", contact_id)
    return None


def mirrored_party_fields(party) -> tuple[str, ...]:
    """The fields of ``party`` that only its owning record may change."""
    if party_owner(party) is None:
        return ()
    return MIRRORED_PERSON_FIELDS if party.kind == PERSON else MIRRORED_ORGANISATION_FIELDS


def contact_point_owner(row) -> dict | None:
    """The record a PartyEmail / PartyPhone / PartyAddress is mirrored from."""
    from .models import PartyAddress

    if isinstance(row, PartyAddress):
        # .all() so a prefetch_related("customer_addresses") serves it (no N+1).
        linked = list(row.customer_addresses.all()[:1])
        return _ref("customers.CustomerAddress", linked[0].pk) if linked else None
    source = row.source or ""
    for prefix, label in (
        ("customer:", "customers.Customer"),
        ("contact:", "customers.CustomerContact"),
        ("user:", "auth.User"),
    ):
        if source.startswith(prefix):
            return _ref(label, source[len(prefix):])
    return None


def relationship_owner(rel) -> dict | None:
    """The record a sync-opened relationship belongs to, or None."""
    if rel.source not in MIRRORED_RELATIONSHIP_SOURCES:
        return None
    meta = rel.metadata or {}
    if meta.get("customer"):
        return _ref("customers.Customer", meta["customer"])
    if meta.get("contact"):
        return _ref("customers.CustomerContact", meta["contact"])
    return {"model": "parties.sync", "id": rel.source}


def owner_message(owner: dict) -> str:
    return (
        f"Mirrored from {owner['model']} {owner['id']}: edit it there "
        "(docs/crm/phase2-party.md decision P2)."
    )
