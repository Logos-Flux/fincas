"""Erase a person's personal data (docs/crm/phase2-party.md 7.4, step 12).

``erase_party(party, actor, reason)`` runs in one transaction. It scrubs the
person everywhere golden copied them, keeps every row that accounting or
provenance needs, and cannot be undone. In order:

1. **Suppressions first.** One active ``Suppression(reason="erasure")`` per
   address the person had (email; SMS and phone for each number), so they
   are never contacted for marketing again even after the addresses are
   gone. Existing active suppressions stay (their reason may block more),
   lose their ``address_hint`` and are marked ``source_detail="erasure; …"``
   so ``erased_address`` and the importers still see the erasure.
2. **Commerce rows.** A business account the person only holds or speaks
   for stays open and loses just the person's fields (names, phone, email,
   login). The person's own accounts (each Customer whose party is them, or
   an individual one they hold) are tombstoned: names become ``Erased``, the email
   ``erased+<customer uuid>@invalid`` (unique, so the per-tenant constraint
   holds), phone, notes, tax-exemption details, legacy source ids (only
   ``stripe_customer_id`` stays, for refunds) and the login link are
   cleared, ``is_active`` is False. Orders, quotes, invoices
   and payments stay, still pointing at the tombstone (GDPR art. 17(3)(b):
   tax and accounting retention). A ``CustomerAddress`` an order, quote or
   invoice references keeps its postal lines (blanked names and phone) for
   the same reason; the others are deleted. ``Quote.sent_to_email`` /
   ``accepted_by_*``, free-text notes, order IPs, raw payment payloads,
   pick-ticket labels, guest-order emails and ``EmailLog`` recipients of
   those documents are scrubbed too; ``result.kept`` lists what stays.
2b. **CRM text** (Phase 3): engagement titles (tombstoned) and loss notes,
   stage-change notes, task titles and descriptions, communication
   subjects, bodies, summaries and metadata, participant addresses and
   names, raw provider payloads (``{"erased": true}``) and touchpoint URLs
   and click ids. The person's own rows lose their text whole; rows where
   they are only a contact, or that quote one of their addresses, lose the
   fields that name or reach them. Every row stays (``_scrub_crm_text``).
3. **CustomerContact, Lead, ActivityLog:** personal fields blanked; the
   rows stay so the documents that reference them still resolve.
4. **Party side:** ``PartyEmail``, ``PartyPhone``, ``PartyAddress``,
   ``ConsentRecord`` (the suppressions carry the withdrawal now),
   ``PartyRelationship`` and the login's ``UserIdentity`` rows are deleted.
   The Party row stays as ``"Erased party"`` with ``erased_at`` set.
5. **Login:** ``Party.user`` is cleared. The ``auth.User`` is deactivated
   and scrubbed (not deleted) unless it still reaches another live party
   (the same human in another tenant).
6. **ExternalRefs stay**, re-keyed to ``erased:<sha256(source id)>`` with
   ``metadata.erased``, so ``core.provenance.is_erased_source`` lets every
   importer skip the source record instead of resurrecting the person
   (1.7). A source id is often the person's name (ERPNext, eBay), hence the
   hash.
6b. **CRM sidecars** (``metadata`` on Engagement, Task, Touchpoint and
   PartyRole; phase4-fields.md 3.6). On the person's own rows only system
   keys (``core.provenance.is_system_metadata_key``: ``{"lead": <pk>}``,
   ``backfill``…) that do not name or reach them stay, so the syncs stay
   idempotent; provider, ``custom.*`` (until FieldDefinitions can say which
   are not personal) and any other client key go. On rows where they are only a contact
   (an engagement they are an ``EngagementContact`` on, its tasks and
   touchpoints) or that quote one of their addresses or numbers, only the
   keys that name or reach them go.
7. **AuditLog:** ``changes`` of every entry about an erased row, or quoting
   one of the person's addresses, becomes ``{"erased": true}``. One new
   entry (``action="erase"``) records who erased whom, when and why.
   Webhook delivery payloads, ``unify`` staging rows and Django admin
   history that copy the person are scrubbed as well.
8. ``party_erased`` is sent after commit for each erased party id.

**Merges.** A merge says two parties are one person, so erasure covers the
whole merge tree of the party: every party merged into it (their rows still
hold names), and, when the party itself was merged away, every row its
merges moved onto the survivor (contact points, consent, customers,
contacts, leads, refs) plus any survivor column the merge took from it
(names, login). The survivor's own data is left alone. Each affected
``PartyMergeLog`` loses its ``before`` snapshot (it held the PII), so those
merges can no longer be undone. A CRM import's write log loses the
``before`` snapshot of every row of the person it updated, so undo refuses
those rows instead of writing the person back (phase5-importer.md 4.5).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from urllib.parse import quote

from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.db.models import Q, TextField
from django.db.models.functions import Cast
from django.utils import timezone

from core.provenance import erased_source_id, is_system_metadata_key

from .consent import address_hash
from .constants import ORGANISATION, PERSON
from .mirror import CUSTOMER_LINK_SOURCE, safe_email
from .normalize import normalize_phone, person_display_name
from .relationship_registry import EMPLOYMENT
from .role_registry import STAFF_ROLE

ERASED_PARTY_NAME = "Erased party"
ERASED_NAME = "Erased"
ERASED_ACTIVITY_SUBJECT = "Erased"
ERASED_EMAIL_DOMAIN = "invalid"  # RFC 2606: never delivers
# What a scrubbed JSON copy of the person becomes (staged rows, webhook
# payloads, raw provider payloads); ``communications.ingest`` reads it too.
ERASED_PAYLOAD = {"erased": True}
# Survivor columns a merge may have copied from a loser (parties.merge.PICKABLE_FIELDS).
NAME_COLUMNS = (
    "given_name", "family_name", "display_name", "legal_name", "primary_domain",
    "registration_number", "tax_id",
)
PHONE_CHANNELS = ("sms", "phone")
TOMBSTONE_WORDS = frozenset({"erased", "erased party", "unnamed"})


class ErasureError(ValueError):
    """Erasure was refused; the message says why. Nothing was written."""


def tombstone_email(pk) -> str:
    """``erased+<uuid>@invalid``: unique per row, so ``Customer.email`` stays unique."""
    return f"erased+{pk}@{ERASED_EMAIL_DOMAIN}"


def is_tombstone_email(value, pk=None) -> bool:
    value = (value or "").strip().lower()
    if pk is not None:
        return value == tombstone_email(pk)
    return value.startswith("erased+") and value.endswith(f"@{ERASED_EMAIL_DOMAIN}")


def is_erased_customer(customer) -> bool:
    """A Customer row erasure turned into a tombstone (``parties.sync`` skips it)."""
    return is_tombstone_email(customer.email, customer.pk)


# ``Suppression.source_detail`` of an address erasure touched. An address that
# already had an active suppression (an opt-out, a bounce) keeps it (its
# reason may block transactional mail an erasure suppression would not), and
# erasure marks it with this prefix instead of adding a second active row.
ERASURE_DETAIL = "erasure"


def is_erasure_suppression(suppression) -> bool:
    """Whether an active suppression records an erasure (its own, or one that
    erasure marked: ``source_detail`` ``erasure`` / ``erasure; <earlier>``)."""
    detail = suppression.source_detail or ""
    return (suppression.reason == "erasure" or detail == ERASURE_DETAIL
            or detail.startswith(ERASURE_DETAIL + "; "))


def erasure_suppression_q():
    """The same test as a ``Suppression`` filter, for bulk lookups (importers)."""
    return (Q(reason="erasure") | Q(source_detail=ERASURE_DETAIL)
            | Q(source_detail__startswith=ERASURE_DETAIL + "; "))


def erased_address(company_id, email) -> bool:
    """Whether ``email`` belongs to a person erased in this tenant (an active
    erasure suppression). Importers that match people by email, not by a
    source id, check this before creating anyone."""
    from .consent import active_suppression

    suppression = active_suppression(company_id, "email", email or "")
    return suppression is not None and is_erasure_suppression(suppression)


@dataclass
class ErasureResult:
    party_id: uuid.UUID
    company_id: int | None
    erased_party_ids: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    kept: list = field(default_factory=list)
    # One per run (phase6-adapters.md 4.1): the ``<provider>.erase`` outbox
    # rows are keyed by it, and the audit entry records it.
    erasure_id: uuid.UUID | None = None

    def as_dict(self) -> dict:
        return {
            "party_id": str(self.party_id),
            "erasure_id": str(self.erasure_id) if self.erasure_id else "",
            "erased_party_ids": [str(pk) for pk in self.erased_party_ids],
            "counts": dict(self.counts),
            "kept": list(self.kept),
        }


def erasure_summary(result: ErasureResult) -> str:
    """One sentence for staff: what was kept and why."""
    kept = []
    for item in result.kept:
        if item["model"] == "orders":
            kept.append(f"{item['orders']} order(s), {item['quotes']} quote(s) and {item['invoices']} invoice(s)")
        elif item["model"] == "customers.CustomerAddress":
            kept.append(f"{len(item['ids'])} address(es) on those documents")
    text = f"{result.counts.get('suppressions', 0)} address suppression(s) recorded."
    if kept:
        text += " Kept for accounting, under the erased name: " + "; ".join(kept) + "."
    return text


# --- the footprint ----------------------------------------------------------------


@dataclass
class _Footprint:
    """Every row erasure touches, by primary key."""

    parties: set = field(default_factory=set)
    emails: set = field(default_factory=set)
    phones: set = field(default_factory=set)
    addresses: set = field(default_factory=set)
    consents: set = field(default_factory=set)
    relationships: set = field(default_factory=set)
    customers: set = field(default_factory=set)
    # The person's own accounts (individual customers, or any whose party is
    # in the tree), tombstoned in full. The rest are business accounts the
    # person only holds or speaks for: only the person's fields go.
    personal_customers: set = field(default_factory=set)
    contacts: set = field(default_factory=set)
    leads: set = field(default_factory=set)
    refs: set = field(default_factory=set)
    users: set = field(default_factory=set)
    loser_logs: list = field(default_factory=list)  # merges that moved our rows onto another party
    logs: set = field(default_factory=set)  # every merge log whose snapshot holds our rows
    # Filled by the scrub (``_scrub_crm_text``, ``_scrub_activity_copies``):
    # the CRM rows whose text erasure scrubbed. The provider collectors read
    # them (a ClickUp task mirroring one is renamed at ClickUp), and their
    # mirror refs are flagged erased (phase6-adapters.md 4.1, 4.3).
    tasks: set = field(default_factory=set)
    communications: set = field(default_factory=set)
    touchpoints: set = field(default_factory=set)


MOVED_LABELS = {
    "parties.PartyEmail": "emails",
    "parties.PartyPhone": "phones",
    "parties.PartyAddress": "addresses",
    "parties.ConsentRecord": "consents",
    "parties.PartyRelationship": "relationships",
    "customers.Customer": "customers",
    "customers.CustomerContact": "contacts",
    "crm.Lead": "leads",
    "core.ExternalRef": "refs",
}


def _merge_tree(party) -> set:
    """``party`` plus every party merged into it, transitively."""
    from .models import Party

    tree, frontier = {party.pk}, {party.pk}
    while frontier:
        frontier = set(
            Party._base_manager.filter(merged_into_id__in=frontier).values_list("pk", flat=True)
        ) - tree
        tree |= frontier
    return tree


def _collect(party) -> _Footprint:
    from django.contrib.contenttypes.models import ContentType

    from core.models import ExternalRef
    from crm.models import Lead
    from customers.models import Customer, CustomerAddress, CustomerContact

    from .models import (
        ConsentRecord,
        Party,
        PartyAddress,
        PartyEmail,
        PartyMergeLog,
        PartyPhone,
        PartyRelationship,
    )

    fp = _Footprint(parties=_merge_tree(party))
    ids = fp.parties

    # Merges: the tree's own (their snapshots hold our rows), and the ones
    # that moved our rows onto a party outside the tree.
    for log in PartyMergeLog.objects.filter(Q(loser_id__in=ids) | Q(survivor_id__in=ids)):
        fp.logs.add(log.pk)
        if log.loser_id in ids and log.survivor_id not in ids and log.undone_at is None:
            fp.loser_logs.append(log)
            for label, attr in MOVED_LABELS.items():
                getattr(fp, attr).update(uuid.UUID(pk) for pk in log.moved.get(label, ()))

    def pks(qs):
        return set(qs.values_list("pk", flat=True))

    fp.emails |= pks(PartyEmail._base_manager.filter(party_id__in=ids))
    fp.phones |= pks(PartyPhone._base_manager.filter(party_id__in=ids))
    fp.addresses |= pks(PartyAddress._base_manager.filter(party_id__in=ids))
    fp.consents |= pks(ConsentRecord._base_manager.filter(party_id__in=ids))
    # Party-less records of the person's addresses (an import keeps a
    # withdrawal no single party held: crm_import.appliers.consent).
    own = set(PartyEmail._base_manager.filter(pk__in=fp.emails).values_list("email_normalized", flat=True))
    own |= set(PartyPhone._base_manager.filter(pk__in=fp.phones).values_list("number_e164", flat=True))
    own.discard("")
    if own:
        fp.consents |= pks(ConsentRecord._base_manager.filter(
            company_id=party.company_id, party__isnull=True, address_normalized__in=own))
    fp.relationships |= pks(PartyRelationship._base_manager.filter(
        Q(from_party_id__in=ids) | Q(to_party_id__in=ids)))

    # Commerce accounts that are this person's: as party, as account holder,
    # or as the person a business customer's own facts are mirrored onto.
    tagged = set()
    for meta in PartyRelationship._base_manager.filter(
        from_party_id__in=ids, source=CUSTOMER_LINK_SOURCE, kind=EMPLOYMENT,
    ).values_list("metadata", flat=True):
        if isinstance(meta, dict) and meta.get("customer"):
            try:
                tagged.add(uuid.UUID(str(meta["customer"])))
            except ValueError:
                continue
    fp.customers |= pks(Customer._base_manager.filter(
        Q(party_id__in=ids) | Q(account_holder_id__in=ids) | Q(pk__in=tagged)))
    fp.personal_customers = pks(Customer._base_manager.filter(pk__in=fp.customers).filter(
        Q(party_id__in=ids) | ~Q(customer_type="business")))
    fp.contacts |= pks(CustomerContact._base_manager.filter(party_id__in=ids))
    fp.leads |= pks(Lead._base_manager.filter(
        Q(party_id__in=ids) | Q(converted_to_customer_id__in=fp.personal_customers)))

    fp.users |= {uid for uid in Party._base_manager.filter(pk__in=ids).values_list("user_id", flat=True) if uid}
    fp.users |= {uid for uid in Customer._base_manager.filter(pk__in=fp.customers).values_list(
        "user_id", flat=True) if uid}

    ct = ContentType.objects.get_for_models(Party, Customer, CustomerAddress, CustomerContact, Lead)
    address_ids = pks(CustomerAddress._base_manager.filter(customer_id__in=fp.customers))
    targets = Q()
    for model, object_ids in ((Party, ids), (Customer, fp.customers), (CustomerAddress, address_ids),
                              (CustomerContact, fp.contacts), (Lead, fp.leads)):
        if object_ids:
            targets |= Q(content_type=ct[model], object_id__in=object_ids)
    if targets:
        fp.refs |= pks(ExternalRef._base_manager.filter(targets))
    return fp


def _refuse_if_blocked(party, fp) -> None:
    from django.contrib.auth import get_user_model

    from core.models import UserProfile

    from .models import PartyRole

    if party.erased_at is not None:
        raise ErasureError("This party has already been erased.")
    if party.is_self:
        raise ErasureError("This is your own organization; it cannot be erased.")
    if party.kind == ORGANISATION:
        raise ErasureError(
            "Erasure is for people. An organization holds no personal data of its own: "
            "erase the people related to it instead."
        )
    if PartyRole.objects.filter(party_id__in=fp.parties, role=STAFF_ROLE, valid_to__isnull=True).exists():
        raise ErasureError("This person is staff. Close their staff role and offboard their login first.")
    User = get_user_model()
    if fp.users and (
        User.objects.filter(pk__in=fp.users).filter(Q(is_staff=True) | Q(is_superuser=True)).exists()
        or UserProfile.objects.filter(user_id__in=fp.users).exists()
    ):
        raise ErasureError("This person's login is a staff login. Offboard it before erasing them.")


# --- the service ------------------------------------------------------------------


def erase_party(party, actor=None, reason: str = "", *, source: str = "erp") -> ErasureResult:
    """Erase ``party`` (a person) and every copy of their personal data. See the module docstring.

    ``actor`` is the ``auth.User`` doing it (None from a management command
    run without ``--actor``). ``reason`` is required and kept in the audit
    entry. ``source`` is the ``Suppression.source`` (``erp`` / ``api``).
    Raises ``ErasureError`` (nothing written) when the party may not be erased.
    """
    reason = (reason or "").strip()
    if not reason:
        raise ErasureError("Give a reason for the erasure; it is kept in the audit log.")
    with transaction.atomic():
        party, fp = _lock_footprint(party.pk)
        _refuse_if_blocked(party, fp)
        now = timezone.now()
        result = ErasureResult(party_id=party.pk, company_id=party.company_id,
                               erased_party_ids=sorted(fp.parties, key=str), erasure_id=uuid.uuid4())
        counts = result.counts

        needles = _collect_addresses(fp)
        lowered = reason.lower()
        if any(n.lower() in lowered for n in needles["raw"] | needles["names"]):
            raise ErasureError(
                "The reason must not repeat the person's name, address or number: it is kept after erasure."
            )
        counts["suppressions"] = _suppress(party.company_id, needles, actor, source)
        _restore_survivors(fp)  # before the snapshots are scrubbed
        counts.update(_scrub_commerce(fp, party, result.kept, needles))
        counts["crm_sidecars"] = _scrub_crm_sidecars(fp, needles, party.company_id)
        counts.update(_scrub_party_side(fp, now))
        counts["users_deactivated"] = _scrub_users(fp)
        ref_keys = _ref_keys(fp)  # before _flag_refs re-keys them
        # The provider records golden wrote about the person, named while
        # their refs are still in clear (phase6-adapters.md 4.1).
        sweep = _collect_provider_erasures(fp, party.company_id)
        counts["external_refs_flagged"] = _flag_refs(fp, now)
        counts["mirror_refs_flagged"] = _flag_mirror_refs(fp, now)
        _mark_scrubbed_tasks(fp, now)
        counts["merge_logs_scrubbed"] = _scrub_merge_logs(fp)
        counts["import_write_logs_scrubbed"] = _scrub_import_write_logs(fp, needles, party.company_id)
        counts["audit_entries_scrubbed"] = _scrub_audit(fp, needles, party.company_id)
        counts.update(_scrub_traces(fp, needles, party.company_id, ref_keys))
        counts.update(_scrub_outbox(fp, needles, party.company_id))
        counts["outbox_deleted"] += _purge_adapter_queues(fp, party.company_id)
        counts["provider_erasures"] = _enqueue_provider_erasures(sweep, result)
        _audit(party, actor, reason, result)
        _emit_erased(fp.parties, party.company_id)
    return result


def _collect_addresses(fp) -> dict:
    """``{"email": {normalized}, "phone": {e164}, "raw": {as typed}}`` before anything is deleted."""
    from crm.models import Lead
    from customers.models import Customer, CustomerContact

    from .models import PartyEmail, PartyPhone

    emails, phones, raw = set(), set(), set()
    for normalized, typed in PartyEmail._base_manager.filter(pk__in=fp.emails).values_list(
            "email_normalized", "email"):
        emails.add(normalized)
        raw.add(typed)
    for e164, typed in PartyPhone._base_manager.filter(pk__in=fp.phones).values_list("number_e164", "number_raw"):
        if e164:
            phones.add(e164)
        raw.add(typed)
    for model, pks in ((Customer, fp.customers), (CustomerContact, fp.contacts), (Lead, fp.leads)):
        for email, phone in model._base_manager.filter(pk__in=pks).values_list("email", "phone"):
            if email and not is_tombstone_email(email):
                raw.add(email)
                if safe_email(email):
                    emails.add(safe_email(email))
            if phone:
                raw.add(phone)
                e164, _ext = normalize_phone(phone, None)
                if e164:
                    phones.add(e164)
    raw = {r.strip() for r in raw if r and len(r.strip()) >= 6}
    from .models import Party

    names = set()
    for given, family in Party._base_manager.filter(pk__in=fp.parties).values_list("given_name", "family_name"):
        names |= {given.strip(), family.strip(), f"{given} {family}".strip()}
    for model, pks in ((Customer, fp.customers), (CustomerContact, fp.contacts), (Lead, fp.leads)):
        for first, last in model._base_manager.filter(pk__in=pks).values_list("first_name", "last_name"):
            names |= {(first or "").strip(), (last or "").strip(), f"{first} {last}".strip()}
    names = {n for n in names if len(n) >= 3 and n.lower() not in TOMBSTONE_WORDS}
    return {"email": emails, "phone": phones, "raw": raw | emails | phones, "names": names}


def _suppress(company_id, needles, actor, source) -> int:
    """1. One active erasure suppression per address and channel; hints blanked."""
    from .models import Suppression

    keys = [("email", e) for e in sorted(needles["email"])]
    keys += [(channel, p) for p in sorted(needles["phone"]) for channel in PHONE_CHANNELS]
    created = 0
    for channel, address in keys:
        digest = address_hash(address)
        active = Suppression.objects.filter(
            company_id=company_id, channel=channel, address_hash=digest, lifted_at__isnull=True)
        existing = active.first()
        if existing is not None:
            detail = existing.source_detail or ""
            if not is_erasure_suppression(existing):
                detail = f"{ERASURE_DETAIL}; {detail}"[:255] if detail else ERASURE_DETAIL
            active.update(address_hint="", source_detail=detail)
            continue
        Suppression.objects.create(
            company_id=company_id, channel=channel, address_hash=digest,
            reason=Suppression.Reason.ERASURE, source=source, source_detail="erasure",
            created_by=actor if getattr(actor, "pk", None) else None,
        )
        created += 1
    # Hints of earlier suppressions of these addresses that were lifted.
    Suppression.objects.filter(
        company_id=company_id, address_hash__in=[address_hash(a) for _c, a in keys],
    ).exclude(address_hint="").update(address_hint="")
    return created


def _restore_survivors(fp) -> None:
    """Undo what a merge copied onto a survivor *from* an erased party: the
    columns it took (names, ids) and the login it moved (parties.merge)."""
    from .models import Party

    for log in fp.loser_logs:
        rows = (log.before or {}).get("parties.Party", {})
        before_s, before_l = rows.get(str(log.survivor_id)), rows.get(str(log.loser_id))
        if not before_s or not before_l:
            continue
        survivor = Party._base_manager.filter(pk=log.survivor_id).first()
        if survivor is None:
            continue
        updates = {}
        for name in NAME_COLUMNS:
            took = before_l.get(name) or ""
            if took and getattr(survivor, name) == took and before_s.get(name) != took:
                updates[name] = before_s.get(name) or ""
        if survivor.kind == PERSON and not survivor.display_name_locked and (
                {"given_name", "family_name"} & set(updates)):
            # The merge re-derived the display name from the names it took.
            updates["display_name"] = person_display_name(
                updates.get("given_name", survivor.given_name), updates.get("family_name", survivor.family_name),
            ) or before_s.get("display_name") or "Unnamed"
        if "display_name" in updates and not updates["display_name"]:
            updates["display_name"] = before_s.get("display_name") or "Unnamed"
        moved_login = before_l.get("user_id")
        if moved_login is not None and survivor.user_id == moved_login and before_s.get("user_id") is None:
            updates["user_id"] = None
            fp.users.add(moved_login)
        if updates:
            Party._base_manager.filter(pk=survivor.pk).update(**updates)


def _referenced_ids(model, pks) -> dict:
    """``{pk: {"app.Model", ...}}`` for rows of ``model`` another table points at."""
    out = {}
    if not pks:
        return out
    # include_hidden: documents point at addresses with related_name="+".
    for rel in model._meta.get_fields(include_hidden=True):
        if not (rel.auto_created and not rel.concrete and (rel.one_to_many or rel.one_to_one)):
            continue
        if rel.related_model is model:
            continue
        column = rel.field.attname
        for pk in rel.related_model._base_manager.filter(**{f"{column}__in": pks}).values_list(column, flat=True):
            out.setdefault(pk, set()).add(rel.related_model._meta.label)
    return out


# A footprint that keeps growing under concurrent edits is refused after this
# many tries rather than locked out of order.
LOCK_ATTEMPTS = 3


class _FootprintMoved(Exception):
    """The footprint grew between collecting it and locking it."""


def _lock_targets(fp) -> tuple[set, set, set]:
    """The Customer, Party and User rows erasure writes: ``fp`` plus the parties
    it updates outside the merge tree (merge survivors, and the party and
    holder of each account, whose customer role closes)."""
    from customers.models import Customer

    parties = set(fp.parties) | {log.survivor_id for log in fp.loser_logs}
    for party_id, holder_id in Customer._base_manager.filter(pk__in=fp.customers).values_list(
            "party_id", "account_holder_id"):
        parties |= {party_id, holder_id} - {None}
    return set(fp.customers), parties, set(fp.users)


def _lock_footprint(party_pk):
    """Lock what erasure writes in the login order, Customer → Party → User.

    Portal login (``core.identity.resolve``) locks a Customer, then its Party,
    then the User; locking the Party first here would deadlock against it on
    Postgres. The footprint is found unlocked, locked in that order through
    the same helper (``lock_login_rows``), then collected again under the
    locks. If it grew meanwhile, the savepoint is rolled back (which releases
    its row locks) and the whole footprint, newcomers included, is locked
    again in order, rather than locking the newcomers out of order.
    """
    from core.identity.resolve import lock_login_rows

    from .models import Party

    customers, parties, users = set(), set(), set()
    for _attempt in range(LOCK_ATTEMPTS):
        try:
            with transaction.atomic():
                fp = _collect(Party._base_manager.get(pk=party_pk))
                # A retry locks what the last attempt found as well: extra
                # rows are harmless, and the footprint then settles sooner.
                c, p, u = _lock_targets(fp)
                customers, parties, users = customers | c, parties | p, users | u
                users = lock_login_rows(customers=customers, parties=parties, users=users)
                party = Party._base_manager.get(pk=party_pk)
                fresh = _collect(party)
                c, p, u = _lock_targets(fresh)
                if not (c <= customers and p <= parties and u <= users):
                    customers, parties, users = customers | c, parties | p, users | u
                    raise _FootprintMoved
                return party, fresh
        except _FootprintMoved:
            continue
    raise ErasureError("This person's records kept changing during the erasure. Try again.")


def _scrub_commerce(fp, party, kept, needles) -> dict:
    """2-3. Customer, CustomerAddress, CustomerContact, Lead, ActivityLog, documents."""
    from communications.models import EmailLog
    from core.models import ExternalRef
    from crm.models import ActivityLog, Lead
    from customers.models import Customer, CustomerAddress, CustomerContact
    from fulfillment.models import PickTicket
    from orders.models import Order, Quote, SalesInvoice
    from payments.models import Payment

    counts = {}
    personal = fp.personal_customers
    business = fp.customers - personal
    for pk in personal:
        Customer._base_manager.filter(pk=pk, customer_type="business").update(company_name="")
        Customer._base_manager.filter(pk=pk).update(
            first_name=ERASED_NAME, last_name="", email=tombstone_email(pk), phone="", notes="",
            job_title="", tax_exempt_reason="", tax_exempt_certificate_number="",
            tax_exempt_document_url="", tax_exempt_expiration=None, tax_exempt_states=[],
            is_active=False, accepts_marketing=False, user=None,
            ebay_username="", erpnext_name="", bc_id="",
        )
    # A business account the person held or spoke for stays open: only the
    # person's own fields go (the commerce email was theirs, so it becomes the
    # tombstone; staff set a new contact, which resumes the party sync).
    for pk in business:
        Customer._base_manager.filter(pk=pk).update(
            first_name=ERASED_NAME, last_name="", email=tombstone_email(pk), phone="", job_title="",
            accepts_marketing=False, user=None, account_holder=None,
        )
    counts["customers"] = len(personal)
    counts["business_accounts_unlinked"] = len(business)

    addresses = list(CustomerAddress._base_manager.filter(customer_id__in=personal).values_list("pk", flat=True))
    referenced = _referenced_ids(CustomerAddress, addresses)
    keep = [pk for pk in addresses if pk in referenced]
    drop = [pk for pk in addresses if pk not in referenced]
    CustomerAddress._base_manager.filter(pk__in=keep).update(
        first_name="", last_name="", phone="", label="", delivery_instructions="", party_address=None,
    )
    # A deleted address's provenance moves to its (kept) customer, so the
    # source record still reads as erased.
    if drop:
        from django.contrib.contenttypes.models import ContentType

        ct = ContentType.objects.get_for_models(CustomerAddress, Customer)
        owner = dict(CustomerAddress._base_manager.filter(pk__in=drop).values_list("pk", "customer_id"))
        for ref in ExternalRef._base_manager.filter(content_type=ct[CustomerAddress], object_id__in=drop):
            ExternalRef._base_manager.filter(pk=ref.pk).update(
                content_type=ct[Customer], object_id=owner[ref.object_id])
        CustomerAddress._base_manager.filter(pk__in=drop).update(party_address=None)
        for address in CustomerAddress._base_manager.filter(pk__in=drop):
            address.delete()
    counts["customer_addresses_deleted"] = len(drop)
    counts["customer_addresses_kept"] = len(keep)
    if keep:
        kept.append({
            "model": "customers.CustomerAddress", "ids": sorted(str(pk) for pk in keep),
            "fields": "postal lines only (names and phone blanked)",
            "why": "referenced by " + ", ".join(sorted({m for pk in keep for m in referenced[pk]}))
                   + "; tax and accounting retention (GDPR art. 17(3)(b))",
        })

    contacts = CustomerContact._base_manager.filter(pk__in=fp.contacts)
    counts["contacts"] = contacts.update(
        first_name=ERASED_NAME, last_name="", email="", phone="", job_title="", notes="")

    leads = list(Lead._base_manager.filter(pk__in=fp.leads).values_list("pk", flat=True))
    for pk in leads:
        Lead._base_manager.filter(pk=pk).update(
            first_name=ERASED_NAME, last_name="", email=tombstone_email(pk), phone="", company_name="",
            job_title="", city="", state_province="", postal_code="", message="", notes="", status_note="",
            loss_notes="", ip_address=None, referrer_url="", metadata={}, tags=[],
        )
    counts["leads"] = len(leads)

    activity_ids = list(ActivityLog._base_manager.filter(
        Q(customer_id__in=personal) | Q(lead_id__in=fp.leads)).values_list("pk", flat=True))
    counts["activities"] = ActivityLog._base_manager.filter(pk__in=activity_ids).update(
        subject=ERASED_ACTIVITY_SUBJECT, description="", metadata={})
    counts["activity_copies"] = _scrub_activity_copies(activity_ids, personal, fp.leads, fp)

    # Documents stay; only the personal details on them go. The person's own
    # documents lose their free text, IP and raw processor payloads; a
    # business account's documents lose only what names or reaches the person.
    own_quotes = Quote._base_manager.filter(customer_id__in=personal)
    quote_ids = set(own_quotes.values_list("pk", flat=True))
    own_quotes.update(sent_to_email="", accepted_by_name="", accepted_by_title="", customer_notes="",
                      internal_notes="", legacy_loss_notes="")
    for pk, email, name in Quote._base_manager.filter(
            Q(customer_id__in=business) | Q(contact_id__in=fp.contacts)).exclude(pk__in=quote_ids).values_list(
            "pk", "sent_to_email", "accepted_by_name"):
        updates = {}
        if email and safe_email(email) in needles["email"]:
            updates["sent_to_email"] = ""
        if name and name.strip().lower() in {n.lower() for n in needles["names"]}:
            updates.update(accepted_by_name="", accepted_by_title="")
        if updates:
            Quote._base_manager.filter(pk=pk).update(**updates)
            quote_ids.add(pk)
    guest = Order._base_manager.filter(company_id=party.company_id, customer__isnull=True).exclude(guest_email="")
    guest_ids = [pk for pk, email in guest.values_list("pk", "guest_email") if safe_email(email) in needles["email"]]
    Order._base_manager.filter(pk__in=guest_ids).update(guest_email=tombstone_email(party.pk))
    own_orders = set(Order._base_manager.filter(customer_id__in=personal).values_list("pk", flat=True))
    own_orders |= set(guest_ids)
    Order._base_manager.filter(pk__in=own_orders - set(guest_ids)).update(guest_email="")
    Order._base_manager.filter(pk__in=own_orders).update(ip_address=None, customer_message="", internal_notes="")
    order_ids = own_orders | set(Order._base_manager.filter(contact_id__in=fp.contacts).values_list("pk", flat=True))
    invoices = SalesInvoice._base_manager.filter(order_id__in=own_orders)
    invoice_ids = set(invoices.values_list("pk", flat=True))
    invoices.update(internal_notes="")
    Payment._base_manager.filter(order_id__in=own_orders).update(gateway_response={}, risk_response_raw={})
    tickets = PickTicket._base_manager.filter(order_id__in=own_orders)
    tickets.update(ship_to_label="", ship_to_attention="", notes="", metadata={})
    own_docs = own_orders | invoice_ids | set(own_quotes.values_list("pk", flat=True))
    logs = EmailLog.objects.filter(document_id__in=own_docs)
    logs_count = logs.update(recipient_email=f"erased@{ERASED_EMAIL_DOMAIN}")
    for pk, email in EmailLog.objects.filter(document_id__in=order_ids | quote_ids).exclude(
            document_id__in=own_docs).values_list("pk", "recipient_email"):
        if safe_email(email) in needles["email"]:
            logs_count += EmailLog.objects.filter(pk=pk).update(recipient_email=f"erased@{ERASED_EMAIL_DOMAIN}")
    counts["email_logs"] = logs_count
    if own_orders or quote_ids or invoice_ids:
        kept.append({
            "model": "orders", "orders": len(own_orders), "quotes": len(quote_ids), "invoices": len(invoice_ids),
            "why": "kept for tax and accounting retention (GDPR art. 17(3)(b)); they point at the "
                   "tombstone customer",
        })
    if tickets.exclude(ship_to_address="").exists():
        kept.append({"model": "fulfillment.PickTicket", "count": tickets.exclude(ship_to_address="").count(),
                     "fields": "ship_to_address",
                     "why": "the shipping record of a kept order; name and attention line blanked"})
    if invoices.exclude(notes="").exists():
        kept.append({"model": "orders.SalesInvoice", "fields": "notes",
                     "why": "printed on an issued invoice (accounting retention)"})
    counts.update(_scrub_crm_text(fp, needles, party.company_id, quote_ids))
    _kept_ledger(party.company_id, needles, kept)
    if personal:
        kept.append({
            "model": "customers.Customer", "ids": sorted(str(pk) for pk in personal),
            "why": "kept as a tombstone so documents keep their customer",
        })
    return counts


def _scrub_activity_copies(activity_ids, customer_ids, lead_ids, fp) -> int:
    """CRM Phase 3 step 6 copies: an ActivityLog row's Communication / Task
    (same id, or created through the compat endpoint for the person's lead or
    customer) and the person's lead messages (intake notes) lose their text.
    Their ids join ``fp.communications`` / ``fp.tasks``."""
    from communications.models import Communication
    from crm.models import Task

    leads = [str(pk) for pk in lead_ids]
    customers = [str(pk) for pk in customer_ids]
    about = (Q(pk__in=activity_ids) | Q(metadata__activitylog__lead__in=leads)
             | Q(metadata__activitylog__customer__in=customers))
    marker = {"metadata__from_activitylog": True}
    comms = set(Communication._base_manager.filter(about, **marker).values_list("pk", flat=True))
    tasks = set(Task._base_manager.filter(about, **marker).values_list("pk", flat=True))
    notes = set(Communication._base_manager.filter(provider="lead_form", metadata__lead__in=leads).values_list(
        "pk", flat=True))
    count = Communication._base_manager.filter(pk__in=comms).update(
        subject=ERASED_ACTIVITY_SUBJECT, body_text="", summary="", metadata={"from_activitylog": True})
    count += Task._base_manager.filter(pk__in=tasks).update(
        title=ERASED_ACTIVITY_SUBJECT, description="", metadata={"from_activitylog": True})
    count += Communication._base_manager.filter(pk__in=notes).update(body_text="", summary="")
    fp.communications |= comms | notes
    fp.tasks |= tasks
    return count


def _scrub_crm_text(fp, needles, company_id, quote_ids) -> dict:
    """CRM Phase 3 free text: engagements (and their stage-change notes),
    tasks, communications (with their participants and raw payloads) and
    touchpoints. Every row stays: timelines, reports and accounting keep
    their counts and dates.

    **Own** rows lose their free text whole: an engagement whose party is
    the person or that holds one of their own quotes; a communication they
    are the party or a participant of, that sits on one of those
    engagements, or that logs their lead (``metadata.lead``); a task or
    touchpoint about them or on one of those engagements (or communications).
    Subjects and task titles become tombstones; notes, bodies, summaries,
    URLs and click ids are blanked; a raw provider payload becomes
    ``{"erased": true}`` (its ``content_hash`` stays, and
    ``communications.ingest`` refuses to rewrite an erased payload, so a
    provider re-fetch cannot write the person back). An engagement title is
    tombstoned when its party is the person, otherwise only the segments
    that name them go (``_erased_title``).

    **Related** rows (an engagement they are only a contact on, or that holds
    a business quote sent to them, and the tasks, communications and
    touchpoints on it) and any of this tenant's rows quoting one of their
    addresses or numbers lose only the fields that name or reach them. A
    related communication that loses anything also loses its raw payload:
    the next re-fetch would otherwise rewrite the scrubbed fields from it.
    """
    from communications.models import Communication, CommunicationParticipant, CommunicationRaw
    from crm.models import Engagement, EngagementContact, EngagementStageChange, Task, Touchpoint

    terms = _sidecar_terms(needles)
    raw = sorted(needles["raw"])
    tree = fp.parties

    # URLs carry addresses percent-encoded (``?email=a%40b.test``).
    url_raw = sorted(set(raw) | {quote(n, safe="").lower() for n in raw})
    url_terms = (url_raw, terms[1])

    def says(text, terms=terms) -> bool:
        return bool(text) and _text_mentions(text, terms)

    def quoting(model, fields, needles=raw):
        """This tenant's rows of ``model`` whose ``fields`` quote an address or number."""
        if not needles:
            return set()
        cond = Q()
        for name in fields:
            cond |= _text_matches(name, needles)
        return set(model._base_manager.filter(cond, company_id=company_id).values_list("pk", flat=True))

    def ids(qs):
        return set(qs.values_list("pk", flat=True))

    counts = {}

    # Engagements.
    own_eng = ids(Engagement._base_manager.filter(
        Q(party_id__in=tree) | Q(quotes__customer_id__in=fp.personal_customers), company_id=company_id))
    quote_eng = ids(Engagement._base_manager.filter(quotes__in=quote_ids, company_id=company_id)) - own_eng
    contact_eng = set(EngagementContact._base_manager.filter(party_id__in=tree, company_id=company_id).values_list(
        "engagement_id", flat=True)) - own_eng
    related_eng = quote_eng | contact_eng
    scope = own_eng | related_eng | quoting(Engagement, ("title", "loss_notes"))
    changed = 0
    for pk, party_id, title, loss_notes, metadata in Engagement._base_manager.filter(pk__in=scope).values_list(
            "pk", "party_id", "title", "loss_notes", "metadata"):
        updates = {}
        new_title = _erased_title(title, terms, own=party_id in tree)
        if new_title != title:
            updates["title"] = new_title
        if loss_notes and (pk in own_eng or pk in quote_eng or says(loss_notes)):
            updates["loss_notes"] = ""
        legacy = (metadata or {}).get("quote_legacy") if isinstance(metadata, dict) else None
        if isinstance(legacy, dict) and "loss_notes" in legacy and (pk in own_eng or pk in quote_eng):
            updates["metadata"] = {**metadata, "quote_legacy": {
                k: v for k, v in legacy.items() if k != "loss_notes"}}
        if updates:
            changed += Engagement._base_manager.filter(pk=pk).update(**updates)
    counts["engagements"] = changed
    # Stage-change notes are append-only history; the note is free text.
    notes = 0
    notes += EngagementStageChange._base_manager.filter(engagement_id__in=own_eng).exclude(note="").update(note="")
    for pk, note in EngagementStageChange._base_manager.filter(
            pk__in=ids(EngagementStageChange._base_manager.filter(engagement_id__in=related_eng))
            | quoting(EngagementStageChange, ("note",))).exclude(note="").values_list("pk", "note"):
        if says(note):
            notes += EngagementStageChange._base_manager.filter(pk=pk).update(note="")
    counts["stage_change_notes"] = notes

    # Communications: the person's own correspondence and lead notes.
    addresses = needles["email"] | needles["phone"]
    theirs = Q(party_id__in=tree)
    if addresses:
        theirs |= Q(address_normalized__in=addresses)
    their_participants = CommunicationParticipant._base_manager.filter(theirs, company_id=company_id)
    own_comm = Q(party_id__in=tree) | Q(engagement_id__in=own_eng) | Q(
        pk__in=their_participants.values("communication_id"))
    if fp.leads:
        own_comm |= Q(metadata__lead__in=[str(pk) for pk in fp.leads])
    own_comms = ids(Communication._base_manager.filter(own_comm, company_id=company_id))
    related_comms = (ids(Communication._base_manager.filter(engagement_id__in=related_eng, company_id=company_id))
                     | quoting(Communication, ("subject", "body_text", "summary", "thread_key"))) - own_comms
    touched = set()  # related communications that lost a field
    changed = Communication._base_manager.filter(pk__in=own_comms).update(
        subject=ERASED_ACTIVITY_SUBJECT, body_text="", summary="")
    for pk, subject, body, summary, thread_key, meta in Communication._base_manager.filter(
            pk__in=own_comms | related_comms).values_list(
            "pk", "subject", "body_text", "summary", "thread_key", "metadata"):
        own = pk in own_comms
        updates = {}
        if not own:
            if says(subject):
                updates["subject"] = ERASED_ACTIVITY_SUBJECT
            if says(body):
                updates["body_text"] = ""
            if says(summary):
                updates["summary"] = ""
        if says(thread_key):
            updates["thread_key"] = ""
        new_meta = _scrubbed_sidecar(meta, terms, own=own)
        if new_meta is not None:
            updates["metadata"] = new_meta
        if updates:
            Communication._base_manager.filter(pk=pk).update(**updates)
            if not own:
                changed += 1
                touched.add(pk)
    counts["communications"] = changed
    fp.communications |= own_comms | touched
    blanked = {"address_raw": "", "address_normalized": "", "display_name": ""}
    parts = their_participants.update(**blanked)
    for pk, comm_id, address, name in CommunicationParticipant._base_manager.filter(
            communication_id__in=own_comms | related_comms).exclude(
            address_raw="", address_normalized="", display_name="").values_list(
            "pk", "communication_id", "address_raw", "display_name"):
        if says(address) or says(name):
            parts += CommunicationParticipant._base_manager.filter(pk=pk).update(**blanked)
            touched.add(comm_id)
            fp.communications.add(comm_id)
    counts["communication_participants"] = parts
    payloads = own_comms | touched
    for pk, payload in CommunicationRaw._base_manager.filter(communication_id__in=related_comms).values_list(
            "pk", "payload"):
        if _mentions("", payload, terms):
            payloads.add(pk)
    if raw:
        payloads |= set(CommunicationRaw._base_manager.filter(company_id=company_id).annotate(
            _text=Cast("payload", TextField())).filter(_text_matches("_text", raw)).values_list("pk", flat=True))
    counts["communication_payloads"] = CommunicationRaw._base_manager.filter(pk__in=payloads).exclude(
        payload=ERASED_PAYLOAD).update(payload=ERASED_PAYLOAD)
    # CommunicationRaw's pk is its communication's (one-to-one).
    fp.communications |= set(CommunicationRaw._base_manager.filter(pk__in=payloads).values_list(
        "communication_id", flat=True))

    # Tasks: about them, or on their engagements and communications.
    own_tasks = ids(Task._base_manager.filter(
        Q(party_id__in=tree) | Q(engagement_id__in=own_eng) | Q(communication_id__in=own_comms),
        company_id=company_id))
    related_tasks = (ids(Task._base_manager.filter(
        Q(engagement_id__in=related_eng) | Q(communication_id__in=related_comms)))
        | quoting(Task, ("title", "description"))) - own_tasks
    changed = Task._base_manager.filter(pk__in=own_tasks).update(title=ERASED_ACTIVITY_SUBJECT, description="")
    fp.tasks |= own_tasks
    for pk, title, description in Task._base_manager.filter(pk__in=related_tasks).values_list(
            "pk", "title", "description"):
        updates = {}
        if says(title):
            updates["title"] = ERASED_ACTIVITY_SUBJECT
        if says(description):
            updates["description"] = ""
        if updates:
            changed += Task._base_manager.filter(pk=pk).update(**updates)
            fp.tasks.add(pk)
    counts["tasks"] = changed

    # Touchpoints: URLs and click ids identify the visitor; the channel and
    # campaign stay for attribution reports unless they name the person.
    url_fields = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
                  "referrer_url", "landing_url", "click_id")
    own_touch = ids(Touchpoint._base_manager.filter(
        Q(party_id__in=tree) | Q(engagement_id__in=own_eng), company_id=company_id))
    related_touch = (ids(Touchpoint._base_manager.filter(engagement_id__in=related_eng, company_id=company_id))
                     | quoting(Touchpoint, url_fields, url_raw)) - own_touch
    changed = 0
    for row in Touchpoint._base_manager.filter(pk__in=own_touch | related_touch).values("pk", *url_fields):
        own = row["pk"] in own_touch
        updates = {name: "" for name in url_fields if row[name] and (
            says(row[name], url_terms) or (own and name not in ("utm_source", "utm_medium", "utm_campaign")))}
        if own and row["click_id"]:
            updates["click_id_kind"] = ""
        if updates:
            changed += Touchpoint._base_manager.filter(pk=row["pk"]).update(**updates)
            fp.touchpoints.add(row["pk"])
    fp.touchpoints |= own_touch
    counts["touchpoints"] = changed
    return counts


