"""Party backfill, data steps D2-D10 (docs/crm/phase2-party.md section 4).

Every step is a pair of plain functions taking an ``apps`` registry, used by
the data migration ``parties.0002_backfill`` and by ``manage.py
backfill_parties --step <n>``. Both run on *historical* models (the command
loads the migration state), so the two paths cannot drift apart.

Rules every step keeps:

- **No signals.** Rows are written with ``bulk_create`` / ``bulk_update`` /
  ``QuerySet.update`` only, so no plugin signal (``customer_registered``,
  later ``party_created``) and no ``post_save`` receiver fires, and a cutover
  sends no flood.
- **Idempotent.** A source row whose link FK is already set is skipped, so a
  re-run after a partial failure continues where it stopped.
- **Reversible.** The reverse deletes only what the forward pass wrote:
  parties with ``origin`` ``backfill`` / ``system``, rows tagged
  ``metadata.backfill``, and link FKs pointing at them. Parties made since by
  hand or by the live sync survive (they lose only their link FKs, and only
  if the schema migration is reversed too).
- **Tenant-bound.** Every party and child row carries the company of the
  customer / contact / lead it came from (3.1).

"Account holder" means a real portal login; see ``parties.mirror``.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from django.db.models import Q

from .constants import ORGANISATION, PERSON
from .mirror import (
    ADDRESS_REF,
    ADDRESS_SOURCE_BACKFILL,
    CONTACT_LINK_SOURCE,
    CUSTOMER_LINK_SOURCE,
    CUSTOMER_ROLE_SOURCE,
    LEAD_ROLE_SOURCE,
    MARKETABLE_SOURCE,
    STAFF_LINK_SOURCE,
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
    user_source,
)
from .normalize import phone_line_type
from .relationship_registry import CONTACT_FOR, EMPLOYMENT
from .role_registry import CUSTOMER_ROLE, LEAD_ROLE, STAFF_ROLE

CHUNK = 2000
BATCH = 1000
BACKFILL = "backfill"
SYSTEM = "system"
PREVIEW = 20  # ids listed per problem in the summary lines


class BackfillRefused(RuntimeError):
    """The data cannot be backfilled as is; the message lists what to fix."""


def _refuse_if_tightened(m) -> None:
    """D3/D4 reverses NULL links that CRM Phase 2 step 11 made NOT NULL.

    Asks the database, not the model: tests run the reverses on a relaxed
    schema under the live (NOT NULL) models.
    """
    from django.db import connection

    def nullable(model, column):
        with connection.cursor() as cursor:
            rows = connection.introspection.get_table_description(cursor, model._meta.db_table)
        return next(r for r in rows if r.name == column).null_ok

    if not nullable(m.Customer, "party_id") or not nullable(m.CustomerContact, "party_id"):
        raise BackfillRefused(
            "the party links are NOT NULL since step 11 (D13): unapply crm 0014_tighten and "
            "customers 0007_tighten first (`migrate crm 0013 && migrate customers 0006`)"
        )


class _Models:
    def __init__(self, apps):
        get = apps.get_model
        self.Company = get("core", "Company")
        self.UserProfile = get("core", "UserProfile")
        self.ExternalRef = get("core", "ExternalRef")
        self.ContentType = get("contenttypes", "ContentType")
        self.Customer = get("customers", "Customer")
        self.CustomerContact = get("customers", "CustomerContact")
        self.CustomerAddress = get("customers", "CustomerAddress")
        self.Lead = get("crm", "Lead")
        self.Party = get("parties", "Party")
        self.PartyRole = get("parties", "PartyRole")
        self.PartyRelationship = get("parties", "PartyRelationship")
        self.PartyEmail = get("parties", "PartyEmail")
        self.PartyPhone = get("parties", "PartyPhone")
        self.PartyAddress = get("parties", "PartyAddress")
        self.ConsentRecord = get("parties", "ConsentRecord")
        self.User = get("auth", "User")

    def q(self, model):
        return model._base_manager


def _chunks(iterable, size=BATCH):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _ids(values) -> str:
    values = [str(v) for v in values]
    more = f" (+{len(values) - PREVIEW} more)" if len(values) > PREVIEW else ""
    return ", ".join(values[:PREVIEW]) + more


class _Writer:
    """Collects new rows and writes them in dependency order with bulk_create."""

    def __init__(self, m: _Models):
        self.m = m
        self.parties, self.emails, self.phones = [], [], []
        self.roles, self.rels, self.addresses = [], [], []
        self.primary_email: dict = {}
        self.primary_phone: dict = {}

    def party(self, **kwargs):
        p = self.m.Party(**kwargs)
        self.parties.append(p)
        return p

    def email(self, party, raw, normalized, *, purpose, source, primary):
        self.emails.append(self.m.PartyEmail(
            party_id=party.pk, company_id=party.company_id, email=raw.strip()[:254],
            email_normalized=normalized, purpose=purpose, is_primary=primary, source=source,
        ))
        if primary:
            party.primary_email_normalized = normalized

    def phone(self, party, raw, e164, ext, *, purpose, source, primary):
        self.phones.append(self.m.PartyPhone(
            party_id=party.pk, company_id=party.company_id, number_raw=raw, number_e164=e164,
            extension=ext, line_type=phone_line_type(e164), purpose=purpose, is_primary=primary,
            source=source,
        ))
        if primary:
            party.primary_phone_e164 = e164

    def role(self, party, role, valid_from, valid_to=None, source="", metadata=None):
        self.roles.append(self.m.PartyRole(
            party_id=party.pk, company_id=party.company_id, role=role, valid_from=valid_from,
            valid_to=valid_to, source=source, metadata=metadata or {},
        ))

    def rel(self, from_party, to_party, kind, *, title="", is_primary=False, source="", metadata=None):
        self.rels.append(self.m.PartyRelationship(
            from_party_id=from_party.pk, to_party_id=to_party.pk, company_id=from_party.company_id,
            kind=kind, is_symmetric=False, title=(title or "")[:100], is_primary=is_primary,
            source=source, metadata=metadata or {},
        ))

    def flush(self):
        q = self.m.q
        for model, rows in (
            (self.m.Party, self.parties), (self.m.PartyEmail, self.emails),
            (self.m.PartyPhone, self.phones), (self.m.PartyRole, self.roles),
            (self.m.PartyRelationship, self.rels), (self.m.PartyAddress, self.addresses),
        ):
            if rows:
                q(model).bulk_create(rows, batch_size=BATCH)
            rows.clear()


def _refresh_primaries(m: _Models, party_ids) -> None:
    """Re-derive ``primary_email_normalized`` / ``primary_phone_e164`` (1.1)."""
    for pid in set(party_ids):
        email = m.q(m.PartyEmail).filter(party_id=pid, is_primary=True).values_list(
            "email_normalized", flat=True).first() or ""
        phone = m.q(m.PartyPhone).filter(party_id=pid, is_primary=True).values_list(
            "number_e164", flat=True).first() or ""
        m.q(m.Party).filter(pk=pid).update(primary_email_normalized=email, primary_phone_e164=phone)


def customer_regions(m: _Models, customer_ids=None) -> dict:
    """Phone region per customer: its default address's country, else its first (7.3)."""
    qs = m.q(m.CustomerAddress).all()
    if customer_ids is not None:
        qs = qs.filter(customer_id__in=list(customer_ids))
    region = {}
    for cid, country in qs.order_by("customer_id", "-is_default", "created_at").values_list(
        "customer_id", "country",
    ):
        region.setdefault(cid, (country or "").strip().upper())
    return region


