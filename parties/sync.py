"""Keep parties in step with Customer, CustomerContact, CustomerAddress and
Lead (docs/crm/phase2-party.md 2.1, 2.5; decision P2: one writer per fact).

While a Customer exists it owns the mirrored facts (names, commerce email
and phone, company name, marketing flag, active flag, legacy ids); the
party side is written only from here. The sync never runs the other way.

Entry points are called from the source model's ``save()``, inside the same
transaction, after the row is written:

- ``sync_from_customer(customer)``
- ``sync_from_contact(contact)``
- ``sync_from_address(address)``
- ``sync_from_lead(lead)``

Paths that bypass ``save()`` (``QuerySet.update``, ``bulk_create``) are
caught by ``manage.py reconcile_parties --check`` and repaired by ``--fix``,
which calls the same functions.

A sync never blocks the commerce write that triggered it: ``run_guarded``
runs it in a savepoint and logs a failure (``reconcile_parties`` then
reports the drift), unless ``GOLDEN_PARTY_SYNC_STRICT`` is set (the test
settings set it, so a sync bug fails the test that hit it).
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .constants import ORGANISATION, PERSON
from .match import MATCH_1, MATCH_N, find
from .mirror import (
    ADDRESS_FIELDS,
    ADDRESS_REF,
    ADDRESS_SOURCE_SYNC,
    CONTACT_LINK_SOURCE,
    CUSTOMER_LINK_SOURCE,
    CUSTOMER_ROLE_SOURCE,
    LEAD_ROLE_SOURCE,
    SYNC_MARKETABLE_SOURCE,
    address_values,
    contact_source,
    customer_source,
    default_company_id,
    has_login,
    is_business,
    lead_role_dates,
    legacy_refs,
    organisation_name,
    person_display,
    person_names,
    phone_parts,
    safe_email,
)
from .relationship_registry import CONTACT_FOR, EMPLOYMENT
from .role_registry import CUSTOMER_ROLE, LEAD_ROLE, STAFF_ROLE

logger = logging.getLogger(__name__)

# Customer columns the sync reads. A save(update_fields=...) touching none of
# them (e.g. ``last_order_at``) skips the sync.
CUSTOMER_SYNC_FIELDS = frozenset({
    "first_name", "last_name", "email", "phone", "company_name", "company", "customer_type", "job_title",
    "accepts_marketing", "is_active", "user", "party", "account_holder",
    "bc_id", "erpnext_name", "ebay_username", "stripe_customer_id",
})
CONTACT_SYNC_FIELDS = frozenset({
    "first_name", "last_name", "email", "phone", "job_title", "is_primary", "customer", "party",
})
ADDRESS_SYNC_FIELDS = frozenset({
    *ADDRESS_FIELDS, "country", "label", "address_type", "is_default", "customer", "party_address",
    "bc_address_id",
})
LEAD_SYNC_FIELDS = frozenset({
    "first_name", "last_name", "email", "phone", "country", "status", "company", "party",
    "converted_to_customer", "converted_at", "lost_at",
})

CONSENT_SOURCE_FOR_ORIGIN = {"api": "api", "import": "import", "portal": "portal"}


def wants_sync(update_fields, relevant) -> bool:
    return update_fields is None or bool(set(update_fields) & relevant)


def run_guarded(fn, *args, **kwargs):
    """Run a sync in a savepoint; log instead of raising unless strict."""
    if getattr(settings, "GOLDEN_PARTY_SYNC_STRICT", False):
        with transaction.atomic():
            return fn(*args, **kwargs)
    try:
        with transaction.atomic():
            return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001 - a party mirror must never block a commerce write
        logger.exception("party sync %s failed; reconcile_parties will report the drift", fn.__name__)
        return None


def _today():
    return timezone.localdate()


# --- shared helpers --------------------------------------------------------------


def _erased_customer(customer) -> bool:
    """An erased customer is a tombstone (parties.erasure): never mirrored again."""
    from .erasure import is_erased_customer

    return is_erased_customer(customer)


def is_tombstone_email(value, pk) -> bool:
    from .erasure import is_tombstone_email as check

    return check(value, pk)


def _erased_party(party_id) -> bool:
    from .models import Party

    return party_id is not None and Party._base_manager.filter(pk=party_id, erased_at__isnull=False).exists()


def customer_person(customer):
    """The person party a customer's own facts are mirrored onto, or None.

    The account holder when set; else the party itself when it is a person
    (an individual customer); else the person tied to the organisation by the
    ``employment`` relationship tagged with this customer.
    """
    from .models import Party, PartyRelationship

    if customer.account_holder_id:
        return Party.objects.get(pk=customer.account_holder_id)
    if customer.party_id:
        party = Party.objects.get(pk=customer.party_id)
        if party.kind == PERSON:
            return party
    rel = PartyRelationship.objects.filter(
        source=CUSTOMER_LINK_SOURCE, kind=EMPLOYMENT, valid_to__isnull=True,
        metadata__customer=str(customer.pk),
    ).select_related("from_party").order_by("created_at").first()
    return rel.from_party if rel else None


def _holds_commerce(party, customer) -> bool:
    """Whether ``party`` already stands for another commerce account."""
    from customers.models import Customer

    from .models import PartyRelationship

    if Customer.objects.filter(Q(party=party) | Q(account_holder=party)).exclude(pk=customer.pk).exists():
        return True
    return PartyRelationship.objects.filter(
        from_party=party, source=CUSTOMER_LINK_SOURCE, valid_to__isnull=True,
    ).exclude(metadata__customer=str(customer.pk)).exists()


def _reusable(party, customer) -> bool:
    """3.2 / 2.1: a match-1 counts only if the candidate holds no other
    commerce account, is not staff (2.3: staff are never merged
    automatically) and has no other login."""
    from .models import PartyRole

    if party.user_id is not None and party.user_id != customer.user_id:
        return False
    if PartyRole.objects.filter(party=party, role=STAFF_ROLE, valid_to__isnull=True).exists():
        return False
    from .models import PartyRelationship

    if PartyRelationship.objects.filter(from_party=party, source=CONTACT_LINK_SOURCE, valid_to__isnull=True).exists():
        return False  # a contact owns this party's names: never give it a second writer
    return not _holds_commerce(party, customer)


def _promote_primary(model, party) -> None:
    """Keep one primary row when the primary one was deleted (1.4)."""
    if model.objects.filter(party=party, is_primary=True).exists():
        return
    rows = model.objects.filter(party=party)
    if model.__name__ == "PartyPhone":
        rows = rows.exclude(number_e164="")
    first = rows.order_by("created_at").first()
    if first is not None:
        first.is_primary = True
        first.save(update_fields=["is_primary"])


def _drop(mirror) -> None:
    party = mirror.party
    mirror.delete()
    _promote_primary(type(mirror), party)


def mirror_email(party, raw, *, source, purpose) -> None:
    """Make ``party`` carry the address in ``raw``, tracked by ``source``."""
    from .models import PartyEmail

    email = safe_email(raw)
    mirror = PartyEmail.objects.filter(party=party, source=source).first()
    if not email:
        if mirror is not None:
            _drop(mirror)
        return
    raw = raw.strip()[:254]
    if mirror is not None and mirror.email_normalized == email:
        if mirror.email != raw:
            mirror.email = raw
            mirror.save(update_fields=["email"])
        return
    if PartyEmail.objects.filter(party=party, email_normalized=email).exists():
        # Another row already holds this address (an A1 contact, a lead).
        if mirror is not None:
            _drop(mirror)
        return
    if mirror is not None:
        mirror.email = raw
        mirror.save(update_fields=["email"])
        return
    PartyEmail.objects.create(
        party=party, email=raw, purpose=purpose, source=source,
        is_primary=not PartyEmail.objects.filter(party=party, is_primary=True).exists(),
    )


def mirror_phone(party, raw, *, source, purpose, region=None) -> None:
    from .models import PartyPhone

    raw, e164, _ext = phone_parts(raw, region)
    mirror = PartyPhone.objects.filter(party=party, source=source).first()
    if not raw:
        if mirror is not None:
            _drop(mirror)
        return
    if mirror is not None and mirror.number_raw == raw:
        return
    others = PartyPhone.objects.filter(party=party)
    if mirror is not None:
        others = others.exclude(pk=mirror.pk)
    if (e164 and others.filter(number_e164=e164).exists()) or others.filter(number_raw=raw).exists():
        if mirror is not None:
            _drop(mirror)
        return
    if mirror is None:
        mirror = PartyPhone(
            party=party, source=source, purpose=purpose,
            is_primary=bool(e164) and not PartyPhone.objects.filter(party=party, is_primary=True).exists(),
        )
    mirror.number_raw = raw
    mirror.phone_region = region
    mirror.save()


def _set_person_names(party, first, last, fallback) -> None:
    given, family = person_names(first, last)
    fields = []
    if (party.given_name, party.family_name) != (given, family):
        party.given_name, party.family_name = given, family
        fields += ["given_name", "family_name"]
    if not party.display_name_locked:
        display = person_display(first, last, fallback)
        if party.display_name != display:
            party.display_name = display
            fields.append("display_name")
    if fields:
        party.save(update_fields=fields)


def _new_person(company_id, first, last, fallback, origin, **extra):
    from .models import Party

    given, family = person_names(first, last)
    return Party.objects.create(
        company_id=company_id, kind=PERSON, given_name=given, family_name=family,
        display_name=person_display(first, last, fallback), origin=origin, **extra,
    )


def sync_customer_role(party_id) -> None:
    """``PartyRole(customer)`` is open exactly while an active Customer has the party (2.1)."""
    from customers.models import Customer

    from .models import PartyRole

    active = Customer.objects.filter(party_id=party_id, is_active=True).exists()
    role = PartyRole.objects.filter(party_id=party_id, role=CUSTOMER_ROLE, valid_to__isnull=True).first()
    if active and role is None:
        PartyRole.objects.create(party_id=party_id, role=CUSTOMER_ROLE, valid_from=_today(), source=CUSTOMER_ROLE_SOURCE)
    elif not active and role is not None:
        role.valid_to = max(_today(), role.valid_from)
        role.save(update_fields=["valid_to"])


def latest_marketing_consent(party, address=""):
    from .models import ConsentRecord

    return ConsentRecord.objects.filter(
        party=party, channel="email", purpose="marketing", address_normalized__in=["", address],
    ).order_by("-occurred_at", "-recorded_at").first()


def _sync_consent(customer, person, origin) -> None:
    """``accepts_marketing`` appends a ConsentRecord on change (2.1); False on a
    party with no record writes nothing (false means unknown)."""
    from .models import ConsentRecord

    latest = latest_marketing_consent(person, safe_email(customer.email))
    granted = latest is not None and latest.state == ConsentRecord.State.GRANTED
    if bool(customer.accepts_marketing) == granted:
        return
    if not customer.accepts_marketing and latest is None:
        return
    ConsentRecord.objects.create(
        party=person, channel="email", purpose="marketing",
        state=ConsentRecord.State.GRANTED if customer.accepts_marketing else ConsentRecord.State.WITHDRAWN,
        occurred_at=timezone.now(), source=CONSENT_SOURCE_FOR_ORIGIN.get(origin, "erp"),
        source_detail=f"customer:{customer.pk} accepts_marketing",
    )
    if customer.accepts_marketing and not person.marketable:
        person.marketable = True
        person.marketable_source = SYNC_MARKETABLE_SOURCE
        person.marketable_changed_at = timezone.now()
        person.save(update_fields=["marketable", "marketable_source", "marketable_changed_at"])


def _sync_refs(obj, company_id, wanted) -> None:
    """Dual-write legacy id columns to ExternalRef (2.1, D8). ``wanted`` is
    ``[(column, source, object_type, source_id)]``. A key that already points
    at another row is a duplicate customer: left alone, reported by reconcile."""
    from django.contrib.contenttypes.models import ContentType

    from core.models import ExternalRef
    from core.provenance import is_erased_source

    ct = ContentType.objects.get_for_model(type(obj))
    for column, source, object_type, value in wanted:
        if is_erased_source(company_id, source, object_type, value):
            continue  # an erased person's source record: never re-linked (1.7)
        ref = ExternalRef.objects.filter(
            company_id=company_id, source=source, object_type=object_type, source_id=value,
        ).first()
        if ref is None:
            ExternalRef.objects.create(
                company_id=company_id, source=source, object_type=object_type, source_id=value,
                content_type=ct, object_id=obj.pk, metadata={"legacy_column": column},
            )
        elif (ref.content_type_id, ref.object_id) != (ct.pk, obj.pk):
            logger.warning("ExternalRef %s:%s:%s already targets another row; %s %s not linked",
                           source, object_type, value, ct.model, obj.pk)
    # A cleared or changed column drops the ref it wrote before.
    current = {(column, value) for column, _s, _t, value in wanted}
    for ref in ExternalRef.objects.filter(content_type=ct, object_id=obj.pk, metadata__has_key="legacy_column"):
        column = ref.metadata.get("legacy_column")
        if (column, ref.source_id) not in current:
            ref.delete()


def _region_for(customer):
    from customers.models import CustomerAddress

    return CustomerAddress.objects.filter(customer_id=customer.pk).order_by("-is_default", "created_at").values_list(
        "country", flat=True).first()


# --- Customer ----------------------------------------------------------------------


def sync_from_customer(customer, *, origin: str = "erp") -> dict:
    """Bring the customer's parties in line with it. Idempotent."""
    from core.models import Company
    from customers.models import Customer

    from .models import Party, PartyRelationship

    if _erased_customer(customer):
        return {"skipped": "erased"}
    done = {}
    if customer.company_id is None:
        company_id = default_company_id(Company)
        if company_id is None:
            return {"skipped": "no tenant (set GOLDEN_PORTAL_COMPANY)"}
        Customer.objects.filter(pk=customer.pk).update(company_id=company_id)
        customer.company_id = company_id
        done["company"] = company_id
    company_id = customer.company_id

    person = customer_person(customer)
    if person is not None and person.company_id != company_id:
        person = None  # the customer moved tenant: never reuse a party across tenants (3.1)
    if person is None:
        result = find(company_id, email=customer.email)
        if result.state == MATCH_1 and _reusable(result.party, customer):
            person = result.party
            done["person"] = "matched"
        else:
            if result.state == MATCH_N:
                logger.info("customer %s: %d candidate parties; created a new one (merge queue: step 6)",
                            customer.pk, len(result.candidates))
            person = _new_person(company_id, customer.first_name, customer.last_name, customer.email, origin)
            done["person"] = "created"
    _set_person_names(person, customer.first_name, customer.last_name, customer.email)
    mirror_email(person, customer.email, source=customer_source(customer.pk), purpose="billing")
    mirror_phone(person, customer.phone, source=customer_source(customer.pk), purpose="billing",
                 region=_region_for(customer))

    tag = {"metadata__customer": str(customer.pk), "source": CUSTOMER_LINK_SOURCE, "valid_to__isnull": True}
    if is_business(customer):
        name, inferred = organisation_name(customer)
        org = Party.objects.filter(pk=customer.party_id, kind=ORGANISATION, company_id=company_id).first()
        if org is None:
            org = Party.objects.create(
                company_id=company_id, kind=ORGANISATION, display_name=name, origin=origin,
                metadata={"name_inferred": True} if inferred else {},
            )
            done["organisation"] = "created"
        else:
            fields = []
            if not org.display_name_locked and org.display_name != name:
                org.display_name = name
                fields.append("display_name")
            if bool(org.metadata.get("name_inferred")) != inferred:
                org.metadata = {k: v for k, v in org.metadata.items() if k != "name_inferred"}
                if inferred:
                    org.metadata["name_inferred"] = True
                fields.append("metadata")
            if fields:
                org.save(update_fields=fields)
        rel = PartyRelationship.objects.filter(kind=EMPLOYMENT, **tag).first()
        if rel is not None and (rel.from_party_id, rel.to_party_id) != (person.pk, org.pk):
            rel.valid_to = max(_today(), rel.valid_from or _today())
            rel.save(update_fields=["valid_to"])
            rel = None
        if rel is None:
            rel = PartyRelationship.objects.filter(
                from_party=person, to_party=org, kind=EMPLOYMENT, valid_to__isnull=True).first()
            if rel is None:
                PartyRelationship.objects.create(
                    from_party=person, to_party=org, kind=EMPLOYMENT, title=(customer.job_title or "")[:100],
                    is_primary=True, source=CUSTOMER_LINK_SOURCE, metadata={"customer": str(customer.pk)},
                )
        elif rel.title != (customer.job_title or "")[:100]:
            rel.title = (customer.job_title or "")[:100]
            rel.save(update_fields=["title"])
        party = org
    else:
        for rel in PartyRelationship.objects.filter(kind=EMPLOYMENT, **tag):
            rel.valid_to = max(_today(), rel.valid_from or _today())
            rel.save(update_fields=["valid_to"])
        party = person

    holder = person if has_login(customer) else None
    old_party_id = customer.party_id
    updates = {}
    if customer.party_id != party.pk:
        updates["party"] = party
    if customer.account_holder_id != (holder.pk if holder else None):
        updates["account_holder"] = holder
    if updates:
        Customer.objects.filter(pk=customer.pk).update(**updates)
        for name, value in updates.items():
            setattr(customer, name, value)
        done.update({k: getattr(v, "pk", v) for k, v in updates.items()})

    if holder is not None and customer.user_id and holder.user_id != customer.user_id:
        taken = Party.objects.filter(company_id=company_id, user_id=customer.user_id).exclude(pk=holder.pk).exists()
        if holder.user_id is None and not taken:
            holder.user_id = customer.user_id
            holder.save(update_fields=["user"])
            done["login"] = "linked"
        else:
            logger.warning("customer %s: login %s not linked to party %s (conflict)",
                           customer.pk, customer.user_id, holder.pk)

    sync_customer_role(party.pk)
    if old_party_id and old_party_id != party.pk:
        sync_customer_role(old_party_id)
        from customers.models import CustomerAddress

        for address in CustomerAddress.objects.filter(customer_id=customer.pk):
            sync_from_address(address, origin=origin)
    _sync_consent(customer, person, origin)
    _sync_refs(customer, company_id, legacy_refs(customer))
    return done


