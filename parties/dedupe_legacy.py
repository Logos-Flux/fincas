"""Feed the dedupe engine from pre-Party rows (phase2-party.md step 4).

Before any Party exists, each would-be party is modelled the way the step-5
backfill will create it (section 4, D3-D10):

- a ``Customer`` is a person node, plus an organisation node when
  ``customer_type == business``; the person is an *account holder* only when
  the customer has a portal login (``Customer.user``);
- a ``CustomerContact`` folds into its customer's node when its email equals
  the customer's (A1), into an earlier contact on the same customer sharing
  its email (A2), and is otherwise a person node of its own;
- a ``Lead`` with a company is a person node; its ``company_name`` never
  creates an organisation (2.5). A converted lead links to its customer (C1).

Read-only: every query is a SELECT, scoped to one company (3.1).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db.models import Q

from .constants import ORGANISATION, PERSON
from .dedupe import Node, is_mixed_script
from .normalize import normalize_email, normalize_name, normalize_org_name, normalize_phone

# Mailbox providers whose domain says nothing about an organisation, so it is
# never used as an R6 key when an organisation's domain is taken from its
# account holder's email (pre-Party there is no ``primary_domain``).
FREEMAIL_DOMAINS = frozenset({
    "aol.com", "gmail.com", "googlemail.com", "gmx.com", "gmx.de", "hotmail.com", "icloud.com",
    "live.com", "mac.com", "mail.com", "me.com", "msn.com", "outlook.com", "proton.me",
    "protonmail.com", "web.de", "yahoo.com", "yandex.com", "ymail.com", "zoho.com",
})

_SPACE_RE = re.compile(r"\s+")


def normalize_postcode(postcode: str | None, country: str | None) -> tuple[str, str]:
    """``(postcode, country)`` comparison key for R4, or ``("", "")``.

    Upper-cased with whitespace removed (``sw1a 1aa`` == ``SW1A1AA``); a US
    ZIP+4 is cut to its five-digit ZIP.
    """
    country = (country or "").strip().upper()
    code = _SPACE_RE.sub("", postcode or "").upper()
    if not code:
        return "", ""
    if country == "US" and re.fullmatch(r"\d{5}-?\d{4}", code):
        code = code[:5]
    return code, country


@dataclass
class LegacyStats:
    """Counts the report prints next to the clusters."""

    rows: Counter = field(default_factory=Counter)  # customers / contacts / leads
    invalid_email: Counter = field(default_factory=Counter)
    unparseable_phone: Counter = field(default_factory=Counter)
    mixed_script_email: list[str] = field(default_factory=list)  # node keys
    name_inferred_orgs: list[str] = field(default_factory=list)  # node keys
    cross_tenant_conversions: list[str] = field(default_factory=list)  # lead node keys

    def merge(self, other: LegacyStats) -> None:
        self.rows.update(other.rows)
        self.invalid_email.update(other.invalid_email)
        self.unparseable_phone.update(other.unparseable_phone)
        self.mixed_script_email += other.mixed_script_email
        self.name_inferred_orgs += other.name_inferred_orgs
        self.cross_tenant_conversions += other.cross_tenant_conversions


class _Builder:
    def __init__(self, company_id: str, stats: LegacyStats):
        self.company = company_id
        self.stats = stats

    def email(self, raw: str, source: str) -> str:
        raw = (raw or "").strip()
        if not raw:
            return ""
        try:
            return normalize_email(raw)
        except ValidationError:
            self.stats.invalid_email[source] += 1
            return ""

    def add_email(self, node: Node, email: str, raw: str) -> None:
        """Attach a normalised email; 7.1 lists mixed-script addresses."""
        if not email or email in node.emails:
            return
        node.emails.add(email)
        node.display.setdefault("emails", []).append(raw.strip())
        if is_mixed_script(email.rpartition("@")[0]) and node.key not in self.stats.mixed_script_email:
            self.stats.mixed_script_email.append(node.key)

    def phone(self, raw: str, region: str, source: str) -> str:
        if not (raw or "").strip():
            return ""
        e164, _ext = normalize_phone(raw, region)
        if not e164:
            self.stats.unparseable_phone[source] += 1
        return e164

    def person(self, key: str, source: str, row_id, first: str, last: str) -> Node:
        node = Node(key=key, company=self.company, kind=PERSON, sources=[(source, str(row_id))])
        self.add_name(node, first, last)
        return node

    @staticmethod
    def add_name(node: Node, first: str, last: str) -> None:
        given, family = normalize_name(first), normalize_name(last)
        if given or family:
            node.names.add((given, family))
            node.display.setdefault("name", f"{(first or '').strip()} {(last or '').strip()}".strip())


def staff_emails(company) -> set[str]:
    """Normalised emails of active staff of ``company``, plus staff with no company."""
    out = set()
    for raw in get_user_model().objects.filter(
        Q(is_staff=True) | Q(is_superuser=True), is_active=True,
    ).filter(
        Q(userprofile__company=company) | Q(userprofile__isnull=True) | Q(userprofile__company__isnull=True)
    ).values_list("email", flat=True):
        try:
            out.add(normalize_email(raw or ""))
        except ValidationError:
            pass
    return out


def legacy_nodes(company, stats: LegacyStats | None = None) -> tuple[list[Node], LegacyStats]:
    """Nodes for one company's Customer, CustomerContact and Lead rows."""
    from crm.models import Lead
    from customers.models import Customer, CustomerAddress, CustomerContact

    stats = stats if stats is not None else LegacyStats()
    b = _Builder(str(company.pk), stats)

    staff = staff_emails(company)

    # Postcodes and the phone region come from the customer's addresses
    # (default address first).
    postcodes: dict = {}
    region: dict = {}
    for cid, code, country in CustomerAddress.objects.filter(customer__company=company).order_by(
        "customer_id", "-is_default", "created_at"
    ).values_list("customer_id", "postal_code", "country"):
        key = normalize_postcode(code, country)
        if key[0]:
            postcodes.setdefault(cid, set()).add(key)
        region.setdefault(cid, (country or "").strip().upper())

    nodes: dict[str, Node] = {}
    customer_email: dict = {}
    for row in Customer.objects.filter(company=company).order_by("created_at", "id").values(
        "id", "customer_type", "first_name", "last_name", "email", "phone", "company_name", "user_id",
    ).iterator(chunk_size=2000):
        stats.rows["customers"] += 1
        cid = row["id"]
        node = b.person(f"customer:{cid}", "customer", cid, row["first_name"], row["last_name"])
        # An account holder is a customer with a real portal login, not every
        # customer (step 5; the downstream report blocked 11 of 15 clusters as
        # two_account_holders for customers nobody can sign in as).
        login = row["user_id"]
        node.is_account_holder = login is not None
        node.user = str(login or "")
        email = b.email(row["email"], "customers")
        b.add_email(node, email, row["email"])
        if email:
            customer_email[cid] = email
        phone = b.phone(row["phone"], region.get(cid, ""), "customers")
        if phone:
            node.phones.add(phone)
        node.postcodes |= postcodes.get(cid, set())
        node.is_staff = email in staff
        nodes[node.key] = node

        if row["customer_type"] == Customer.CustomerType.BUSINESS:
            org = Node(key=f"org:{cid}", company=b.company, kind=ORGANISATION, sources=[("customer", str(cid))])
            name = normalize_org_name(row["company_name"])
            if name:
                org.org_names.add(name)
                org.display["org_name"] = row["company_name"].strip()
            else:
                # 7.1: named after the person and flagged; an inferred name is
                # a person's name, so it is not R5 evidence.
                stats.name_inferred_orgs.append(org.key)
            domain = email.rpartition("@")[2] if email else ""
            if domain and domain not in FREEMAIL_DOMAINS:
                org.domains.add(domain)
            nodes[org.key] = org

    # Contacts: A1 into the customer's node, A2 into an earlier contact.
    contact_node: dict[tuple, Node] = {}
    for row in CustomerContact.objects.filter(customer__company=company).order_by("created_at", "id").values(
        "id", "customer_id", "first_name", "last_name", "email", "phone"
    ).iterator(chunk_size=2000):
        stats.rows["contacts"] += 1
        cid = row["customer_id"]
        key = f"contact:{row['id']}"
        email = b.email(row["email"], "contacts")
        target, rule = None, ""
        if email and customer_email.get(cid) == email:
            target, rule = nodes[f"customer:{cid}"], "A1"
        elif email and (cid, email) in contact_node:
            target, rule = contact_node[(cid, email)], "A2"
        if target is None:
            target = b.person(key, "contact", row["id"], row["first_name"], row["last_name"])
            nodes[key] = target
            if email:
                contact_node[(cid, email)] = target
        else:
            target.sources.append(("contact", str(row["id"])))
            target.applied[rule] = target.applied.get(rule, 0) + 1
            b.add_name(target, row["first_name"], row["last_name"])
        b.add_email(target, email, row["email"])
        target.is_staff = target.is_staff or email in staff
        phone = b.phone(row["phone"], region.get(cid, ""), "contacts")
        if phone:
            target.phones.add(phone)

    for row in Lead.objects.filter(company=company).order_by("created_at", "id").values(
        "id", "first_name", "last_name", "email", "phone", "postal_code", "country", "converted_to_customer_id",
        "converted_to_customer__company_id",
    ).iterator(chunk_size=2000):
        stats.rows["leads"] += 1
        node = b.person(f"lead:{row['id']}", "lead", row["id"], row["first_name"], row["last_name"])
        email = b.email(row["email"], "leads")
        b.add_email(node, email, row["email"])
        node.is_staff = email in staff
        phone = b.phone(row["phone"], row["country"], "leads")
        if phone:
            node.phones.add(phone)
        pc = normalize_postcode(row["postal_code"], row["country"])
        if pc[0]:
            node.postcodes.add(pc)
        if row["converted_to_customer_id"]:
            if row["converted_to_customer__company_id"] == company.pk:
                node.links.add(f"customer:{row['converted_to_customer_id']}")
            else:
                # Converted to another tenant's customer: never evidence (3.1).
                stats.cross_tenant_conversions.append(node.key)
        nodes[node.key] = node

    for node in nodes.values():
        if node.phones:
            node.display["phones"] = sorted(node.phones)
    return list(nodes.values()), stats


def has_usable_key(node: Node) -> bool:
    """Whether any rule could ever put this node in a cluster."""
    if node.kind == ORGANISATION:
        return bool(node.org_names or node.domains)
    named_postcode = node.postcodes and any(g and f for g, f in node.names)
    return bool(node.emails or node.phones or named_postcode or node.links)


def null_company_counts() -> dict[str, int]:
    """Rows with no tenant: never clustered (3.1), reported for cleanup."""
    from crm.models import Lead
    from customers.models import Customer

    return {
        "customers": Customer.objects.filter(company__isnull=True).count(),
        "leads": Lead.objects.filter(company__isnull=True).count(),
    }
