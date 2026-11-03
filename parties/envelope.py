"""The match envelope (docs/crm/phase2-party.md 2.7, 5.2 ``match/``).

One function, :func:`build_envelope`, answers "which party (or customer) does
this free text mean?" for the staff API's ``match/`` endpoint, and through
it for ``erp_assist``'s ``find_party`` / ``find_customer`` and every write
tool that takes a free-text reference. It never creates anything and never
picks between candidates:

=========================================  ==============  ======
Situation                                  match_quality   item
=========================================  ==============  ======
one record whose normalised email or       ``exact``       it
E.164 equals the query
two or more such records (one tenant, or   ``ambiguous``   null
across tenants for a superuser/service)
exactly one search hit, not exact          ``fuzzy``       it
several search hits, none exact            ``ambiguous``   null
nothing                                    ``none``        null
=========================================  ==============  ======

Exact keys are the same normalisation the matcher uses (``parties.mirror``,
spec 7.2/7.3), and each exact candidate carries the dedupe rule its key
corresponds to with that rule's confidence (``parties.dedupe.RULES``: R1 for
an email, R3 for a phone alone). A search hit carries ``rule="search"`` and
no confidence: a name is never a match key (3.2).
"""

from __future__ import annotations

import re

from django.db.models import Max, Q

from .constants import ORGANISATION, PERSON
from .dedupe import RULES
from .mirror import phone_parts, safe_email
from .relationship_registry import EMPLOYMENT

EXACT = "exact"
AMBIGUOUS = "ambiguous"
FUZZY = "fuzzy"
NONE = "none"

TARGET_PARTY = "party"
TARGET_CUSTOMER = "customer"
TARGETS = (TARGET_PARTY, TARGET_CUSTOMER)

SEARCH_RULE = "search"
CANDIDATE_LIMIT = 10
_PHONEISH = re.compile(r"^[\d\s()+.\-x#;=ext]+$", re.IGNORECASE)

MESSAGES = {
    EXACT: "One exact match.",
    AMBIGUOUS: (
        "Several records match. Do not pick one: ask the user which they mean, "
        "quoting the distinguishing fields of the candidates."
    ),
    FUZZY: "One possible match, not exact. Confirm it with the user before any write.",
    NONE: "Nothing matches. Say so, and offer to create a new record.",
}


def _rule(key_kind: str) -> tuple[str, float | None]:
    if key_kind == "email":
        return "R1", RULES["R1"]["confidence"]
    if key_kind == "phone":
        return "R3", RULES["R3"]["confidence"]
    return SEARCH_RULE, None


def _last4(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-4:] if digits else ""


def classify_query(query: str, region: str | None = None) -> tuple[str, str]:
    """``(key_kind, key)`` for free text: an email, a phone, or a name."""
    text = (query or "").strip()
    if "@" in text:
        email = safe_email(text)
        if email:
            return "email", email
    if text and _PHONEISH.match(text) and sum(ch.isdigit() for ch in text) >= 7:
        _raw, e164, _ext = phone_parts(text, region)
        if e164:
            return "phone", e164
    return "name", text


# --- candidates --------------------------------------------------------------


def _party_candidates(parties, rule: tuple[str, float | None]) -> list[dict]:
    from customers.models import Customer

    from .models import PartyEmail, PartyRelationship, PartyRole

    ids = [p.pk for p in parties]
    roles: dict = {}
    for party_id, role in PartyRole.objects.filter(
        party_id__in=ids, valid_to__isnull=True,
    ).order_by("role").values_list("party_id", "role"):
        roles.setdefault(party_id, []).append(role)
    orgs = {}
    for from_id, name in PartyRelationship.objects.filter(
        from_party_id__in=ids, kind=EMPLOYMENT, valid_to__isnull=True,
    ).order_by("-is_primary", "created_at").values_list("from_party_id", "to_party__display_name"):
        orgs.setdefault(from_id, name)
    emails = dict(
        PartyEmail.objects.filter(party_id__in=ids, is_primary=True).values_list("party_id", "email")
    )
    last_order = {}
    customers = {}
    for field in ("party", "account_holder"):
        for party_id, at in (
            Customer.objects.filter(**{f"{field}_id__in": ids}).values(f"{field}_id")
            .annotate(at=Max("last_order_at")).values_list(f"{field}_id", "at")
        ):
            if at and (last_order.get(party_id) is None or at > last_order[party_id]):
                last_order[party_id] = at
        for party_id, customer_id in Customer.objects.filter(
            **{f"{field}_id__in": ids}
        ).values_list(f"{field}_id", "pk"):
            customers.setdefault(party_id, set()).add(str(customer_id))
    out = []
    for p in parties:
        at = last_order.get(p.pk)
        out.append({
            "id": str(p.pk),
            "type": TARGET_PARTY,
            "display_name": p.display_name,
            "kind": p.kind,
            "company": str(p.company_id),
            "company_name": p.company.name,
            "primary_email": emails.get(p.pk, p.primary_email_normalized),
            "primary_phone_last4": _last4(p.primary_phone_e164),
            "roles": roles.get(p.pk, []),
            "organisation": orgs.get(p.pk, ""),
            "last_order_at": at.isoformat() if at else None,
            "customer_ids": sorted(customers.get(p.pk, ())),
            "rule": rule[0],
            "confidence": rule[1],
        })
    return out