def _erased_title(title, terms, *, own: bool) -> str:
    """An engagement title without the person: the person's own deal leads
    with the tombstone (titles default to "<party> — <source>"); any
    `` — `` segment that names or reaches them is dropped."""
    parts = (title or "").split(" — ")
    head = parts[0]
    if own or _text_mentions(head, terms):
        head = ERASED_PARTY_NAME
    rest = [p for p in parts[1:] if not _text_mentions(p, terms)]
    if not own and head == parts[0] and len(rest) == len(parts) - 1:
        return title
    return " — ".join([head, *rest])


def _text_mentions(text, terms) -> bool:
    raw, words = terms
    text = (text or "").lower()
    return any(n in text for n in raw) or any(w.search(text) for w in words)


def _kept_ledger(company_id, needles, kept) -> None:
    """The general ledger keeps its counterparty names (art. 17(3)(b)); say so."""
    from accounting.models import JournalEntry, JournalLine

    names = [n for n in needles["names"] if " " in n]
    if not names:
        return
    cond = Q()
    for name in names:
        cond |= Q(entity_name__iexact=name)
    entries = JournalEntry._base_manager.filter(cond, company_id=company_id).count()
    lines = JournalLine._base_manager.filter(cond, entry__company_id=company_id).count()
    if entries or lines:
        kept.append({"model": "accounting.JournalEntry/JournalLine", "entries": entries, "lines": lines,
                     "fields": "entity_name",
                     "why": "the general ledger is an accounting record (GDPR art. 17(3)(b))"})


