"""Build a refusing fixture: ``valid-small`` plus one case's overlay.

Each directory under ``fixtures/canonical/invalid/<case>/`` holds

- ``case.json``: ``description``; optional ``remove`` (file names deleted
  from the copy), ``refresh`` (default true: recompute the manifest's rows
  and sha256 digests after the overlay, so the case breaks exactly one rule),
  and ``manifest`` (a list of ``{"op": "set" | "delete", "path": [...],
  "value": ...}`` applied to the manifest after the refresh);
- overlay files copied over the valid set (a ``manifest.json`` overlay is
  used verbatim: no refresh, no ops);
- ``validation.expected.json``: the expected errors and warnings
  (``code``, ``file``, ``line``, ``field``) and ``ok``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from crm_import.canonical import MANIFEST_FILE, OBJECT_FILES, count_rows, file_digest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "canonical"
VALID = FIXTURES / "valid-small"
INVALID = FIXTURES / "invalid"
EXPECTED_NAME = "validation.expected.json"


def case_dirs() -> list[Path]:
    return sorted(p for p in INVALID.iterdir() if p.is_dir())


def copy_valid(out: Path) -> Path:
    shutil.copytree(VALID, out, ignore=shutil.ignore_patterns("expected"))
    return out


def _archived_rows(path: Path) -> int:
    archived = 0
    if path.is_file():
        for line in path.read_bytes().splitlines():
            try:
                archived += json.loads(line).get("archived") is True
            except (ValueError, AttributeError):
                pass
    return archived


def refresh_manifest(directory: Path) -> dict:
    """Recompute rows, declared totals and digests from disk; keep every other fact."""
    path = directory / MANIFEST_FILE
    manifest = json.loads(path.read_text(encoding="utf-8"))
    files = {}
    for otype, entry in manifest.get("objects", {}).items():
        fname = OBJECT_FILES.get(otype)
        if not fname:
            continue
        target = directory / fname
        entry["rows"] = count_rows(target) if target.is_file() else 0
        if isinstance(entry.get("declared_total"), int):  # keep "complete" honest: live records
            entry["declared_total"] = entry["rows"] - _archived_rows(target)
        if target.is_file():
            files[fname] = file_digest(target)
    manifest["files"] = files
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def _apply_ops(manifest: dict, ops: list[dict]) -> None:
    for op in ops:
        *parents, last = op["path"]
        node = manifest
        for key in parents:
            node = node[key]
        if op["op"] == "set":
            node[last] = op["value"]
        elif op["op"] == "delete":
            del node[last]
        else:
            raise ValueError(f"unknown op {op['op']!r}")


def build_case(case_dir: Path, out: Path) -> Path:
    spec = json.loads((case_dir / "case.json").read_text(encoding="utf-8"))
    copy_valid(out)
    for name in spec.get("remove", []):
        (out / name).unlink()
    overlay = [p for p in case_dir.iterdir() if p.name not in ("case.json", EXPECTED_NAME)]
    for path in overlay:
        shutil.copyfile(path, out / path.name)
    if any(p.name == MANIFEST_FILE for p in overlay) or not (out / MANIFEST_FILE).exists():
        return out
    if spec.get("refresh", True):
        refresh_manifest(out)
    ops = spec.get("manifest", [])
    if ops:
        path = out / MANIFEST_FILE
        manifest = json.loads(path.read_text(encoding="utf-8"))
        _apply_ops(manifest, ops)
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out
