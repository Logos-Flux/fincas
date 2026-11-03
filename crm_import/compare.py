"""Re-import's three-way compare (docs/crm/phase5-importer.md 3.4).

For a record whose ``ExternalRef`` already exists, three values of every
field are compared:

- **previous**: what the previous import wrote, from that job's applied
  ``StagedRow.mapped_values["values"]`` (``project`` of the record it applied);
- **golden**: what the target row holds now (only for fields with a column,
  ``GOLDEN_COLUMNS``);
- **source**: ``project`` of the new record.

=====================================  ===========================
Field                                  Outcome
=====================================  ===========================
source == previous                     nothing (the source did not change it)
golden == previous, source changed     ``update``
golden == source                       nothing (already equal)
golden differs from both               ``conflict`` (staff changed it)
no golden column, source changed       ``update`` (importer-owned: sidecar values)
=====================================  ===========================

Without a previous import of the record (no ``previous_job``, or none of the
jobs in its chain applied it) there is no record of what the importer wrote,
so every golden-readable field that differs is a ``conflict``, and so is
every source-only field with a value (golden cannot show it, and nothing says
the importer wrote it).

Contact points (``emails``, ``phones``) are sets: addresses the source added
are added, ones it removed are reported and never deleted. Without a
previous import, an address golden lacks is a conflict (staff may have
deleted it on purpose).

``project`` is also what approval stores in ``mapped_values["values"]``, so
the appliers (steps 4-6) write exactly these values and the next re-import
compares against them. The diff stored on the staged row names fields and
states only, never values: the review screen reads live values (3.3).
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError

from parties.mirror import safe_email
from parties.normalize import normalize_domain, normalize_phone

from .sidecar import key_name

UPDATE = "update"
CONFLICT = "conflict"

# Canonical field -> the target model and the column holding the same value.
# A field missing here is source-only (sidecar, refs, history): compared
# against the previous import, never against golden.
TARGET_MODELS: dict[str, str] = {
    "owner": "core.userprofile",
    "pipeline": "crm.pipeline",
    "company": "parties.party",
    "contact": "parties.party",
    "deal": "crm.engagement",
    "task": "crm.task",
    "communication": "communications.communication",
}
GOLDEN_COLUMNS: dict[str, dict[str, str]] = {
    "owner": {"display_name": "display_name"},
    "pipeline": {"name": "name", "sort_order": "sort_order", "active": "is_active"},
    "company": {
        "name": "display_name", "legal_name": "legal_name", "primary_domain": "primary_domain",
        "registration_number": "registration_number", "tax_id": "tax_id",
    },
    "contact": {"given_name": "given_name", "family_name": "family_name"},
    "deal": {
        "title": "title", "amount": "expected_value", "currency": "currency",
        "expected_close_date": "expected_close_date",
    },
    "task": {"title": "title", "due_at": "due_at", "completed_at": "completed_at"},
    "communication": {"subject": "subject", "body_text": "body_text", "occurred_at": "occurred_at"},
}
EMPTY_VALUES = ("", "[]", "{}")
SIDECAR_READABLE = "__sidecar__"  # in the golden dict (not metadata): the target shows its properties
SET_FIELDS = ("emails", "phones")  # contact points: added, never removed
# Envelope keys that are change facts of their own; timestamps and raw_ref
# are not (a re-export that only bumped updated_at is unchanged).
ENVELOPE_FIELDS = ("archived", "aliases")


def record_region(record: dict) -> str | None:
    """The country of the record's primary address, for national phone numbers."""
    addresses = (record.get("data") or {}).get("addresses") or []
    primary = next((a for a in addresses if a.get("primary")), addresses[0] if addresses else None)
    country = (primary or {}).get("country") or None
    return country.strip().upper() if isinstance(country, str) and country.strip() else None


def contact_emails(record: dict) -> list[str]:
    """Normalised addresses, primary first; invalid ones are dropped here (the
    applier keeps them in the sidecar and reports them, 2.6)."""
    out = []
    emails = sorted((record.get("data") or {}).get("emails") or [], key=lambda e: not e.get("primary"))
    for item in emails:
        email = safe_email(item.get("address"))
        if email and email not in out:
            out.append(email)
    return out


def contact_phones(record: dict, region: str | None = None) -> list[str]:
    """E.164 numbers, primary first. Unparseable numbers are kept as
    ``raw:<text>`` so a set compare still sees them."""
    region = region or record_region(record)
    out = []
    phones = sorted((record.get("data") or {}).get("phones") or [], key=lambda p: not p.get("primary"))
    for item in phones:
        raw = (item.get("raw") or "").strip()
        if not raw:
            continue
        e164, _ext = normalize_phone(raw, region)
        key = e164 or f"raw:{raw}"
        if key not in out:
            out.append(key)
    return out


def primary_domain(record: dict) -> str:
    domains = (record.get("data") or {}).get("domains") or []
    if not domains:
        return ""
    try:
        return normalize_domain(domains[0])
    except ValidationError:
        return ""


