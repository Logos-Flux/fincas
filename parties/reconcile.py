"""Drift between the commerce rows and their parties (phase2-party.md 2.1, 7.1).

``check()`` compares every Customer, CustomerContact, CustomerAddress and Lead
with what ``parties.sync`` would have written, plus each party's
denormalised primary email and phone. It reads in bulk (a constant number of
queries per table, not per row), so it runs at 10k rows.

An ``Issue`` is one of:

- ``drift``: the sync would change something. ``--check`` exits 1 on any;
  ``--fix`` re-runs the sync for that row.
- ``conflict``: the data needs a person (a duplicate legacy id, a login
  already on another party, a lead with several candidates). Reported, never
  "fixed", and not a ``--check`` failure: the merge queue (step 6) owns them.
- ``pending``: a backfill-only fact is missing (a company's self party, a
  staff party). ``--fix`` runs ``backfill_parties`` steps 2 and 6 for them.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from django.db.models import F, OuterRef, Q, Subquery, Value
from django.db.models.functions import Coalesce

from .constants import ORGANISATION, PERSON
from .erasure import is_erased_customer
from .match import MATCH_N, find
from .mirror import (
    ADDRESS_REF,
    CUSTOMER_LINK_SOURCE,
    address_values,
    default_company_id,
    has_login,
    is_business,
    legacy_refs,
    organisation_name,
    person_names,
    phone_parts,
    safe_email,
)
from .relationship_registry import EMPLOYMENT
from .role_registry import CUSTOMER_ROLE

DRIFT, CONFLICT, PENDING = "drift", "conflict", "pending"


@dataclass(frozen=True)
class Issue:
    level: str
    model: str  # customer / contact / address / lead / party / company / profile
    pk: str
    what: str

    def __str__(self):
        return f"{self.level:<8} {self.model:<8} {self.pk}  {self.what}"


def _company_filter(company, field="company"):
    return Q(**{field: company}) if company is not None else Q()


def check(company=None) -> list[Issue]:
    from django.contrib.contenttypes.models import ContentType

    from core.models import Company, ExternalRef, UserProfile
    from crm.models import Lead
    from customers.models import Customer, CustomerAddress, CustomerContact

    from .models import ConsentRecord, Party, PartyAddress, PartyEmail, PartyPhone, PartyRelationship, PartyRole

    issues: list[Issue] = []

    def add(level, model, pk, what):
        issues.append(Issue(level, model, str(pk), what))

    parties = {p.pk: p for p in Party.objects.filter(_company_filter(company))}
    emails = defaultdict(set)
    for pid, e in PartyEmail.objects.filter(_company_filter(company)).values_list("party_id", "email_normalized"):
        emails[pid].add(e)
    phones = defaultdict(set)
    for pid, raw, e164 in PartyPhone.objects.filter(_company_filter(company)).values_list(
            "party_id", "number_raw", "number_e164"):
        phones[pid] |= {raw, e164} - {""}
    open_customer_role = set(PartyRole.objects.filter(
        _company_filter(company), role=CUSTOMER_ROLE, valid_to__isnull=True).values_list("party_id", flat=True))
    tagged = {}
    for meta, from_id, to_id in PartyRelationship.objects.filter(
        _company_filter(company), source=CUSTOMER_LINK_SOURCE, kind=EMPLOYMENT, valid_to__isnull=True,
    ).values_list("metadata", "from_party_id", "to_party_id"):
        if isinstance(meta, dict) and meta.get("customer"):
            tagged[meta["customer"]] = (from_id, to_id)
    users_in_company = defaultdict(set)
    for co, uid, pid in Party.objects.filter(_company_filter(company), user__isnull=False).values_list(
            "company_id", "user_id", "pk"):
        users_in_company[(co, uid)].add(pid)
    latest_consent = {}
    for pid, state, addr in ConsentRecord.objects.filter(
        _company_filter(company), channel="email", purpose="marketing", party__isnull=False,
    ).order_by("party_id", "occurred_at", "recorded_at").values_list("party_id", "state", "address_normalized"):
        latest_consent.setdefault(pid, []).append((state, addr))
    ct_customer = ContentType.objects.get_for_model(Customer)
    ct_address = ContentType.objects.get_for_model(CustomerAddress)
    refs = {
        (co, s, t, i): (ct, oid) for co, s, t, i, ct, oid in ExternalRef.objects.filter(
            _company_filter(company)).values_list(
            "company_id", "source", "object_type", "source_id", "content_type_id", "object_id")
    }
    region = {}
    for cid, country in CustomerAddress.objects.order_by("customer_id", "-is_default", "created_at").values_list(
            "customer_id", "country"):
        region.setdefault(cid, country)

    def ref_issue(model, obj_pk, ct, company_id, wanted):
        for _column, source, object_type, value in wanted:
            found = refs.get((company_id, source, object_type, value))
            if found is None:
                add(DRIFT, model, obj_pk, f"no ExternalRef {source}:{object_type}:{value}")
            elif found != (ct.pk, obj_pk):
                add(CONFLICT, model, obj_pk, f"ExternalRef {source}:{object_type}:{value} targets another row")

    default_cid = None
    customers = Customer.objects.all()
    if company is not None:
        customers = customers.filter(company=company)
    elif Customer.objects.filter(company__isnull=True).exists():
        default_cid = default_company_id(Company)
        if default_cid is None:
            for pk in Customer.objects.filter(company__isnull=True).values_list("pk", flat=True):
                add(CONFLICT, "customer", pk, "no company and no default tenant (GOLDEN_PORTAL_COMPANY)")
        customers = customers.filter(company__isnull=False) if default_cid is None else customers

    customer_email = {}
    customer_person_id = {}
    for c in customers.iterator(chunk_size=2000):
        if is_erased_customer(c):
            continue  # a tombstone: erasure removed what the sync would mirror (7.4)
        if c.company_id is None:
            add(DRIFT, "customer", c.pk, "no company (the default tenant applies)")
            continue
        party = parties.get(c.party_id)
        if party is None:
            add(DRIFT, "customer", c.pk, "no party")
            continue
        if c.account_holder_id:
            person = parties.get(c.account_holder_id)
        elif party.kind == PERSON:
            person = party
        else:
            pair = tagged.get(str(c.pk))
            person = parties.get(pair[0]) if pair and pair[1] == party.pk else None
        if person is None:
            add(DRIFT, "customer", c.pk, "no person party")
            continue
        customer_person_id[c.pk] = person.pk
        if party.company_id != c.company_id or person.company_id != c.company_id:
            add(DRIFT, "customer", c.pk, "party belongs to another company")
        if is_business(c) != (party.kind == ORGANISATION):
            add(DRIFT, "customer", c.pk, f"party is a {party.kind} for a {c.customer_type} customer")
        if has_login(c) != (c.account_holder_id is not None):
            add(DRIFT, "customer", c.pk, "account holder does not follow the portal login")
        if c.account_holder_id and c.user_id and person.user_id != c.user_id:
            others = users_in_company[(c.company_id, c.user_id)] - {person.pk}
            if person.user_id is None and not others:
                add(DRIFT, "customer", c.pk, "account holder party is not linked to the customer's login")
            else:
                add(CONFLICT, "customer", c.pk, "the customer's login is already linked to another party")
        if (person.given_name, person.family_name) != person_names(c.first_name, c.last_name):
            add(DRIFT, "customer", c.pk, "person name differs")
        email = safe_email(c.email)
        customer_email[c.pk] = email
        if email and email not in emails[person.pk]:
            add(DRIFT, "customer", c.pk, "email missing on the person party")
        raw, e164, _ = phone_parts(c.phone, region.get(c.pk))
        if raw and not ({raw, e164} - {""}) & phones[person.pk]:
            add(DRIFT, "customer", c.pk, "phone missing on the person party")
        if party.kind == ORGANISATION and not party.display_name_locked and party.display_name != organisation_name(c)[0]:
            add(DRIFT, "customer", c.pk, "organization name differs")
        if c.is_active and party.pk not in open_customer_role:
            add(DRIFT, "customer", c.pk, "no open customer role on the party")
        history = [s for s, a in latest_consent.get(person.pk, []) if a in ("", email)]
        granted = bool(history) and history[-1] == ConsentRecord.State.GRANTED
        if bool(c.accepts_marketing) != granted and (c.accepts_marketing or history):
            add(DRIFT, "customer", c.pk, "marketing consent does not follow accepts_marketing")
        ref_issue("customer", c.pk, ct_customer, c.company_id, legacy_refs(c))

    active_parties = set(Customer.objects.filter(is_active=True, party__isnull=False).values_list("party_id", flat=True))
    for pid in open_customer_role - active_parties:
        add(DRIFT, "party", pid, "customer role open but no active customer")

    contacts = CustomerContact.objects.select_related("customer")
    if company is not None:
        contacts = contacts.filter(customer__company=company)
    for ct in contacts.iterator(chunk_size=2000):
        if ct.customer.party_id is None:
            continue  # reported on the customer
        party = parties.get(ct.party_id)
        if is_erased_customer(ct.customer) or (party is not None and party.erased_at is not None):
            continue
        if party is None:
            add(DRIFT, "contact", ct.pk, "no party")
            continue
        if party.company_id != ct.customer.company_id:
            add(DRIFT, "contact", ct.pk, "party belongs to another company")
        email = safe_email(ct.email)
        if email and email not in emails[party.pk]:
            add(DRIFT, "contact", ct.pk, "email missing on the party")
        raw, e164, _ = phone_parts(ct.phone, region.get(ct.customer_id))
        if raw and not ({raw, e164} - {""}) & phones[party.pk]:
            add(DRIFT, "contact", ct.pk, "phone missing on the party")

    party_addresses = {pa.pk: pa for pa in PartyAddress.objects.filter(_company_filter(company))}
    addresses = CustomerAddress.objects.select_related("customer")
    if company is not None:
        addresses = addresses.filter(customer__company=company)
    for a in addresses.iterator(chunk_size=2000):
        if a.customer.party_id is None or is_erased_customer(a.customer):
            continue
        pa = party_addresses.get(a.party_address_id)
        if pa is None:
            add(DRIFT, "address", a.pk, "no party address")
            continue
        if pa.party_id != a.customer.party_id:
            add(DRIFT, "address", a.pk, "party address is on another party")
        if any(getattr(pa, k) != v for k, v in address_values(a).items()):
            add(DRIFT, "address", a.pk, "party address fields differ")
        if (a.bc_address_id or "").strip() and a.customer.company_id:
            column, source, object_type = ADDRESS_REF
            ref_issue("address", a.pk, ct_address, a.customer.company_id,
                      [(column, source, object_type, a.bc_address_id.strip()[:255])])

    moved = Lead.objects.filter(company__isnull=False, party__isnull=False).exclude(party__company=F("company"))
    if company is not None:
        moved = moved.filter(company=company)
    for pk in moved.values_list("pk", flat=True):
        add(DRIFT, "lead", pk, "party belongs to another company")
    leads = Lead.objects.filter(company__isnull=False, party__isnull=True)
    if company is not None:
        leads = leads.filter(company=company)
    for lead in leads.iterator(chunk_size=2000):
        result = find(lead.company_id, email=lead.email, phone=lead.phone, region=lead.country)
        if result.state == MATCH_N:
            add(CONFLICT, "lead", lead.pk, f"{len(result.candidates)} candidate parties (merge queue)")
        else:
            add(DRIFT, "lead", lead.pk, "no party")

    primary_email = PartyEmail.objects.filter(party=OuterRef("pk"), is_primary=True).values("email_normalized")[:1]
    primary_phone = PartyPhone.objects.filter(party=OuterRef("pk"), is_primary=True).values("number_e164")[:1]
    for pid, pe, pp, have_e, have_p in Party.objects.filter(_company_filter(company)).annotate(
        pe=Coalesce(Subquery(primary_email), Value("")), pp=Coalesce(Subquery(primary_phone), Value("")),
    ).values_list("pk", "pe", "pp", "primary_email_normalized", "primary_phone_e164"):
        if (pe, pp) != (have_e, have_p):
            add(DRIFT, "party", pid, "primary email/phone columns are stale")

    companies = Company.objects.all() if company is None else Company.objects.filter(pk=company.pk)
    has_self = set(Party.objects.filter(is_self=True).values_list("company_id", flat=True))
    for co in companies:
        if co.pk not in has_self:
            add(PENDING, "company", co.pk, "no self party (backfill_parties --step 2)")
    linked = set(Party.objects.filter(user__isnull=False).values_list("company_id", "user_id"))
    profiles = UserProfile.objects.filter(user__isnull=False, company__isnull=False)
    if company is not None:
        profiles = profiles.filter(company=company)
    for pk, co, uid in profiles.values_list("pk", "company_id", "user_id"):
        if (co, uid) not in linked:
            add(PENDING, "profile", pk, "no staff party (backfill_parties --step 6)")
    return issues


def fix(issues: list[Issue]) -> dict:
    """Re-run the sync for every drifting row; run the backfill for pending facts."""
    from django.apps import apps

    from crm.models import Lead
    from customers.models import Customer, CustomerAddress, CustomerContact

    from . import backfill, sync
    from .models import Party

    done = defaultdict(int)
    todo = defaultdict(set)
    for issue in issues:
        if issue.level == DRIFT:
            todo[issue.model].add(issue.pk)
        elif issue.level == PENDING:
            todo[f"pending:{issue.model}"].add(issue.pk)
    for pk in sorted(todo["customer"]):
        sync.sync_from_customer(Customer.objects.get(pk=pk))
        done["customers"] += 1
    for pk in sorted(todo["contact"]):
        sync.sync_from_contact(CustomerContact.objects.get(pk=pk))
        done["contacts"] += 1
    for pk in sorted(todo["address"]):
        sync.sync_from_address(CustomerAddress.objects.get(pk=pk))
        done["addresses"] += 1
    for pk in sorted(todo["lead"]):
        sync.sync_from_lead(Lead.objects.get(pk=pk))
        done["leads"] += 1
    for pk in sorted(todo["party"]):
        party = Party.objects.filter(pk=pk).first()
        if party is not None:
            party.refresh_primary_contact()
            sync.sync_customer_role(party.pk)
            done["parties"] += 1
    if todo["pending:company"]:
        done["self parties"] += backfill.d2_self_parties(apps).get("self parties created", 0)
    if todo["pending:profile"]:
        done["staff parties"] += backfill.d6_staff_parties(apps).get("staff parties", 0)
    return dict(done)