def customer_persons(m: _Models) -> dict:
    """``{customer_id: party_id}`` of each customer's person party.

    The account holder when set; else the party itself when it is a person;
    else the person tied to the business organisation by the ``employment``
    relationship tagged with the customer (``source=customer-sync``).
    """
    out = {}
    tagged = {}
    for meta, from_id in m.q(m.PartyRelationship).filter(
        source=CUSTOMER_LINK_SOURCE, kind=EMPLOYMENT, valid_to__isnull=True,
    ).values_list("metadata", "from_party_id"):
        if isinstance(meta, dict) and meta.get("customer"):
            tagged[meta["customer"]] = from_id
    for cid, party_id, party_kind, holder_id in m.q(m.Customer).filter(party__isnull=False).values_list(
        "pk", "party_id", "party__kind", "account_holder_id",
    ):
        if holder_id:
            out[cid] = holder_id
        elif party_kind == PERSON:
            out[cid] = party_id
        elif str(cid) in tagged:
            out[cid] = tagged[str(cid)]
    return out


def _deletable(m: _Models, **filters) -> int:
    """Delete backfilled parties matching ``filters`` that nothing commercial
    points at (the live sync may since have adopted one as a customer's
    person). Returns the number of parties deleted."""
    q = m.q
    used = set(q(m.Customer).filter(party__isnull=False).values_list("party_id", flat=True))
    used |= set(q(m.Customer).filter(account_holder__isnull=False).values_list("account_holder_id", flat=True))
    used |= set(q(m.CustomerContact).filter(party__isnull=False).values_list("party_id", flat=True))
    used |= set(customer_persons(m).values())  # a business customer's person (tagged employment)
    ids = [pk for pk in q(m.Party).filter(origin=BACKFILL, **filters).values_list("pk", flat=True) if pk not in used]
    for pids in _chunks(ids):
        q(m.Lead).filter(party_id__in=pids).update(party=None)
        q(m.Party).filter(pk__in=pids).delete()
    return len(ids)