# --- CustomerContact ---------------------------------------------------------------


def contact_owner_rel(contact):
    """The relationship that marks ``contact`` as the owner of its party's names."""
    from .models import PartyRelationship

    if contact.party_id is None:
        return None
    return PartyRelationship.objects.filter(
        from_party_id=contact.party_id, source=CONTACT_LINK_SOURCE, metadata__contact=str(contact.pk),
    ).order_by("created_at").first()


def sync_from_contact(contact, *, origin: str = "erp") -> dict:
    """2.5: A1 / A2 fold into an existing party; otherwise a person of its own,
    related to ``customer.party`` (``employment`` for a business, else
    ``contact_for``). The contact owns its own party's names."""
    from customers.models import Customer, CustomerContact

    from .models import Party, PartyRelationship

    customer = Customer.objects.get(pk=contact.customer_id)
    if _erased_party(contact.party_id) or _erased_customer(customer):
        return {"skipped": "erased"}
    if customer.party_id is None:
        sync_from_customer(customer, origin=origin)
        customer.refresh_from_db()
        if customer.party_id is None:
            return {"skipped": "customer has no party"}
    done = {}
    email = safe_email(contact.email)
    if contact.party_id is not None and Party.objects.filter(pk=contact.party_id).exclude(
            company_id=customer.company_id).exists():
        contact.party_id = None  # the customer moved tenant: never keep a cross-tenant link (3.1)
        done["relinked"] = "tenant"
    if contact.party_id is None:
        target = None
        person = customer_person(customer)
        if email and person is not None and email == safe_email(customer.email):
            target, done["rule"] = person, "A1"
        elif email:
            for other in CustomerContact.objects.filter(customer=customer, party__isnull=False).exclude(pk=contact.pk):
                if safe_email(other.email) == email:
                    target, done["rule"] = Party.objects.get(pk=other.party_id), "A2"
                    break
        if target is None:
            target = _new_person(customer.company_id, contact.first_name, contact.last_name, contact.email, origin)
            org = Party.objects.get(pk=customer.party_id)
            PartyRelationship.objects.create(
                from_party=target, to_party=org, kind=EMPLOYMENT if org.kind == ORGANISATION else CONTACT_FOR,
                title=(contact.job_title or "")[:100], is_primary=contact.is_primary,
                source=CONTACT_LINK_SOURCE, metadata={"contact": str(contact.pk)},
            )
            done["person"] = "created"
        CustomerContact.objects.filter(pk=contact.pk).update(party=target)
        contact.party = target
    party = Party.objects.get(pk=contact.party_id)
    owner = contact_owner_rel(contact)
    if owner is not None:
        _set_person_names(party, contact.first_name, contact.last_name, contact.email)
        title = (contact.job_title or "")[:100]
        if (owner.title, owner.is_primary) != (title, contact.is_primary):
            owner.title, owner.is_primary = title, contact.is_primary
            owner.save(update_fields=["title", "is_primary"])
    mirror_email(party, contact.email, source=contact_source(contact.pk), purpose="work")
    mirror_phone(party, contact.phone, source=contact_source(contact.pk), purpose="work",
                 region=_region_for(customer))
    return done