def _stable(value) -> str:
    """One comparable string per value: ``None`` and ``""`` are the same
    absence, decimals compare by value, instants in UTC, lists as JSON."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return _decimal(value)
    if isinstance(value, dt.datetime):
        if value.tzinfo is not None:
            value = value.astimezone(dt.UTC).replace(tzinfo=None)
        return value.isoformat(timespec="microseconds")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    text = str(value).strip()
    if len(text) >= 20 and text.endswith("Z") and "T" in text:  # canonical instant
        try:
            return _stable(dt.datetime.fromisoformat(text[:-1]))
        except ValueError:
            return text
    return text


def _decimal(value) -> str:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    return format(d.normalize(), "f")


def project(object_type: str, record: dict) -> dict:
    """The comparable values of one canonical record (JSON-safe).

    Keys: every ``data`` field (``company`` adds ``primary_domain``; ``deal``
    amounts compare by value), contact points as sorted lists, every source
    property as ``properties.<name>`` (``<name>`` spelled as its sidecar key,
    ``sidecar.key_name``), ``archived``, ``aliases`` and, when
    exported, ``history``.
    """
    data = dict(record.get("data") or {})
    out: dict = {}
    if object_type == "contact":
        data.pop("emails", None)
        data.pop("phones", None)
        out["emails"] = sorted(contact_emails(record))
        out["phones"] = sorted(contact_phones(record))
    if object_type == "company":
        out["primary_domain"] = primary_domain(record)
        out["phones"] = sorted(contact_phones(record))
        data.pop("phones", None)
    if object_type == "deal" and data.get("amount") is not None:
        data["amount"] = _decimal(data["amount"])
    if object_type == "communication":
        data.pop("raw", None)  # CommunicationRaw keeps it; body fields carry the content
    for key, value in data.items():
        out[key] = _stable(value)
    for key, value in sorted((record.get("properties") or {}).items()):
        out[f"properties.{key_name(key)}"] = _stable(value)
    for key in ENVELOPE_FIELDS:
        out[key] = _stable(record.get(key))
    if "history" in record:
        out["history"] = _stable(record["history"])
    return out


def golden_values(object_type: str, obj, *, provider: str = "", emails=(), phones=()) -> dict:
    """The golden side of the compare for ``obj`` (the ExternalRef target).

    Source properties are read back from the record's sidecar: Phase 4's flat
    ``"<provider>.<name>"`` keys (``crm_import.sidecar.properties``), where the
    appliers put them through ``core.sidecar.set_fields``."""
    columns = GOLDEN_COLUMNS.get(object_type, {})
    out = {field: _stable(getattr(obj, column, None)) for field, column in columns.items()}
    from .sidecar import properties

    sidecar = properties(obj, provider)
    if sidecar is not None:
        out[SIDECAR_READABLE] = "1"
        for key, value in sidecar.items():
            out[f"properties.{key}"] = _stable(value)
    if object_type == "contact":
        out["emails"] = sorted(set(emails))
        out["phones"] = sorted(set(phones))
    if object_type == "company":
        out["phones"] = sorted(set(phones))
    return out


def three_way(object_type: str, previous: dict | None, golden: dict | None, source: dict) -> dict:
    """Compare and return ``{"fields": {name: state}, "added": {..}, "removed": {..}}``.

    ``golden`` is None when the target could not be read (then every field is
    source-only). ``previous`` is None without a previous import of the record.
    """
    readable = set(GOLDEN_COLUMNS.get(object_type, {})) if golden is not None else set()
    fields: dict[str, str] = {}
    added: dict[str, int] = {}
    removed: dict[str, int] = {}
    for name in sorted(set(source) | set(previous or {})):
        new = source.get(name)
        old = (previous or {}).get(name) if previous is not None else None
        if name in SET_FIELDS and name in source:
            new_set, old_set = set(new or []), set(old or [])
            gold_set = set((golden or {}).get(name) or [])
            if previous is not None:
                fresh = new_set - old_set - gold_set
                gone = old_set - new_set
                if fresh:
                    fields[name] = UPDATE
                    added[name] = len(fresh)
                if gone:
                    removed[name] = len(gone)
            elif golden is not None:
                missing = new_set - gold_set
                if missing:
                    fields[name] = CONFLICT
                    added[name] = len(missing)
            else:
                fields[name] = UPDATE
            continue
        if name not in source:
            continue  # a field the new record no longer has: never a deletion
        if name in readable or (name.startswith("properties.") and SIDECAR_READABLE in (golden or {})):
            gold = golden.get(name, "")
            if previous is not None and name in previous:
                if new == old or new == gold:
                    continue
                fields[name] = UPDATE if gold == old else CONFLICT
            elif new != gold:
                fields[name] = CONFLICT if previous is None else UPDATE if not gold else CONFLICT
            continue
        if previous is None:
            # Unreadable and never recorded: the source has a value golden
            # cannot show and no import is known to have written. A human
            # decides rather than the importer silently writing or dropping it.
            if new not in EMPTY_VALUES and not (name in ENVELOPE_FIELDS and new in ("false", "[]")):
                fields[name] = CONFLICT
            continue
        if name in previous:
            if new != old:
                fields[name] = UPDATE
        elif new:
            fields[name] = UPDATE
    return {"fields": fields, "added": added, "removed": removed}


def outcome(diff: dict) -> str:
    """``conflict`` / ``update`` / ``unchanged`` for a three-way diff."""
    states = set(diff["fields"].values())
    if CONFLICT in states:
        return CONFLICT
    if states:
        return UPDATE
    return "unchanged"
