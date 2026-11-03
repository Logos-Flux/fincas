"""Companies and contacts → parties (docs/crm/phase5-importer.md 2.6, 3.4).

One canonical record becomes one ``Party`` (``create``), or is attached to
one existing party (``link``, ``update``); never two, and two records never
become one here (I5).

- **create**: the party (``origin="import"``, ``owner`` from ``owner_ref``,
  ``owner_team`` stamped by ``save()``), its contact points, its roles (only
  through the mapping file, 2.6), its sidecar and one ExternalRef per id and
  alias. Contact points are ``bulk_create``d (4.3), normalised here as their
  ``save()`` would, and the party's primary columns are then re-derived once
  (``refresh_primary_party_contact``): no ``post_save`` receiver runs, so the
  communications participant linker does not touch rows outside the write
  log (nor does step 6: ``manage.py resolve_participants`` after the import).
- **link** (a match, 3.1): the ExternalRefs, the contact points the party
  lacks (added as non-primary, unless the party has no primary of that kind),
  mapped roles it does not hold, and the sidecar. Columns golden already has
  are never overwritten by a link.
- **update** (a re-import, 3.4): the fields the three-way compare said to
  update, minus the ones the reviewer kept; contact points the source added;
  never a deletion. If golden changed a field since the decision was taken,
  the row fails ``conflict_stale`` and goes back to review.

Emails go through ``parties.normalize.email``; an address that fails is kept
in the sidecar (``<provider>.import_unparsed_emails``) and never dropped.
Phones keep ``number_raw``; E.164 uses the record's address country, else the
mapping file's ``default_phone_region``, else ``GOLDEN_DEFAULT_PHONE_REGION``.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.utils import timezone

from parties.constants import ORGANISATION, PERSON
from parties.normalize import person_display_name

from .. import compare
from ..sidecar import DERIVED_PREFIX, SidecarRefused, key_name, put
from .base import CONFLICT_STALE, ERASED, INVALID_VALUE, Row, RowError, applied_ref

LINE_TYPES = {"mobile": "mobile", "fax": "fax"}
PURPOSE = "other"  # the source does not say whether an address is work or personal

# Party columns a ``column:party.<field>`` mapping may write (2.12). Identity,
# tenancy, merge and erasure state, the denormalised contact columns and the
# sidecar itself are never a mapping target.
PARTY_COLUMN_TARGETS = frozenset({
    "legal_name", "registration_number", "tax_id", "lifecycle_stage", "contact_type", "primary_domain",
})


# --- helpers ---------------------------------------------------------------------


def _locked_party(row: Row):
    """The approved target party, locked (Party → User: the login order after
    the job lock), refused when merged away or erased since the decision."""
    from core.identity.resolve import lock_login_rows
    from parties.models import Party

    target = row.target(Party)
    lock_login_rows(parties=[target.pk])
    party = Party._base_manager.get(pk=target.pk)
    if party.erased_at is not None:
        raise RowError(ERASED, "the matched party has been erased since; it is not imported again.")
    if party.merged_into_id is not None:
        raise RowError(CONFLICT_STALE, "the matched party has been merged into another since; re-run matching.")
    expected = PERSON if row.object_type == "contact" else ORGANISATION
    if party.kind != expected:
        raise RowError(INVALID_VALUE, f"a {row.object_type} cannot link to a {party.kind} party.")
    return party


def _region(row: Row) -> str | None:
    return compare.record_region(row.record) or (row.mapping.get("default_phone_region") or None)


def _emails(row: Row) -> tuple[list[tuple[str, str, bool]], list[str]]:
    """``[(address, normalised, primary)]`` primary first, plus the unparseable ones."""
    from parties.normalize import normalize_email

    good, bad, seen = [], [], set()
    items = sorted(row.data.get("emails") or [], key=lambda e: not e.get("primary"))
    for item in items:
        address = (item.get("address") or "").strip()
        if not address:
            continue
        try:
            normalised = normalize_email(address)
        except ValidationError:
            bad.append(address)
            continue
        if len(address) > 254 or normalised in seen:
            if normalised not in seen:
                bad.append(address)
            continue
        seen.add(normalised)
        good.append((address, normalised, bool(item.get("primary"))))
    return good, bad


def _phones(row: Row) -> list[dict]:
    from parties.normalize import normalize_phone, phone_line_type

    out, seen = [], set()
    region = _region(row)
    items = sorted(row.data.get("phones") or [], key=lambda p: not p.get("primary"))
    for item in items:
        raw = (item.get("raw") or "").strip()[:50]
        if not raw:
            continue
        e164, extension = normalize_phone(raw, region)
        key = e164 or f"raw:{raw}"
        if key in seen:
            continue
        seen.add(key)
        line = LINE_TYPES.get(item.get("kind")) or phone_line_type(e164)
        out.append({"number_raw": raw, "number_e164": e164, "extension": extension[:10], "line_type": line,
                    "primary": bool(item.get("primary"))})
    return out


def _addresses(row: Row) -> list[dict]:
    out = []
    for item in row.data.get("addresses") or []:
        parts = {k: (item.get(k) or "").strip() for k in ("line_1", "line_2", "city", "region", "postal_code",
                                                           "country")}
        if not any(parts.values()):
            continue
        country = parts["country"].upper()
        out.append({
            "purpose": item.get("purpose") or "other", "address_line_1": parts["line_1"][:255],
            "address_line_2": parts["line_2"][:255], "city": parts["city"][:100],
            "state_province": parts["region"][:100], "postal_code": parts["postal_code"][:20],
            "country": country if len(country) == 2 else "", "primary": bool(item.get("primary")),
        })
    return out


def _one_primary(items: list[dict], has_primary: bool) -> None:
    """At most one primary: the source's first, unless the party has one already."""
    taken = has_primary
    for item in items:
        item["is_primary"] = bool(item.pop("primary", False)) and not taken
        taken = taken or item["is_primary"]
    if not taken and items:
        items[0]["is_primary"] = True


