"""The send decision (docs/crm/phase2-party.md 1.6).

``can_contact(party, channel, purpose, address)`` is the one function every
sender asks before contacting a party. In order:

1. An active ``Suppression`` for ``(channel, sha256(address))`` → **no**
   (``suppressed:<reason>``). For transactional messages only a
   ``hard_bounce`` or ``legal`` suppression blocks: an opt-out, or an
   erased person who later orders again as a guest, still gets a receipt.
2. Transactional email → **yes** (``transactional``); no record needed.
3. The latest ``ConsentRecord`` for ``(party, channel, purpose)``, an
   address-specific row beating a blank-address one, ordered by
   ``occurred_at`` then ``recorded_at``: ``granted`` continues;
   ``withdrawn`` or no row → **no** (``withdrawn`` / ``no_consent``).
4. Marketing to a party that is not ``marketable`` → **no**
   (``not_marketable``). Otherwise **yes**.

Suppressions are address-keyed and party-independent, so they still answer
after erasure has deleted the party's addresses and consent records.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from django.core.exceptions import ValidationError

from .normalize import normalize_email, normalize_phone

TRANSACTIONAL_BLOCKING_REASONS = frozenset({"hard_bounce", "legal"})


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.allowed


def address_hash(normalized: str) -> str:
    """``Suppression.address_hash``: sha256 of the normalised address (1.6)."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def normalize_address(channel: str, address: str) -> str:
    """The key a suppression of ``channel`` is hashed from: 7.2 email, 7.3 E.164."""
    address = (address or "").strip()
    if not address:
        return ""
    if channel == "email":
        try:
            return normalize_email(address)
        except ValidationError:
            return ""
    if channel in ("sms", "phone"):
        if address.startswith("+") and address[1:].isdigit():
            return address
        return normalize_phone(address, None)[0]
    return address


def active_suppression(company_id, channel: str, address: str):
    """The active ``Suppression`` of this address on ``channel``, or None."""
    from .models import Suppression

    normalized = normalize_address(channel, address)
    if not normalized:
        return None
    return Suppression.objects.filter(
        company_id=company_id, channel=channel, address_hash=address_hash(normalized),
        lifted_at__isnull=True,
    ).order_by("created_at").first()


def can_contact(party, channel: str, purpose: str, address: str) -> Decision:
    """May golden send ``purpose`` (``marketing`` / ``transactional``) to
    ``address`` on ``channel`` for ``party``? See the module docstring."""
    from .models import ConsentRecord

    suppression = active_suppression(party.company_id, channel, address)
    if suppression is not None and (
        purpose != ConsentRecord.Purpose.TRANSACTIONAL
        or suppression.reason in TRANSACTIONAL_BLOCKING_REASONS
    ):
        return Decision(False, f"suppressed:{suppression.reason}")
    if purpose == ConsentRecord.Purpose.TRANSACTIONAL and channel == "email":
        return Decision(True, "transactional")
    normalized = normalize_address(channel, address)
    records = ConsentRecord.objects.filter(party=party, channel=channel, purpose=purpose)
    latest = (
        records.filter(address_normalized=normalized).order_by("-occurred_at", "-recorded_at").first()
        if normalized else None
    ) or records.filter(address_normalized="").order_by("-occurred_at", "-recorded_at").first()
    if latest is None:
        return Decision(False, "no_consent")
    if latest.state != ConsentRecord.State.GRANTED:
        return Decision(False, "withdrawn")
    if purpose == ConsentRecord.Purpose.MARKETING and not party.marketable:
        return Decision(False, "not_marketable")
    return Decision(True, "granted")


# --- provider write-backs (docs/crm/phase6-adapters.md 3.2) ---------------------
#
# What an inbound adapter may write about consent: withdrawals and
# suppressions, never a grant. A provider-side subscribe is not golden consent
# (A12); only a person's own act recorded in golden (portal, ERP, an
# evidenced import) grants.

#: How much an active suppression blocks. A new reason replaces the active one
#: only when it ranks higher (``uniq_suppression_active`` allows one per
#: address and channel): a hard bounce over an opt-out or a complaint, because
#: it also blocks transactional mail. Erasure is never replaced.
SUPPRESSION_RANK = {"opt_out": 1, "complaint": 1, "hard_bounce": 2, "legal": 2, "erasure": 3}