def _sidecar_terms(needles) -> tuple:
    """``(substrings, word patterns)``: addresses and numbers match anywhere,
    names only as whole words (a short given name is inside many hex ids)."""
    import re

    raw = sorted({n.lower() for n in needles["raw"]})
    words = [re.compile(r"(?<![\w])" + re.escape(n.lower()) + r"(?![\w])") for n in sorted(needles["names"])]
    return raw, words


def _mentions(key, value, terms) -> bool:
    raw, words = terms
    text = json.dumps([key, value], ensure_ascii=False, default=str).lower()
    return any(n in text for n in raw) or any(w.search(text) for w in words)


#: System keys that reach the person although they name no one: a visitor's
#: cookie id and the tag manager's event id (crm.touchpoints). Dropped from
#: the person's own rows like any client key.
REACHING_SYSTEM_KEYS = frozenset({"visitor_id", "event_id"})


def _scrubbed_sidecar(meta, terms, *, own: bool):
    """The ``metadata`` to keep, or None when nothing changes."""
    if not isinstance(meta, dict):
        wipe = meta not in (None, {}) and (own or _mentions("", meta, terms))
        return {} if wipe else None
    kept = {k: v for k, v in meta.items()
            if ((is_system_metadata_key(k) and k not in REACHING_SYSTEM_KEYS) or not own)
            and not _mentions(k, v, terms)}
    return kept if kept != meta else None