# --- CustomerAddress -----------------------------------------------------------------


def sync_from_address(address, *, origin: str = "erp") -> dict:
    """Mirror the postal fields one way onto a PartyAddress on ``customer.party`` (2.5)."""
    from customers.models import Customer, CustomerAddress

    from .models import PartyAddress

    customer = Customer.objects.get(pk=address.customer_id)
    if _erased_customer(customer):
        return {"skipped": "erased"}
    if customer.party_id is None:
        sync_from_customer(customer, origin=origin)
        customer.refresh_from_db()
        if customer.party_id is None:
            return {"skipped": "customer has no party"}
    values = address_values(address)
    pa = PartyAddress.objects.filter(pk=address.party_address_id).first() if address.party_address_id else None
    done = {}
    if pa is None:
        primary = bool(address.is_default) and not PartyAddress.objects.filter(
            party_id=customer.party_id, is_primary=True).exists()
        pa = PartyAddress.objects.create(
            party_id=customer.party_id, is_primary=primary, source=ADDRESS_SOURCE_SYNC, **values,
        )
        CustomerAddress.objects.filter(pk=address.pk).update(party_address=pa)
        address.party_address = pa
        done["party_address"] = "created"
    else:
        changed = [k for k, v in values.items() if getattr(pa, k) != v]
        for k in changed:
            setattr(pa, k, values[k])
        if pa.party_id != customer.party_id:
            pa.party_id = customer.party_id
            pa.is_primary = False
            changed += ["party", "is_primary"]
        if changed:
            pa.save(update_fields=changed)
            done["updated"] = changed
    column, source, object_type = ADDRESS_REF
    value = (address.bc_address_id or "").strip()[:255]
    _sync_refs(address, customer.company_id, [(column, source, object_type, value)] if value else [])
    return done


