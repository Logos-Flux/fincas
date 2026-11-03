"""The mapping file (docs/crm/phase5-importer.md 2.12) and the standard property map.

The mapping file is TOML, one per source, kept with the export
(``<input>/mapping.toml``, or ``--mapping``). The adapter reads the parts
that change the canonical output and validates the whole file, so a typo
fails here instead of silently dropping a field at import:

- ``source``: ``hubspot:<portal id>`` (else the portal id from the account details);
- ``default_currency``: a deal's currency when neither the deal nor the portal has one;
- ``[pipelines.<id>] key`` and ``[pipelines.<id>.stages] "<stage id>" = "<key>"``: keys
  instead of the slugged ids (2.4);
- ``[deal.properties] <name> = "follow_up"``: joins ``data.follow_up`` (2.5);
- ``[task.kinds] "<hs_task_type>" = "<kind>"``: task types beyond TODO / CALL / EMAIL.

Everything else (``default_phone_region``, ``drop``/``sidecar``/``column:`` targets,
``[deal.loss_reasons]``, ``[tickets]``, ``[roles]``, ``[associations]``) is the
importer's: the adapter only checks it and keeps every property in the
record's ``properties`` whatever its target (nothing is dropped at the adapter).

``STANDARD`` lists the HubSpot properties the adapter reads into canonical
``data`` fields. It follows the standard property catalog
``docs/crm/fields/hubspot-standard.csv`` (CRM Phase 4 step 1); a test checks
the two agree whenever the catalog is present.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Mapping

from core.plugins.crm_export import AdapterError

from .values import DOCS, is_key

#: Mapping-file object tables -> HubSpot objects.
MAPPING_OBJECTS = {
    "contact": "contacts", "company": "companies", "deal": "deals", "ticket": "tickets", "task": "tasks",
    "note": "notes", "meeting": "meetings", "call": "calls", "email": "emails",
}
TOP_LEVEL = {"source", "default_currency", "default_phone_region", "pipelines", "tickets", "roles",
             "associations", "communication", *MAPPING_OBJECTS}
#: ``[communication]``: read by the importer's communications applier, not here.
COMMUNICATION_KEYS = {"default_direction", "raw_schema_version"}
OBJECT_TABLES = {"properties", "loss_reasons", "kinds"}
TARGET_RE = re.compile(r"^(sidecar|drop|follow_up|column:[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*)$")

#: HubSpot property -> the canonical ``data`` field it feeds, per object.
#: Properties not listed here go to ``properties`` verbatim.
STANDARD: dict[str, dict[str, str]] = {
    "contacts": {
        "hs_object_id": "source_id", "firstname": "given_name", "lastname": "family_name",
        "email": "emails", "hs_additional_emails": "emails", "phone": "phones", "mobilephone": "phones",
        "fax": "phones", "address": "addresses", "city": "addresses", "state": "addresses",
        "zip": "addresses", "country": "addresses", "hubspot_owner_id": "owner_ref",
        "associatedcompanyid": "company_refs", "hs_marketable_status": "marketable", "jobtitle": "job_title",
        "hs_merged_object_ids": "aliases", "createdate": "created_at", "lastmodifieddate": "updated_at",
    },
    "companies": {
        "hs_object_id": "source_id", "name": "name", "domain": "domains", "hs_additional_domains": "domains",
        "phone": "phones", "address": "addresses", "address2": "addresses", "city": "addresses",
        "state": "addresses", "zip": "addresses", "country": "addresses", "hubspot_owner_id": "owner_ref",
        "hs_merged_object_ids": "aliases", "createdate": "created_at", "hs_lastmodifieddate": "updated_at",
    },
    "deals": {
        "hs_object_id": "source_id", "dealname": "title", "pipeline": "pipeline_ref", "dealstage": "stage_ref",
        "hs_is_closed_won": "status", "hs_is_closed": "status", "closedate": "closed_at",
        "amount": "amount", "deal_currency_code": "currency", "hubspot_owner_id": "owner_ref",
        "closed_lost_reason": "loss_reason_raw", "hs_next_step": "follow_up",
        "notes_next_activity_date": "follow_up", "hs_merged_object_ids": "aliases",
        "createdate": "created_at", "hs_lastmodifieddate": "updated_at",
    },
    "tickets": {
        "hs_object_id": "source_id", "subject": "subject", "content": "description", "hs_pipeline": "pipeline_ref",
        "hs_pipeline_stage": "stage_ref", "hubspot_owner_id": "owner_ref", "createdate": "opened_at",
        "closed_date": "closed_at", "hs_lastmodifieddate": "updated_at",
    },
    "tasks": {
        "hs_object_id": "source_id", "hs_task_subject": "title", "hs_task_body": "body_text",
        "hs_task_type": "kind", "hs_task_status": "status", "hs_task_priority": "priority",
        "hs_timestamp": "due_at", "hs_task_completion_date": "completed_at", "hubspot_owner_id": "owner_ref",
        "hs_createdate": "created_at", "hs_lastmodifieddate": "updated_at",
    },
}

#: Properties whose value lives entirely in ``data`` or the envelope; every
#: other property (including the source of a lossy translation, such as
#: ``hs_task_type`` or an HTML body) is also kept in ``properties``.
CONSUMED: dict[str, frozenset[str]] = {
    "contacts": frozenset({"hs_object_id", "firstname", "lastname", "email", "hs_additional_emails", "phone",
                           "mobilephone", "fax", "address", "city", "state", "zip", "country", "hubspot_owner_id",
                           "jobtitle", "hs_merged_object_ids", "createdate", "lastmodifieddate"}),
    "companies": frozenset({"hs_object_id", "name", "domain", "hs_additional_domains", "phone", "address",
                            "address2", "city", "state", "zip", "country", "hubspot_owner_id",
                            "hs_merged_object_ids", "createdate", "hs_lastmodifieddate"}),
    "deals": frozenset({"hs_object_id", "dealname", "pipeline", "dealstage", "amount", "deal_currency_code",
                        "hubspot_owner_id", "closed_lost_reason", "hs_merged_object_ids", "createdate",
                        "hs_lastmodifieddate"}),
    "tickets": frozenset({"hs_object_id", "subject", "hs_pipeline", "hs_pipeline_stage",
                          "hubspot_owner_id", "createdate", "closed_date", "hs_lastmodifieddate"}),
    "tasks": frozenset({"hs_object_id", "hs_task_subject", "hs_timestamp", "hs_task_completion_date",
                        "hubspot_owner_id", "hs_createdate", "hs_lastmodifieddate"}),
    "notes": frozenset({"hs_object_id", "hs_timestamp", "hubspot_owner_id", "hs_createdate", "hs_lastmodifieddate"}),
    "meetings": frozenset({"hs_object_id", "hs_timestamp", "hs_meeting_title", "hs_meeting_start_time",
                           "hs_meeting_end_time", "hubspot_owner_id", "hs_createdate", "hs_lastmodifieddate"}),
    "calls": frozenset({"hs_object_id", "hs_timestamp", "hs_call_title", "hs_call_duration", "hubspot_owner_id",
                        "hs_createdate", "hs_lastmodifieddate"}),
    "emails": frozenset({"hs_object_id", "hs_timestamp", "hs_email_subject", "hubspot_owner_id", "hs_createdate",
                         "hs_lastmodifieddate"}),
}


def _fail(problem: str) -> AdapterError:
    return AdapterError(
        f"mapping file: {problem} — cause: a typo or an option this adapter does not know. "
        "Fix: correct the mapping file (docs/crm/phase5-importer.md 2.12). " + DOCS
    )


def load_mapping(path: str | Path) -> dict:
    try:
        with Path(path).open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise _fail(f"{path} is not valid TOML ({exc})") from exc


def _strings(table: Any, where: str) -> dict:
    if not isinstance(table, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in table.items()):
        raise _fail(f"[{where}] must map names to strings")
    return table


def validate_mapping(mapping: Mapping[str, Any], property_names: Mapping[str, set[str] | None]) -> dict:
    """Check the whole mapping file and return it as a plain dict.

    ``property_names`` maps each HubSpot object to the property names its
    definitions declare (None when no definitions were exported: names
    cannot be checked then). A name the definitions do not declare fails.
    """
    mapping = dict(mapping or {})
    unknown = sorted(set(mapping) - TOP_LEVEL)
    if unknown:
        raise _fail(f"unknown top-level key(s) {', '.join(unknown)}")
    source = mapping.get("source")
    if source is not None and not (isinstance(source, str) and re.match(r"^hubspot:\S+$", source)):
        raise _fail(f"source {source!r} must look like 'hubspot:<portal id>'")
    currency = mapping.get("default_currency")
    if currency is not None and not (isinstance(currency, str) and re.match(r"^[A-Z]{3}$", currency)):
        raise _fail(f"default_currency {currency!r} must be an ISO 4217 code such as 'USD'")
    region = mapping.get("default_phone_region")
    if region is not None and not (isinstance(region, str) and re.match(r"^[A-Z]{2}$", region)):
        raise _fail(f"default_phone_region {region!r} must be a two-letter region such as 'US'")

    comm = mapping.get("communication")
    if comm is not None:
        if not isinstance(comm, dict) or set(comm) - COMMUNICATION_KEYS:
            raise _fail(f"[communication] may hold only {', '.join(sorted(COMMUNICATION_KEYS))}")
        if comm.get("default_direction") not in (None, "inbound", "outbound"):
            raise _fail(f"[communication] default_direction {comm.get('default_direction')!r} must be "
                        "'inbound' or 'outbound'")

    for name, obj in MAPPING_OBJECTS.items():
        table = mapping.get(name)
        if table is None:
            continue
        if not isinstance(table, dict) or set(table) - OBJECT_TABLES:
            raise _fail(f"[{name}] may hold only {', '.join(sorted(OBJECT_TABLES))} tables")
        props = _strings(table.get("properties", {}), f"{name}.properties")
        known = property_names.get(obj)
        for prop, target in sorted(props.items()):
            if not TARGET_RE.match(target):
                raise _fail(f"[{name}.properties] {prop} = {target!r}: the target must be sidecar, drop, "
                            "follow_up or column:<model>.<field>")
            if target == "follow_up" and name != "deal":
                raise _fail(f"[{name}.properties] {prop}: follow_up applies to deals only")
            if known is not None and prop not in known:
                raise _fail(f"[{name}.properties] names {prop!r}, which is not a {obj} property in the export")
        if "loss_reasons" in table and name != "deal":
            raise _fail(f"[{name}.loss_reasons]: loss reasons apply to deals only")
        for raw, key in _strings(table.get("loss_reasons", {}), f"{name}.loss_reasons").items():
            if not is_key(key):
                raise _fail(f"[deal.loss_reasons] {raw!r} = {key!r} is not a loss reason key")
        if "kinds" in table and name != "task":
            raise _fail(f"[{name}.kinds]: kinds apply to tasks only")
        for raw, kind in _strings(table.get("kinds", {}), f"{name}.kinds").items():
            if not is_key(kind):
                raise _fail(f"[task.kinds] {raw!r} = {kind!r} is not a kind name ([a-z][a-z0-9_]*)")

    pipelines = mapping.get("pipelines", {})
    if not isinstance(pipelines, dict):
        raise _fail("[pipelines] must be a table of pipeline ids")
    for pid, spec in pipelines.items():
        if not isinstance(spec, dict) or set(spec) - {"key", "stages"}:
            raise _fail(f"[pipelines.{pid}] may hold only key and stages")
        if "key" in spec and not is_key(spec["key"]):
            raise _fail(f"[pipelines.{pid}] key {spec['key']!r} must match [a-z][a-z0-9_]{{0,31}}")
        for sid, key in _strings(spec.get("stages", {}), f"pipelines.{pid}.stages").items():
            if not is_key(key):
                raise _fail(f"[pipelines.{pid}.stages] {sid!r} = {key!r} must match [a-z][a-z0-9_]{{0,31}}")

    tickets = mapping.get("tickets", {})
    if not isinstance(tickets, dict) or set(tickets) - {"relationship_kind", "ended_stages"}:
        raise _fail("[tickets] may hold only relationship_kind and ended_stages")
    if "relationship_kind" in tickets and not is_key(tickets["relationship_kind"]):
        raise _fail(f"[tickets] relationship_kind {tickets['relationship_kind']!r} is not a kind key")
    ended = tickets.get("ended_stages", [])
    if not isinstance(ended, list) or not all(isinstance(x, str) for x in ended):
        raise _fail("[tickets] ended_stages must be a list of stage keys")

    roles = mapping.get("roles", {})
    if not isinstance(roles, dict):
        raise _fail("[roles] must be a table")
    for obj_name, by_prop in roles.items():
        if obj_name not in ("contact", "company") or not isinstance(by_prop, dict):
            raise _fail(f"[roles.{obj_name}]: roles map contact or company properties")
        known = property_names.get(MAPPING_OBJECTS[obj_name])
        for prop, values in by_prop.items():
            _strings(values, f"roles.{obj_name}.{prop}")
            if known is not None and prop not in known:
                raise _fail(f"[roles.{obj_name}] names {prop!r}, which is not a property in the export")
    assoc = mapping.get("associations", {})
    if not isinstance(assoc, dict):
        raise _fail("[associations] must be a table")
    for pair, labels in assoc.items():
        if pair not in ("contact_company", "deal_contact", "deal_company", "ticket_contact", "ticket_company"):
            raise _fail(f"[associations.{pair}] is not an association pair")
        _strings(labels, f"associations.{pair}")
    return mapping