def _scrub_crm_sidecars(fp, needles, company_id) -> int:
    """6b. ``metadata`` on Engagement, Task, Touchpoint and PartyRole."""
    from crm.models import Engagement, EngagementContact, Task, Touchpoint

    from .models import PartyRole

    terms = _sidecar_terms(needles)
    if not fp.parties:
        return 0
    contact_engagements = set(EngagementContact._base_manager.filter(
        party_id__in=fp.parties).values_list("engagement_id", flat=True))
    raw = sorted(needles["raw"])
    total = 0
    for model, via_engagement in ((Engagement, "pk"), (Task, "engagement_id"),
                                  (Touchpoint, "engagement_id"), (PartyRole, None)):
        base = model._base_manager.filter(company_id=company_id)
        own = Q(party_id__in=fp.parties)
        related = Q(**{f"{via_engagement}__in": contact_engagements}) if via_engagement else Q(pk__in=[])
        quoting = Q(pk__in=base.annotate(_text=Cast("metadata", TextField())).filter(
            _text_matches("_text", raw)).values("pk")) if raw else Q(pk__in=[])
        for pk, party_id, meta in base.filter(own | related | quoting).values_list(
                "pk", "party_id", "metadata"):
            new = _scrubbed_sidecar(meta, terms, own=party_id in fp.parties)
            if new is not None:
                total += model._base_manager.filter(pk=pk).update(metadata=new)
    return total


