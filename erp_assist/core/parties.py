"""Parties, and the one resolver every free-text reference goes through.

docs/crm/phase2-party.md 2.7: the assistant never guesses which party or
customer a name means. ``resolve_ref`` is the single path from free text to
a UUID for every write tool; it asks the server's match envelope
(``/api/v1/parties/match/``) and proceeds only on an ``exact`` match. On
``ambiguous`` it raises :class:`ERPAmbiguousError` with the candidates; on a
lone ``fuzzy`` hit it raises the same error asking for confirmation; on
``none`` it raises :class:`ERPNotFoundError`. A UUID passes straight through.
"""

from __future__ import annotations

import uuid
from typing import Any

from .client import ERPAmbiguousError, ERPClient, ERPNotFoundError

PARTIES_URL = "/api/v1/parties/parties/"
MATCH_URL = "/api/v1/parties/match/"
EMAILS_URL = "/api/v1/parties/emails/"
PHONES_URL = "/api/v1/parties/phones/"
RELATIONSHIPS_URL = "/api/v1/parties/relationships/"

EXACT = "exact"
AMBIGUOUS = "ambiguous"
FUZZY = "fuzzy"
NONE = "none"

TARGET_PARTY = "party"
TARGET_CUSTOMER = "customer"

DUPLICATE_MESSAGE = (
    "A party with this email already exists. Show the user the candidates and "
    "ask whether one of them is meant; only call again with confirm_new=True if "
    "the user confirms this is a different party."
)


def is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (TypeError, ValueError):
        return False
    return True


async def match(
    client: ERPClient,
    *,
    query: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    target: str = TARGET_PARTY,
    company: str | None = None,
) -> dict[str, Any]:
    """The raw match envelope from the server. Never creates anything."""
    payload: dict[str, Any] = {"target": target}
    for key, value in (("q", query), ("email", email), ("phone", phone), ("company", company)):
        if value:
            payload[key] = value
    return await client.post(MATCH_URL, json=payload)


def raise_if_ambiguous(envelope: dict[str, Any]) -> dict[str, Any]:
    """The 2.7 rule for lookups: ``ambiguous`` never comes back as an item."""
    if envelope.get("match_quality") == AMBIGUOUS:
        raise ERPAmbiguousError(envelope)
    return envelope


async def resolve_ref(
    client: ERPClient, ref: str, *, target: str, what: str | None = None,
) -> str:
    """UUID for a free-text party/customer reference, or raise. Never guesses."""
    if is_uuid(ref):
        return str(ref)
    what = what or target
    envelope = await match(client, query=ref, target=target)
    quality = envelope.get("match_quality")
    if quality == EXACT and envelope.get("item"):
        return envelope["item"]["id"]
    if quality == AMBIGUOUS:
        raise ERPAmbiguousError(
            envelope,
            f"'{ref}' matches several {what} records. Do not pick one: ask the user "
            "which they mean, then pass its id.",
        )
    if quality == FUZZY:
        raise ERPAmbiguousError(
            envelope,
            f"'{ref}' is only a possible match for a {what}, not an exact one. Confirm "
            "the candidate with the user, then pass its id.",
        )
    raise ERPNotFoundError(f"No {what} matches '{ref}'. Say so, and offer to create one.")


async def find_party(client: ERPClient, query: str, *, company: str | None = None) -> dict[str, Any]:
    """Find a party (person or organisation) by email, phone or name.

    Returns the envelope ``{match_quality, item, candidates, message}``:
    ``exact`` (normalised email or E.164 equal, one party), ``fuzzy`` (one
    name/search hit: confirm before any write) or ``none``. Several hits
    raise :class:`ERPAmbiguousError`; nothing is ever picked.
    """
    return raise_if_ambiguous(await match(client, query=query, target=TARGET_PARTY, company=company))


async def get_party(client: ERPClient, party_id: str) -> dict[str, Any]:
    """Party detail: names, contact points, roles, and which fields are mirrored."""
    return await client.get(f"{PARTIES_URL}{party_id}/")


async def list_parties(
    client: ERPClient,
    *,
    search: str | None = None,
    kind: str | None = None,
    role: str | None = None,
    limit: int | None = 20,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"page_size": 100 if limit is None else min(limit, 100)}
    for key, value in (("search", search), ("kind", kind), ("role", role)):
        if value:
            params[key] = value
    return await client.list(PARTIES_URL, params=params, max_items=limit)