# --- D2 self parties ---------------------------------------------------------


def d2_self_parties(apps) -> dict:
    m = _Models(apps)
    have = set(m.q(m.Party).filter(is_self=True).values_list("company_id", flat=True))
    w = _Writer(m)
    for company in m.q(m.Company).exclude(pk__in=have).order_by("created_at", "pk"):
        w.party(
            company_id=company.pk, kind=ORGANISATION, display_name=(company.name or "Our company")[:255],
            is_self=True, origin=SYSTEM, metadata={"backfill": {"step": 2}},
        )
    created = len(w.parties)
    w.flush()
    return {"self parties created": created}


def d2_reverse(apps) -> dict:
    m = _Models(apps)
    n, _ = m.q(m.Party).filter(origin=SYSTEM, is_self=True).delete()
    return {"rows deleted": n}


# --- D3 customer parties -------------------------------------------------------


def d3_customer_parties(apps) -> dict:
    m = _Models(apps)
    stats = Counter()
    invalid_email, unparseable = [], []
    pending = m.q(m.Customer).filter(party__isnull=True)
    default_cid = None
    if pending.filter(company__isnull=True).exists():
        default_cid = default_company_id(m.Company)
        if default_cid is None:
            orphans = list(pending.filter(company__isnull=True).values_list("pk", flat=True))
            if m.q(m.Company).count() > 1:
                raise BackfillRefused(
                    f"{len(orphans)} customer(s) have no company and more than one Company exists. "
                    "Set GOLDEN_PORTAL_COMPANY, or run `manage.py backfill_tenancy --also "
                    "customers.Customer` first. Customers: " + _ids(orphans)
                )
            stats["skipped: no Company exists"] = len(orphans)

    region = customer_regions(m)
    rows = pending if default_cid is not None else pending.filter(company__isnull=False)
    rows = rows.order_by("created_at", "pk").iterator(chunk_size=CHUNK)
    for batch in _chunks(rows):
        w = _Writer(m)
        updated = []
        for c in batch:
            stats["customers"] += 1
            company_id = c.company_id or default_cid
            meta = {"customer": str(c.pk)}
            if c.company_id is None:
                meta["assigned_company"] = True
                stats["NULL-company customers assigned"] += 1
            given, family = person_names(c.first_name, c.last_name)
            person = w.party(
                company_id=company_id, kind=PERSON, given_name=given, family_name=family,
                display_name=person_display(c.first_name, c.last_name, c.email), origin=BACKFILL,
                metadata={"backfill": meta},
            )
            stats["person parties"] += 1
            email = safe_email(c.email)
            if email:
                w.email(person, c.email, email, purpose="billing", source=customer_source(c.pk), primary=True)
            elif (c.email or "").strip():
                invalid_email.append(c.pk)
            raw, e164, ext = phone_parts(c.phone, region.get(c.pk))
            if raw:
                w.phone(person, raw, e164, ext, purpose="billing", source=customer_source(c.pk), primary=bool(e164))
                if not e164:
                    unparseable.append(c.pk)
            party = person
            if is_business(c):
                name, inferred = organisation_name(c)
                org_meta = {"backfill": {"customer": str(c.pk)}}
                if inferred:
                    org_meta["name_inferred"] = True
                    stats["organizations named after their person"] += 1
                party = w.party(
                    company_id=company_id, kind=ORGANISATION, display_name=name, origin=BACKFILL,
                    metadata=org_meta,
                )
                stats["organization parties"] += 1
                w.rel(person, party, EMPLOYMENT, title=c.job_title, is_primary=True,
                      source=CUSTOMER_LINK_SOURCE, metadata={"customer": str(c.pk), "backfill": True})
            start = c.created_at.date()
            end = None if c.is_active else max(start, c.updated_at.date())
            w.role(party, CUSTOMER_ROLE, start, end, source=CUSTOMER_ROLE_SOURCE,
                   metadata={"customer": str(c.pk), "backfill": True})
            c.party_id = party.pk
            c.company_id = company_id
            if has_login(c):
                c.account_holder_id = person.pk
                stats["account holders (portal login)"] += 1
            updated.append(c)
        w.flush()
        m.q(m.Customer).bulk_update(updated, ["party", "account_holder", "company"], batch_size=BATCH)
    if invalid_email:
        stats["invalid emails (no PartyEmail)"] = len(invalid_email)
    if unparseable:
        stats["unparseable phones (raw kept)"] = len(unparseable)
    return dict(stats) | ({"invalid email customers": _ids(invalid_email)} if invalid_email else {})