def _scrub_party_side(fp, now) -> dict:
    """4. Contact points, consent and relationships deleted; party rows tombstoned."""
    from customers.models import Customer, CustomerAddress

    from .models import ConsentRecord, Party, PartyAddress, PartyEmail, PartyPhone, PartyRelationship
    from .sync import sync_customer_role

    counts = {}
    # A PartyAddress mirrored by a kept CustomerAddress was unlinked above;
    # any other CustomerAddress (another customer's) pointing here keeps its row.
    CustomerAddress._base_manager.filter(party_address_id__in=fp.addresses).update(party_address=None)
    counts["party_emails"] = PartyEmail._base_manager.filter(pk__in=fp.emails).delete()[0]
    counts["party_phones"] = PartyPhone._base_manager.filter(pk__in=fp.phones).delete()[0]
    counts["party_addresses"] = PartyAddress._base_manager.filter(pk__in=fp.addresses).delete()[0]
    counts["consent_records"] = ConsentRecord._base_manager.filter(pk__in=fp.consents).delete()[0]
    counts["relationships"] = PartyRelationship._base_manager.filter(pk__in=fp.relationships).delete()[0]
    counts["parties"] = Party._base_manager.filter(pk__in=fp.parties).update(
        display_name=ERASED_PARTY_NAME, display_name_locked=True, given_name="", family_name="",
        legal_name="", primary_domain="", registration_number="", tax_id="", user=None,
        marketable=False, marketable_source="", marketable_changed_at=now,
        primary_email_normalized="", primary_phone_e164="", metadata={}, tags=[],
        is_active=False, erased_at=now,
    )
    # Survivors that lost moved rows: their denormalised primary columns.
    from .models import refresh_primary_party_contact

    survivors = {log.survivor_id for log in fp.loser_logs}
    for pid in survivors:
        refresh_primary_party_contact(pid)
    # The customer role closes with the (now inactive) commerce accounts.
    touched = set()
    for party_id, holder_id in Customer._base_manager.filter(pk__in=fp.customers).values_list(
            "party_id", "account_holder_id"):
        touched |= {party_id, holder_id} - {None}
    for pid in touched:
        sync_customer_role(pid)
    return counts


