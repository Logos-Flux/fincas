"""Merge and unmerge parties (docs/crm/phase2-party.md 3.4).

``merge(survivor, losers, actor=...)`` folds each loser into the survivor in
one transaction per cluster, writing one ``PartyMergeLog`` per loser:

- ``PartyEmail`` / ``PartyPhone`` / ``PartyAddress`` move to the survivor,
  deduplicated on the normalised key (email, E.164, address text). A loser's
  duplicate row is deleted; a ``CustomerAddress`` mirroring it is re-pointed
  to the survivor's copy first. One primary per table survives: the
  ``field_picks`` choice, else the survivor's, else the loser's.
- ``PartyRole`` rows of one role coalesce where their date ranges overlap.
- ``PartyRelationship`` rows are retargeted; one that would become a
  self-relation, or would duplicate an open relation of the survivor, is
  deleted. Symmetric rows are re-stored in canonical order.
- ``ConsentRecord``, ``ExternalRef`` (on the party), ``Lead.party``,
  ``Customer.party``, ``Customer.account_holder``, ``CustomerContact.party``
  and any other FK to ``Party`` (found from the model graph) move to the
  survivor. A loser's login moves too when the survivor has none.
- The loser gets ``merged_into``, ``merged_at`` and ``is_active=False``.

Every write is a ``QuerySet.update()`` / ``delete()``: no ``save()`` runs, so
no party sync, no ``auto_now`` and no consent append-only check fires, and
``before`` (the serialised rows as they were) is exactly what the database
held. ``unmerge(log)`` writes ``before`` back (re-inserting deleted rows with
their original ids and timestamps), so merge then undo leaves every row
byte-identical. It refuses when a newer merge of the same survivor is still
in force, when the survivor has since been merged away, or when a row the
merge touched has changed since (``force=True`` overrides only the last).

Blocked merges (3.4) raise ``MergeBlocked``: two account holders, two
different logins, or a self party.
"""

from __future__ import annotations

import datetime
import decimal
import hashlib
import json
import uuid
from collections import defaultdict

from django.apps import apps
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from .dedupe import BLOCK_DIFFERENT_USERS, BLOCK_SELF_PARTY, BLOCK_TWO_ACCOUNT_HOLDERS
from .normalize import person_display_name

BLOCK_MESSAGES = {
    BLOCK_TWO_ACCOUNT_HOLDERS: "Two of these parties hold a commerce account login. Deactivate one "
                               "account first.",
    BLOCK_DIFFERENT_USERS: "These parties have different logins. Unlink one login first.",
    BLOCK_SELF_PARTY: "One of these parties is your own organization.",
}

# Survivor fields a ``field_picks`` entry may take from another party:
# ``{"given_name": "<party id>"}``. Blank survivor fields are filled from the
# loser when no pick names them. ``primary_email`` / ``primary_phone`` /
# ``primary_address`` pick a contact-point row id instead.
PICKABLE_FIELDS = (
    "given_name", "family_name", "display_name", "legal_name", "primary_domain",
    "registration_number", "tax_id",
    # Phase 4 (phase4-fields.md 5.2): classification and owner team.
    "lifecycle_stage", "contact_type", "owner_team",
)
# Taken from a loser only when a pick names it (never blank-filled): a
# survivor's owner team follows its own owner unless someone with
# crm.reassign_engagement picks another (2.7).
PICK_ONLY = frozenset({"owner_team"})
# A pick or blank-fill of these columns brings the companion column along.
PICK_COMPANIONS = {"lifecycle_stage": ("lifecycle_stage_changed_at",)}
# Sidecar keys (``custom.*`` and provider keys) follow 5.2: the survivor keeps
# its value per key, a pick (``{"custom.package": "<party id>"}``) takes a
# loser's, keys only a loser has move to the survivor, and a loser's
# conflicting value stays in the merge log's ``before`` snapshot (unmerge
# restores it). ``field_picks["sidecar_conflicts"]`` lists those keys
# (names only: the values live in the snapshot erasure already scrubs).
SIDECAR_CONFLICTS = "sidecar_conflicts"
PRIMARY_PICKS = {"primary_email": "PartyEmail", "primary_phone": "PartyPhone",
                 "primary_address": "PartyAddress"}


class MergeError(ValueError):
    """A merge or unmerge was refused; the message says why."""


