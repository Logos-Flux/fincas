"""The canonical import format, as data (docs/crm/importing.md).

One directory per export: ``manifest.json`` plus one UTF-8 JSONL file per
canonical object type, every line a record with the shared envelope. This
module only *declares* the format; ``validate.py`` checks a directory against
it and ``io.py`` reads and writes it.

Version 1 is the format of docs/crm/phase5-importer.md section 1.2 and 1.3.
Any change to a field, a closed list or a rule below is a new
``SCHEMA_VERSION``: readers refuse a newer version (``require_known_version``)
instead of misreading it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Mapping

#: The canonical schema version this code reads and writes.
SCHEMA_VERSION = 1
#: The newest version a reader accepts. Bumped together with SCHEMA_VERSION.
KNOWN_MAX_VERSION = 1

MANIFEST_FILE = "manifest.json"

#: Canonical object type -> its file, in the importer's apply order (4.2).
OBJECT_FILES: dict[str, str] = {
    "owner": "owners.jsonl",
    "pipeline": "pipelines.jsonl",
    "company": "companies.jsonl",
    "contact": "contacts.jsonl",
    "deal": "deals.jsonl",
    "association": "associations.jsonl",
    "relationship": "relationships.jsonl",
    "task": "tasks.jsonl",
    "communication": "communications.jsonl",
    "consent": "consent.jsonl",
    "property_def": "property_defs.jsonl",
}
OBJECT_TYPES: tuple[str, ...] = tuple(OBJECT_FILES)

#: Pseudo-type for a reference to a pipeline stage (stages live inside pipelines).
STAGE = "stage"
REF_TARGET_TYPES: tuple[str, ...] = OBJECT_TYPES + (STAGE,)

COVERAGE_VALUES = ("complete", "partial", "unknown")
#: Closed list (spec 1.3). A reason outside it is a validation error.
COVERAGE_REASONS = (
    "forbidden",
    "search_cap",
    "page_missing",
    "count_mismatch",
    "archived_not_enumerated",
    "history_not_exported",
    "pair_not_exported",
    "file_absent",
    "truncated_export",
)
ARCHIVED_ENUMERATION = ("enumerated", "not_enumerated", "not_applicable")

#: Every issue code the validator emits (docs/crm/importing.md, "Issue codes").
ISSUE_CODES = (
    # file / line level
    "manifest_missing",
    "file_missing",
    "unlisted_file",
    "invalid_encoding",
    "invalid_json",
    "blank_line",
    "not_an_object",
    # shape
    "missing_key",
    "unknown_key",
    "wrong_type",
    "invalid_value",
    # versioning
    "unknown_version",
    "version_mismatch",
    # identity and order
    "wrong_object_type",
    "source_mismatch",
    "duplicate_source_id",
    "duplicate_key",
    "unsorted",
    # references
    "wrong_ref_type",
    "dangling_ref",
    "unresolved_ref",  # warning: target type's coverage is not complete
    "stage_not_in_pipeline",
    "pipeline_kind_mismatch",
    # manifest bookkeeping
    "object_type_missing",
    "row_count_mismatch",
    "hash_mismatch",
    "coverage_reason_missing",
    "unknown_reason_code",
    # cross-field
    "inconsistent",
)

SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}:[^\s:][^\s]*$")
PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
#: Pipeline/stage keys (spec 2.4: numeric provider ids get a prefix such as
#: ``hs_``) and communication/task kind names share one pattern.
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
DECIMAL_RE = re.compile(r"^-?\d{1,15}(\.\d{1,6})?$")
INSTANT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class F:
    """One field's rule. ``kind`` picks the check; the rest narrow it."""

    kind: str
    nullable: bool = False
    choices: tuple = ()
    ref_types: tuple[str, ...] = ()
    item: "F | None" = None
    fields: Mapping[str, "F"] | None = None
    optional: Mapping[str, "F"] = field(default_factory=dict)
    max_length: int | None = None
    min_value: int | None = None
    max_value: int | None = None
    non_empty: bool = False


# kinds: str, bool, int, decimal, instant, date, enum, regex-backed (key,
# kind_name, currency), ref, list, object (fixed keys), dict (free-form JSON
# object), json (any JSON value).

def S(nullable=False, max_length=None, non_empty=False) -> F:
    return F("str", nullable=nullable, max_length=max_length, non_empty=non_empty)


def B(nullable=False) -> F:
    return F("bool", nullable=nullable)


def I(nullable=False, min_value=None, max_value=None) -> F:  # noqa: E743
    return F("int", nullable=nullable, min_value=min_value, max_value=max_value)


def E(*choices, nullable=False) -> F:
    return F("enum", choices=tuple(choices), nullable=nullable)


def L(item: F) -> F:
    return F("list", item=item)


def O(nullable=False, **fields: F) -> F:  # noqa: E743
    return F("object", fields=fields, nullable=nullable)


def R(*types: str, nullable=False) -> F:
    return F("ref", ref_types=tuple(types), nullable=nullable)