def _scrub_users(fp) -> int:
    """5. Deactivate and scrub each login that reaches no other live party."""
    from django.contrib.auth import get_user_model

    from core.identity.models import UserIdentity
    from customers.models import Customer

    from .models import Party

    User = get_user_model()
    done = 0
    for uid in sorted(fp.users):
        in_use = (
            Party._base_manager.filter(user_id=uid, erased_at__isnull=True).exclude(pk__in=fp.parties).exists()
            or Customer._base_manager.filter(user_id=uid).exclude(pk__in=fp.customers).exists()
        )
        if in_use:
            continue  # the same human in another tenant keeps their login
        UserIdentity.objects.filter(user_id=uid).delete()
        done += User.objects.filter(pk=uid).update(
            is_active=False, email="", first_name="", last_name="",
            username=f"erased-{uuid.uuid4().hex[:24]}", password=make_password(None),
        )
    return done


def _flag_refs(fp, now) -> int:
    """6. Keep every ref, re-keyed so importers see "erased" but no name."""
    from core.models import ExternalRef

    flagged = 0
    for ref in ExternalRef._base_manager.filter(pk__in=fp.refs):
        ExternalRef._base_manager.filter(pk=ref.pk).update(
            source_id=erased_source_id(ref.source_id), source_url="",
            metadata={"erased": True, "erased_at": now.isoformat()},
        )
        flagged += 1
    return flagged


