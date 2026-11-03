"""The HubSpot export adapter (docs/crm/phase5-importer.md section 2).

``HubSpotExportAdapter().export(input_dir, out_dir, mapping=…, time_zone=…)``
reads a recorded crawl (``<input>/crawl/``), a file export
(``<input>/export/``) or both, and writes the canonical directory with its
manifest last. It is a pure function of its input files: no database, no
network, and the same input always gives byte-identical output.
"""

from __future__ import annotations

import zoneinfo
from pathlib import Path
from typing import Any, Mapping

from core.plugins.crm_export import AdapterError, BaseCrmExportAdapter
from crm_import.canonical import MANIFEST_FILE as MANIFEST
from crm_import.canonical import OBJECT_FILES, OBJECT_TYPES, SCHEMA_VERSION, write_manifest, write_records

from .convert import RAW_SCHEMA_VERSION, Converter
from .mapping import load_mapping, validate_mapping
from .model import Bundle
from .reader_crawl import read_crawl
from .reader_csv import read_export
from .values import DOCS, parse_instant, text

ADAPTER_ID = "hubspot_export"
ADAPTER_VERSION = "0.1.0"
MAPPING_FILE = "mapping.toml"


def _ref_targets(value, out: list) -> None:
    if isinstance(value, dict):
        if set(value) == {"object_type", "source_id"}:
            out.append((value["object_type"], value["source_id"]))
            return
        for key, item in value.items():
            if key != "raw":
                _ref_targets(item, out)
    elif isinstance(value, list):
        for item in value:
            _ref_targets(item, out)


def check_references(records: dict[str, list[dict]], objects: dict[str, dict]) -> None:
    """A reference to a record the export does not hold makes its type partial.

    The validator calls such a reference ``dangling_ref`` (an error) while the
    target type claims ``complete``. A HubSpot record can point at an id that
    is in no listing (an owner deleted from the portal, a record purged after
    the association was recorded): the honest manifest says the target type is
    partial, ``count_mismatch``, and the importer waits for the record.
    """
    present: dict[str, set[str]] = {t: set() for t in OBJECT_TYPES}
    stages: set[str] = set()
    for otype, items in records.items():
        for record in items:
            present[otype].add(record["source_id"])
            present[otype].update(record.get("aliases") or [])
            if otype == "pipeline":
                stages.update(s["source_id"] for s in record["data"]["stages"])
    missing: dict[str, set[str]] = {}
    for items in records.values():
        for record in items:
            found: list = []
            _ref_targets(record["data"], found)
            for otype, sid in found:
                target = "pipeline" if otype == "stage" else otype
                known = stages if otype == "stage" else present.get(otype, set())
                if sid not in known:
                    missing.setdefault(target, set()).add(f"{otype}:{sid}" if otype == "stage" else sid)
    for target, ids in sorted(missing.items()):
        entry = objects.get(target)
        if entry is None or entry["coverage"] != "complete":
            continue
        sample = ", ".join(sorted(ids)[:3])
        entry["coverage"] = "partial"
        entry["reasons"] = [{"code": "count_mismatch",
                             "detail": f"{len(ids)} {target} id(s) referenced by other records are in no listing "
                                       f"(e.g. {sample}); the importer waits for them"}]


class HubSpotExportAdapter(BaseCrmExportAdapter):
    provider_id = "hubspot"
    schema_version = SCHEMA_VERSION
    raw_schema_version = RAW_SCHEMA_VERSION

    def export(self, input_dir: Path, out_dir: Path, *, mapping: Mapping[str, Any] | None,
               time_zone: str | None) -> dict:
        input_dir, out_dir = Path(input_dir), Path(out_dir)
        zone = None
        if time_zone is not None:
            try:
                zone = zoneinfo.ZoneInfo(time_zone)
            except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
                raise AdapterError(f"--time-zone {time_zone!r} is not an IANA time zone (e.g. "
                                   "America/New_York). " + DOCS) from exc
        has_crawl, has_export = (input_dir / "crawl").is_dir(), (input_dir / "export").is_dir()
        if not (has_crawl or has_export):
            raise AdapterError(
                f"{input_dir} holds neither crawl/ nor export/ — cause: the adapter reads a recorded API "
                "crawl or a HubSpot file export laid out as docs/crm/importing.md section 8 describes. "
                "Fix: point --input at the directory that contains them. " + DOCS)

        # Read everything first: a refusal (a failed crawl, a CSV without a
        # time zone, a newer recording) leaves out_dir untouched.
        bundle = Bundle()
        if has_crawl:
            read_crawl(bundle, input_dir)
        if has_export:
            read_export(bundle, input_dir, zone)

        if mapping is None and (input_dir / MAPPING_FILE).is_file():
            mapping = load_mapping(input_dir / MAPPING_FILE)
        names = {obj: ({str(i.get("name")) for i, _ in defs if isinstance(i, dict)} if defs else None)
                 for obj, defs in bundle.properties.items()}
        mapping = validate_mapping(mapping or {}, names)
        source = self._source(bundle, mapping)
        exported_at = max(parse_instant(v, where="exported_at") for v in bundle.exported_at)

        converter = Converter(bundle, mapping=mapping, source=source, time_zone=time_zone)
        records = converter.run()
        objects = converter.coverage()
        check_references(records, objects)

        out_dir.mkdir(parents=True, exist_ok=True)
        ours = set(OBJECT_FILES.values()) | {MANIFEST}
        stray = sorted(p.name for p in out_dir.iterdir() if p.name not in ours)
        if stray:
            raise AdapterError(
                f"{out_dir} holds {', '.join(stray[:3])}, which is not canonical output — cause: the output "
                "directory was used for something else. Fix: write to a new or empty directory. " + DOCS)
        for otype in OBJECT_TYPES:
            path = out_dir / OBJECT_FILES[otype]
            if objects[otype]["coverage"] == "unknown" and not records[otype]:
                if path.exists():
                    path.unlink()
                continue
            write_records(out_dir, otype, records[otype])
        return write_manifest(
            out_dir, provider=self.provider_id, source=source,
            adapter={"id": ADAPTER_ID, "version": ADAPTER_VERSION, "schema_version": self.schema_version},
            exported_at=exported_at,
            input={"kind": "+".join(bundle.kinds), "time_zone": time_zone},
            objects=objects,
        )

    @staticmethod
    def _source(bundle: Bundle, mapping: dict) -> str:
        portal = text(bundle.account.get("portalId"))
        mapped = mapping.get("source")
        if mapped and portal and mapped != f"hubspot:{portal}":
            raise AdapterError(
                f"the mapping file says source {mapped!r} but the recording is of portal {portal} — cause: the "
                "mapping file belongs to another portal. Fix: use that portal's mapping file. " + DOCS)
        if mapped:
            return mapped
        if portal:
            return f"hubspot:{portal}"
        raise AdapterError(
            "no portal id — cause: neither the mapping file's source nor the recorded account details "
            "(payload.account.portalId) name the portal. Fix: add source = \"hubspot:<portal id>\" to the "
            "mapping file. " + DOCS)
