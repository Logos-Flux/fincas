"""Participant resolution (docs/crm/phase3-engagement.md 2.2).

**Participants never create parties.** An address is linked to a party only
when exactly one live party of the tenant holds it (``match-1``): a
``PartyEmail.email_normalized`` for an email address, a
``PartyPhone.number_e164`` for a phone number. ``match-N`` and no match leave
the row unlinked with its normalised address, so a later
``resolve_unlinked`` (``manage.py resolve_participants``, or the
PartyEmail / PartyPhone save hook in ``CommunicationsConfig.ready``) can link
it. Auto-creating parties from headers would turn every newsletter reply and
CC'd assistant into a contact.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from django.core.exceptions import ValidationError

from parties.normalize import normalize_email, normalize_phone

EMAIL, PHONE = "email", "phone"


@dataclass(frozen=True)
class Participant:
    """One participant as an adapter or the ERP describes it.

    ``address`` is an email address or a phone number, as it appeared.
    ``party`` links a known party directly (a staff member, or a party the
    caller already resolved); its contact point is still looked up.
    """

    role: str
    address: str = ""
    display_name: str = ""
    party: Any = None


def as_participant(value: Any) -> Participant:
    if isinstance(value, Participant):
        return value
    if isinstance(value, dict):
        unknown = set(value) - {"role", "address", "display_name", "party"}
        if unknown:
            raise ValueError(f"participant has unknown field(s) {sorted(unknown)}")
        return Participant(**value)
    raise ValueError(f"participant must be a Participant or a dict, got {type(value).__name__}")


def channel_of(address: str) -> str:
    """``email`` when the address has an ``@``, ``phone`` when it has any digit."""
    address = (address or "").strip()
    if "@" in address:
        return EMAIL
    if any(ch.isdigit() for ch in address):
        return PHONE
    return ""


def normalize_address(address: str) -> tuple[str, str]:
    """``(channel, normalised)``; normalised is blank when the address does not
    parse, so it never matches a party."""
    channel = channel_of(address)
    if channel == EMAIL:
        try:
            return EMAIL, normalize_email(address)
        except ValidationError:
            return EMAIL, ""
    if channel == PHONE:
        return PHONE, normalize_phone(address)[0]
    return "", ""


def _live(qs):
    return qs.filter(party__merged_into__isnull=True, party__erased_at__isnull=True)


def _registry(apps):
    """The model registry: a migration's historical ``apps``, else the live one.

    Every lookup here goes through it, so the data migration that builds
    participants (``communications.0003``) runs on the models of its own
    schema state, never on live models that may carry later columns.
    """
    if apps is None:
        from django.apps import apps as live_apps

        return live_apps
    return apps


def contact_points(company_id, channel: str, normalized: str, *, apps=None) -> list[tuple[Any, Any]]:
    """``(party_id, contact point id)`` of every live party holding the address."""
    registry = _registry(apps)
    PartyEmail = registry.get_model("parties", "PartyEmail")
    PartyPhone = registry.get_model("parties", "PartyPhone")

    if not normalized:
        return []
    if channel == EMAIL:
        qs = PartyEmail._base_manager.filter(company_id=company_id, email_normalized=normalized)
    elif channel == PHONE:
        qs = PartyPhone._base_manager.filter(company_id=company_id, number_e164=normalized)
    else:
        return []
    return list(_live(qs).order_by("party_id", "-is_primary", "created_at").values_list("party_id", "pk"))


def match(company_id, channel: str, normalized: str, *, prefer_party_id=None, apps=None):
    """``(party_id, contact point id)`` for ``match-1``, else ``(None, None)``.

    ``prefer_party_id`` (the document's customer, for golden-sent email)
    wins when that party holds the address, even if another party does too.
    """
    rows = contact_points(company_id, channel, normalized, apps=apps)
    if prefer_party_id is not None:
        for party_id, point_id in rows:
            if party_id == prefer_party_id:
                return party_id, point_id
    parties = {party_id for party_id, _ in rows}
    if len(parties) != 1:
        return None, None
    return rows[0]


def build(communication, spec: Participant, *, prefer_party_id=None, apps=None):
    """An unsaved ``CommunicationParticipant`` for ``spec``, resolved (match-1).

    ``apps`` is a migration's historical registry; the row is then an instance
    of that state's model (the role choices are constants, read from the live
    enum).
    """
    from .models import CommunicationParticipant as LiveParticipant

    CommunicationParticipant = _registry(apps).get_model("communications", "CommunicationParticipant")
    roles = LiveParticipant.Role.values
    if spec.role not in roles:
        raise ValueError(f"participant role {spec.role!r} is not one of {', '.join(roles)}")
    address = (spec.address or "").strip()
    channel, normalized = normalize_address(address)
    row = CommunicationParticipant(
        communication=communication, company_id=communication.company_id, role=spec.role,
        address_raw=address[:254], address_normalized=normalized[:254],
        display_name=(spec.display_name or "")[:255],
    )
    if spec.party is not None:
        if spec.party.company_id != communication.company_id:
            raise ValueError("a participant's party belongs to another company")
        row.party = spec.party
        point = next(
            (p for party_id, p in contact_points(communication.company_id, channel, normalized,
                                                 apps=apps)
             if party_id == spec.party.pk),
            None,
        )
    else:
        party_id, point = match(
            communication.company_id, channel, normalized, prefer_party_id=prefer_party_id, apps=apps,
        )
        row.party_id = party_id
    if point is not None:
        if channel == EMAIL:
            row.party_email_id = point
        else:
            row.party_phone_id = point
    return row


def resolve_unlinked(*, company_id=None, addresses: Iterable[str] | None = None,
                     dry_run: bool = False) -> int:
    """Link unlinked participants whose address now matches exactly one party.

    Narrow with ``company_id`` and ``addresses`` (normalised). Returns the
    number of rows linked (or that would be, with ``dry_run``).
    """
    from .models import CommunicationParticipant

    qs = CommunicationParticipant._base_manager.filter(party__isnull=True).exclude(address_normalized="")
    if company_id is not None:
        qs = qs.filter(company_id=company_id)
    if addresses is not None:
        addresses = [a for a in addresses if a]
        if not addresses:
            return 0
        qs = qs.filter(address_normalized__in=addresses)
    groups: dict[tuple, list] = defaultdict(list)
    for pk, company, normalized in qs.values_list("pk", "company_id", "address_normalized").iterator():
        groups[(company, normalized)].append(pk)
    linked = 0
    for (company, normalized), pks in groups.items():
        channel = EMAIL if "@" in normalized else PHONE
        party_id, point = match(company, channel, normalized)
        if party_id is None:
            continue
        linked += len(pks)
        if dry_run:
            continue
        values = {"party_id": party_id}
        values["party_email_id" if channel == EMAIL else "party_phone_id"] = point
        CommunicationParticipant._base_manager.filter(pk__in=pks, party__isnull=True).update(**values)
    return linked


def party_email_saved(sender, instance, raw=False, **kwargs):
    """A new or changed PartyEmail may resolve participants waiting on it."""
    if raw:
        return
    resolve_unlinked(company_id=instance.company_id, addresses=[instance.email_normalized])


def party_phone_saved(sender, instance, raw=False, **kwargs):
    if raw:
        return
    resolve_unlinked(company_id=instance.company_id, addresses=[instance.number_e164])