async def duplicate_check(
    client: ERPClient, *, email: str | None, phone: str | None = None, company: str | None = None,
) -> dict[str, Any] | None:
    """The envelope when ``email`` (else ``phone``) already belongs to a party, else None.

    2.7: ``create_party`` and ``create_customer`` call this first and stop on
    an ``exact`` or ``ambiguous`` result unless ``confirm_new=True``.
    """
    for key, value in (("email", email), ("phone", phone)):
        if not value:
            continue
        envelope = await match(client, target=TARGET_PARTY, company=company, **{key: value})
        if envelope.get("match_quality") in (EXACT, AMBIGUOUS):
            return {**envelope, "created": False, "message": DUPLICATE_MESSAGE}
    return None


async def create_party(
    client: ERPClient,
    *,
    kind: str,
    display_name: str | None = None,
    given_name: str | None = None,
    family_name: str | None = None,
    legal_name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    region: str | None = None,
    primary_domain: str | None = None,
    marketable: bool | None = None,
    company: str | None = None,
    confirm_new: bool = False,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a person or organisation party, with its first email/phone.

    Checks the match envelope first: if the email (or phone) already belongs
    to a party, returns ``{"created": false, match_quality, candidates,
    message}`` and creates nothing, unless ``confirm_new=True``.
    """
    if not confirm_new:
        duplicate = await duplicate_check(client, email=email, phone=phone, company=company)
        if duplicate is not None:
            return duplicate
    payload: dict[str, Any] = {"kind": kind}
    optional = {
        "display_name": display_name,
        "given_name": given_name,
        "family_name": family_name,
        "legal_name": legal_name,
        "email": email,
        "phone": phone,
        "region": region,
        "primary_domain": primary_domain,
        "marketable": marketable,
        "company": company,
    }
    for key, value in optional.items():
        if value is not None:
            payload[key] = value
    if extra_fields:
        payload.update(extra_fields)
    return await client.post(PARTIES_URL, json=payload)


async def add_party_email(
    client: ERPClient,
    party_id: str,
    *,
    email: str,
    purpose: str = "work",
    is_primary: bool = False,
) -> dict[str, Any]:
    """Add an email address to a party.

    ``purpose`` is one of personal / work / billing / other. A customer's
    billing address for invoices is its party's ``billing`` email.
    """
    return await client.post(
        EMAILS_URL,
        json={"party": party_id, "email": email, "purpose": purpose, "is_primary": is_primary},
    )


async def add_party_phone(
    client: ERPClient,
    party_id: str,
    *,
    number: str,
    purpose: str = "work",
    extension: str | None = None,
    region: str | None = None,
    is_primary: bool = False,
) -> dict[str, Any]:
    """Add a phone number to a party.

    ``region`` is the ISO country of a national-format number (the server
    default applies when omitted); an international ``+`` number needs none.
    """
    payload: dict[str, Any] = {
        "party": party_id, "number_raw": number, "purpose": purpose, "is_primary": is_primary,
    }
    if extension:
        payload["extension"] = extension
    if region:
        payload["region"] = region
    return await client.post(PHONES_URL, json=payload)


async def list_party_relationships(
    client: ERPClient, party_id: str, *, limit: int | None = 50
) -> list[dict[str, Any]]:
    """Relationships a party takes part in, on either side."""
    params: dict[str, Any] = {"party": party_id, "page_size": 100 if limit is None else min(limit, 100)}
    return await client.list(RELATIONSHIPS_URL, params=params, max_items=limit)


async def create_party_relationship(
    client: ERPClient,
    *,
    from_party: str,
    to_party: str,
    kind: str,
    title: str | None = None,
    is_primary: bool = False,
    notes: str | None = None,
) -> dict[str, Any]:
    """Link two parties, e.g. a person employed by an organisation.

    ``kind`` is a registered relationship kind (``employment``: from the
    person to the organisation, with the job ``title``; ``contact_for``: from
    the person to the party they are the contact for). The server refuses an
    unknown kind or a direction the kind does not allow and names the reason.
    """
    payload: dict[str, Any] = {
        "from_party": from_party, "to_party": to_party, "kind": kind, "is_primary": is_primary,
    }
    if title:
        payload["title"] = title
    if notes:
        payload["notes"] = notes
    return await client.post(RELATIONSHIPS_URL, json=payload)
