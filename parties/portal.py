"""Party-side reads and writes for the customer portal
(docs/crm/phase2-party.md 2.4, 2.6, 5.3).

The portal resolver (``core.identity.resolve``) asks this module three
things, all scoped to the portal tenant (``GOLDEN_PORTAL_COMPANY``):

- :func:`find_candidates`: the live person parties an email could belong to
  (the matcher, 3.2).
- :func:`customer_for_user`: the Customer a portal login acts for, reached
  through the login's person party (``Party.user``), its merge survivor, and
  the commerce account that party holds.
- :func:`held_customers`: the commerce accounts a person party stands for.

The ``me/`` and ``me/consents/`` routes use :func:`me_party`,
:func:`phone_view`, :func:`consent_state` and :func:`record_consent`.

Nothing here crosses tenants: every query starts from the portal company or
from a party already resolved inside it (3.1).
"""

from __future__ import annotations

import hashlib
import logging

from django.db.models import Q
from django.utils import timezone

from .constants import PERSON
from .match import find
from .mirror import CUSTOMER_LINK_SOURCE, default_company_id, safe_email
from .relationship_registry import EMPLOYMENT

logger = logging.getLogger(__name__)

# A merge chain longer than this is treated as a cycle (2.6 "merged": 500).
MERGE_CHAIN_LIMIT = 32

# Persisted in PartyPhone.source for numbers a customer adds in the portal:
# never rename. Only rows with this source are editable by the customer.
PORTAL_SOURCE = "portal"

# The consent pairs a customer sees and may change in the portal (5.3).
PORTAL_CONSENTS = (("email", "marketing"), ("sms", "marketing"))

SUPPRESSED_MESSAGE = "We can't send marketing to this address. Contact us if you'd like that changed."


class PortalCompanyMissing(Exception):
    """No portal tenant: ``GOLDEN_PORTAL_COMPANY`` is unset and there is not
    exactly one Company (``parties.E001`` reports the multi-company case)."""


class MergeCycle(Exception):
    """A party's ``merged_into`` chain loops, is implausibly long, or leaves
    the tenant (a merge never crosses tenants, 3.1)."""


class PartyErased(Exception):
    """The login's party (or its merge survivor) is erased: the account is closed."""


class AmbiguousAccount(Exception):
    """A person party stands for several commerce accounts (2.1: one at most)."""

    def __init__(self, customers):
        super().__init__(f"{len(customers)} accounts")
        self.customers = customers


def portal_company_id():
    """The portal tenant's Company pk (2.4), or raise :class:`PortalCompanyMissing`."""
    from django.core.exceptions import ValidationError

    from core.models import Company

    try:
        company_id = default_company_id(Company)
    except (ValidationError, ValueError):  # GOLDEN_PORTAL_COMPANY is not a pk (parties.W001)
        company_id = None
    if company_id is None:
        raise PortalCompanyMissing()
    return company_id


def survivor(party):
    """Follow ``merged_into`` to the live survivor (2.6 "merged")."""
    from .models import Party

    seen = {party.pk}
    company_id = party.company_id
    while party.merged_into_id is not None:
        if party.merged_into_id in seen or len(seen) > MERGE_CHAIN_LIMIT:
            raise MergeCycle(party.pk)
        seen.add(party.merged_into_id)
        party = Party.objects.get(pk=party.merged_into_id)
        if party.company_id != company_id:
            raise MergeCycle(party.pk)
    return party


def find_candidates(email: str) -> list:
    """Live person parties of the portal tenant holding ``email``, oldest first.

    The matcher compares normalised addresses (7.2), which fold look-alikes
    onto plain ones: NFKC + casefold turn ``straße`` into ``strasse``, ``ﬁ``
    into ``fi`` and full-width letters into ASCII. A login must never reach a
    party through such a fold, so a candidate is kept only when one of its
    addresses, as written, equals E apart from case (the step-4 rule,
    ``Lower(email) == lower(E)``). Look-alike parties are then simply not
    candidates: a different mailbox gets its own account. Both sides must be
    ASCII before ``lower()`` is trusted: Python lower-cases the Kelvin sign
    (U+212A) to ``k``.
    """
    candidates = find(portal_company_id(), email=email).candidates
    return [party for party in candidates if _holds_exactly(party, email)]


def _same_address(raw: str, email: str) -> bool:
    raw, email = (raw or "").strip(), (email or "").strip()
    return raw.isascii() and email.isascii() and raw.lower() == email.lower()


def _holds_exactly(party, email: str) -> bool:
    return any(_same_address(raw, email) for raw in party.emails.values_list("email", flat=True))