def _flag_mirror_refs(fp, now) -> int:
    """6c. The refs of the tasks, communications and touchpoints erasure
    scrubbed get ``metadata.erased`` **without being re-keyed**
    (phase6-adapters.md 4.3): ``communications.ingest`` finds a communication
    by its clear ``external_id``, so a re-keyed ref would make a re-fetch
    create a fresh copy, and ``is_erased_source`` reads the clear id too.
    Their ids are opaque (a ClickUp task id, a Message-ID hash, a call id).
    Other keys stay: they are sync bookkeeping, never personal."""
    from django.contrib.contenttypes.models import ContentType

    from communications.models import Communication
    from core.models import ExternalRef
    from crm.models import Task, Touchpoint

    ct = ContentType.objects.get_for_models(Task, Communication, Touchpoint)
    targets = Q()
    for model, ids in ((Task, fp.tasks), (Communication, fp.communications), (Touchpoint, fp.touchpoints)):
        if ids:
            targets |= Q(content_type=ct[model], object_id__in=ids)
    if not targets:
        return 0
    flagged = 0
    # Filtered in Python: ``exclude(metadata__erased=True)`` would also drop
    # every ref without the key (SQL NULL), i.e. all of them.
    for pk, meta in ExternalRef._base_manager.filter(targets).values_list("pk", "metadata"):
        meta = meta if isinstance(meta, dict) else {}
        if meta.get("erased") is True:
            continue
        flagged += ExternalRef._base_manager.filter(pk=pk).update(
            source_url="", metadata={**meta, "erased": True, "erased_at": now.isoformat()})
    return flagged


def _mark_scrubbed_tasks(fp, now) -> None:
    """``metadata["erased_at"]`` on every task erasure scrubbed (a system key).

    A task push queued before this instant may carry, or have read, the text
    erasure removed; an adapter compares its row's ``created_at`` with the
    mark and sends nothing (ClickUp's ``_erased_task``). A task golden pushed
    but never recorded is recognised by it when ClickUp later names it.
    """
    from crm.models import Task

    stamp = now.isoformat()
    for pk, meta in Task._base_manager.filter(pk__in=fp.tasks).values_list("pk", "metadata"):
        meta = meta if isinstance(meta, dict) else {}
        Task._base_manager.filter(pk=pk).update(metadata={**meta, "erased_at": stamp})


def _collect_provider_erasures(fp, company_id) -> list:
    """Each installed adapter's collector, inside the transaction and before
    ``_flag_refs`` (``core.integrations.erasure.collect``)."""
    from core.integrations.erasure import collect

    return collect(fp, company_id)


def _purge_adapter_queues(fp, company_id) -> int:
    """Each installed adapter's ``purge_parties`` hook (``core.integrations.erasure.purge``)."""
    from core.integrations.erasure import purge

    return purge(fp, company_id)


def _enqueue_provider_erasures(sweep, result) -> int:
    """One ``<provider>.erase`` outbox row per adapter with refs, keyed
    ``erase:<erasure id>``. Written after ``_scrub_outbox``, which never
    touches ``.erase`` rows."""
    from core.integrations.erasure import enqueue

    return enqueue(sweep, erasure_id=result.erasure_id, party_id=result.party_id, company_id=result.company_id)


def _scrub_merge_logs(fp) -> int:
    """The snapshots that let a merge be undone hold the erased rows as they were."""
    from .models import PartyMergeLog

    return PartyMergeLog.objects.filter(pk__in=fp.logs).update(before={}, removed={})


def _scrub_import_write_logs(fp, needles, company_id) -> int:
    """A CRM import's undo snapshot (``crm_import.ImportWriteLog.before`` of
    an ``update``) holds the row as it was. Scrubbed to ``{}`` for every row
    of the person (and of a merge survivor that took their columns), and for
    any of this tenant's snapshots that quotes one of their addresses or
    numbers; the entry stays, and undo refuses it (phase5-importer.md 4.5)."""
    from django.apps import apps as django_apps
    from django.contrib.contenttypes.models import ContentType

    if not django_apps.is_installed("crm_import"):
        return 0
    from core.models import ExternalRef

    from .models import ConsentRecord, Party, PartyAddress, PartyEmail, PartyPhone, PartyRelationship

    WriteLog = django_apps.get_model("crm_import", "ImportWriteLog")
    live = WriteLog._base_manager.filter(action="update").exclude(before={})
    parties = set(fp.parties) | {log.survivor_id for log in fp.loser_logs}
    targets = ((Party, parties), (PartyEmail, fp.emails), (PartyPhone, fp.phones),
               (PartyAddress, fp.addresses), (ConsentRecord, fp.consents),
               (PartyRelationship, fp.relationships), (ExternalRef, fp.refs))
    total = 0
    for model, pks in targets:
        ids = sorted(str(pk) for pk in pks)
        if not ids:
            continue
        ct = ContentType.objects.get_for_model(model)
        for start in range(0, len(ids), 500):
            total += live.filter(content_type=ct, object_id__in=ids[start:start + 500]).update(before={})
    if needles["raw"]:
        quoting = live.filter(company_id=company_id).annotate(_text=Cast("before", TextField())).filter(
            _text_matches("_text", sorted(needles["raw"])))
        total += WriteLog._base_manager.filter(pk__in=quoting.values("pk")).update(before={})
    return total


def _audit_ids(fp) -> set:
    from customers.models import CustomerAddress

    ids = set()
    for attr in ("parties", "emails", "phones", "addresses", "consents", "relationships", "customers",
                 "contacts", "leads", "refs"):
        ids |= getattr(fp, attr)
    ids |= set(CustomerAddress._base_manager.filter(customer_id__in=fp.personal_customers).values_list(
        "pk", flat=True))
    return ids


def _tenant_audit_scope(company_id) -> Q:
    """AuditLog has no company: an entry is this tenant's when its object is."""
    from crm.models import Lead
    from customers.models import Customer, CustomerAddress, CustomerContact
    from orders.models import Order, Quote, SalesInvoice
    from payments.models import Payment

    from .models import Party

    scope = Q(user__company_id=company_id)
    for model, path in ((Party, "company_id"), (Customer, "company_id"), (Lead, "company_id"),
                        (Order, "company_id"), (Quote, "company_id"), (SalesInvoice, "company_id"),
                        (CustomerAddress, "customer__company_id"), (CustomerContact, "customer__company_id"),
                        (Payment, "order__company_id")):
        scope |= Q(object_id__in=model._base_manager.filter(**{path: company_id}).values("pk"))
    return scope