def d3_reverse(apps) -> dict:
    """Null every link to a backfilled party, restore NULL companies, delete the parties."""
    m = _Models(apps)
    _refuse_if_tightened(m)
    backfilled = m.q(m.Party).filter(origin=BACKFILL)
    restore = [
        meta["backfill"]["customer"]
        for meta in backfilled.filter(kind=PERSON).values_list("metadata", flat=True)
        if isinstance(meta, dict) and (meta.get("backfill") or {}).get("assigned_company")
    ]
    ids = list(backfilled.values_list("pk", flat=True))
    q = m.q
    for pids in _chunks(ids):
        q(m.Customer).filter(party_id__in=pids).update(party=None)
        q(m.Customer).filter(account_holder_id__in=pids).update(account_holder=None)
        q(m.CustomerContact).filter(party_id__in=pids).update(party=None)
        q(m.CustomerAddress).filter(party_address__party_id__in=pids).update(party_address=None)
        q(m.Lead).filter(party_id__in=pids).update(party=None)
        q(m.ConsentRecord).filter(party_id__in=pids, source=BACKFILL).delete()
    for cids in _chunks(restore):
        q(m.Customer).filter(pk__in=cids).update(company=None)
    deleted = 0
    for pids in _chunks(ids):
        n, _ = q(m.Party).filter(pk__in=pids).delete()
        deleted += n
    return {"parties deleted (with their rows)": deleted, "customer companies reset to NULL": len(restore)}


# --- D4 contacts ---------------------------------------------------------------


def d4_contact_parties(apps) -> dict:
    m = _Models(apps)
    q = m.q
    stats = Counter()
    persons = customer_persons(m)
    customers = {
        c.pk: c for c in q(m.Customer).filter(party__isnull=False).only(
            "pk", "customer_type", "email", "party_id", "company_id")
    }
    customer_email = {cid: safe_email(c.email) for cid, c in customers.items()}
    region = customer_regions(m)
    # A2 across runs: contacts already linked, by (customer, normalised email).
    shared: dict = {}
    for cid, email, pid in q(m.CustomerContact).filter(party__isnull=False).values_list(
        "customer_id", "email", "party_id",
    ):
        key = safe_email(email)
        if key:
            shared.setdefault((cid, key), pid)
    parties = {}  # party_id -> Party (only those the run touches)
    known_phones: dict = defaultdict(set)
    known_emails: dict = defaultdict(set)
    primary_email, primary_phone = set(), set()  # parties that already have a primary row
    touched = set()

    def load(pid):
        if pid not in parties:
            parties[pid] = q(m.Party).get(pk=pid)
            known_phones[pid] |= {
                e or r for e, r in q(m.PartyPhone).filter(party_id=pid).values_list("number_e164", "number_raw")}
            known_emails[pid] |= set(q(m.PartyEmail).filter(party_id=pid).values_list("email_normalized", flat=True))
            if q(m.PartyEmail).filter(party_id=pid, is_primary=True).exists():
                primary_email.add(pid)
            if q(m.PartyPhone).filter(party_id=pid, is_primary=True).exists():
                primary_phone.add(pid)
        return parties[pid]

    rows = q(m.CustomerContact).filter(party__isnull=True, customer__party__isnull=False)
    for batch in _chunks(rows.order_by("created_at", "pk").iterator(chunk_size=CHUNK)):
        w = _Writer(m)
        updated = []
        for ct in batch:
            stats["contacts"] += 1
            c = customers[ct.customer_id]
            email = safe_email(ct.email)
            if (ct.email or "").strip() and not email:
                stats["invalid emails (no PartyEmail)"] += 1
            target_id, rule = None, ""
            if email and email == customer_email.get(c.pk) and c.pk in persons:
                target_id, rule = persons[c.pk], "A1"
            elif email and (c.pk, email) in shared:
                target_id, rule = shared[(c.pk, email)], "A2"
            if target_id is not None:
                stats[f"folded by {rule}"] += 1
                target = parties.get(target_id) or load(target_id)
            else:
                given, family = person_names(ct.first_name, ct.last_name)
                target = w.party(
                    company_id=c.company_id, kind=PERSON, given_name=given, family_name=family,
                    display_name=person_display(ct.first_name, ct.last_name, ct.email), origin=BACKFILL,
                    metadata={"backfill": {"contact": str(ct.pk)}},
                )
                parties[target.pk] = target
                stats["person parties"] += 1
                org = q(m.Party).get(pk=c.party_id) if c.party_id not in parties else parties[c.party_id]
                parties[org.pk] = org
                kind = EMPLOYMENT if c.customer_type == "business" else CONTACT_FOR
                w.rel(target, org, kind, title=ct.job_title, is_primary=ct.is_primary,
                      source=CONTACT_LINK_SOURCE, metadata={"contact": str(ct.pk), "backfill": True})
                if email:
                    shared[(c.pk, email)] = target.pk
            in_db = target.pk not in {p.pk for p in w.parties}
            if email and email not in known_emails[target.pk]:
                primary = target.pk not in primary_email
                if primary:
                    primary_email.add(target.pk)
                w.email(target, ct.email, email, purpose="work", source=contact_source(ct.pk), primary=primary)
                known_emails[target.pk].add(email)
                if in_db:
                    touched.add(target.pk)
            raw, e164, ext = phone_parts(ct.phone, region.get(c.pk))
            if raw and (e164 or raw) not in known_phones[target.pk]:
                primary = bool(e164) and target.pk not in primary_phone
                if primary:
                    primary_phone.add(target.pk)
                w.phone(target, raw, e164, ext, purpose="work", source=contact_source(ct.pk), primary=primary)
                known_phones[target.pk].add(e164 or raw)
                if not e164:
                    stats["unparseable phones (raw kept)"] += 1
                if in_db:
                    touched.add(target.pk)
            ct.party_id = target.pk
            updated.append(ct)
        w.flush()
        q(m.CustomerContact).bulk_update(updated, ["party"], batch_size=BATCH)
    _refresh_primaries(m, touched)
    return dict(stats)