class MergeBlocked(MergeError):
    def __init__(self, reasons):
        self.reasons = list(reasons)
        super().__init__(" ".join(BLOCK_MESSAGES.get(r, r) for r in self.reasons))


# --- serialisation -------------------------------------------------------------


def _encode(value):
    if isinstance(value, datetime.datetime | datetime.date | datetime.time):
        return value.isoformat()  # full microseconds, unlike DjangoJSONEncoder
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _jsonable(row: dict) -> dict:
    return json.loads(json.dumps(row, default=_encode))


def _label(model) -> str:
    return model._meta.label


# Rows whose own state moves on after a merge (deal stage, next action, task
# status): the ledger keeps and restores only the party link, so an unmerge
# never rewinds the deal and is not refused because the deal moved.
_PARTY_LINK_ONLY = {
    "crm.Engagement": ("id", "party_id"),
    "crm.Task": ("id", "party_id"),
    "crm.Touchpoint": ("id", "party_id"),
    "communications.Communication": ("id", "party_id"),
    "communications.CommunicationParticipant": ("id", "party_id", "party_email_id", "party_phone_id"),
}


def _attnames(model) -> list[str]:
    narrowed = _PARTY_LINK_ONLY.get(_label(model))
    if narrowed is not None:
        return list(narrowed)
    return [f.attname for f in model._meta.concrete_fields]


def _decode(model, row: dict) -> dict:
    fields = {f.attname: f for f in model._meta.concrete_fields}
    return {name: fields[name].to_python(value) for name, value in row.items()}


class _Ledger:
    """What one loser's merge touched: rows as they were, and ids moved/removed."""

    def __init__(self):
        self.before: dict[str, dict[str, dict]] = defaultdict(dict)
        self.moved: dict[str, set[str]] = defaultdict(set)
        self.removed: dict[str, set[str]] = defaultdict(set)

    def capture(self, model, qs) -> list[dict]:
        """Record every row of ``qs`` not yet recorded; return all of them, decoded."""
        rows = list(qs.values(*_attnames(model)))
        store = self.before[_label(model)]
        for row in rows:
            store.setdefault(str(row["id"]), _jsonable(row))
        return rows

    def as_json(self):
        return (
            {k: sorted(v) for k, v in self.moved.items() if v},
            {k: sorted(v) for k, v in self.removed.items() if v},
            {k: v for k, v in self.before.items() if v},
        )


