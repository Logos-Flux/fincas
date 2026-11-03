"""Read a HubSpot file export (docs/crm/phase5-importer.md 2.1).

Layout::

    export/export.json                 envelope; payload {exported_at, account?}, written by the operator
    export/<object>.csv                "Export all properties" of contacts, companies, deals, tickets, tasks
    export/properties-<object>.json    GET /crm/v3/properties/<object> (required: it maps headers and labels)
    export/pipelines-deals.json, export/pipelines-tickets.json
    export/owners.json, export/owners-archived.json

What a HubSpot CSV export is assumed to look like (each assumption is a
fixture under ``fixtures/export-csv/``):

- UTF-8 (a byte-order mark is allowed), comma-separated, double-quoted, a header row;
- headers are property **labels** ("First Name"), or internal names; both are
  resolved through the property definitions, and an unknown header is refused;
- enumeration and boolean cells hold the option **label**; they are mapped
  back to the internal value (an unmatched cell is kept verbatim);
- date-time cells have **no UTC offset** (the exporting user's zone): refused
  without ``--time-zone``; date-only cells stay dates;
- associations come only as ``Associated <Object> IDs`` columns
  (``;``-separated ids): no labels, no primary flag;
- no archived records (the recycle bin is not exported), no history.
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from zoneinfo import ZoneInfo

from core.plugins.crm_export import AdapterError

from .model import Bundle, Feed, HsRecord, Status, orient
from .reader_crawl import _single_list
from .recording import read_envelope
from .values import DOCS, blank, parse_date, parse_instant, split_multi

CSV_OBJECTS = ("contacts", "companies", "deals", "tickets", "tasks")
NOT_EXPORTABLE = ("notes", "meetings", "calls", "emails")
ASSOC_RE = re.compile(r"^Associated (Contact|Company|Deal|Ticket) IDs$")
ASSOC_OBJECTS = {"Contact": "contacts", "Company": "companies", "Deal": "deals", "Ticket": "tickets"}

#: Row counts at which an export is known to stop (spec 2.13 "export
#: truncation"). An export of exactly this many rows is assumed cut off.
EXPORT_ROW_LIMITS = (1_000_000,)


def _definitions(defs: list) -> tuple[dict, dict]:
    by_name: dict[str, dict] = {}
    by_label: dict[str, list[str]] = {}
    for item, _ref in defs:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            by_name[item["name"]] = item
            label = item.get("label")
            if isinstance(label, str):
                by_label.setdefault(label, []).append(item["name"])
    return by_name, by_label


def _resolve_headers(obj: str, headers: list[str], by_name: dict, by_label: dict, rel: str):
    columns: list[tuple[str, str]] = []  # (kind, name): kind "prop" | "assoc"
    unknown: list[str] = []
    for header in headers:
        match = ASSOC_RE.match(header)
        if match:
            columns.append(("assoc", ASSOC_OBJECTS[match.group(1)]))
        elif header in by_name:
            columns.append(("prop", header))
        elif len(by_label.get(header, [])) == 1:
            columns.append(("prop", by_label[header][0]))
        elif len(by_label.get(header, [])) > 1:
            raise AdapterError(
                f"{rel}: the header {header!r} is the label of several properties "
                f"({', '.join(sorted(by_label[header]))}) — cause: the export uses labels, and two "
                "properties share one. Fix: export with internal names, or rename one label in HubSpot. " + DOCS
            )
        else:
            unknown.append(header)
    if unknown:
        raise AdapterError(
            f"{rel}: header(s) {', '.join(repr(h) for h in unknown)} match no {obj} property — cause: the "
            f"property list (properties-{obj}.json) is older than the export, or the file is not a HubSpot "
            "export. Fix: record the property list again with the export. " + DOCS
        )
    if ("prop", "hs_object_id") not in columns:
        raise AdapterError(f"{rel} has no Record ID column — not a HubSpot export of {obj}. " + DOCS)
    return columns


def _cell(name: str, value: str, definition: dict, zone: ZoneInfo | None, where: str) -> str | None:
    if blank(value):
        return None
    kind = definition.get("type")
    if kind == "datetime":
        return parse_instant(value, where=where, local_zone=zone)
    if kind == "date":
        return parse_date(value, where=where)
    if kind in ("enumeration", "bool"):
        options = [o for o in definition.get("options") or [] if isinstance(o, dict)]
        values = {str(o.get("value")) for o in options}
        by_label: dict[str, set[str]] = {}
        for o in options:
            by_label.setdefault(str(o.get("label")), set()).add(str(o.get("value")))

        def one(part: str) -> str:
            if part in values:
                return part
            matches = by_label.get(part, set())
            # A label two options share (a stage name used in two pipelines) is
            # kept verbatim rather than guessed; stages are resolved per pipeline.
            return next(iter(matches)) if len(matches) == 1 else part

        if definition.get("fieldType") == "checkbox":
            return ";".join(one(part) for part in split_multi(value))
        return one(value)
    return value


#: CSV stage columns hold the stage label, which is unique only within its pipeline.
STAGE_COLUMNS = {"deals": ("pipeline", "dealstage"), "tickets": ("hs_pipeline", "hs_pipeline_stage")}


def _owner_index(bundle: Bundle) -> dict[str, set[str]]:
    """Owner name and email -> owner ids, from owners.json (CSV owner cells hold the name)."""
    index: dict[str, set[str]] = {}
    for item, _archived, _ref in bundle.owners:
        if not isinstance(item, dict) or item.get("id") in (None, ""):
            continue
        oid = str(item["id"])
        name = " ".join(str(item.get(k) or "").strip() for k in ("firstName", "lastName")).strip()
        for key in (name, str(item.get("email") or "").strip()):
            if key:
                index.setdefault(key.lower(), set()).add(oid)
    return index


def _owner_id(index: dict[str, set[str]], cell: str, where: str) -> str | None:
    if any(cell in ids for ids in index.values()):
        return cell  # already an owner id
    ids = index.get(cell.strip().lower(), set())
    if len(ids) > 1:
        raise AdapterError(
            f"{where}: the owner {cell!r} matches owners {', '.join(sorted(ids))} — cause: two owners share a "
            "name, and a CSV export names owners instead of identifying them. Fix: rename one owner in "
            "HubSpot, or record a crawl. " + DOCS
        )
    return next(iter(ids)) if ids else None


def _stage_id(bundle: Bundle, obj: str, pipeline_id: str | None, cell: str) -> str:
    for item, _ref in bundle.pipelines.get(obj, []):
        if isinstance(item, dict) and str(item.get("id")) == pipeline_id:
            stages = [s for s in item.get("stages") or [] if isinstance(s, dict)]
            if any(str(s.get("id")) == cell for s in stages):
                return cell
            matches = [str(s.get("id")) for s in stages if s.get("label") == cell]
            if len(matches) == 1:
                return matches[0]
    return cell


def _read_csv(bundle: Bundle, export: Path, root: Path, obj: str, zone: ZoneInfo | None) -> Feed | None:
    path = export / f"{obj}.csv"
    if not path.is_file():
        return None
    rel = path.relative_to(root).as_posix()
    defs = bundle.properties.get(obj) or []
    if not defs:
        raise AdapterError(
            f"{rel} needs export/properties-{obj}.json — cause: CSV headers are labels and enumeration "
            "cells are option labels; only the property list maps them back. Fix: save "
            f"GET /crm/v3/properties/{obj} next to the export. " + DOCS
        )
    by_name, by_label = _definitions(defs)
    raw = path.read_bytes()
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise AdapterError(f"{rel} is not UTF-8 ({exc}). Fix: export again as UTF-8 CSV. " + DOCS) from exc
    rows = list(csv.reader(io.StringIO(content, newline="")))
    if not rows:
        raise AdapterError(f"{rel} is empty (no header row). " + DOCS)
    header = rows[0]
    body = [row for row in rows[1:] if row]  # blank lines (an editor's trailing newline) are not rows
    columns = _resolve_headers(obj, header, by_name, by_label, rel)
    datetime_columns = [n for k, n in columns if k == "prop" and by_name[n].get("type") == "datetime"]
    if datetime_columns and zone is None:
        raise AdapterError(
            f"{rel} has date-time columns ({', '.join(datetime_columns[:3])}{'…' if len(datetime_columns) > 3 else ''}) "
            "without a UTC offset — cause: HubSpot renders CSV date-times in the exporting user's time "
            "zone. Fix: pass --time-zone <IANA name> (that user's zone, e.g. America/New_York); it is "
            "recorded in the manifest. " + DOCS
        )
    feed = Feed(obj=obj, input="export")
    feed.status.add("archived_not_enumerated", f"{rel}: a file export holds no archived {obj}")
    if body and len(body[-1]) < len(header):
        feed.status.add("truncated_export",
                        f"{rel}: the last row has {len(body[-1])} of {len(header)} columns (the file was cut off)")
        body = body[:-1]
    if len(body) in EXPORT_ROW_LIMITS:
        feed.status.add("truncated_export", f"{rel}: {len(body):,} rows is HubSpot's export row limit; "
                                             "more records may exist")
    for other in sorted({n for k, n in columns if k == "assoc"}):
        if orient(obj, other):
            bundle.pair(obj, other).exported = True
    owners = _owner_index(bundle)
    for line_no, row in enumerate(body, 2):
        if len(row) != len(header):
            raise AdapterError(f"{rel}:{line_no} has {len(row)} columns, the header {len(header)} — a "
                               "malformed export. Fix: export again. " + DOCS)
        props: dict[str, str] = {}
        assoc: dict[str, list[str]] = {}
        for (kind, name), value in zip(columns, row):
            if kind == "assoc":
                assoc[name] = split_multi(value)
                continue
            converted = _cell(name, value, by_name[name], zone, f"{rel}:{line_no} {name}")
            if converted is not None:
                props[name] = converted
        owner = props.get("hubspot_owner_id")
        if owner is not None:
            resolved = _owner_id(owners, owner, f"{rel}:{line_no}")
            if resolved is None:
                # An owner in neither owners list (removed from the portal): keep the
                # name, never invent an id.
                del props["hubspot_owner_id"]
                props["hubspot_owner_name"] = owner
            else:
                props["hubspot_owner_id"] = resolved
        if obj in STAGE_COLUMNS:
            pipe_prop, stage_prop = STAGE_COLUMNS[obj]
            raw_stage = next((v for (k, n), v in zip(columns, row) if k == "prop" and n == stage_prop), "")
            if not blank(raw_stage):
                props[stage_prop] = _stage_id(bundle, obj, props.get(pipe_prop), raw_stage)
        record_id = props.get("hs_object_id")
        if not record_id:
            raise AdapterError(f"{rel}:{line_no} has no Record ID. " + DOCS)
        if record_id in feed.records:
            raise AdapterError(f"{rel}:{line_no} repeats Record ID {record_id} — a damaged export. "
                               "Fix: export again. " + DOCS)
        record = HsRecord(
            obj=obj, id=record_id, properties=props, archived=False,
            created_at=props.get("createdate") or props.get("hs_createdate"),
            updated_at=props.get("lastmodifieddate") or props.get("hs_lastmodifieddate"),
            raw_ref=f"{rel}#row={line_no}",
            payload=dict(zip(header, row)),
        )
        feed.add(record)
        for other, ids in assoc.items():
            if orient(obj, other) is None:
                continue
            pair = bundle.pair(obj, other)
            for other_id in ids:
                if (pair.a, pair.b) == (obj, other):
                    pair.link(record_id, other_id)
                else:
                    pair.link(other_id, record_id)
    return feed


def read_export(bundle: Bundle, input_dir: Path, zone: ZoneInfo | None) -> None:
    export = input_dir / "export"
    root = input_dir
    info_path = export / "export.json"
    if not info_path.is_file():
        raise AdapterError(
            "export/export.json is missing — cause: a CSV export carries no export time or portal id. "
            "Fix: write {schema_version: 1, provider: \"hubspot\", payload: {exported_at: <UTC instant "
            "of the export>, account: <GET /account-info/v3/details, optional>}}. " + DOCS
        )
    info = read_envelope(info_path, root).payload
    if not isinstance(info, dict) or not isinstance(info.get("exported_at"), str):
        raise AdapterError("export/export.json needs payload.exported_at (a UTC instant). " + DOCS)
    for obj in NOT_EXPORTABLE:
        if (export / f"{obj}.csv").exists():
            raise AdapterError(
                f"export/{obj}.csv: the adapter reads {obj} only from a recorded crawl — cause: activity "
                "exports carry no ids for their associations. Fix: record a crawl of "
                f"/crm/v3/objects/{obj}. " + DOCS
            )
    bundle.kinds.append("export")
    bundle.exported_at.append(info["exported_at"])
    if isinstance(info.get("account"), dict):
        bundle.account = {**info["account"], **bundle.account}

    for obj in CSV_OBJECTS:
        if obj not in bundle.properties or not bundle.properties[obj]:
            status = Status()
            defs = _single_list(export / f"properties-{obj}.json", root, status, "file_absent",
                                f"export/properties-{obj}.json is absent", [])
            if defs:
                bundle.properties[obj] = defs
                bundle.property_status[obj] = status
    for obj in ("deals", "tickets"):
        if not bundle.pipelines.get(obj):
            status = Status()
            pipes = _single_list(export / f"pipelines-{obj}.json", root, status, "file_absent",
                                 f"export/pipelines-{obj}.json is absent", [])
            if pipes or obj not in bundle.pipelines:
                bundle.pipelines[obj] = pipes
                bundle.pipeline_status[obj] = status
    if not bundle.owner_status.enumerated:
        status = Status()
        owners = _single_list(export / "owners.json", root, status, "file_absent", "export/owners.json is absent", [])
        if owners:
            bundle.owner_status = status
            bundle.owners = [(item, False, ref) for item, ref in owners]
            archived_status = Status()
            archived = _single_list(export / "owners-archived.json", root, archived_status,
                                    "archived_not_enumerated",
                                    "export/owners-archived.json is absent: archived owners were not enumerated", [])
            for code, detail in archived_status.reasons:
                bundle.owner_status.add(code, detail)
            if archived_status.enumerated and not archived_status.reasons:
                bundle.owners_archived = "enumerated"
            bundle.owners += [(item, True, ref) for item, ref in archived]
        elif not bundle.owner_status.reasons:
            bundle.owner_status = status

    for obj in CSV_OBJECTS:
        current = bundle.feeds.get(obj)
        if current is not None and current.status.enumerated:
            continue  # the crawl has this object: the crawl wins
        feed = _read_csv(bundle, export, root, obj, zone)
        if feed is not None:
            bundle.feeds[obj] = feed
        elif current is None:
            missing = Feed(obj=obj, input="export")
            missing.status.enumerated = False
            missing.status.add("file_absent", f"export/{obj}.csv is absent")
            bundle.feeds[obj] = missing