def add_contact_points(row: Row, party, *, existing: bool, emails: bool = True, phones: bool = True) -> None:
    """Add the record's emails, phones and addresses the party does not hold
    (``emails`` / ``phones`` False: a reviewer kept golden's set)."""
    from parties.models import PartyAddress, PartyEmail, PartyPhone, refresh_primary_party_contact

    s = row.session
    emails = _emails(row)[0] if emails else []
    have = set(PartyEmail._base_manager.filter(party_id=party.pk).values_list("email_normalized", flat=True))
    new_emails = [{"email": a, "email_normalized": n, "primary": p} for a, n, p in emails if n not in have]
    _one_primary(new_emails, PartyEmail._base_manager.filter(party_id=party.pk, is_primary=True).exists())

    have_phones = set(PartyPhone._base_manager.filter(party_id=party.pk).values_list("number_e164", "number_raw"))
    have_e164 = {e for e, _ in have_phones if e}
    have_raw = {r for e, r in have_phones if not e}
    new_phones = [p for p in (_phones(row) if phones else [])
                  if (p["number_e164"] and p["number_e164"] not in have_e164)
                  or (not p["number_e164"] and p["number_raw"] not in have_raw)]
    _one_primary(new_phones, PartyPhone._base_manager.filter(party_id=party.pk, is_primary=True).exists())

    have_addr = set(PartyAddress._base_manager.filter(party_id=party.pk).values_list(
        "address_line_1", "city", "postal_code", "country"))
    new_addresses = [a for a in _addresses(row)
                     if (a["address_line_1"], a["city"], a["postal_code"], a["country"]) not in have_addr]
    _one_primary(new_addresses, PartyAddress._base_manager.filter(party_id=party.pk, is_primary=True).exists())

    common = {"party_id": party.pk, "company_id": party.company_id, "source": row.source_tag}
    rows = (
        [PartyEmail(**common, purpose=PURPOSE, **e) for e in new_emails],
        [PartyPhone(**common, purpose=PURPOSE, **p) for p in new_phones],
        [PartyAddress(**common, **a) for a in new_addresses],
    )
    if existing and (new_emails or new_phones):
        s.touch(type(party), party.pk)  # its primary columns are re-derived below
    for objs in rows:
        if objs:
            type(objs[0]).objects.bulk_create(objs)
            for obj in objs:
                s.create(obj)
    if new_emails or new_phones:
        refresh_primary_party_contact(party.pk)