def d4_reverse(apps) -> dict:
    m = _Models(apps)
    _refuse_if_tightened(m)
    n = m.q(m.CustomerContact).filter(party__origin=BACKFILL).update(party=None)
    # Rows the forward pass added to customer persons (A1/A2 folds); the
    # contact-born parties themselves go with D3's reverse.
    removed = 0
    for model in (m.PartyEmail, m.PartyPhone):
        k, _ = m.q(model).filter(source__startswith="contact:", party__origin=BACKFILL).delete()
        removed += k
    rels, _ = m.q(m.PartyRelationship).filter(source=CONTACT_LINK_SOURCE, metadata__backfill=True).delete()
    parties = _deletable(m, metadata__backfill__has_key="contact")
    return {"contact links nulled": n, "contact emails/phones deleted": removed, "relationships deleted": rels,
            "contact parties deleted": parties}


# --- D5 addresses -------------------------------------------------------------


def d5_addresses(apps) -> dict:
    m = _Models(apps)
    q = m.q
    stats = Counter()
    has_primary = set(q(m.PartyAddress).filter(is_primary=True).values_list("party_id", flat=True))
    company_of = dict(q(m.Party).values_list("pk", "company_id"))
    rows = q(m.CustomerAddress).filter(party_address__isnull=True, customer__party__isnull=False).select_related(
        "customer").order_by("customer_id", "-is_default", "created_at", "pk")
    for batch in _chunks(rows.iterator(chunk_size=CHUNK)):
        made, updated = [], []
        for a in batch:
            pid = a.customer.party_id
            primary = bool(a.is_default) and pid not in has_primary
            if primary:
                has_primary.add(pid)
            pa = m.PartyAddress(
                party_id=pid, company_id=company_of[pid], is_primary=primary, source=ADDRESS_SOURCE_BACKFILL,
                **address_values(a),
            )
            made.append(pa)
            a.party_address_id = pa.pk
            updated.append(a)
            stats["addresses"] += 1
        q(m.PartyAddress).bulk_create(made, batch_size=BATCH)
        q(m.CustomerAddress).bulk_update(updated, ["party_address"], batch_size=BATCH)
    return dict(stats)


def d5_reverse(apps) -> dict:
    m = _Models(apps)
    mine = m.q(m.CustomerAddress).filter(party_address__source=ADDRESS_SOURCE_BACKFILL)
    linked = list(mine.values_list("party_address_id", flat=True))
    n = mine.update(party_address=None)
    deleted = 0
    for ids in _chunks(linked):
        k, _ = m.q(m.PartyAddress).filter(pk__in=ids).delete()
        deleted += k
    return {"address links nulled": n, "party addresses deleted": deleted}


# --- D6 staff ---------------------------------------------------------------------