INSTANT = F("instant")
INSTANT_OR_NULL = F("instant", nullable=True)
DATE_OR_NULL = F("date", nullable=True)

PARTY_TARGETS = ("contact", "company")
ACTIVITY_TARGETS = ("contact", "company", "deal", "relationship")
ASSOCIATION_ENDS = ("contact", "company", "deal", "relationship", "task", "communication")

PHONE_KINDS = ("phone", "mobile", "fax", "other")
ADDRESS_PURPOSES = ("billing", "shipping", "mailing", "registered", "other")
STAGE_OUTCOMES = ("", "won", "lost", "closed")
DEAL_STATUSES = ("open", "won", "lost")
TASK_STATUSES = ("open", "in_progress", "waiting", "done")
TASK_PRIORITIES = ("low", "normal", "high")
COMMUNICATION_KINDS = ("note", "meeting", "call", "email", "sms")
DIRECTIONS = ("inbound", "outbound", "internal")
PARTICIPANT_ROLES = ("from", "to", "cc", "bcc", "organizer", "attendee", "caller", "callee")
CONSENT_CHANNELS = ("email", "sms", "phone", "post")
CONSENT_PURPOSES = ("marketing", "transactional")
CONSENT_STATES = ("granted", "withdrawn")
SUPPRESS_REASONS = ("opt_out", "hard_bounce", "complaint")
PROPERTY_DEF_TARGETS = ("owner", "company", "contact", "deal", "relationship", "task", "communication")

EMAIL = O(address=S(non_empty=True, max_length=254), primary=B())
PHONE = O(raw=S(non_empty=True, max_length=64), kind=E(*PHONE_KINDS), primary=B())
ADDRESS = O(
    purpose=E(*ADDRESS_PURPOSES),
    line_1=S(nullable=True),
    line_2=S(nullable=True),
    city=S(nullable=True),
    region=S(nullable=True),
    postal_code=S(nullable=True),
    country=S(nullable=True),
    primary=B(),
)
TEAM = O(source_id=S(non_empty=True, max_length=255), name=S(max_length=255), primary=B())
STAGE_SPEC = O(
    source_id=S(non_empty=True, max_length=255),
    key=F("key"),
    name=S(non_empty=True, max_length=100),
    sort_order=I(min_value=0),
    probability=I(nullable=True, min_value=0, max_value=100),
    outcome=E(*STAGE_OUTCOMES),
)
FOLLOW_UP = O(field=S(non_empty=True), value=S(nullable=True))
PARTICIPANT = O(role=E(*PARTICIPANT_ROLES), address=S(nullable=True), name=S(nullable=True))
OPTION = O(value=S(), label=S())
RAW_ENVELOPE = O(schema_version=I(min_value=1), provider=S(non_empty=True), payload=F("dict"))

