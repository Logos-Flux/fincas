"""Read and write canonical export directories.

Writing is deterministic: records are sorted by ``source_id`` and serialised
with sorted keys and fixed separators, so two runs over the same input are
byte-identical (diffable), and the manifest is written last with the files'
row counts and sha256 digests.

Reading streams one record at a time and refuses a newer schema version
(``require_known_version``) instead of misreading it. The reader does not
validate records; run :func:`validate_canonical` first (the importer does).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from core.plugins.payroll import AdapterError, require_known_version

from . import schema as s


class CanonicalFormatError(AdapterError):
    """A canonical directory cannot be read. Message = problem + cause + fix + docs."""


def dump_record(record: Mapping[str, Any]) -> str:
    """One JSONL line (without the newline), byte-stable for equal input."""
    return json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def make_record(
    object_type: str,
    *,
    source: str,
    source_id: str,
    data: Mapping[str, Any],
    source_object_type: str | None = None,
    aliases: Iterable[str] = (),
    archived: bool = False,
    created_at: str | None = None,
    updated_at: str | None = None,
    properties: Mapping[str, Any] | None = None,
    history: list[Mapping[str, Any]] | None = None,
    raw_ref: str | None = None,
) -> dict:
    """A record with the full envelope (spec 1.2). ``history`` is omitted
    when None: "history not exported" is a manifest fact, not an empty list."""
    if object_type not in s.OBJECT_FILES:
        raise ValueError(f"{object_type!r} is not a canonical object type")
    record = {
        "schema_version": s.SCHEMA_VERSION,
        "object_type": object_type,
        "source": source,
        "source_id": str(source_id),
        "source_object_type": source_object_type or object_type,
        "aliases": sorted(str(a) for a in aliases),
        "archived": archived,
        "created_at": created_at,
        "updated_at": updated_at,
        "data": dict(data),
        "properties": dict(properties or {}),
        "raw_ref": raw_ref,
    }
    if history is not None:
        record["history"] = list(history)
    return record


def write_records(directory: str | Path, object_type: str, records: Iterable[Mapping[str, Any]]) -> tuple[int, str]:
    """Write ``records`` to the type's file, sorted by source_id.

    Returns ``(rows, "sha256:<hex>")`` for the manifest.
    """
    path = Path(directory) / s.OBJECT_FILES[object_type]
    ordered = sorted(records, key=lambda r: str(r["source_id"]))
    digest = hashlib.sha256()
    with path.open("wb") as fh:
        for record in ordered:
            try:
                line = (dump_record(record) + "\n").encode("utf-8")
            except UnicodeEncodeError as exc:  # a lone surrogate from a broken provider escape
                raise CanonicalFormatError(
                    f"{object_type} {record['source_id']!r} holds text that is not valid Unicode ({exc}) — "
                    "cause: the provider payload has a broken \\u escape. Fix: repair the text in the "
                    "adapter (e.g. errors='replace') before writing. See docs/crm/importing.md."
                ) from exc
            digest.update(line)
            fh.write(line)
    return len(ordered), f"sha256:{digest.hexdigest()}"


def file_digest(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def count_rows(path: str | Path) -> int:
    with Path(path).open("rb") as fh:
        return sum(1 for line in fh if line.strip())


def write_manifest(
    directory: str | Path,
    *,
    provider: str,
    source: str,
    adapter: Mapping[str, Any],
    exported_at: str,
    input: Mapping[str, Any],  # the manifest key's name
    objects: Mapping[str, Mapping[str, Any]],
) -> dict:
    """Write ``manifest.json`` last. ``objects`` maps each canonical type to its
    coverage facts (``coverage``, and optionally ``reasons``,
    ``declared_total``, ``archived``, ``history``); ``file``, ``rows`` and the
    ``files`` digests are filled in from what is on disk."""
    directory = Path(directory)
    out_objects: dict[str, dict] = {}
    files: dict[str, str] = {}
    for otype in s.OBJECT_TYPES:
        if otype not in objects:
            continue
        fname = s.OBJECT_FILES[otype]
        path = directory / fname
        entry = {"file": fname, **dict(objects[otype])}
        entry["rows"] = count_rows(path) if path.is_file() else 0
        if path.is_file():
            files[fname] = file_digest(path)
        out_objects[otype] = entry
    manifest = {
        "schema_version": s.SCHEMA_VERSION,
        "provider": provider,
        "source": source,
        "adapter": dict(adapter),
        "exported_at": exported_at,
        "input": dict(input),
        "objects": out_objects,
        "files": files,
    }
    tmp = directory / (s.MANIFEST_FILE + ".tmp")
    tmp.write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, directory / s.MANIFEST_FILE)
    return manifest


def read_manifest(directory: str | Path) -> dict:
    """Load ``manifest.json``, refusing a missing file or a newer schema."""
    path = Path(directory) / s.MANIFEST_FILE
    if not path.is_file():
        raise CanonicalFormatError(
            f"{directory} has no manifest.json — cause: the export adapter did not finish "
            "(it writes the manifest last). Fix: re-run the adapter. See docs/crm/importing.md."
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CanonicalFormatError(
            f"{path} is not valid UTF-8 JSON ({exc}) — cause: a damaged or hand-edited "
            "manifest. Fix: re-run the adapter. See docs/crm/importing.md."
        ) from exc
    if not isinstance(manifest, dict):
        raise CanonicalFormatError(f"{path} must hold a JSON object. See docs/crm/importing.md.")
    _require_known(manifest, "manifest.json")
    return manifest


def _require_known(envelope: dict, where: str) -> int:
    try:
        return require_known_version(envelope, s.KNOWN_MAX_VERSION)
    except AdapterError as exc:
        raise CanonicalFormatError(s.newer_version_message(envelope.get("schema_version"), where)) from exc


def iter_records(directory: str | Path, object_type: str) -> Iterator[tuple[int, dict]]:
    """Stream ``(line_number, record)`` from one type's file.

    A missing file yields nothing (the manifest's coverage says why). A line
    that is not a JSON object, or a record of a newer schema version, raises
    :class:`CanonicalFormatError` naming the line.
    """
    if object_type not in s.OBJECT_FILES:
        raise ValueError(f"{object_type!r} is not a canonical object type")
    path = Path(directory) / s.OBJECT_FILES[object_type]
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise CanonicalFormatError(
                    f"{path.name}:{line_no} is not valid JSON ({exc}) — cause: a damaged export. "
                    "Fix: run `manage.py check_canonical_export`. See docs/crm/importing.md."
                ) from exc
            if not isinstance(record, dict):
                raise CanonicalFormatError(f"{path.name}:{line_no} is not a JSON object. See docs/crm/importing.md.")
            _require_known(record, f"{path.name}:{line_no}")
            yield line_no, record