def _digest(before: dict) -> str:
    """Fingerprint the current state of every row named in ``before``."""
    current = {}
    for label, rows in sorted(before.items()):
        model = apps.get_model(label)
        found = {
            str(r["id"]): _jsonable(r)
            for r in model._base_manager.filter(pk__in=list(rows)).values(*_attnames(model))
        }
        current[label] = {pk: found.get(pk) for pk in sorted(rows)}
    payload = json.dumps(current, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


# --- blocks --------------------------------------------------------------------


def merge_blocks(party_ids) -> list[str]:
    """Live 3.4 blocks for a would-be cluster (not the proposal's stale copy)."""
    from customers.models import Customer

    from .models import Party

    ids = list(party_ids)
    holders = set(
        Customer.objects.filter(account_holder_id__in=ids).values_list("account_holder_id", flat=True)
    )
    rows = list(Party.objects.filter(pk__in=ids).values_list("user_id", "is_self"))
    blocked = []
    if len(holders) > 1:
        blocked.append(BLOCK_TWO_ACCOUNT_HOLDERS)
    if len({u for u, _ in rows if u is not None}) > 1:
        blocked.append(BLOCK_DIFFERENT_USERS)
    if any(is_self for _, is_self in rows):
        blocked.append(BLOCK_SELF_PARTY)
    return blocked


# --- merge ---------------------------------------------------------------------


def merge(survivor, losers, *, actor: str, field_picks: dict | None = None, approval_row=None):
    """Fold ``losers`` into ``survivor``. Returns the ``PartyMergeLog`` rows.

    One transaction for the cluster: all losers merge, or none does.
    """
    from .models import Party

    if not actor:
        raise MergeError("A merge always names its decider: actor is required.")
    field_picks = dict(field_picks or {})
    loser_ids = [p.pk if isinstance(p, Party) else uuid.UUID(str(p)) for p in losers]
    survivor_id = survivor.pk if isinstance(survivor, Party) else uuid.UUID(str(survivor))
    if not loser_ids:
        raise MergeError("Nothing to merge: no duplicates given.")
    if survivor_id in loser_ids or len(set(loser_ids)) != len(loser_ids):
        raise MergeError("The survivor and the duplicates must be distinct parties.")

    merge_id = uuid.uuid4()
    with transaction.atomic():
        _lock_in_login_order([survivor_id, *loser_ids])
        everyone = {p.pk: p for p in Party.objects.filter(pk__in=[survivor_id, *loser_ids])}
        if len(everyone) != len(loser_ids) + 1:
            raise MergeError("A party in this merge no longer exists.")
        s = everyone[survivor_id]
        for p in everyone.values():
            if p.company_id != s.company_id:
                raise MergeError("Parties of different companies are never merged.")
            if p.kind != s.kind:
                raise MergeError("A person and an organization cannot be merged.")
            if p.merged_into_id is not None:
                raise MergeError(f"{p.display_name} has already been merged into another party.")
            if p.erased_at is not None:
                raise MergeError(f"{p.display_name} has been erased.")
        blocked = merge_blocks(everyone)
        if blocked:
            raise MergeBlocked(blocked)
        logs = [
            _merge_one(s, everyone[lid], merge_id=merge_id, actor=actor, field_picks=field_picks,
                       approval_row=approval_row)
            for lid in loser_ids
        ]
        if _signal("party_merged") is not None:
            _emit("party_merged", survivor=Party.objects.get(pk=s.pk),
                  merged=[Party.objects.get(pk=lid) for lid in loser_ids], merge_id=merge_id)
    return logs


# Tables ``_merge_one`` handles by name; any other FK to Party is re-pointed generically.
HANDLED_TABLES = frozenset({
    "parties.PartyEmail", "parties.PartyPhone", "parties.PartyAddress", "parties.PartyRole",
    "parties.PartyRelationship", "parties.ConsentRecord", "parties.Party", "parties.PartyMergeLog",
    "customers.Customer", "customers.CustomerContact", "crm.Lead",
    "crm.Engagement", "crm.EngagementContact", "crm.Task", "crm.Touchpoint",
    "communications.Communication", "communications.CommunicationParticipant",
    "orders.DocumentContact",
})


def _related_party_fks():
    """Every FK to Party outside the tables this module handles by name."""
    from .models import Party

    handled = HANDLED_TABLES
    out = []
    for rel in Party._meta.related_objects:
        # Reverse relations: a ForeignKey shows up as one_to_many here.
        if not (rel.one_to_many or rel.one_to_one) or _label(rel.related_model) in handled:
            continue
        out.append((rel.related_model, rel.field.name))
    return out


def _retarget_engagement_contacts(led, model, sid, lid):
    """Phase 3 1.3: move the loser's engagement contacts to the survivor.

    Where the survivor is already a contact on the same engagement
    (``uniq_engcontact_party``), the survivor's row is kept and the loser's is
    removed. Where the survivor is the engagement's own party (before or after
    the merge), the contact row is removed: that person is implicitly a
    contact. Removed roles stay in the merge log's ``before``; unmerge
    restores them.
    """
    from crm.models import Engagement

    # Engagements whose own party is (or becomes) the survivor: the person is
    # implicitly a contact there and is never duplicated as a row (1.3).
    implied = set(
        Engagement._base_manager.filter(party_id__in=[sid, lid]).values_list("pk", flat=True)
    )
    survivor_rows = led.capture(
        model, model._base_manager.filter(party_id=sid, engagement_id__in=implied)
    )
    rows = led.capture(model, model._base_manager.filter(party_id=lid))
    label = _label(model)
    drop = [r["id"] for r in survivor_rows]
    taken = set(
        model._base_manager.filter(
            party_id=sid, engagement_id__in=[r["engagement_id"] for r in rows]
        ).values_list("engagement_id", flat=True)
    ) | implied
    drop += [r["id"] for r in rows if r["engagement_id"] in taken]
    move = [r["id"] for r in rows if r["engagement_id"] not in taken]
    if drop:
        model._base_manager.filter(pk__in=drop).delete()
        led.removed[label].update(str(pk) for pk in drop)
    if move:
        model._base_manager.filter(pk__in=move).update(party_id=sid)
        led.moved[label].update(str(pk) for pk in move)


def _retarget_document_contacts(led, model, sid, lid):
    """Move the loser's places on quotes and orders to the survivor. Where the
    survivor is already on the same document (``uniq_doccontact_*``), the
    loser's row is removed; the merge log's ``before`` keeps it for unmerge."""
    rows = led.capture(model, model._base_manager.filter(party_id=lid))
    if not rows:
        return
    label = _label(model)
    survivor = model._base_manager.filter(party_id=sid)
    on_quotes = set(survivor.filter(quote__isnull=False).values_list("quote_id", flat=True))
    on_orders = set(survivor.filter(order__isnull=False).values_list("order_id", flat=True))
    drop = [r["id"] for r in rows
            if (r["quote_id"] is not None and r["quote_id"] in on_quotes)
            or (r["order_id"] is not None and r["order_id"] in on_orders)]
    move = [r["id"] for r in rows if r["id"] not in drop]
    if drop:
        model._base_manager.filter(pk__in=drop).delete()
        led.removed[label].update(str(pk) for pk in drop)
    if move:
        model._base_manager.filter(pk__in=move).update(party_id=sid)
        led.moved[label].update(str(pk) for pk in move)


def _address_key(row) -> tuple:
    def norm(v):
        return " ".join((v or "").lower().split())

    return tuple(norm(row[k]) for k in ("address_line_1", "address_line_2", "city", "state_province",
                                        "postal_code", "country"))


def _merge_one(s, loser, *, merge_id, actor, field_picks, approval_row):
    from django.contrib.contenttypes.models import ContentType

    from communications.models import Communication, CommunicationParticipant
    from core.models import ExternalRef
    from crm.models import Engagement, EngagementContact, Lead, Task, Touchpoint
    from customers.models import Customer, CustomerAddress, CustomerContact
    from orders.models import DocumentContact

    from .models import (
        ConsentRecord,
        Party,
        PartyAddress,
        PartyEmail,
        PartyMergeLog,
        PartyPhone,
        PartyRelationship,
        PartyRole,
        refresh_primary_party_contact,
    )

    led = _Ledger()
    pair = [s.pk, loser.pk]
    sid, lid = s.pk, loser.pk

    led.capture(Party, Party._base_manager.filter(pk__in=pair))

    # Contact points: move, dedupe on the normalised key, keep one primary.
    for model, key in ((PartyEmail, lambda r: r["email_normalized"]),
                       (PartyPhone, lambda r: r["number_e164"] or None),
                       (PartyAddress, _address_key)):
        rows = led.capture(model, model._base_manager.filter(party_id__in=pair))
        mine = {key(r): r["id"] for r in rows if r["party_id"] == sid and key(r) is not None}
        move, drop, twin = [], [], {}
        for r in rows:
            if r["party_id"] != lid:
                continue
            k = key(r)
            if k is not None and k in mine:
                drop.append(r["id"])
                twin[r["id"]] = mine[k]
            else:
                move.append(r["id"])
        label = _label(model)
        picked = field_picks.get(next(p for p, m in PRIMARY_PICKS.items() if m == model.__name__))
        picked = twin.get(_as_uuid(picked), _as_uuid(picked))
        current = next((r["id"] for r in rows if r["party_id"] == sid and r["is_primary"]), None)
        loser_primary = next((r["id"] for r in rows if r["party_id"] == lid and r["is_primary"]), None)
        loser_primary = twin.get(loser_primary, loser_primary)
        valid = {r["id"] for r in rows if r["party_id"] == sid} | set(move)
        if picked in valid:
            target = picked
        else:
            target = current or loser_primary
        if model is PartyAddress and drop:
            for loser_addr, survivor_addr in twin.items():
                qs = CustomerAddress._base_manager.filter(party_address_id=loser_addr)
                linked = led.capture(CustomerAddress, qs)
                if linked:
                    qs.update(party_address_id=survivor_addr)
                    led.moved[_label(CustomerAddress)].update(str(r["id"]) for r in linked)
        if model in (PartyEmail, PartyPhone) and drop:
            # A participant that used the loser's duplicate address now names
            # the survivor's copy, before the duplicate is deleted (which
            # would SET_NULL it outside the ledger).
            column = "party_email_id" if model is PartyEmail else "party_phone_id"
            for loser_point, survivor_point in twin.items():
                qs = CommunicationParticipant._base_manager.filter(**{column: loser_point})
                linked = led.capture(CommunicationParticipant, qs)
                if linked:
                    qs.update(**{column: survivor_point})
                    led.moved[_label(CommunicationParticipant)].update(str(r["id"]) for r in linked)
        if drop:
            model._base_manager.filter(pk__in=drop).delete()
            led.removed[label].update(str(pk) for pk in drop)
        if move:
            model._base_manager.filter(pk__in=move).update(
                party_id=sid, company_id=s.company_id, is_primary=False,
            )
            led.moved[label].update(str(pk) for pk in move)
        if target is not None and target != current:
            model._base_manager.filter(party_id=sid, is_primary=True).exclude(pk=target).update(is_primary=False)
            model._base_manager.filter(pk=target).update(is_primary=True)

    _coalesce_roles(led, PartyRole, sid, lid, s.company_id)
    _retarget_relationships(led, PartyRelationship, sid, lid)
    _retarget_engagement_contacts(led, EngagementContact, sid, lid)
    _retarget_document_contacts(led, DocumentContact, sid, lid)

    # Plain re-points.
    party_ct = ContentType.objects.get_for_model(Party)
    repoints = [
        (ConsentRecord, ConsentRecord._base_manager.filter(party_id=lid), {"party_id": sid}),
        (ExternalRef, ExternalRef._base_manager.filter(content_type=party_ct, object_id=lid),
         {"object_id": sid}),
        (Lead, Lead._base_manager.filter(party_id=lid), {"party_id": sid}),
        (CustomerContact, CustomerContact._base_manager.filter(party_id=lid), {"party_id": sid}),
        # CRM Phase 3 (phase3-engagement.md 1.3).
        (Engagement, Engagement._base_manager.filter(party_id=lid), {"party_id": sid}),
        (Task, Task._base_manager.filter(party_id=lid), {"party_id": sid}),
        (Touchpoint, Touchpoint._base_manager.filter(party_id=lid), {"party_id": sid}),
        (Communication, Communication._base_manager.filter(party_id=lid), {"party_id": sid}),
        (CommunicationParticipant, CommunicationParticipant._base_manager.filter(party_id=lid),
         {"party_id": sid}),
    ]
    for model, field in _related_party_fks():
        repoints.append((model, model._base_manager.filter(**{field: lid}), {field: sid}))
    # Customer.party and .account_holder move in one statement per row:
    # ``customer_individual_holder_is_party`` holds before and after, not between.
    for r in led.capture(Customer, Customer._base_manager.filter(Q(party_id=lid) | Q(account_holder_id=lid))):
        Customer._base_manager.filter(pk=r["id"]).update(
            party_id=sid if r["party_id"] == lid else r["party_id"],
            account_holder_id=sid if r["account_holder_id"] == lid else r["account_holder_id"],
        )
        led.moved[_label(Customer)].add(str(r["id"]))
    for model, qs, values in repoints:
        rows = led.capture(model, qs)
        if rows:
            model._base_manager.filter(pk__in=[r["id"] for r in rows]).update(**values)
            led.moved[_label(model)].update(str(r["id"]) for r in rows)

    # The survivor's own columns: login, names (field picks, else fill blanks).
    survivor_updates = {}
    if loser.user_id is not None and s.user_id is None:
        Party._base_manager.filter(pk=lid).update(user_id=None)
        survivor_updates["user_id"] = loser.user_id
        s.user_id = loser.user_id
    for name in PICKABLE_FIELDS:
        pick = _as_uuid(field_picks.get(name))
        attname = Party._meta.get_field(name).attname
        blank_fill = name not in PICK_ONLY and pick is None and not getattr(s, attname) and getattr(loser, attname)
        if pick == lid or blank_fill:
            value = getattr(loser, attname)
            if value != getattr(s, attname):
                survivor_updates[attname] = value
                setattr(s, attname, value)
                for companion in PICK_COMPANIONS.get(name, ()):
                    survivor_updates[companion] = getattr(loser, companion)
                    setattr(s, companion, getattr(loser, companion))
    sidecar, conflicts, moved_keys = _merged_sidecar(s, loser, field_picks)
    if sidecar is not None:
        survivor_updates["metadata"] = s.metadata = sidecar
    log_picks = {k: v for k, v in field_picks.items() if k != SIDECAR_CONFLICTS}
    if conflicts:
        log_picks[SIDECAR_CONFLICTS] = sorted(conflicts)
    if s.kind == "person" and not s.display_name_locked and {"given_name", "family_name"} & set(survivor_updates):
        derived = person_display_name(s.given_name, s.family_name)
        if derived and derived != s.display_name:
            survivor_updates["display_name"] = s.display_name = derived
    if survivor_updates:
        Party._base_manager.filter(pk=sid).update(**survivor_updates)

    now = timezone.now()
    loser_updates = {"merged_into_id": sid, "merged_at": now, "is_active": False}
    if moved_keys:  # keys only the loser had move (5.2); unmerge restores them from ``before``
        loser.metadata = {k: v for k, v in loser.metadata.items() if k not in moved_keys}
        loser_updates["metadata"] = loser.metadata
    Party._base_manager.filter(pk=lid).update(**loser_updates)
    loser.merged_into_id, loser.merged_at, loser.is_active = sid, now, False
    refresh_primary_party_contact(sid)
    refresh_primary_party_contact(lid)

    moved, removed, before = led.as_json()
    return PartyMergeLog.objects.create(
        company_id=s.company_id, merge_id=merge_id, survivor_id=sid, loser_id=lid,
        moved=moved, removed=removed, before=before, after_digest=_digest(before),
        field_picks=_jsonable(log_picks), actor=actor[:255], approval_row=approval_row,
    )


def _merged_sidecar(s, loser, field_picks):
    """``(new survivor metadata or None, conflicting keys, keys moved off the loser)``."""
    from core import sidecar

    mine = dict(s.metadata) if isinstance(s.metadata, dict) else {}
    theirs = loser.metadata if isinstance(loser.metadata, dict) else {}
    result, conflicts, moved = dict(mine), [], []
    for key, value in theirs.items():
        if sidecar.namespace(key) not in (sidecar.CUSTOM, sidecar.PROVIDER):
            continue  # system keys belong to the row that wrote them
        if key not in mine:
            result[key] = value
            moved.append(key)
        elif mine[key] != value:
            if _as_uuid(field_picks.get(key)) == loser.pk:
                result[key] = value
            conflicts.append(key)
    if result == mine:
        return None, conflicts, moved
    if len(result) > sidecar.MAX_KEYS or sidecar.size_of(result) > sidecar.MAX_BYTES:
        raise MergeError("The merged parties' extra fields would exceed the sidecar limits; "
                         "remove some imported values first.")
    return result, conflicts, moved


def _as_uuid(value):
    if value in (None, ""):
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _coalesce_roles(led, PartyRole, sid, lid, company_id):
    """Union overlapping date ranges per role, only where a loser row takes part.

    The survivor's own history is left alone otherwise. In a group, the open
    row is kept (then the survivor's, then the earliest), so the survivor
    never loses its live role row.
    """
    rows = led.capture(PartyRole, PartyRole._base_manager.filter(party_id__in=[sid, lid]))
    by_role = defaultdict(list)
    for r in rows:
        by_role[r["role"]].append(r)
    deletes, updates = [], []
    for role_rows in by_role.values():
        role_rows.sort(key=lambda r: (r["valid_from"], r["party_id"] != sid, str(r["id"])))
        groups: list[list[dict]] = []
        end = None  # None = open-ended
        for r in role_rows:
            if groups and (end is None or r["valid_from"] <= end):
                groups[-1].append(r)
                if end is not None:
                    end = None if r["valid_to"] is None else max(end, r["valid_to"])
            else:
                groups.append([r])
                end = r["valid_to"]
        for group in groups:
            if all(r["party_id"] == sid for r in group):
                continue
            keep = min(group, key=lambda r: (r["valid_to"] is not None, r["party_id"] != sid,
                                             r["valid_from"], str(r["id"])))
            valid_from = min(r["valid_from"] for r in group)
            valid_to = None if any(r["valid_to"] is None for r in group) else max(r["valid_to"] for r in group)
            deletes += [r["id"] for r in group if r is not keep]
            change = {}
            if keep["party_id"] != sid:
                change.update(party_id=sid, company_id=company_id)
            if (valid_from, valid_to) != (keep["valid_from"], keep["valid_to"]):
                change.update(valid_from=valid_from, valid_to=valid_to)
            if change:
                updates.append((keep["id"], change))
    label = _label(PartyRole)
    if deletes:
        PartyRole._base_manager.filter(pk__in=deletes).delete()
        led.removed[label].update(str(pk) for pk in deletes)
    for pk, change in updates:
        PartyRole._base_manager.filter(pk=pk).update(**change)
        led.moved[label].add(str(pk))


def _retarget_relationships(led, PartyRelationship, sid, lid):
    rows = led.capture(
        PartyRelationship,
        PartyRelationship._base_manager.filter(Q(from_party_id=lid) | Q(to_party_id=lid)),
    )
    if not rows:
        return
    open_triples = set(
        PartyRelationship._base_manager.filter(
            Q(from_party_id=sid) | Q(to_party_id=sid), valid_to__isnull=True,
        ).exclude(pk__in=[r["id"] for r in rows]).values_list("from_party_id", "to_party_id", "kind")
    )
    label = _label(PartyRelationship)
    deletes = []
    for r in rows:
        new_from = sid if r["from_party_id"] == lid else r["from_party_id"]
        new_to = sid if r["to_party_id"] == lid else r["to_party_id"]
        if new_from == new_to:
            deletes.append(r["id"])
            continue
        if r["is_symmetric"] and new_from > new_to:
            new_from, new_to = new_to, new_from
        if r["valid_to"] is None:
            triple = (new_from, new_to, r["kind"])
            if triple in open_triples:
                deletes.append(r["id"])
                continue
            open_triples.add(triple)
        PartyRelationship._base_manager.filter(pk=r["id"]).update(from_party_id=new_from, to_party_id=new_to)
        led.moved[label].add(str(r["id"]))
    if deletes:
        PartyRelationship._base_manager.filter(pk__in=deletes).delete()
        led.removed[label].update(str(pk) for pk in deletes)


# --- unmerge -------------------------------------------------------------------


def _lock_in_login_order(party_ids, customer_ids=()) -> None:
    """Lock the parties and the Customers that name them, Customer first.

    A merge repoints Customers and moves ``Party.user``, so it takes the
    login order (``core.identity.resolve.lock_login_rows``: Customer → Party
    → User) that portal login and erasure take; locking the parties first
    deadlocks against them on Postgres. A Customer that starts naming one of
    these parties between the two statements is locked after them: the only
    order break left, and only in that race.
    """
    from core.identity.resolve import lock_login_rows
    from customers.models import Customer

    def naming():
        return set(Customer._base_manager.filter(
            Q(party_id__in=party_ids) | Q(account_holder_id__in=party_ids)).values_list("pk", flat=True))

    customers = naming() | set(customer_ids)
    lock_login_rows(customers=customers, parties=party_ids)
    late = naming() - customers
    if late:
        lock_login_rows(customers=late)


def unmerge(log, *, actor: str = "", force: bool = False, emit_signal: bool = True):
    """Restore exactly what ``log``'s merge changed. Returns the log."""
    from .models import Party, PartyMergeLog

    with transaction.atomic():
        log = PartyMergeLog.objects.select_for_update().get(pk=log.pk)
        if log.undone_at is not None:
            raise MergeError("This merge has already been undone.")
        newer = PartyMergeLog.objects.filter(
            Q(survivor_id=log.survivor_id) | Q(loser_id=log.survivor_id) | Q(survivor_id=log.loser_id),
            undone_at__isnull=True, created_at__gt=log.created_at,
        ).exclude(merge_id=log.merge_id)
        if newer.exists():
            raise MergeError("A newer merge involving this survivor is still in force. Undo it first.")
        later_same = PartyMergeLog.objects.filter(
            merge_id=log.merge_id, undone_at__isnull=True, created_at__gt=log.created_at,
        )
        if later_same.exists():
            raise MergeError("Undo the later parties of this merge first (newest first).")
        survivor = Party._base_manager.get(pk=log.survivor_id)
        if Party._base_manager.filter(pk__in=[log.survivor_id, log.loser_id], erased_at__isnull=False).exists():
            # parties.erasure dropped this merge's snapshot with the person's data.
            raise MergeError("A party in this merge has been erased; it cannot be undone.")
        if survivor.merged_into_id is not None:
            raise MergeError("The survivor has since been merged into another party. Undo that merge first.")
        mine = {(label, pk) for label, rows in log.before.items() for pk in rows}
        for other in PartyMergeLog.objects.filter(
            company_id=log.company_id, undone_at__isnull=True, created_at__gt=log.created_at,
        ).exclude(merge_id=log.merge_id).only("moved", "removed"):
            theirs = {(label, pk) for part in (other.moved, other.removed) for label, ids in part.items()
                      for pk in ids}
            if mine & theirs:
                raise MergeError("A newer merge moved rows this merge touched. Undo that merge first.")
        if not force and _digest(log.before) != log.after_digest:
            raise MergeError(
                "Rows this merge moved have changed since. Undoing would overwrite those changes; "
                "review them, or undo with force."
            )
        _lock_in_login_order(
            [log.survivor_id, log.loser_id,
             *(uuid.UUID(pk) for pk in log.before.get("parties.Party", ()))],
            [uuid.UUID(pk) for pk in log.before.get("customers.Customer", ())],
        )
        try:
            with transaction.atomic():
                _restore(log.before, log.removed)
        except IntegrityError as exc:
            raise MergeError(
                f"Rows added since the merge conflict with the restore ({exc}). Resolve them first."
            ) from exc
        log.undone_at = timezone.now()
        log.undone_by = (actor or "")[:255]
        log.save(update_fields=["undone_at", "undone_by", "updated_at"])
        if emit_signal and _signal("party_unmerged") is not None:
            _emit("party_unmerged", survivor=survivor, restored=[Party._base_manager.get(pk=log.loser_id)],
                  merge_id=log.merge_id)
    return log


def unmerge_group(merge_id, *, actor: str = "", force: bool = False) -> list:
    """Undo every live log of one cluster merge, newest first; emit once."""
    from .models import Party, PartyMergeLog

    with transaction.atomic():
        logs = list(PartyMergeLog.objects.filter(merge_id=merge_id, undone_at__isnull=True)
                    .order_by("-created_at", "-id"))
        if not logs:
            raise MergeError("Nothing to undo for this merge.")
        for log in logs:
            unmerge(log, actor=actor, force=force, emit_signal=False)
        if _signal("party_unmerged") is None:
            return logs
        restored = list(Party._base_manager.filter(pk__in=[log.loser_id for log in logs]))
        _emit("party_unmerged", survivor=Party._base_manager.get(pk=logs[0].survivor_id),
              restored=restored, merge_id=merge_id)
    return logs


def _restore(before: dict, removed: dict):
    """Write ``before`` back. Order avoids transient unique-constraint clashes:
    release primaries and logins, re-insert deleted rows, then restore every
    surviving row in full."""
    decoded = {label: (apps.get_model(label), rows) for label, rows in before.items()}
    for model, rows in decoded.values():
        names = {f.attname for f in model._meta.concrete_fields}
        release = {}
        if "is_primary" in names:
            release["is_primary"] = False
        if _label(model) == "parties.Party":
            release["user_id"] = None
        if _label(model) == "customers.Customer":
            release["account_holder_id"] = None
        if _label(model) == "parties.PartyRole":
            release["valid_to"] = F("valid_from")  # closes it: no uniq_partyrole_open clash
        if release:
            model._base_manager.filter(pk__in=list(rows)).update(**release)
    for label, ids in removed.items():
        model, rows = decoded[label]
        for pk in ids:
            obj = model(**_decode(model, rows[pk]))
            obj.save_base(raw=True, force_insert=True)
    for label, (model, rows) in decoded.items():
        skip = set(removed.get(label, ()))
        for pk, row in rows.items():
            if pk in skip:
                continue
            values = _decode(model, row)
            values.pop("id")
            model._base_manager.filter(pk=pk).update(**values)


# --- signals -------------------------------------------------------------------


def _signal(name: str):
    from core.plugins import signals

    return getattr(signals, name, None)


def _emit(name: str, **kwargs):
    """Send ``core.plugins.signals.<name>`` on commit, if this build defines it.

    ``party_merged`` / ``party_unmerged`` arrive with CRM Phase 2 step 10;
    until then this is a no-op, so step 6 does not depend on that PR.
    TODO(step 10): once the signals are on main, import them directly and drop
    the ``getattr`` fallback.
    """
    signal = _signal(name)
    if signal is None:
        return
    from core.plugins.dispatch import emit

    from .models import Party

    emit(signal, Party, kwargs["survivor"], **kwargs)


__all__ = [
    "BLOCK_MESSAGES",
    "MergeBlocked",
    "MergeError",
    "PICKABLE_FIELDS",
    "merge",
    "merge_blocks",
    "unmerge",
    "unmerge_group",
]