def d6_staff_parties(apps) -> dict:
    m = _Models(apps)
    q = m.q
    stats = Counter()
    self_party = {p.company_id: p for p in q(m.Party).filter(is_self=True)}
    linked = set(q(m.Party).filter(user__isnull=False).values_list("company_id", "user_id"))
    w = _Writer(m)
    for profile in q(m.UserProfile).filter(user__isnull=False, company__isnull=False).select_related(
        "user").order_by("created_at", "pk"):
        if (profile.company_id, profile.user_id) in linked:
            continue
        u = profile.user
        given, family = person_names(u.first_name, u.last_name)
        p = w.party(
            company_id=profile.company_id, kind=PERSON, given_name=given, family_name=family,
            display_name=person_display(u.first_name, u.last_name, profile.display_name or u.username),
            user_id=u.pk, origin=BACKFILL, metadata={"backfill": {"profile": str(profile.pk)}},
        )
        linked.add((profile.company_id, u.pk))
        stats["staff parties"] += 1
        email = safe_email(u.email)
        if email:
            w.email(p, u.email, email, purpose="work", source=user_source(u.pk), primary=True)
        w.role(p, STAFF_ROLE, profile.created_at.date(), source=STAFF_LINK_SOURCE, metadata={"backfill": True})
        us = self_party.get(profile.company_id)
        if us is not None:
            w.rel(p, us, EMPLOYMENT, is_primary=True, source=STAFF_LINK_SOURCE, metadata={"backfill": True})
        else:
            stats["no self party (run step 2)"] += 1
    w.flush()
    return dict(stats)


def d6_reverse(apps) -> dict:
    m = _Models(apps)
    return {"staff parties deleted": _deletable(m, metadata__backfill__has_key="profile")}


# --- D7 portal logins -------------------------------------------------------------


def _has_field(model, name) -> bool:
    return any(f.name == name for f in model._meta.get_fields())


def d7_portal_logins(apps) -> dict:
    """Account holder and ``Party.user`` from the real login link (identity step 3)."""
    m = _Models(apps)
    q = m.q
    stats = Counter()
    conflicts = []
    persons = customer_persons(m)
    taken = set(q(m.Party).filter(user__isnull=False).values_list("company_id", "user_id"))
    login = Q(user__isnull=False)
    if _has_field(m.Customer, "supabase_user_id"):
        # Historical state before identity step 8 dropped the legacy column
        # (parties.0002 runs here): a customer the identity backfill could
        # not link still counts as a login holder.
        login |= Q(supabase_user_id__isnull=False)
    rows = q(m.Customer).filter(party__isnull=False).filter(login).order_by("created_at", "pk")
    for c in rows.iterator(chunk_size=CHUNK):
        pid = c.account_holder_id or persons.get(c.pk)
        if pid is None:
            stats["login customers with no person party"] += 1
            continue
        if c.account_holder_id is None:
            q(m.Customer).filter(pk=c.pk).update(account_holder_id=pid)
            stats["account holders set"] += 1
        if c.user_id is None:
            continue
        holder = q(m.Party).filter(pk=pid).values("user_id", "company_id").first()
        if holder["user_id"] == c.user_id:
            continue
        if holder["user_id"] is not None or (holder["company_id"], c.user_id) in taken:
            conflicts.append(c.pk)
            continue
        q(m.Party).filter(pk=pid).update(user_id=c.user_id)
        taken.add((holder["company_id"], c.user_id))
        stats["party logins linked"] += 1
    if conflicts:
        stats["login conflicts (not linked)"] = len(conflicts)
    return dict(stats) | ({"conflict customers": _ids(conflicts)} if conflicts else {})


def d7_reverse(apps) -> dict:
    m = _Models(apps)
    n = 0
    for pid, uid in m.q(m.Customer).filter(account_holder__isnull=False, user__isnull=False).values_list(
        "account_holder_id", "user_id",
    ):
        n += m.q(m.Party).filter(pk=pid, user_id=uid).update(user=None)
    return {"party logins nulled": n}


# --- D8 external refs -------------------------------------------------------------