def delete_orphan_party_address(party_address_id) -> None:
    """A deleted CustomerAddress takes its mirror with it, unless another still uses it."""
    from customers.models import CustomerAddress

    from .models import PartyAddress

    if party_address_id and not CustomerAddress.objects.filter(party_address_id=party_address_id).exists():
        PartyAddress.objects.filter(pk=party_address_id).delete()


# --- Lead -------------------------------------------------------------------------------


def sync_from_lead(lead, *, origin: str = "lead_form") -> dict:
    """2.5: link a lead to its person (match-1), create one (match-0) or leave
    it unlinked (match-N, queued in step 6); keep the ``lead`` role's dates."""
    from crm.models import Lead

    from .models import Party, PartyRole

    if lead.company_id is None:
        return {"skipped": "lead has no company"}
    if _erased_party(lead.party_id) or is_tombstone_email(lead.email, lead.pk):
        return {"skipped": "erased"}
    done = {}
    if lead.party_id is not None and Party.objects.filter(pk=lead.party_id).exclude(
            company_id=lead.company_id).exists():
        lead.party_id = None  # moved tenant (3.1)
        Lead.objects.filter(pk=lead.pk).update(party=None)
    if lead.party_id is None:
        result = find(lead.company_id, email=lead.email, phone=lead.phone, region=lead.country)
        if result.state == MATCH_N:
            return {"skipped": f"ambiguous: {len(result.candidates)} candidates"}
        party = result.party
        if party is None:
            party = _new_person(lead.company_id, lead.first_name, lead.last_name, lead.email, origin)
            mirror_email(party, lead.email, source=f"lead:{lead.pk}", purpose="other")
            mirror_phone(party, lead.phone, source=f"lead:{lead.pk}", purpose="other", region=lead.country)
            done["person"] = "created"
        Lead.objects.filter(pk=lead.pk).update(party=party)
        lead.party = party
        done["party"] = party.pk
    party = Party.objects.get(pk=lead.party_id)
    if party.kind != PERSON:
        return done
    start, end = lead_role_dates(lead)
    role = PartyRole.objects.filter(party=party, role=LEAD_ROLE, metadata__lead=str(lead.pk)).first()
    open_other = PartyRole.objects.filter(party=party, role=LEAD_ROLE, valid_to__isnull=True)
    if role is not None:
        open_other = open_other.exclude(pk=role.pk)
    if role is None:
        if end is not None or not open_other.exists():
            PartyRole.objects.create(party=party, role=LEAD_ROLE, valid_from=start, valid_to=end,
                                     source=LEAD_ROLE_SOURCE, metadata={"lead": str(lead.pk)})
            done["role"] = "opened" if end is None else "closed"
    elif role.valid_to != end and (end is not None or not open_other.exists()):
        role.valid_to = end
        role.save(update_fields=["valid_to"])
        done["role"] = "updated"
    return done


def rename_self_party(company) -> None:
    """2.4: the ``is_self`` party is named after its Company and follows a rename."""
    from .models import Party

    name = (company.name or "")[:255]
    if name:
        Party.objects.filter(company=company, is_self=True, display_name_locked=False).exclude(
            display_name=name).update(display_name=name)
