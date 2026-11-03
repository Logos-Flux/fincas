"""Feed the dedupe engine from Party rows (phase2-party.md 3.3, after step 5).

The counterpart of ``parties.dedupe_legacy`` once the backfill has run: one
node per live party, shaped so the same people cluster the same way as the
pre-Party report did (A1/A2 are already folded into one party by the
backfill and the sync, so they no longer appear as clusters):

- a person node carries its emails, parsed phones and name; its postcodes
  are its own addresses plus, for a business customer's person, the
  addresses of the organisation it holds the customer for;
- an organisation node carries its name (unless the name was inferred from a
  person) and its domain: ``primary_domain``, else the non-freemail domain of
  its customer person's email, as the legacy report derived it;
- ``is_account_holder`` is true only for a party that is some customer's
  ``account_holder``, which step 5 sets only for a real portal login;
- ``user`` is the party's login; ``is_staff`` an open ``staff`` role or a
  staff user's email; ``is_self`` the tenant's own organisation;
- a lead converted to a customer whose person is another party links the two
  (C1).

Read-only, scoped to one company (3.1).
"""

from __future__ import annotations

from collections import defaultdict

from django.db.models import Q

from .constants import ORGANISATION, PERSON
from .dedupe import Node, is_mixed_script
from .dedupe_legacy import FREEMAIL_DOMAINS, LegacyStats, normalize_postcode, staff_emails
from .mirror import CUSTOMER_LINK_SOURCE
from .normalize import normalize_name, normalize_org_name
from .relationship_registry import EMPLOYMENT
from .role_registry import STAFF_ROLE


def party_nodes(company, stats: LegacyStats | None = None) -> tuple[list[Node], LegacyStats]:
    from crm.models import Lead
    from customers.models import Customer, CustomerContact

    from .models import Party, PartyAddress, PartyEmail, PartyPhone, PartyRelationship, PartyRole

    stats = stats if stats is not None else LegacyStats()
    co = str(company.pk)
    live = Party.objects.filter(company=company, merged_into__isnull=True, erased_at__isnull=True)
    staff = staff_emails(company)

    nodes: dict = {}
    for p in live.order_by("created_at", "pk"):
        node = Node(key=f"party:{p.pk}", company=co, kind=p.kind)
        node.user = str(p.user_id or "")
        node.is_self = p.is_self
        if p.kind == PERSON:
            given, family = normalize_name(p.given_name), normalize_name(p.family_name)
            if given or family:
                node.names.add((given, family))
            node.display["name"] = p.display_name
        else:
            if not (p.metadata or {}).get("name_inferred"):
                for raw in {p.display_name, p.legal_name} - {""}:
                    key = normalize_org_name(raw)
                    if key:
                        node.org_names.add(key)
            else:
                stats.name_inferred_orgs.append(node.key)
            if p.primary_domain:
                node.domains.add(p.primary_domain)
            node.display["org_name"] = p.display_name
        nodes[p.pk] = node

    for pid, email, raw in PartyEmail.objects.filter(party__in=live).values_list(
            "party_id", "email_normalized", "email"):
        node = nodes[pid]
        if node.kind != PERSON:
            continue
        node.emails.add(email)
        node.display.setdefault("emails", []).append(raw)
        node.is_staff = node.is_staff or email in staff
        if is_mixed_script(email.rpartition("@")[0]) and node.key not in stats.mixed_script_email:
            stats.mixed_script_email.append(node.key)
    for pid, e164 in PartyPhone.objects.filter(party__in=live).values_list("party_id", "number_e164"):
        if not e164:
            stats.unparseable_phone["parties"] += 1
        elif nodes[pid].kind == PERSON:
            nodes[pid].phones.add(e164)
    for pid in PartyRole.objects.filter(party__in=live, role=STAFF_ROLE, valid_to__isnull=True).values_list(
            "party_id", flat=True):
        nodes[pid].is_staff = True

    postcodes = defaultdict(set)
    for pid, code, country in PartyAddress.objects.filter(party__in=live).values_list(
            "party_id", "postal_code", "country"):
        key = normalize_postcode(code, country)
        if key[0]:
            postcodes[pid].add(key)
    for pid, keys in postcodes.items():
        if nodes[pid].kind == PERSON:
            nodes[pid].postcodes |= keys
    customer_person = {}
    for from_id, to_id, meta in PartyRelationship.objects.filter(
        company=company, source=CUSTOMER_LINK_SOURCE, kind=EMPLOYMENT, valid_to__isnull=True,
    ).values_list("from_party_id", "to_party_id", "metadata"):
        if from_id in nodes and to_id in nodes:
            nodes[from_id].postcodes |= postcodes.get(to_id, set())
            org = nodes[to_id]
            if not org.domains:
                for email in nodes[from_id].emails:
                    domain = email.rpartition("@")[2]
                    if domain and domain not in FREEMAIL_DOMAINS:
                        org.domains.add(domain)
            if isinstance(meta, dict) and meta.get("customer"):
                customer_person[meta["customer"]] = from_id

    for cid, party_id, holder_id in Customer.objects.filter(company=company).values_list(
            "pk", "party_id", "account_holder_id"):
        stats.rows["customers"] += 1
        if holder_id in nodes:
            nodes[holder_id].is_account_holder = True
            customer_person[str(cid)] = holder_id
        elif party_id in nodes and nodes[party_id].kind == PERSON:
            customer_person[str(cid)] = party_id
        for pid in {party_id, holder_id} - {None}:
            if pid in nodes:
                nodes[pid].sources.append(("customer", str(cid)))
    for ct_id, pid in CustomerContact.objects.filter(customer__company=company).values_list("pk", "party_id"):
        stats.rows["contacts"] += 1
        if pid in nodes:
            nodes[pid].sources.append(("contact", str(ct_id)))
    for lead_id, pid, conv, conv_company in Lead.objects.filter(company=company).values_list(
            "pk", "party_id", "converted_to_customer_id", "converted_to_customer__company_id"):
        stats.rows["leads"] += 1
        if pid not in nodes:
            continue
        nodes[pid].sources.append(("lead", str(lead_id)))
        if conv is None:
            continue
        if conv_company != company.pk:
            stats.cross_tenant_conversions.append(f"lead:{lead_id}")
            continue
        other = customer_person.get(str(conv))
        if other is not None and other != pid and other in nodes:
            nodes[pid].links.add(nodes[other].key)
    for node in nodes.values():
        if node.phones:
            node.display["phones"] = sorted(node.phones)
    return list(nodes.values()), stats


def has_parties(companies) -> bool:
    """Whether the backfill has run for any of ``companies`` (beyond self parties)."""
    from .models import Party

    return Party.objects.filter(company__in=list(companies)).filter(~Q(is_self=True)).exists()


__all__ = ["ORGANISATION", "has_parties", "party_nodes"]