def held_customers(party) -> list:
    """The commerce accounts ``party`` stands for, oldest first.

    As account holder (a login), as the countersigning person of an
    individual account, or as the customer person of a business account
    (the ``employment`` relationship the sync tags with the customer).
    """
    from customers.models import Customer

    from .models import PartyRelationship

    tagged = [
        rel.metadata.get("customer")
        for rel in PartyRelationship.objects.filter(
            from_party=party, kind=EMPLOYMENT, source=CUSTOMER_LINK_SOURCE, valid_to__isnull=True,
        )
    ]
    tagged = [pk for pk in tagged if pk]
    query = Q(account_holder=party) | Q(party=party) | Q(pk__in=tagged)
    return list(
        Customer.objects.filter(query, company_id=party.company_id)
        .select_related("user").distinct().order_by("created_at", "pk")
    )


def held_customer(party):
    """The one commerce account ``party`` stands for, None, or raise
    :class:`AmbiguousAccount` (the portal never picks among several)."""
    customers = held_customers(party)
    if len(customers) > 1:
        raise AmbiguousAccount(customers)
    return customers[0] if customers else None


def party_for_user(user, company_id):
    """The login's person party in this tenant (``uniq_party_user_per_company``),
    its merge survivor, or None. Raises :class:`PartyErased` when it is erased."""
    from .models import Party

    party = Party.objects.filter(company_id=company_id, user=user).first()
    if party is None:
        return None
    party = survivor(party)
    if party.erased_at is not None:
        raise PartyErased(party.pk)
    return party


def customer_for_user(user):
    """The Customer a portal login acts for, in the portal tenant, or None.

    1. The login's person party (following merges) → the account it holds.
       The account's dual-written ``Customer.user`` must be this login.
    2. Otherwise ``Customer.user`` in the portal tenant (the dual-written
       link, kept until Phase 4). A customer whose login has no account
       holder yet (the sync failed, or a row written around ``save()``) gets
       ``parties.sync`` run on demand, the same repair ``reconcile_parties
       --fix`` makes.

    Raises :class:`PortalCompanyMissing` or :class:`MergeCycle`.
    """
    from customers.models import Customer

    from .sync import run_guarded, sync_from_customer

    company_id = portal_company_id()
    try:
        party = party_for_user(user, company_id)
    except PartyErased:
        return None  # closed: never fall back to Customer.user for an erased person
    if party is not None:
        try:
            customer = held_customer(party)
        except AmbiguousAccount:
            logger.warning("portal: party %s holds several accounts; refusing user %s", party.pk, user.pk)
            return None
        if customer is not None and customer.user_id == user.pk:
            return customer

    customer = Customer.objects.filter(
        Q(company_id=company_id) | Q(company__isnull=True), user=user,
    ).first()
    if customer is None:
        return None
    if customer.account_holder_id is None or customer.company_id is None:
        run_guarded(sync_from_customer, customer, origin="portal")
        customer.refresh_from_db()
        if customer.company_id != company_id:
            return None
    return customer


def new_portal_person(company_id, first_name: str, last_name: str, email: str):
    """A fresh person party for a portal sign-up (2.6 match-0, ``origin=portal``).

    Created here, not by the customer sync's matcher: the sync matches on the
    normalised address, which folds look-alikes (``straße`` / ``strasse``), and
    a sign-up must never adopt somebody else's party through that fold.
    """
    from .sync import _new_person

    return _new_person(company_id, first_name, last_name, email, "portal")


def link_login(party, user) -> bool:
    """Set ``party.user`` when free (one login per party, one party per login
    per tenant). Returns False on a conflict, which is logged."""
    from .models import Party

    if party.user_id == user.pk:
        return True
    taken = Party.objects.filter(company_id=party.company_id, user=user).exclude(pk=party.pk).exists()
    if party.user_id is not None or taken:
        logger.warning("portal: login %s not linked to party %s (conflict)", user.pk, party.pk)
        return False
    party.user = user
    party.save(update_fields=["user", "updated_at"])
    return True


def mark_email_verified(party, email: str) -> None:
    """Record that the provider confirmed ``email`` for this party (2.6)."""
    from .models import PartyEmail

    key = safe_email(email)
    if not key:
        return
    # The exact address only, never a look-alike sibling sharing the normalised key.
    rows = [row.pk for row in PartyEmail.objects.filter(party=party, email_normalized=key, verified_at__isnull=True)
            if _same_address(row.email, email)]
    PartyEmail.objects.filter(pk__in=rows).update(verified_at=timezone.now())


# --- me/ --------------------------------------------------------------------------