def _role_names(row: Row) -> list[str]:
    """Roles the mapping file opens for this record (``[roles.<type>.<property>]``)."""
    section = ((row.mapping.get("roles") or {}).get(row.object_type) or {})
    roles = []
    for prop, values in section.items():
        value = row.properties.get(prop)
        if isinstance(values, dict) and isinstance(value, str) and value in values:
            roles.append(values[value])
    return sorted(set(roles))


def add_roles(row: Row, party) -> None:
    from parties.models import PartyRole
    from parties.role_registry import get_party_role

    for role in _role_names(row):
        try:
            config = get_party_role(role)
        except KeyError:
            raise RowError(INVALID_VALUE, f"the mapping names role {role!r}, which is not a party role.") from None
        if config.managed_by == "system":
            raise RowError(INVALID_VALUE, f"{role!r} is maintained by golden itself; an import never opens it.")
        if party.kind not in config.kinds:
            raise RowError(INVALID_VALUE, f"a {party.kind} party cannot hold the {role!r} role.")
        if PartyRole._base_manager.filter(party_id=party.pk, role=role, valid_to__isnull=True).exists():
            continue
        obj = PartyRole(party=party, company_id=party.company_id, role=role,
                        valid_from=timezone.localdate(), source=row.source_tag)
        obj.save()
        row.session.create(obj)


def _sidecar_values(row: Row) -> tuple[dict, dict]:
    """Source properties not mapped elsewhere, plus what has no column (2.6)."""
    targets = row.property_targets()
    values = {name: value for name, value in row.properties.items()
              if targets.get(name, "sidecar") == "sidecar"
              and f"properties.{key_name(name)}" not in row.kept}  # a reviewer kept golden's value
    derived = {}
    _good, bad = _emails(row)
    if bad:
        derived["unparsed_emails"] = bad
    domains = row.data.get("domains") or []
    if len(domains) > 1:
        derived["domains"] = list(domains)
    if row.data.get("job_title"):
        derived["job_title"] = row.data["job_title"]
    owner_ref = row.data.get("owner_ref")
    if owner_ref and not row.resolve(owner_ref, _profile_model(), required=False):
        derived["owner_unresolved"] = str(owner_ref.get("source_id"))
    return values, {f"{DERIVED_PREFIX}{k}": v for k, v in derived.items()}


def _profile_model():
    from core.models import UserProfile

    return UserProfile


def _column_values(row: Row) -> dict:
    """``column:party.<field>`` mapping targets (2.12)."""
    out = {}
    for name, target in row.property_targets().items():
        if not isinstance(target, str) or not target.startswith("column:"):
            continue
        model, _, column = target.removeprefix("column:").partition(".")
        if model != "party":
            continue  # other models' columns belong to their own appliers (relationship.title: step 5)
        if column not in PARTY_COLUMN_TARGETS:
            raise RowError(INVALID_VALUE, f"{target!r} is not a writable party column.")
        value = row.properties.get(name)
        if value is not None:
            out[column] = str(value)
    return out


def _write_sidecar(row: Row, party) -> None:
    try:
        values, derived = _sidecar_values(row)
        put(party, row.provider, values, derived=derived, session=row.session)
    except SidecarRefused as exc:
        raise RowError(INVALID_VALUE, str(exc)) from exc


def _owner(row: Row):
    return row.resolve(row.data.get("owner_ref"), _profile_model(), required=False)


# --- the three decisions --------------------------------------------------------