def d8_external_refs(apps) -> dict:
    m = _Models(apps)
    q = m.q
    stats = Counter()
    ct_customer, _ = q(m.ContentType).get_or_create(app_label="customers", model="customer")
    ct_address, _ = q(m.ContentType).get_or_create(app_label="customers", model="customeraddress")
    wanted = []  # (company_id, source, object_type, source_id, ct, object_id, column)
    for c in q(m.Customer).filter(company__isnull=False).only(
        "pk", "company_id", "bc_id", "erpnext_name", "ebay_username", "stripe_customer_id",
    ).iterator(chunk_size=CHUNK):
        for column, source, object_type, value in legacy_refs(c):
            wanted.append((c.company_id, source, object_type, value, ct_customer.pk, c.pk, column))
    column, source, object_type = ADDRESS_REF
    for pk, value, company_id in q(m.CustomerAddress).filter(customer__company__isnull=False).exclude(
        bc_address_id="").values_list("pk", "bc_address_id", "customer__company_id"):
        wanted.append((company_id, source, object_type, value.strip()[:255], ct_address.pk, pk, column))

    by_key = defaultdict(list)
    for row in wanted:
        by_key[row[:4]].append(row)
    duplicates = {k: v for k, v in by_key.items() if len(v) > 1}
    existing = {
        (co, s, t, i): (ct, oid)
        for co, s, t, i, ct, oid in q(m.ExternalRef).values_list(
            "company_id", "source", "object_type", "source_id", "content_type_id", "object_id")
    }
    new = []
    for key, rows in by_key.items():
        if key in duplicates:
            continue
        _co, _s, _t, _i, ct, oid, col = rows[0]
        if key in existing:
            if existing[key] != (ct, oid):
                stats["refs already pointing elsewhere (not written)"] += 1
            continue
        new.append(m.ExternalRef(
            company_id=key[0], source=key[1], object_type=key[2], source_id=key[3], content_type_id=ct,
            object_id=oid, metadata={"backfill": True, "legacy_column": col},
        ))
        stats[f"{key[1]}:{key[2]}"] += 1
    q(m.ExternalRef).bulk_create(new, batch_size=BATCH)
    stats["refs written"] = len(new)
    if duplicates:
        stats["duplicate source keys (reported, not written)"] = len(duplicates)
        rows = sorted(f"{k[1]}:{k[2]}={k[3]} x{len(v)}" for k, v in duplicates.items())
        return dict(stats) | {"duplicate keys": _ids(rows)}
    return dict(stats)


def d8_reverse(apps) -> dict:
    m = _Models(apps)
    n, _ = m.q(m.ExternalRef).filter(metadata__backfill=True).delete()
    return {"refs deleted": n}


# --- D9 consent ---------------------------------------------------------------------


def d9_consent(apps) -> dict:
    m = _Models(apps)
    q = m.q
    stats = Counter()
    persons = customer_persons(m)
    done = set(q(m.ConsentRecord).filter(source=BACKFILL, channel="email", purpose="marketing").values_list(
        "party_id", flat=True))
    company_of = dict(q(m.Party).values_list("pk", "company_id"))
    new, marketable = [], []
    for c in q(m.Customer).filter(accepts_marketing=True, party__isnull=False).only(
        "pk", "created_at", "party_id", "account_holder_id", "customer_type",
    ).iterator(chunk_size=CHUNK):
        pid = persons.get(c.pk)
        if pid is None:
            stats["opted-in customers with no person party"] += 1
            continue
        marketable.append(pid)
        if pid in done:
            continue
        done.add(pid)
        new.append(m.ConsentRecord(
            company_id=company_of[pid], party_id=pid, channel="email", purpose="marketing", state="granted",
            occurred_at=c.created_at, source=BACKFILL, source_detail=f"customer:{c.pk} accepts_marketing",
            evidence={"inferred_from": "accepts_marketing", "precision": "unknown"},
        ))
    q(m.ConsentRecord).bulk_create(new, batch_size=BATCH)
    stats["consent records"] = len(new)
    from django.utils import timezone

    now = timezone.now()
    for pids in _chunks(sorted(set(marketable), key=str)):
        stats["marketable parties"] += q(m.Party).filter(pk__in=pids, marketable=False).update(
            marketable=True, marketable_source=MARKETABLE_SOURCE, marketable_changed_at=now)
    return dict(stats)


def d9_reverse(apps) -> dict:
    m = _Models(apps)
    n, _ = m.q(m.ConsentRecord).filter(source=BACKFILL).delete()
    k = m.q(m.Party).filter(marketable_source=MARKETABLE_SOURCE).update(
        marketable=False, marketable_source="", marketable_changed_at=None)
    return {"consent records deleted": n, "marketable reset": k}


# --- D10 leads --------------------------------------------------------------------


def find_person_ids(m: _Models, company_id, *, email="", phone="") -> list:
    """3.2 on historical models: live person parties with this exact key.

    Email is the key when given; the phone is used only without an email
    (a shared phone is dedupe evidence, R2/R3, never a link).
    """
    live = {"party__kind": PERSON, "party__merged_into__isnull": True, "party__erased_at__isnull": True,
            "company_id": company_id}
    if email:
        qs = m.q(m.PartyEmail).filter(email_normalized=email, **live)
    elif phone:
        qs = m.q(m.PartyPhone).filter(number_e164=phone, **live)
    else:
        return []
    return sorted(set(qs.values_list("party_id", flat=True)), key=str)