def me_party(customer, user):
    """The customer's own person party: the account holder, else the login's party.

    Only a party carrying this login: an account holder that drifted onto
    someone else's party (a staff edit, a bad merge), with or without a login
    of its own, is never shown or written through the portal (``me/`` then
    answers 404).
    """
    from .models import Party

    if customer.account_holder_id:
        party = Party.objects.filter(pk=customer.account_holder_id, company_id=customer.company_id).first()
        if party is not None:
            party = survivor(party)
            if party.erased_at is None and party.kind == PERSON and party.user_id == user.pk:
                return party
    if customer.company_id is None:
        return None
    try:
        return party_for_user(user, customer.company_id)
    except PartyErased:
        return None


def account_for(customer, party):
    """The organisation a person holds the account for (name only), or None."""
    from .models import Party

    if customer.party_id is None or customer.party_id == party.pk:
        return None
    org = Party.objects.filter(pk=customer.party_id, company_id=customer.company_id).first()
    return {"name": org.display_name} if org is not None else None


def mask_number(number: str) -> str:
    """Everything but the last four digits masked (5.3)."""
    digits = [c for c in number if c.isdigit()]
    if len(digits) <= 4:
        return "•" * len(digits)
    return "•" * (len(digits) - 4) + "".join(digits[-4:])


def phone_view(phone) -> dict:
    """A phone as the portal shows it: the full number only for the primary."""
    number = phone.number_e164 or phone.number_raw
    return {
        "id": str(phone.pk),
        "number": number if phone.is_primary else mask_number(number),
        "extension": phone.extension if phone.is_primary else "",
        "purpose": phone.purpose,
        "is_primary": phone.is_primary,
        "verified": phone.verified_at is not None,
        "editable": phone.source == PORTAL_SOURCE,
    }


# --- me/consents/ -----------------------------------------------------------------


def address_hash(value: str) -> str:
    """``Suppression.address_hash``: sha256 of the normalised address (1.6)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def primary_address(party, channel: str) -> str:
    """The normalised address a channel reaches: primary email, or primary E.164."""
    if channel == "email":
        return party.primary_email_normalized
    return party.primary_phone_e164


def latest_consent(party, channel: str, purpose: str, address: str = ""):
    """Latest record for the pair: party-wide rows and rows for ``address``,
    by ``occurred_at`` then ``recorded_at`` (1.6 step 3)."""
    from .models import ConsentRecord

    return ConsentRecord.objects.filter(
        party=party, channel=channel, purpose=purpose, address_normalized__in=["", address],
    ).order_by("-occurred_at", "-recorded_at").first()


def is_suppressed(party, channel: str, address: str) -> bool:
    from .models import Suppression

    if not address:
        return False
    return Suppression.objects.filter(
        company_id=party.company_id, channel=channel, address_hash=address_hash(address),
        lifted_at__isnull=True,
    ).exists()


def consent_state(party) -> list[dict]:
    """Current state per portal pair. Suppressions show only a message (5.3)."""
    out = []
    for channel, purpose in PORTAL_CONSENTS:
        address = primary_address(party, channel)
        latest = latest_consent(party, channel, purpose, address)
        row = {
            "channel": channel,
            "purpose": purpose,
            "state": latest.state if latest is not None else "unknown",
            "occurred_at": latest.occurred_at if latest is not None else None,
            "suppressed": is_suppressed(party, channel, address),
        }
        if row["suppressed"]:
            row["message"] = SUPPRESSED_MESSAGE
        out.append(row)
    return out


def record_consent(party, customer, *, channel, purpose, state, user, evidence):
    """Append a portal consent record (party-wide on that channel).

    Email marketing is also a Customer-owned fact (2.1, ``accepts_marketing``):
    the flag is brought in line in the same transaction, so the customer sync
    does not append a contradicting record on the next save.
    """
    from django.db import transaction

    from .models import ConsentRecord

    with transaction.atomic():
        record = ConsentRecord.objects.create(
            party=party, channel=channel, purpose=purpose, state=state,
            occurred_at=timezone.now(), source="portal", source_detail="portal:me/consents",
            evidence=evidence, recorded_by=user,
        )
        if (channel, purpose) == ("email", "marketing"):
            from .sync import customer_person

            wanted = state == ConsentRecord.State.GRANTED
            if customer.accepts_marketing != wanted:
                mirrored = customer_person(customer)
                if mirrored is not None and mirrored.pk == party.pk:
                    customer.accepts_marketing = wanted
                    customer.party_origin = "portal"
                    customer.save(update_fields=["accepts_marketing", "updated_at"])
                else:
                    # The flag mirrors onto another party (the account holder
                    # before a merge): changing it would make the next sync
                    # append an evidence-less record there. Leave it; the
                    # merge (step 6) repoints the holder, reconcile reports it.
                    logger.warning(
                        "portal: consent on party %s not mirrored to customer %s (its person is %s)",
                        party.pk, customer.pk, getattr(mirrored, "pk", None),
                    )
    return record