def record_withdrawal(party, channel: str, purpose: str, *, occurred_at, source: str,
                      source_detail: str, address: str = ""):
    """Append a ``withdrawn`` ConsentRecord, once per ``source_detail`` and address.

    ``(record, created)``. A second call with the same ``source_detail`` and
    ``address`` (a re-ingested provider event) returns the existing row and
    writes nothing. ``address=""`` is a party-wide record.
    Email marketing also clears ``Customer.accepts_marketing`` on every
    customer this party is the person of (``parties.sync.customer_person``:
    an individual account, one it holds, or a business account it is tagged
    to), as the portal does (``parties.portal.record_consent``), so the
    customer sync never appends a contradicting grant from a stale flag.
    Never writes a grant.
    """
    from django.db import transaction

    from .models import ConsentRecord

    normalized = normalize_address(channel, address) if address else ""
    existing = ConsentRecord.objects.filter(
        party=party, channel=channel, purpose=purpose, state=ConsentRecord.State.WITHDRAWN,
        source_detail=source_detail, address_normalized=normalized,
    ).first()
    if existing is not None:
        return existing, False
    with transaction.atomic():
        record = ConsentRecord.objects.create(
            party=party, channel=channel, purpose=purpose, state=ConsentRecord.State.WITHDRAWN,
            address_normalized=normalized, occurred_at=occurred_at, source=source,
            source_detail=source_detail[:255],
        )
        if (channel, purpose) == ("email", "marketing"):
            _clear_marketing_flag(party)
    return record, True


def _clear_marketing_flag(party) -> None:
    """``accepts_marketing=False`` on the customers whose mirrored person is ``party``."""
    import uuid

    from django.db.models import Q

    from customers.models import Customer

    from .mirror import CUSTOMER_LINK_SOURCE
    from .models import PartyRelationship
    from .relationship_registry import EMPLOYMENT
    from .sync import customer_person

    tagged = []
    for raw in PartyRelationship.objects.filter(
        from_party=party, source=CUSTOMER_LINK_SOURCE, kind=EMPLOYMENT, valid_to__isnull=True,
    ).values_list("metadata__customer", flat=True):
        try:
            tagged.append(uuid.UUID(str(raw)))
        except ValueError:
            continue
    candidates = Customer.objects.filter(
        Q(party=party) | Q(account_holder=party) | Q(pk__in=tagged), accepts_marketing=True,
    )
    mine = [c.pk for c in candidates if getattr(customer_person(c), "pk", None) == party.pk]
    if mine:
        # update(), not save(): the flag follows the ledger; a save would
        # re-run the customer sync (and its webhooks) for no new fact.
        Customer.objects.filter(pk__in=mine).update(accepts_marketing=False)


def record_suppression(company_id, channel: str, address: str, reason: str, *, source: str,
                       source_detail: str):
    """Suppress ``address`` on ``channel`` for ``reason``, idempotently.

    ``(suppression, created)``. With no active suppression, one is written.
    With one, the new reason replaces it only if it blocks more
    (:data:`SUPPRESSION_RANK`): the old row is lifted with
    ``lift_reason="superseded:<reason>"`` and the new one written in the same
    transaction. Any other repeat is a no-op returning the active row.
    ``(None, False)`` when the address does not normalise.
    """
    from django.db import transaction
    from django.utils import timezone

    from .models import Suppression

    normalized = normalize_address(channel, address)
    if not normalized:
        return None, False
    digest = address_hash(normalized)
    with transaction.atomic():
        active = Suppression.objects.select_for_update().filter(
            company_id=company_id, channel=channel, address_hash=digest, lifted_at__isnull=True,
        ).first()
        if active is not None:
            if SUPPRESSION_RANK.get(reason, 0) <= SUPPRESSION_RANK.get(active.reason, 0):
                return active, False
            Suppression.objects.filter(pk=active.pk).update(
                lifted_at=timezone.now(), lift_reason=f"superseded:{reason}"[:255])
        suppression = Suppression.objects.create(
            company_id=company_id, channel=channel, address_hash=digest, address_hint=_hint(normalized),
            reason=reason, source=source, source_detail=source_detail[:255],
        )
    return suppression, True


def _hint(normalized: str) -> str:
    """``j***@example.com`` / ``***0143`` (the 1.6 masked display): never the full address."""
    if "@" in normalized:
        local, _, domain = normalized.rpartition("@")
        return f"{local[:1]}***@{domain}"[:64]
    return f"***{normalized[-4:]}" if len(normalized) > 4 else "***"