#: ``data`` per object type. Every key is required (null where the source has
#: nothing); a key not listed here is ``unknown_key`` — it belongs in
#: ``properties``.
DATA_SCHEMAS: dict[str, Mapping[str, F]] = {
    "owner": {
        "email": S(nullable=True, max_length=254),
        "given_name": S(nullable=True),
        "family_name": S(nullable=True),
        "display_name": S(non_empty=True),
        "active": B(),
        "teams": L(TEAM),
    },
    "pipeline": {
        "applies_to": E("deal", "ticket"),
        "key": F("key"),
        "name": S(non_empty=True, max_length=100),
        "sort_order": I(min_value=0),
        "active": B(),
        "stages": L(STAGE_SPEC),
    },
    "company": {
        "name": S(nullable=True),
        "legal_name": S(nullable=True),
        "domains": L(S(non_empty=True, max_length=253)),
        "phones": L(PHONE),
        "addresses": L(ADDRESS),
        "owner_ref": R("owner", nullable=True),
        "registration_number": S(nullable=True),
        "tax_id": S(nullable=True),
    },
    "contact": {
        "given_name": S(nullable=True),
        "family_name": S(nullable=True),
        "emails": L(EMAIL),
        "phones": L(PHONE),
        "addresses": L(ADDRESS),
        "owner_ref": R("owner", nullable=True),
        "company_refs": L(R("company")),
        "marketable": B(nullable=True),
        "job_title": S(nullable=True),
    },
    "deal": {
        "title": S(nullable=True),
        "pipeline_ref": R("pipeline"),
        "stage_ref": R(STAGE),
        "status": E(*DEAL_STATUSES, nullable=True),
        "closed_at": INSTANT_OR_NULL,
        "amount": F("decimal", nullable=True),
        "currency": F("currency", nullable=True),
        "expected_close_date": DATE_OR_NULL,
        "owner_ref": R("owner", nullable=True),
        "company_refs": L(R("company")),
        "contact_refs": L(R("contact")),
        "loss_reason_raw": S(nullable=True),
        "follow_up": L(FOLLOW_UP),
    },
    "association": {
        "from_ref": R(*ASSOCIATION_ENDS),
        "to_ref": R(*ASSOCIATION_ENDS),
        "label": S(nullable=True),
        "primary": B(),
    },
    "relationship": {
        "pipeline_ref": R("pipeline"),
        "stage_ref": R(STAGE),
        "state": E("open", "closed"),
        "subject": S(nullable=True),
        "description": S(nullable=True),
        "opened_at": INSTANT_OR_NULL,
        "closed_at": INSTANT_OR_NULL,
        "owner_ref": R("owner", nullable=True),
        "contact_refs": L(R("contact")),
        "company_refs": L(R("company")),
    },
    "task": {
        "title": S(nullable=True),
        "body_text": S(nullable=True),
        "kind": F("kind_name"),
        "status": E(*TASK_STATUSES),
        "priority": E(*TASK_PRIORITIES, nullable=True),
        "due_at": F("instant_or_date", nullable=True),  # date when due_is_date
        "due_is_date": B(),
        "completed_at": INSTANT_OR_NULL,
        "owner_ref": R("owner", nullable=True),
        "target_refs": L(R(*ACTIVITY_TARGETS)),
    },
    "communication": {
        "kind": E(*COMMUNICATION_KINDS),
        "direction": E(*DIRECTIONS, nullable=True),
        "status": S(nullable=True),
        "subject": S(nullable=True),
        "body_text": S(nullable=True),
        "body_html": S(nullable=True),
        "summary": S(nullable=True),
        "occurred_at": INSTANT,
        "ended_at": INSTANT_OR_NULL,
        "duration_seconds": I(nullable=True, min_value=0),
        "owner_ref": R("owner", nullable=True),
        "participants": L(PARTICIPANT),
        "target_refs": L(R(*ACTIVITY_TARGETS)),
        "raw": RAW_ENVELOPE,
    },
    "consent": {
        "address": S(non_empty=True, max_length=254),
        "channel": E(*CONSENT_CHANNELS),
        "purpose": E(*CONSENT_PURPOSES, nullable=True),  # null for a suppression-only fact
        "state": E(*CONSENT_STATES, nullable=True),
        "suppress": E(*SUPPRESS_REASONS, nullable=True),
        "occurred_at": INSTANT_OR_NULL,
        "subscription": O(nullable=True, id=S(non_empty=True), name=S(nullable=True)),
        "evidence": S(nullable=True),
    },
    "property_def": {
        "object_type": E(*PROPERTY_DEF_TARGETS),
        "name": S(non_empty=True),
        "label": S(),
        "type": S(non_empty=True),
        "field_type": S(nullable=True),
        "options": L(OPTION),
        "group": S(nullable=True),
        "provider_defined": B(),
    },
}

HISTORY_ENTRY = O(property=S(non_empty=True), value=F("json"), at=INSTANT, source=S(nullable=True))

#: The envelope every record shares (spec 1.2).
ENVELOPE_REQUIRED: dict[str, F] = {
    "schema_version": I(min_value=1),
    "object_type": E(*OBJECT_TYPES),
    "source": F("source"),
    "source_id": S(non_empty=True, max_length=255),
    "source_object_type": S(non_empty=True, max_length=64),
    "data": F("dict"),  # checked against DATA_SCHEMAS[object_type]
}
ENVELOPE_OPTIONAL: dict[str, F] = {
    "aliases": L(S(non_empty=True, max_length=255)),
    "archived": B(),
    "created_at": INSTANT_OR_NULL,
    "updated_at": INSTANT_OR_NULL,
    "properties": F("dict"),
    "history": L(HISTORY_ENTRY),
    "raw_ref": S(nullable=True),
}

REASON = O(code=F("reason_code"), detail=S())
MANIFEST_OBJECT_REQUIRED: dict[str, F] = {
    "file": S(non_empty=True),
    "rows": I(min_value=0),
    "coverage": E(*COVERAGE_VALUES),
}
MANIFEST_OBJECT_OPTIONAL: dict[str, F] = {
    "declared_total": I(nullable=True, min_value=0),
    "archived": E(*ARCHIVED_ENUMERATION),
    "history": L(S(non_empty=True)),
    "reasons": L(REASON),
}
MANIFEST_REQUIRED: dict[str, F] = {
    "schema_version": I(min_value=1),
    "provider": F("provider"),
    "source": F("source"),
    "adapter": O(id=S(non_empty=True), version=S(non_empty=True), schema_version=I(min_value=1)),
    "exported_at": INSTANT,
    "input": O(kind=S(non_empty=True), time_zone=F("time_zone", nullable=True)),
    "objects": F("dict"),
    "files": F("dict"),
}


def newer_version_message(version, where: str) -> str:
    """The refusal for a newer schema (problem + cause + fix + docs link)."""
    return (
        f"{where} has schema_version {version!r}, newer than this importer reads "
        f"(max {KNOWN_MAX_VERSION}) — cause: the export was written by a newer adapter. "
        "Fix: upgrade golden-erp before importing, or re-export with an adapter that "
        "writes this version. See docs/crm/importing.md."
    )