def _create(row: Row, kind: str):
    from parties.models import Party

    data = row.data
    fields = {"company_id": row.company.pk, "kind": kind, "origin": Party.Origin.IMPORT, "owner": _owner(row)}
    if kind == PERSON:
        fields.update(given_name=(data.get("given_name") or "")[:100], family_name=(data.get("family_name") or "")[:100])
        fields["display_name"] = (" ".join(p for p in (fields["given_name"], fields["family_name"]) if p))[:255]
        if data.get("marketable") is True:
            fields.update(marketable=True, marketable_source=row.source_tag, marketable_changed_at=timezone.now())
    else:
        domain = compare.primary_domain(row.record)
        name = data.get("name") or data.get("legal_name") or domain
        fields.update(display_name=(name or "")[:255], legal_name=(data.get("legal_name") or "")[:255],
                      primary_domain=domain, registration_number=(data.get("registration_number") or "")[:64],
                      tax_id=(data.get("tax_id") or "")[:64])
    fields.update(_column_values(row))
    party = Party(**fields)
    party.clean()
    party.save()
    row.session.create(party)
    add_contact_points(row, party, existing=False)
    add_roles(row, party)
    _write_sidecar(row, party)
    row.refs_for(party)
    return party


def _link(row: Row):
    party = _locked_party(row)
    row.refs_for(party)
    add_contact_points(row, party, existing=True)
    add_roles(row, party)
    _write_sidecar(row, party)
    return party


# Re-import: a projected field -> the Party column it writes (compare.GOLDEN_COLUMNS).
UPDATE_COLUMNS = {
    "contact": {"given_name": "given_name", "family_name": "family_name"},
    "company": {"name": "display_name", "legal_name": "legal_name", "primary_domain": "primary_domain",
                "registration_number": "registration_number", "tax_id": "tax_id"},
}


def _stale_fields(row: Row, party) -> list[str]:
    """Fields the approved decision wrote over golden's value, where golden
    has changed again since the decision (3.4: staff edits are never lost)."""
    from ..match import decide
    from ..stage import previous_chain

    approved = ((row.staged.proposed_mapping or {}).get("diff") or {}).get("fields") or {}
    now = decide(row.company, row.record, source=row.job.source, previous_jobs=previous_chain(row.job),
                 options=row.job.options)
    fresh = (now.diff or {}).get("fields") or {}
    return sorted(f for f, state in fresh.items()
                  if state == compare.CONFLICT and approved.get(f) != compare.CONFLICT and f not in row.kept)


def _update(row: Row):
    party = _locked_party(row)
    stale = _stale_fields(row, party)
    if stale:
        raise RowError(CONFLICT_STALE, f"golden changed {', '.join(stale)} after the decision; review it again.")
    diff = ((row.staged.proposed_mapping or {}).get("diff") or {}).get("fields") or {}
    columns = {}
    for field, column in UPDATE_COLUMNS[row.object_type].items():
        if field in diff and field not in row.kept and field in row.values:
            columns[column] = (row.values[field] or "")[: party._meta.get_field(column).max_length]
    if "owner_ref" in diff and "owner_ref" not in row.kept:
        owner = _owner(row)
        columns["owner_id"] = owner.pk if owner else None
        columns["owner_team_id"] = _team_of(owner, party)  # stamped as save() would (core.teams)
    if columns:
        for name, value in columns.items():
            setattr(party, name, value)
        party.clean()
        if party.kind == PERSON and not party.display_name_locked and {"given_name", "family_name"} & set(columns):
            derived = person_display_name(party.given_name, party.family_name)
            if derived:  # as Party.save() would; the write is an update(), so do it here
                columns["display_name"] = derived[:255]
        row.session.update(party, **columns)
    row.refs_for(party)
    add_contact_points(row, party, existing=True, emails="emails" not in row.kept,
                       phones="phones" not in row.kept)
    add_roles(row, party)
    _write_sidecar(row, party)
    return party


def _team_of(owner, party):
    from core.teams import team_of

    return team_of(owner, party.company_id)


def apply(row: Row) -> str:
    kind = PERSON if row.object_type == "contact" else ORGANISATION
    handler = {"create": lambda r: _create(r, kind), "link": _link, "update": _update}.get(row.decision)
    if handler is None:
        raise RowError(INVALID_VALUE, f"a {row.object_type} row cannot be applied as {row.decision!r}.")
    party = handler(row)
    return applied_ref(type(party), party.pk)


__all__ = ["apply", "add_contact_points", "add_roles"]