def _text_matches(field_name, needles) -> Q:
    cond = Q()
    for needle in needles:
        for variant in {needle, json.dumps(needle)[1:-1]}:  # SQLite stores non-ASCII escaped
            cond |= Q(**{f"{field_name}__icontains": variant})
    return cond


def _scrub_audit(fp, needles, company_id) -> int:
    """7. ``changes`` of entries about an erased row, or of this tenant's
    entries that quote one of the person's addresses or numbers."""
    from core.models import AuditLog

    scrubbed = {"erased": True}
    total = 0
    ids = list(_audit_ids(fp))
    for start in range(0, len(ids), 500):
        total += AuditLog.objects.filter(object_id__in=ids[start:start + 500]).exclude(
            changes=scrubbed).update(changes=scrubbed)
    if needles["raw"]:
        matching = AuditLog.objects.annotate(_text=Cast("changes", TextField())).filter(
            _tenant_audit_scope(company_id), _text_matches("_text", sorted(needles["raw"]))).exclude(
            changes=scrubbed)
        total += AuditLog.objects.filter(pk__in=matching.values("pk")).update(changes=scrubbed)
    return total


def _ref_keys(fp) -> list[tuple[str, str, str]]:
    """``(source, object_type, source_id)`` of the person's ExternalRefs, in clear."""
    from core.models import ExternalRef

    return list(ExternalRef._base_manager.filter(pk__in=fp.refs).values_list("source", "object_type", "source_id"))


def _scrub_traces(fp, needles, company_id, ref_keys=()) -> dict:
    """Copies outside the CRM: outbound webhook payloads about the person,
    this tenant's import staging rows, and Django admin history."""
    from django.contrib.admin.models import LogEntry

    from unify.models import StagedRow
    from webhooks.models import WebhookDeliveryLog

    counts = {}
    # Webhook payloads: those naming one of the erased rows' ids (unique across
    # tenants), or this tenant's that quote one of the person's addresses.
    ids = sorted(str(pk) for pk in fp.parties | fp.customers | fp.contacts | fp.leads)
    hook_match = _text_matches("_text", ids)
    if needles["raw"]:
        hook_match |= Q(subscription__company_id=company_id) & _text_matches("_text", sorted(needles["raw"]))
    hooks = WebhookDeliveryLog._base_manager.annotate(_text=Cast("payload", TextField())).filter(hook_match)
    counts["webhook_deliveries"] = WebhookDeliveryLog._base_manager.filter(
        pk__in=hooks.values("pk")).update(payload={"erased": True}, response_body="")
    # Import staging: the person's raw source rows, found by an address or
    # name they quote, or (CRM imports) by the source record's key, which
    # catches a record that formats the phone differently.
    staged = StagedRow._base_manager.filter(batch__company_id=company_id).annotate(
        _raw=Cast("raw", TextField()), _mapped=Cast("mapped_values", TextField()))
    terms = sorted(needles["raw"])
    match = _text_matches("_raw", terms) | _text_matches("_mapped", terms) if terms else Q(pk__in=[])
    for source, object_type, source_id in ref_keys:
        match |= Q(raw__source=source, raw__source_object_type=object_type, raw__source_id=source_id)
    pks = list(staged.filter(match).values_list("pk", flat=True))
    rows = StagedRow._base_manager.filter(pk__in=pks)
    counts["staged_rows"] = rows.update(raw={"erased": True}, mapped_values={}, proposed_mapping={})
    # A row not applied yet must not be decided or applied any more.
    rows.filter(mapping_state__in=["unmapped", "proposed", "approved"]).update(
        mapping_state="na", proposed_mapping={"decision": "na", "reason": "erased"})
    # Django admin history names the object in object_repr.
    counts["admin_log_entries"] = LogEntry.objects.filter(
        object_id__in=[str(pk) for pk in _audit_ids(fp)]).update(object_repr=ERASED_NAME, change_message="")
    return counts


def _scrub_outbox(fp, needles, company_id) -> dict:
    """The outbox (docs/crm/phase6-adapters.md 4.2): no queued work about the
    person may run after erasure. ``pending`` / ``dead`` rows naming one of
    the erased parties are deleted; ``running`` / ``done`` / ``cancelled``
    ones keep only ``{"erased": true}`` (a running handler finishes on the
    tombstone). Any other row that quotes one of the erased rows' ids, or (in
    this tenant) one of the person's addresses, is scrubbed too. Runs before
    ``_emit_erased``, whose ``party.erased`` row is ids only and untouched.

    The ``<provider>.erase`` rows (step 8) are never touched: this run's are
    written after this sweep, and an earlier erasure's (a party merged into
    this one and erased before) must still reach its provider.
    """
    from core.integrations.erasure import ERASE_SUFFIX
    from core.models import OutboxEvent

    rows = OutboxEvent.objects.exclude(consumer__endswith=ERASE_SUFFIX)
    party_ids = list(fp.parties)
    ids = sorted(str(pk) for pk in fp.parties | fp.customers | fp.contacts | fp.leads)
    match = _text_matches("_text", ids)
    if needles["raw"]:
        # Company-less rows too: a portal customer's event has no tenant.
        match |= (Q(company_id=company_id) | Q(company_id__isnull=True)) \
            & _text_matches("_text", sorted(needles["raw"]))
    # A party.erased event is ids only, and is what subscribers must still
    # receive (a second erasure run would otherwise match the first's).
    quoting = (rows.annotate(_text=Cast("payload", TextField())).filter(match)
               .exclude(payload={"erased": True}).exclude(event="party.erased"))
    # Rows about one of the footprint's objects, keyed by the object and not
    # the party (a ``clickup.task`` push pins the task's title): matched by
    # ``(object_type, object_id)``.
    about = _outbox_objects_q(fp) & Q(company_id=company_id)
    touched = set(rows.filter(party_id__in=party_ids).exclude(event="party.erased").values_list("pk", flat=True))
    touched |= set(rows.filter(about).exclude(event="party.erased").values_list("pk", flat=True))
    touched |= set(quoting.values_list("pk", flat=True))
    # Scrub every such row first, whatever its state: the UPDATE waits for a
    # worker that claimed one meanwhile (a ``webhooks`` expansion holds its
    # parent row: webhooks.outbox.expand), so rows that worker wrote are
    # committed, and visible to the DELETE below, by the time it runs; and a
    # worker that claims a row after this sees only the tombstone.
    rows.filter(pk__in=touched).update(payload={"erased": True})
    deleted = rows.filter(Q(party_id__in=party_ids) | about, state__in=["pending", "dead"]).exclude(
        event="party.erased").delete()[0]
    scrubbed = rows.filter(pk__in=touched).count()
    return {"outbox_deleted": deleted, "outbox_scrubbed": scrubbed}


def _outbox_objects_q(fp) -> Q:
    """``OutboxEvent`` rows whose object (``label_lower``, pk) is a row of the footprint."""
    from communications.models import Communication
    from crm.models import Lead, Task, Touchpoint
    from customers.models import Customer, CustomerAddress, CustomerContact

    from .models import Party

    # The person's own accounts only: a business account they merely hold or
    # speak for stays open, and its queued work stays (rows quoting the
    # person are caught by the text match).
    addresses = set(CustomerAddress._base_manager.filter(customer_id__in=fp.personal_customers).values_list(
        "pk", flat=True))
    cond = Q(pk__in=[])
    for model, ids in ((Party, fp.parties), (Customer, fp.personal_customers), (CustomerAddress, addresses),
                       (CustomerContact, fp.contacts), (Lead, fp.leads), (Task, fp.tasks),
                       (Communication, fp.communications), (Touchpoint, fp.touchpoints)):
        if ids:
            cond |= Q(object_type=model._meta.label_lower, object_id__in=list(ids))
    return cond


def _audit(party, actor, reason, result) -> None:
    from core.models import AuditLog

    AuditLog.objects.create(
        user=getattr(actor, "userprofile", None) if actor is not None else None,
        action="erase", model_name="parties.Party", object_id=party.pk,
        changes={"reason": reason[:1000], **result.as_dict()},
    )


def _emit_erased(party_ids, company_id) -> None:
    """8. ``party_erased`` after commit, once per erased party (ids only)."""
    from core.plugins import signals

    signal = getattr(signals, "party_erased", None)
    if signal is None:
        return
    from core.plugins.dispatch import emit

    from .models import Party

    for pk in sorted(party_ids, key=str):
        emit(signal, Party, Party._base_manager.get(pk=pk), party_id=pk, company_id=company_id)