def d10_leads(apps) -> dict:
    m = _Models(apps)
    q = m.q
    stats = Counter()
    ambiguous = []
    open_roles = set(q(m.PartyRole).filter(role=LEAD_ROLE, valid_to__isnull=True).values_list("party_id", flat=True))
    made: dict = {}  # (company, key) -> party id, for parties this run creates
    stats["leads without a company (skipped)"] = q(m.Lead).filter(company__isnull=True, party__isnull=True).count()
    for lead in q(m.Lead).filter(company__isnull=False, party__isnull=True).order_by("created_at", "pk").iterator(
        chunk_size=CHUNK,
    ):
        stats["leads"] += 1
        email = safe_email(lead.email)
        raw, e164, ext = phone_parts(lead.phone, lead.country)
        key = ("e", email) if email else ("p", e164) if e164 else None
        ids = find_person_ids(m, lead.company_id, email=email, phone="" if email else e164)
        if key and (lead.company_id, key) in made and made[(lead.company_id, key)] not in ids:
            ids.append(made[(lead.company_id, key)])
        w = _Writer(m)
        if len(ids) > 1:
            ambiguous.append(lead.pk)
            continue
        start, end = lead_role_dates(lead)
        role_meta = {"lead": str(lead.pk), "backfill": True}
        if ids:
            pid = ids[0]
            stats["linked to an existing party"] += 1
            if end is not None:
                w.role(q(m.Party).get(pk=pid), LEAD_ROLE, start, end, LEAD_ROLE_SOURCE, role_meta)
            elif pid not in open_roles:
                w.role(q(m.Party).get(pk=pid), LEAD_ROLE, start, None, LEAD_ROLE_SOURCE, role_meta)
                open_roles.add(pid)
        else:
            given, family = person_names(lead.first_name, lead.last_name)
            p = w.party(
                company_id=lead.company_id, kind=PERSON, given_name=given, family_name=family,
                display_name=person_display(lead.first_name, lead.last_name, lead.email), origin=BACKFILL,
                metadata={"backfill": {"lead": str(lead.pk)}},
            )
            pid = p.pk
            if email:
                w.email(p, lead.email, email, purpose="other", source=f"lead:{lead.pk}", primary=True)
            if raw:
                w.phone(p, raw, e164, ext, purpose="other", source=f"lead:{lead.pk}", primary=bool(e164))
            w.role(p, LEAD_ROLE, start, end, LEAD_ROLE_SOURCE, role_meta)
            if end is None:
                open_roles.add(pid)
            if key:
                made[(lead.company_id, key)] = pid
            stats["person parties"] += 1
        w.flush()
        q(m.Lead).filter(pk=lead.pk).update(party_id=pid)
    if ambiguous:
        stats["ambiguous (match-N, left unlinked)"] = len(ambiguous)
    return dict(stats) | ({"ambiguous leads": _ids(ambiguous)} if ambiguous else {})


def d10_reverse(apps) -> dict:
    m = _Models(apps)
    n = m.q(m.Lead).filter(party__origin=BACKFILL).update(party=None)
    k, _ = m.q(m.PartyRole).filter(role=LEAD_ROLE, metadata__backfill=True).delete()
    parties = _deletable(m, metadata__backfill__has_key="lead")
    return {"lead links nulled": n, "lead roles deleted": k, "lead parties deleted": parties}


STEPS = {
    2: ("self parties", d2_self_parties, d2_reverse),
    3: ("customer parties", d3_customer_parties, d3_reverse),
    4: ("contact parties", d4_contact_parties, d4_reverse),
    5: ("addresses", d5_addresses, d5_reverse),
    6: ("staff parties", d6_staff_parties, d6_reverse),
    7: ("portal logins", d7_portal_logins, d7_reverse),
    8: ("external refs", d8_external_refs, d8_reverse),
    9: ("consent", d9_consent, d9_reverse),
    10: ("leads", d10_leads, d10_reverse),
}


def run_step(apps, step: int, *, reverse: bool = False) -> dict:
    _label, forward, backward = STEPS[step]
    return (backward if reverse else forward)(apps)


def format_line(step: int, report: dict, *, reverse: bool = False) -> str:
    """The summary line the downstream rehearsal records verbatim (section 4)."""
    label = STEPS[step][0]
    body = "; ".join(f"{k}: {v}" for k, v in report.items() if v not in (0, "")) or "nothing to do"
    return f"parties backfill D{step} {label}{' (reverse)' if reverse else ''}: {body}"