def _customer_candidates(customers, rule: tuple[str, float | None]) -> list[dict]:
    from .models import PartyRole

    party_ids = {c.party_id for c in customers if c.party_id}
    roles: dict = {}
    for party_id, role in PartyRole.objects.filter(
        party_id__in=party_ids, valid_to__isnull=True,
    ).order_by("role").values_list("party_id", "role"):
        roles.setdefault(party_id, []).append(role)
    out = []
    for c in customers:
        person = " ".join(x for x in (c.first_name, c.last_name) if x).strip()
        business = c.customer_type == "business"
        out.append({
            "id": str(c.pk),
            "type": TARGET_CUSTOMER,
            "display_name": (c.company_name if business and c.company_name else person) or c.email,
            "kind": ORGANISATION if business else PERSON,
            "company": str(c.company_id) if c.company_id else None,
            "company_name": c.company.name if c.company_id else "",
            "primary_email": c.email,
            "primary_phone_last4": _last4(c.phone),
            "roles": roles.get(c.party_id, []),
            "organisation": c.company_name or "",
            "last_order_at": c.last_order_at.isoformat() if c.last_order_at else None,
            "party_id": str(c.party_id) if c.party_id else None,
            "rule": rule[0],
            "confidence": rule[1],
        })
    return out


# --- lookups -----------------------------------------------------------------


def _live_parties(company_ids):
    from .models import Party

    qs = Party.objects.filter(merged_into__isnull=True, erased_at__isnull=True).select_related("company")
    if company_ids is not None:
        qs = qs.filter(company_id__in=company_ids)
    return qs


def _customers(company_ids):
    from customers.models import Customer

    qs = Customer.objects.select_related("company")
    if company_ids is not None:
        qs = qs.filter(company_id__in=company_ids)
    return qs


def _exact(target, company_ids, key_kind, key) -> list:
    if target == TARGET_PARTY:
        qs = _live_parties(company_ids)
        qs = (qs.filter(emails__email_normalized=key) if key_kind == "email"
              else qs.filter(phones__number_e164=key))
        return list(qs.distinct().order_by("created_at", "pk")[: CANDIDATE_LIMIT + 1])
    qs = _customers(company_ids)
    if key_kind == "email":
        # Customer.email is stored as entered; compare in normalised form (7.2),
        # and also through the party the sync mirrors it onto (catches IDN and
        # stray whitespace that iexact on the raw column misses).
        rows = (
            qs.filter(
                Q(email__iexact=key)
                | Q(party__emails__email_normalized=key)
                | Q(account_holder__emails__email_normalized=key)
            ).distinct().order_by("created_at", "pk")
        )
        return [c for c in rows if safe_email(c.email) == key][: CANDIDATE_LIMIT + 1]
    # Customer.phone is raw text: match through the parties the sync mirrors it onto.
    return list(
        qs.filter(Q(party__phones__number_e164=key) | Q(account_holder__phones__number_e164=key))
        .distinct().order_by("created_at", "pk")[: CANDIDATE_LIMIT + 1]
    )


def _search(target, company_ids, text) -> list:
    words = text.split()
    if not words:
        return []
    if target == TARGET_PARTY:
        qs = _live_parties(company_ids)
        cond = Q(display_name__istartswith=text) | Q(legal_name__istartswith=text)
        if len(words) > 1:
            cond |= Q(given_name__iexact=words[0], family_name__istartswith=" ".join(words[1:]))
        else:
            cond |= Q(given_name__istartswith=text) | Q(family_name__istartswith=text)
        cond |= Q(primary_email_normalized__startswith=text.casefold())
        return list(qs.filter(cond).distinct().order_by("display_name", "pk")[: CANDIDATE_LIMIT + 1])
    qs = _customers(company_ids)
    for word in words:
        qs = qs.filter(
            Q(first_name__icontains=word) | Q(last_name__icontains=word)
            | Q(email__icontains=word) | Q(company_name__icontains=word)
        )
    return list(qs.order_by("last_name", "first_name", "pk")[: CANDIDATE_LIMIT + 1])


def build_envelope(company_ids, *, target: str = TARGET_PARTY, query: str | None = None,
                   email: str | None = None, phone: str | None = None,
                   region: str | None = None) -> dict:
    """The 2.7 envelope for one lookup.

    ``company_ids`` is the tenant set the caller may see: a one-element list
    for a tenant user, ``None`` for a superuser or service key (all
    tenants; spec 2.7 row 2). ``email`` / ``phone`` are exact keys only;
    ``query`` is free text: an email or phone in it is tried as an exact key
    first, then the text is searched by name (and email prefix).
    """
    if target not in TARGETS:
        raise ValueError(f"target must be one of {', '.join(TARGETS)}")
    to_candidates = _party_candidates if target == TARGET_PARTY else _customer_candidates

    if email:
        key_kind, key, search_text = "email", safe_email(email), None
    elif phone:
        key_kind, key, search_text = "phone", phone_parts(phone, region)[1], None
    else:
        key_kind, key = classify_query(query or "", region)
        search_text = (query or "").strip()
        if key_kind == "name":
            key = None

    if key:
        rows = _exact(target, company_ids, key_kind, key)
        if rows:
            state = EXACT if len(rows) == 1 else AMBIGUOUS
            return _envelope(state, to_candidates(rows[:CANDIDATE_LIMIT], _rule(key_kind)), len(rows))
    if search_text:
        rows = _search(target, company_ids, search_text)
        if rows:
            state = FUZZY if len(rows) == 1 else AMBIGUOUS
            return _envelope(state, to_candidates(rows[:CANDIDATE_LIMIT], _rule("name")), len(rows))
    return _envelope(NONE, [], 0)


def _envelope(state: str, candidates: list[dict], found: int) -> dict:
    item = candidates[0] if state in (EXACT, FUZZY) else None
    message = MESSAGES[state]
    if found > CANDIDATE_LIMIT:
        message += f" Showing the first {CANDIDATE_LIMIT}; narrow the query."
    return {"match_quality": state, "item": item, "candidates": candidates, "message": message}
